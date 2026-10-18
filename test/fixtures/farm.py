# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Farm fixtures: the board under test, a partner board, farm.toml, and secrets.

The `board` fixture is module scoped and parametrized over farm.toml boards by the root
conftest, so every test module runs once per board. It does what run.py does around each test:

- skips the module on boards without the caps, wiring, board or port it asks for (markers)
- holds the board's lock file, the same one run.py uses, so two runs never share a board
- health-checks the board before its first module, and power-cycles it if it starts unhealthy
- health-checks the board after every module; a module that crashed the board fails once
  (as a teardown error), the board is power-cycled, and the next module gets a clean board

It also checks every farm.toml board entry at session start (validate_farm), so a typo fails
the run before any board is touched instead of half way through it.
"""
import fcntl
import os
import re
import time
from contextlib import contextmanager

import pytest

import run as cpr_run  # board_health, recover: the same checks run.py uses
from outcomes import DNF

LOCK_WAIT_S = 900

# --- farm.toml board entries ------------------------------------------------
# Plain functions, no pytest: they could move to lib/farmlib.py so run.py checks the same way.

# The sysfs path of a board's hub port: bus-port.port..., e.g. "3-3.2" or "3-3.3.4.1". At least
# one '.', because Board.power_cycle() splits it into uhubctl's hub (-l) and port (-p). The same
# idea as Brent's USB_HUB_RE (hil_board_utils.py:546 @ cf64e03), which checks the hub half of
# his {"hub", "port"} location.
USB_PATH_RE = re.compile(r"^\d+-\d+(\.\d+)+$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # used in lock paths, xdist groups and --boards
HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")

KNOWN_KEYS = {"name", "board_id", "usb", "port", "caps", "power", "notes", "flash", "wiring",
              "run_tests", "skip_tests", "dns_tests"}
# Keys that would cache what must be resolved from `usb` at the moment of use.
CACHED_KEYS = {"tty", "serial", "dev", "drive", "mount", "label", "circuitpy", "location"}
KNOWN_CAPS = {"native_usb", "uf2", "wifi", "ble", "esp32spi", "usb_host", "reset_ok", "small"}
# key -> smallest legal value (all integers). tries: how many power cycles recover() may spend,
# and how many times a device run that lost the board is run again, for a board that does not
# always come up (the Feather STM32F405's crystal load caps).
POWER_KEYS = {"r": 1, "w": 1, "settle": 0, "tries": 1}

# flash.method -> {key: kind}, required then optional. Kinds: "str", "hex" (a string such as
# "0x1000", because lib/flash.py hands it to esptool or int(x, 0)), "int" (kHz for openocd).
# The optional keys include the opt-in --flash-erase ones lib/flash.py's erase_refusal reads
# (nuke_uf2, app_base), so configuring an erase does not draw an "unknown key" warning.
FLASH_METHODS = {
    "uf2": ({}, {"nuke_uf2": "str"}),
    "esp_uf2": ({"chip": "str", "flash_size": "str", "bootloader_offset": "hex",
                 "tinyuf2_offset": "hex", "tinyuf2_dir": "str"}, {}),
    "swd": ({"probe": "str", "target": "str"},
            {"speed": "int", "fast_speed": "int", "rescue_speed": "int", "connect": "str",
             "bin_base": "hex", "app_base": "hex"}),
}


class FarmConfigError(ValueError):
    """A farm.toml board entry that the harness cannot use."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _selectors(name, key, value, test_dir):
    if not isinstance(value, list) or not all(isinstance(s, str) and s.strip("/") for s in value):
        raise FarmConfigError("board '%s': '%s' must be a list of test paths, e.g. "
                              "[\"hardware/wifi_join_test.py\"], got %r" % (name, key, value))
    if test_dir:
        for s in value:
            path = normalize_selector(s).split("::", 1)[0].split("[", 1)[0]
            if not os.path.exists(os.path.join(test_dir, path)):
                raise FarmConfigError("board '%s': %s entry %r names no file or folder under %s"
                                      % (name, key, s, test_dir))


