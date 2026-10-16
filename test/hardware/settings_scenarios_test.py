# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Named settings.toml scenarios, after Brent's hil/firmware/provisioning/test_secrets.py.

New with the be-more-like-brent fixtures; there is no tests/settings_scenarios.py. Each scenario
in data/settings-scenarios/scenarios.toml (good, default, no file, empty, malformed TOML, wrong
types, overlong values, unicode, and every structure case of his translated to TOML) runs once
per board, in his Arrange / Act / Assert shape:

  Arrange  secrets_path renders the scenario into tmp_path, after scenario_guard; the bytes go
           to CIRCUITPY through flashed_fs's own path (apply_tree with the run's FsBackup, so
           the original settings.toml comes back at the end of the run)
  Act      wifi boards get a hard reset (flashing.hard_reset), because the supervisor's own WiFi
           connection survives a soft reload (wifi_user_reset in ports/espressif); every board
           then soft-reboots so the console shows what the supervisor printed while reading
  Assert   the console: the settings errors the scenario expects on wifi boards and no others
           (no board but a wifi one reads CIRCUITPY_WIFI_* at boot), and code.py still ran;
           on the device: os.getenv, supervisor.get_setting and wifi.radio.connected; and
           boot_out.txt still names this board and the flashed version

Where his firmware halts on bad secrets, CircuitPython never does: it prints an error, skips the
setting and runs code.py. The module puts the farm's settings.toml back when it is done.

The host-only tests at the bottom need no board: they check every scenario's TOML verdict against
Python's tomllib, that every scenario passes scenario_guard, and that the guard refuses the
files it must.