def normalize_selector(s):
    """A run_tests/skip_tests entry relative to test/: trailing '/' and a leading 'test/' go.

    Brent's are relative to his conftest dir (_test_selectors, conftest.py:130 @ cf64e03); ours
    take either "hardware/x_test.py" or the nodeid form "test/hardware/x_test.py".
    """
    s = s.strip().rstrip("/:")
    for prefix in ("./", "test/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def validate_board_entry(entry, test_dir=None):
    """Check one [[board]] table. Raises FarmConfigError on the first problem, else returns a
    list of warnings (unknown keys or caps, which may be newer than this check).

    After Brent's validate_board_entry (hil_board_utils.py:552 @ cf64e03), widened to what a
    farm entry carries: usb path, flash method keys, uhubctl power values, test selectors.
    With test_dir, every run_tests/skip_tests path must exist under it.
    """
    if not isinstance(entry, dict):
        raise FarmConfigError("[[board]] entry must be a table, got %r" % (entry,))
    name = entry.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise FarmConfigError("[[board]] 'name' must be a non-empty string of letters, digits, "
                              "'_', '.' or '-', got %r" % (name,))
    warnings = []
    board_id = entry.get("board_id")
    if not isinstance(board_id, str) or not board_id.strip():
        raise FarmConfigError("board '%s': 'board_id' must be set (the boot_out.txt board id, "
                              "checked before anything is written)" % name)
    usb = entry.get("usb")
    if not isinstance(usb, str) or not USB_PATH_RE.match(usb):
        hint = (" (never a tty or drive: those are resolved from usb at the moment of use)"
                if isinstance(usb, str) and usb.startswith("/") else "")
        raise FarmConfigError("board '%s': 'usb' must be the sysfs path of its port on a hub "
                              "uhubctl can switch, like '3-3.2' or '3-3.3.4.1', got %r%s"
                              % (name, usb, hint))
    if "location" in entry:
        raise FarmConfigError("board '%s': Brent's location {hub, port} is written here as "
                              "usb = \"<hub>.<port>\", e.g. \"1-1.3\" + 2 -> \"1-1.3.2\"" % name)
    cached = sorted(CACHED_KEYS & set(entry))
    if cached:
        raise FarmConfigError("board '%s': %s not allowed; ttys and drives are resolved from "
                              "'usb' at the moment of use" % (name, ", ".join(cached)))
    for key in ("port", "notes"):
        if key in entry and not isinstance(entry[key], str):
            raise FarmConfigError("board '%s': '%s' must be a string" % (name, key))
    caps = entry.get("caps", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise FarmConfigError("board '%s': 'caps' must be a list of strings" % name)
    unknown = sorted(set(caps) - KNOWN_CAPS)
    if unknown:
        warnings.append("board '%s': unknown caps %s" % (name, ", ".join(unknown)))
    if not isinstance(entry.get("wiring", {}), dict):
        raise FarmConfigError("board '%s': 'wiring' must be a table" % name)

    power = entry.get("power", {})
    if not isinstance(power, dict):
        raise FarmConfigError("board '%s': 'power' must be a table like "
                              "{ r = 30, w = 500, settle = 12 }" % name)
    for key, least in POWER_KEYS.items():
        if key in power and (not _is_int(power[key]) or power[key] < least):
            raise FarmConfigError("board '%s': power.%s must be an integer >= %d, got %r"
                                  % (name, key, least, power[key]))
    if "sudo" in power:
        raise FarmConfigError("board '%s': power.sudo not allowed; uhubctl runs unprivileged"
                              % name)
    for key in sorted(set(power) - set(POWER_KEYS)):
        warnings.append("board '%s': unknown power key '%s'" % (name, key))

    flash = entry.get("flash", {})
    if not isinstance(flash, dict):
        raise FarmConfigError("board '%s': 'flash' must be a table" % name)
    if flash:
        method = flash.get("method")
        if method not in FLASH_METHODS:
            raise FarmConfigError("board '%s': flash.method must be one of %s, got %r"
                                  % (name, ", ".join(sorted(FLASH_METHODS)), method))
        required, optional = FLASH_METHODS[method]
        missing = [k for k in required if k not in flash]
        if missing:
            raise FarmConfigError("board '%s': flash method %s needs %s"
                                  % (name, method, ", ".join(missing)))
        for key, kind in list(required.items()) + list(optional.items()):
            if key not in flash:
                continue
            v = flash[key]
            ok = (_is_int(v) and v > 0 if kind == "int"
                  else isinstance(v, str) and bool(HEX_RE.match(v)) if kind == "hex"
                  else isinstance(v, str) and bool(v.strip()))
            if not ok:
                want = {"int": "a positive integer", "hex": "a hex string like \"0x1000\"",
                        "str": "a non-empty string"}[kind]
                raise FarmConfigError("board '%s': flash.%s must be %s, got %r"
                                      % (name, key, want, v))
        for key in sorted(set(flash) - set(required) - set(optional) - {"method"}):
            warnings.append("board '%s': unknown key flash.%s for method %s" % (name, key, method))

    for key in ("run_tests", "skip_tests", "dns_tests"):
        if key in entry:
            _selectors(name, key, entry[key], test_dir)
    for key in sorted(set(entry) - KNOWN_KEYS):
        warnings.append("board '%s': unknown key '%s'" % (name, key))
    return warnings


def validate_farm(cfg, test_dir=None):
    """Check every [[board]] in a loaded farm.toml, and what two boards must not share.

    Returns (errors, warnings), both lists of strings, so one run reports every problem.
    """
    errors, warnings = [], []
    uhubctl = cfg.get("host", {}).get("uhubctl", "uhubctl")
    if not isinstance(uhubctl, str) or "sudo" in uhubctl.split():
        errors.append("[host] uhubctl must be a plain command, never sudo, got %r" % (uhubctl,))
    entries = cfg.get("board", [])
    if not isinstance(entries, list) or not entries:
        return errors + ["farm.toml has no [[board]] entries"], warnings
    good = []
    for entry in entries:
        try:
            warnings += validate_board_entry(entry, test_dir)
            good.append(entry)
        except FarmConfigError as e:
            errors.append(str(e))
    for i, a in enumerate(good):
        for b in good[i + 1:]:
            for key in ("name", "usb", "probe"):
                va = a.get("flash", {}).get(key) if key == "probe" else a[key]
                vb = b.get("flash", {}).get(key) if key == "probe" else b[key]
                if va is not None and va == vb:
                    errors.append("boards '%s' and '%s' share %s %r"
                                  % (a["name"], b["name"], key, va))
            for upper, lower in ((a, b), (b, a)):
                if lower["usb"].startswith(upper["usb"] + "."):  # upper is a hub, not a board
                    errors.append("board '%s' (usb %s) sits below board '%s' (usb %s)"
                                  % (lower["name"], lower["usb"], upper["name"], upper["usb"]))
    return errors, warnings


@contextmanager
def _flock(path, what):
    """Blocking flock with a timeout, so a stuck run fails instead of hanging forever."""
    with open(path, "w") as f:
        t0 = time.time()
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - t0 > LOCK_WAIT_S:
                    pytest.fail("%s busy for %d s (%s)" % (what, LOCK_WAIT_S, path), pytrace=False)
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def board_lock(b):
    """The lock file run.py takes for this board."""
    return _flock("/tmp/cp-regress-%s.lock" % b.usb, "board %s" % b.name)


def _marker_args(node, name):
    m = node.get_closest_marker(name)
    return list(m.args) if m else []


def not_applicable(node, b):
    """Why this module cannot run on board b, or None. Same rules as run.py's applicable()."""
    missing = [c for c in _marker_args(node, "needs") if c not in b.caps]
    if missing:
        return "board lacks " + ",".join(missing)
    missing = [w for w in _marker_args(node, "wiring") if w not in b.wiring]
    if missing:
        return "fixture not wired: " + ",".join(missing)
    if b.port in _marker_args(node, "skip_ports"):
        return "skipped on port " + b.port
    if b.name in _marker_args(node, "skip_boards"):
        return "skipped on board " + b.name
    return None


def _preflight(config, b):
    """First health check of the run for this board; recover once if it starts unhealthy."""
    if b.name in config.cpr_preflight or b.name in config.cpr_down:
        return
    ok, why = cpr_run.board_health(b)
    if not ok:
        ok, rwhy = cpr_run.recover(b)
        if not ok:
            config.cpr_down[b.name] = "unhealthy before the run: %s; power cycle: %s" % (why, rwhy)
    expect = config.getoption("expect_version")
    if ok and expect:
        v = b.version() or ""
        if expect not in v:
            config.cpr_down[b.name] = "version %r does not contain %r" % (v, expect)
    config.cpr_preflight[b.name] = (ok, why)


@pytest.fixture(scope="module")
def board(request):
    """The farmlib.Board under test (one per farm.toml board, set by the root conftest)."""
    config = request.config
    b = next(x for x in config.cpr_boards if x.name == request.param)
    why = not_applicable(request.node, b)
    if why:
        pytest.skip(why)
    if config.getoption("cpr_dry_run"):
        pytest.skip("dry run: would run on %s" % b.name)
    if not b.present():
        pytest.fail("%s is not on the bus at %s" % (b.name, b.usb), pytrace=False)
    multi = request.node.get_closest_marker("multi_board") is not None
    # Multi-board modules take the farm-wide lock first, then their own board, so two workers
    # can never each hold one board of a pair and wait for the other.
    outer = _flock("/tmp/cp-regress-multi.lock", "multi-board slot") if multi else _null()
    with outer, board_lock(b):
        # flashed_firmware's work, before the first health check. Imported here: importing a
        # sibling plugin at module load stops pytest from rewriting its asserts.
        from fixtures.flashing import ensure_firmware  # pylint: disable=import-outside-toplevel
        ensure_firmware(config, b)
        _preflight(config, b)
        if b.name in config.cpr_down:
            pytest.fail("board down: " + config.cpr_down[b.name], pytrace=False)
        b.cpr_crashed = None
        b.cpr_started = 0
        b.cpr_crashed_at = 0
        yield b
        ok, hwhy = cpr_run.board_health(b)
        if not ok:
            rok, rwhy = cpr_run.recover(b)
            if not rok:
                config.cpr_down[b.name] = "did not recover after %s: %s" % (request.node.name, rwhy)
            pytest.fail(DNF + "%s did not survive this module: %s. Power-cycled: %s"
                        % (b.name, hwhy, "recovered" if rok else "still down (%s)" % rwhy),
                        pytrace=False)


@contextmanager
def _null():
    yield


@pytest.fixture(autouse=True)
def _not_after_crash(request):
    """Once a device run has lost the board, the rest of that module skips as not reached.

    The crash itself is charged once: the test whose device run failed, and the module's
    teardown health check, which power-cycles the board.
    """
    if "board" in request.fixturenames:
        b = request.getfixturevalue("board")
        # Module-scoped fixtures (where device runs live) are set up before this one, so a crash
        # during this test's own setup has already happened. Count tests to tell them apart:
        # the test whose setup crashed runs and fails; only the ones after it skip.
        started = getattr(b, "cpr_started", 0)
        b.cpr_started = started + 1
        why = getattr(b, "cpr_crashed", None)
        if why and getattr(b, "cpr_crashed_at", 0) < started:
            pytest.skip(DNF + "not reached: the board stopped answering earlier (%s)" % why[:160])
        b.cpr_in_test = True  # a device run from here on happens inside this test's body
        yield
        b.cpr_in_test = False
    else:
        yield


@pytest.fixture(scope="session")
def farm_config(request):
    """farm.toml as a dict."""
    return request.config.cpr_cfg


@pytest.fixture(scope="session")
def farm_boards(request):
    """Every board in farm.toml, selected or not (partners can be outside --boards)."""
    return request.config.cpr_boards


@pytest.fixture
def partner(request, board, farm_boards):
    """partner(cap) -> another present board with that cap, locked for this test, or a skip.

    Modules that use it should be marked multi_board.
    """
    held = []

    def get(cap, exclude_self=True):
        for b in farm_boards:
            if cap in b.caps and (b is not board or not exclude_self) and b.present():
                lock = board_lock(b)
                lock.__enter__()  # pylint: disable=unnecessary-dunder-call
                held.append(lock)
                return b
        pytest.skip("no partner board with %s on the farm" % cap)
        return None

    yield get
    for lock in reversed(held):
        lock.__exit__(None, None, None)


@pytest.fixture(scope="session")
def secret(farm_config):
    """secret("ssid_env") -> the environment variable farm.toml [network] names, or None.

    Never print what it returns.
    """
    def get(key):
        env = farm_config.get("network", {}).get(key)
        return os.environ.get(env) if env else None

    return get