Never print what secrets_path holds: it carries the farm WiFi password.
"""
import re
import time

import pytest

import farmlib
from fixtures.device import DeviceResults
from fixtures.farm import not_applicable
from fixtures.flashing import FsBackup, apply_tree, hard_reset
from fixtures.secrets import (DEFAULT_SECRETS_DIR, NO_SECRETS_FILE, PASSWORD, PLACEHOLDER_PASSWORD,
                              PLACEHOLDER_SSID, SECRETS_SUFFIX, SSID, WEB_API_PASSWORD,
                              build_generated, fill, load_scenarios, render, scenario_guard,
                              settings_toml, substitutions, tomllib)

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
]

SCENARIOS = load_scenarios(DEFAULT_SECRETS_DIR)
PARAMS = [pytest.param(name, id=name,
                       marks=[pytest.mark.needs("wifi")] if e["needs_wifi"] else [])
          for name, e in SCENARIOS.items()]
ERROR_LINE = re.compile(r"An error occurred while retrieving '([^']*)':")

DEVICE = r'''
import os

EXP = @@EXP@@


def _cpr_show(k, v):
    if "SSID" in k.upper() or "PASSWORD" in k.upper():
        return type(v).__name__
    return repr(v)


def _cpr_main():
    try:
        os.getenv("CPR_NO_SUCH_KEY")
    except NotImplementedError:
        skip("getenv", "settings.toml reading not in this build")
        return
    for k, want in EXP["getenv"]:
        v = os.getenv(k)
        check("getenv:" + k, v == want, _cpr_show(k, v))
    for k in EXP["getenv_none"]:
        v = os.getenv(k)
        check("getenv_none:" + k, v is None, _cpr_show(k, v))
    for k in EXP["getenv_present"]:
        v = os.getenv(k)
        check("getenv_present:" + k, v is not None, "None" if v is None else "len %d" % len(v))
    for k, want in EXP["getenv_raises"]:
        try:
            v = os.getenv(k)
            check("getenv_raises:" + k, False, "returned " + _cpr_show(k, v))
        except Exception as e:
            check("getenv_raises:" + k, type(e).__name__ == want, type(e).__name__)
    if EXP["setting"] or EXP["setting_error"]:
        try:
            import supervisor
            gs = supervisor.get_setting
        except (ImportError, AttributeError):
            gs = None
        if gs is None:
            skip("get_setting", "supervisor.get_setting not in this build (added for 11.x)")
        else:
            for k, want in EXP["setting"]:
                try:
                    v = gs(k)
                    check("setting:" + k, v == want and type(v) == type(want),
                          _cpr_show(k, v) + " " + type(v).__name__)
                except Exception as e:
                    check("setting:" + k, False, type(e).__name__)
            for k in EXP["setting_error"]:
                try:
                    v = gs(k)
                    check("setting_error:" + k, False, "accepted " + _cpr_show(k, v))
                except ValueError:
                    check("setting_error:" + k, True, "")
                except Exception as e:
                    check("setting_error:" + k, False, type(e).__name__)
    if EXP["connected"] is not None:
        try:
            import wifi
        except ImportError:
            skip("wifi_connected", "no wifi module")
            return
        c = wifi.radio.connected
        check("wifi_connected", c == EXP["connected"], "connected=%s" % c)


_cpr_main()
'''


def _raw_subs(good):
    """Placeholder -> the plain value, for expectations (templates get TOML-escaped ones)."""
    subs = {"@@WEB_API_PASSWORD@@": WEB_API_PASSWORD}
    for name, key in (("SSID", SSID), ("PASSWORD", PASSWORD),
                      ("BROKER_HOST", "CPR_BROKER_HOST"), ("BROKER_PORT", "CPR_BROKER_PORT")):
        if good.get(key) not in (None, ""):
            subs["@@%s@@" % name] = str(good[key])
    return subs


def expectations(entry, good, wifi_board):
    """What the device code checks for this scenario on this board. Expectations whose
    placeholder .env does not fill are left out."""
    subs = _raw_subs(good)

    def filled(v):
        if not isinstance(v, str):
            return v
        for ph, val in subs.items():
            v = v.replace(ph, val)
        return None if "@@" in v else v

    getenv = [(k, filled(v)) for k, v in sorted(entry["getenv"].items())]
    connected = entry["connected"] if wifi_board else None
    if connected and not good.get(SSID):
        connected = None  # good / crlf without CPR_WIFI_SSID: nothing to join
    return {
        "getenv": [(k, v) for k, v in getenv if v is not None],
        "getenv_none": list(entry["getenv_none"]),
        "getenv_present": list(entry["getenv_present"]),
        "getenv_raises": sorted(entry["getenv_raises"].items()),
        "setting": [(k, filled(v)) for k, v in sorted(entry["setting"].items())],
        "setting_error": list(entry["setting_error"]),
        "connected": connected,
    }


def device_code(exp):
    return DEVICE.replace("@@EXP@@", repr(exp))


def console_problems(pre, entry, wifi_board):
    """What is wrong with the console text the supervisor printed before code.py started."""
    problems = []
    want = entry["errors"] if wifi_board else []
    for key, message in want:
        rx = r"An error occurred while retrieving '%s':\s*%s" % (re.escape(key),
                                                                  re.escape(message))
        if not re.search(rx, pre):
            problems.append("console: no %r error for %s" % (message, key))
    unexpected = sorted(set(ERROR_LINE.findall(pre)) - {k for k, _ in want})
    if unexpected:
        problems.append("console: unexpected settings error for %s" % ", ".join(unexpected))
    if "safe mode" in pre.lower():
        problems.append("console: board reports safe mode")
    return problems


def device_problems(results, exp):
    """Failed or missing device checks. Returns (problems, skip reason or None)."""
    if results.crashed:
        return ["device run failed: " + results.crashed[-300:]], None
    if results.err:
        return ["device code raised: " + results.err[-300:]], None
    if "getenv" in results.checks:
        return [], results.checks["getenv"][0].get("detail") or "no os.getenv"
    want = (["getenv:" + k for k, _ in exp["getenv"]]
            + ["getenv_none:" + k for k in exp["getenv_none"]]
            + ["getenv_present:" + k for k in exp["getenv_present"]]
            + ["getenv_raises:" + k for k, _ in exp["getenv_raises"]])
    if "get_setting" not in results.checks:
        want += (["setting:" + k for k, _ in exp["setting"]]
                 + ["setting_error:" + k for k in exp["setting_error"]])
    if exp["connected"] is not None and "wifi_connected" not in results.checks:
        want.append("wifi_connected")
    problems = ["device never reported %s" % n for n in want if n not in results.checks]
    for name, rs in sorted(results.checks.items()):
        for r in rs:
            if r.get("ok") is False:
                problems.append("%s: %s" % (name, r.get("detail", "")))
    return problems, None


def write_settings(board, backups, data):
    """settings.toml <- data (None removes it), through flashed_fs's own path: apply_tree with
    the run's FsBackup for this board, so the original is restored at the end of the run."""
    bk = backups.setdefault(board.name, FsBackup(board))
    if data is not None:
        return apply_tree(board, {"settings.toml": data}, bk)
    mp = board.mount()
    if not mp:
        raise farmlib.FarmError("%s: CIRCUITPY not mounted" % board.name)
    bk.save(mp, "settings.toml")
    board.remove_file("settings.toml")
    return ["settings.toml"]


@pytest.fixture(scope="module")
def farm_settings(board, flashed_fs, secrets, _fs_backups):  # pylint: disable=unused-argument
    """After the module, put the farm's settings.toml (from `secrets`) back, the way flashed_fs
    wrote it, so the next module does not boot with the last scenario."""
    yield
    try:
        changed = write_settings(board, _fs_backups, settings_toml(secrets).encode())
        if changed and "wifi" in board.caps and not hard_reset(board):
            pytest.fail("%s did not come back after restoring settings.toml" % board.name,
                        pytrace=False)
    except farmlib.FarmError as e:
        pytest.fail("restoring settings.toml: %s" % e, pytrace=False)


@pytest.mark.destructive
@pytest.mark.parametrize("secrets_name", PARAMS, indirect=True)
def test_scenario(request, board, farm_settings, flashed_firmware, _fs_backups, secrets_name,
                  secrets_path, secrets_scenarios, good_secrets, serial, record_property):
    """One settings.toml scenario on one board: what CircuitPython printed, read and joined."""
    # pylint: disable=unused-argument,too-many-arguments,too-many-locals
    why = not_applicable(request.node, board)  # needs("wifi") on wifi-only scenarios
    if why:
        pytest.skip(why)
    entry = secrets_scenarios[secrets_name]
    wifi_board = "wifi" in board.caps
    exp = expectations(entry, good_secrets, wifi_board)

    # --- Arrange ------------------------------------------------------
    # 1. The scenario is rendered (secrets_path) and written to CIRCUITPY/settings.toml.
    data = None if secrets_path is None else secrets_path.read_bytes()
    serial.close()  # the reset below takes the port away; reopened from sysfs after it
    try:
        record_property("written", ",".join(write_settings(board, _fs_backups, data))
                        or "unchanged")
    except farmlib.FarmError as e:
        pytest.fail(str(e), pytrace=False)

    # --- Act ----------------------------------------------------------
    # 2. Hard reset on wifi boards (a soft reload keeps the supervisor's WiFi connection),
    #    then a soft reboot everywhere, capturing what the supervisor prints before code.py.
    if wifi_board:
        if not hard_reset(board):
            pytest.fail("%s did not come back after the reset" % board.name, pytrace=False)
    else:
        time.sleep(1)  # let the auto-reload from the write start before the REPL is opened
    try:
        serial.repl = board.repl()
        serial.repl.prompt(timeout=60)  # a failing WiFi join can hold the REPL for seconds
        serial.reset(timeout=30)
    except farmlib.FarmError as e:
        pytest.fail("%s: %s" % (board.name, e), pytrace=False)
    pre = serial.expect(rb"(?s)(.*?)code\.py output:", timeout=60).group(1)
    serial.expect(r"hello world", timeout=20)  # code.py ran: CircuitPython never halts here

    # --- Assert -------------------------------------------------------
    # 3. The settings errors the scenario expects on the console, and no others.
    problems = console_problems(pre.decode("utf-8", "replace"), entry, wifi_board)

    # 4. On the device: os.getenv, supervisor.get_setting, wifi.radio.connected.
    try:
        serial.repl.prompt(timeout=60)
        res, out, err = serial.repl.run_device(device_code(exp), timeout=60)
        results = DeviceResults(board, res, out, err)
    except Exception as e:  # pylint: disable=broad-except
        results = DeviceResults(board, [], "", "", crashed="%s: %s" % (type(e).__name__, e))
    more, skipped = device_problems(results, exp)
    problems += more

    # 5. boot_out.txt still names this board and the version flashed_firmware recorded.
    boot = board.boot_out()
    if "Board ID:%s" % board.board_id not in boot:
        problems.append("boot_out.txt does not name %s" % board.board_id)
    if flashed_firmware.get("version") and board.version() != flashed_firmware["version"]:
        problems.append("boot_out.txt reports %s, flashed %s"
                        % (board.version(), flashed_firmware["version"]))

    assert not problems, "%s on %s (expected: %s)\n%s" % (
        secrets_name, board.name, entry["cp"], "\n".join(problems))
    if skipped:
        pytest.skip("device checks not run: %s" % skipped)


# ---------------------------------------------------------------- host only, no board

DUMMY_CFG = {"network": {"allowed_ssids": ["farmnet"]}}
DUMMY_GOOD = {SSID: "farmnet", PASSWORD: "farm-pass-0123",
              "CIRCUITPY_WEB_API_PASSWORD": WEB_API_PASSWORD,
              "CPR_BROKER_HOST": "192.168.0.76", "CPR_BROKER_PORT": 1884}
DUMMY_DEFAULT = {SSID: PLACEHOLDER_SSID, PASSWORD: PLACEHOLDER_PASSWORD}
RENDERED = [n for n, e in SCENARIOS.items() if e["source"] in ("generated", "file")]


def _dummy_bytes(name):
    entry = SCENARIOS[name]
    if entry["source"] == "generated":
        return render(build_generated(DUMMY_DEFAULT, DUMMY_GOOD)[name])
    with open(DEFAULT_SECRETS_DIR / (name + SECRETS_SUFFIX), "rb") as f:
        return fill(f.read(), substitutions(DUMMY_GOOD))


@pytest.mark.smoke
def test_manifest_matches_sources():
    """Every scenario has exactly the source it names, and nothing is left without an entry."""
    gen = build_generated(DUMMY_DEFAULT, DUMMY_GOOD)
    files = {p.name[:-len(SECRETS_SUFFIX)] for p in DEFAULT_SECRETS_DIR.glob("*" + SECRETS_SUFFIX)}
    problems = []
    for name, e in SCENARIOS.items():
        for key in ("source", "brent", "spec", "spec_ref", "cp"):
            if key not in e:
                problems.append("%s: no %s" % (name, key))
        src = e.get("source")
        if src == "generated" and name not in gen:
            problems.append("%s: build_generated does not make it" % name)
        if src == "file" and name not in files:
            problems.append("%s: no %s%s" % (name, name, SECRETS_SUFFIX))
        if src == "none" and name != NO_SECRETS_FILE:
            problems.append("%s: only %s has no file" % (name, NO_SECRETS_FILE))
        if src not in ("generated", "file", "none", "not_ported"):
            problems.append("%s: unknown source %r" % (name, src))
    problems += ["%s: generated but not in scenarios.toml" % n for n in set(gen) - set(SCENARIOS)]
    problems += ["%s%s: not in scenarios.toml" % (n, SECRETS_SUFFIX)
                 for n in files - set(SCENARIOS)]
    assert not problems, "\n".join(problems)


@pytest.mark.smoke
@pytest.mark.parametrize("name", RENDERED)
def test_spec_verdict(name):
    """scenarios.toml's spec field agrees with Python's tomllib (TOML 1.0.0)."""
    try:
        tomllib.loads(_dummy_bytes(name).decode("utf-8"))
        verdict = "valid"
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        verdict = "invalid"
    assert verdict == SCENARIOS[name]["spec"], SCENARIOS[name]["spec_ref"]


@pytest.mark.smoke
@pytest.mark.parametrize("name", RENDERED)
def test_scenario_passes_guard(name):
    """Every scenario the farm writes passes scenario_guard."""
    assert scenario_guard(DUMMY_CFG, {}, _dummy_bytes(name), DUMMY_GOOD[PASSWORD]) == []


@pytest.mark.smoke
def test_generated_expectations_consistent():
    """getenv expectations of generated scenarios match the values build_generated wrote."""
    gen = build_generated(DUMMY_DEFAULT, DUMMY_GOOD)
    subs = _raw_subs(DUMMY_GOOD)
    problems = []
    for name, value in gen.items():
        if not isinstance(value, dict):
            continue
        for key, want in SCENARIOS[name]["getenv"].items():
            for ph, val in subs.items():
                want = want.replace(ph, val)
            if key in value and str(value[key]) != want:
                problems.append("%s: %s is %r, scenarios.toml expects %r"
                                % (name, key, value[key], want))
    assert not problems, "\n".join(problems)


GUARD_REFUSES = {
    "public_broker": settings_toml(dict(DUMMY_GOOD, CPR_BROKER_HOST="52.1.2.3")),
    "broker_hostname": 'CPR_BROKER_HOST = "broker.example.com"\n',
    "aio_host": 'CPR_BROKER_HOST = "io.adafruit.com"\n',
    "aio_key_name": 'ADAFRUIT_AIO_KEY = "x"\n',
    "aio_key_value": 'CPR_X = "aio_%s"\n' % ("a" * 28),
    "password_with_fake_ssid": settings_toml(dict(DUMMY_GOOD, CIRCUITPY_WIFI_SSID="cpr-fake-x")),
    "foreign_ssid": 'CIRCUITPY_WIFI_SSID = "office-wifi"\nCIRCUITPY_WIFI_PASSWORD = "x"\n',
    "password_with_foreign_literal": "CIRCUITPY_WIFI_SSID = 'office'\n"
                                     'CIRCUITPY_WIFI_PASSWORD = "farm-pass-0123"\n',
}


@pytest.mark.smoke
@pytest.mark.parametrize("case", sorted(GUARD_REFUSES))
def test_guard_refuses(case):
    """scenario_guard stops files that would leak the farm password or leave the farm."""
    assert scenario_guard(DUMMY_CFG, {}, GUARD_REFUSES[case].encode(), DUMMY_GOOD[PASSWORD])
