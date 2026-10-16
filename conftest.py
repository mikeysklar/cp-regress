# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""
pytest entry point for cp-regress.

This file is loaded for every test because it sits in the project root. It puts lib/ and test/
on the path, loads every fixture module in test/fixtures as a plugin (the same layout as
Adafruit_Wippersnapper_Python), adds the farm command-line options, and turns farm.toml boards
into a "board" parameter on every test that asks for one.

farm.toml is checked at session start (fixtures.farm.validate_farm). A board entry can narrow
which tests run on it, after Brent's run_tests/skip_tests (conftest.py:130-180 @ cf64e03), with
paths relative to test/: a folder ("hardware/"), a file ("hardware/wifi_join_test.py") or a test
("hardware/core_vm_test.py::test_vid_pid"):

    run_tests = ["hardware/"]                      # only these run on this board; omit for all
    skip_tests = ["hardware/ble_pair_test.py"]     # these are reported as skipped

They apply on top of --tier, --boards and the needs/wiring/skip_* markers, never instead of
them. Selection on the command line (paths, -k, -m) narrows further; it can't widen it.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, os.path.join(HERE, "test"))
sys.path.insert(0, HERE)

try:  # optional, like Wippersnapper: put CPR_WIFI_SSID / CPR_WIFI_PASSWORD in .env
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"), override=False)
except ImportError:
    pass

# pylint: disable=wrong-import-position
from fixtures import ALL_FIXTURES  # noqa: E402

# Every test/fixtures/*.py, found by glob, so a fixture module another branch adds (identity.py
# with device_uid, broker.py with catcher) registers once it lands and is simply absent before.
pytest_plugins = ALL_FIXTURES

TIERS = ["smoke", "nightly", "release"]
TEST_DIR = Path(HERE) / "test"


def pytest_addoption(parser):
    """Farm options. Tiers are cumulative: nightly includes smoke, release includes both."""
    g = parser.getgroup("cp-regress")
    g.addoption("--farm", default=os.path.join(HERE, "farm.toml"), help="farm description (farm.toml)")
    g.addoption("--boards", default="all", help="comma-separated board names from farm.toml")
    g.addoption("--tier", default="smoke", choices=TIERS, help="highest tier to run (default smoke)")
    g.addoption("--label", default="", help="free text stored with this run's metrics")
    g.addoption("--expect-version", default="",
                help="substring every board's boot_out.txt must contain, e.g. 11.0.0-alpha")
    g.addoption("--no-history", action="store_true",
                help="do not add this run's metrics to history/metrics.jsonl")
    g.addoption("--cpr-dry-run", action="store_true",
                help="collect and apply board skips, but touch no hardware")
    g.addoption("--firmware", default="",
                help="flash each board from this folder first (files named by board_id); "
                     "skipped per board when that image is already running")
    g.addoption("--flash-force", action="store_true", help="flash even when unchanged")
    g.addoption("--bundle", default="",
                help="pinned library bundle folder (with lib/) for modules that list LIBS")
    g.addoption("--keep-fs", action="store_true",
                help="leave flashed_fs files on the boards instead of restoring the originals")
    g.addoption("--broker-keep", action="store_true",
                help="leave ProtoMQ running after the run if this run started it")
    g.addoption("--list-boards", action="store_true",
                help="check farm.toml, list its boards and their test selectors, and exit")


def load_farm(path):
    """farm.toml as (cfg, [farmlib.Board], warnings), checked first.

    Bad entries raise pytest.UsageError naming every problem at once; warnings (unknown keys or
    caps, which may just be newer than the check) are returned for the caller to show.
    """
    import farmlib  # pylint: disable=import-outside-toplevel
    from fixtures.farm import validate_farm  # pylint: disable=import-outside-toplevel

    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore  # pylint: disable=import-outside-toplevel

    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise pytest.UsageError("cannot read farm file %s: %s" % (path, e)) from None
    errors, warnings = validate_farm(cfg, str(TEST_DIR))
    if errors:
        raise pytest.UsageError("%s:\n  %s" % (path, "\n  ".join(errors)))
    boards = [farmlib.Board(b, cfg.get("host", {})) for b in cfg.get("board", [])]
    return cfg, boards, warnings


def board_selectors(cfg):
    """board name -> (run_tests or None, skip_tests or None), normalized."""
    return {e["name"]: (_test_selectors(e, "run_tests"), _test_selectors(e, "skip_tests"))
            for e in cfg.get("board", [])}


def pytest_configure(config):
    """Load and check farm.toml once and pick the boards this run uses."""
    # Brent's marker for tests that need a board (tests_hil_board_utils/*.py @ cf64e03). Here
    # every board-parametrized test gets it, so his files collect and -m "not hardware" works.
    config.addinivalue_line("markers", "hardware: needs a farm board (put on every test that "
                                       "takes `board`; Brent's name)")
    cfg, boards, warnings = load_farm(config.getoption("farm"))
    for w in warnings:
        config.issue_config_time_warning(pytest.PytestConfigWarning("farm.toml: " + w),
                                         stacklevel=2)
    want = config.getoption("boards")
    selected = [b.name for b in boards] if want == "all" else [n for n in want.split(",") if n]
    unknown = [n for n in selected if n not in {b.name for b in boards}]
    if unknown:
        raise pytest.UsageError("unknown board(s) %s; farm.toml has %s"
                                % (",".join(unknown), ",".join(b.name for b in boards)))
    config.cpr_cfg = cfg
    config.cpr_boards = boards
    config.cpr_selected = selected
    config.cpr_selectors = board_selectors(cfg)
    config.cpr_down = {}        # board -> why it is unusable for the rest of this run
    config.cpr_preflight = {}   # board -> (ok, why) from its first health check
    config.cpr_flashed = {}     # board -> what flashed_firmware did this run


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """--list-boards: check farm.toml and print it, the way run.py --list prints tests.

    Reads sysfs for the "bus" column and nothing else: opens no tty, mounts nothing.
    """
    if not config.getoption("list_boards"):
        return None
    cfg, boards, warnings = load_farm(config.getoption("farm"))
    selectors = board_selectors(cfg)
    fmt = "%-18s %-26s %-11s %-11s %-8s %-11s %s"
    print(fmt % ("name", "board_id", "usb", "port", "flash", "r/w/settle", "bus"))
    for b in boards:
        p = b.power
        power = "%s/%s/%s" % (p.get("r", 30), p.get("w", 500), p.get("settle", 12))
        print(fmt % (b.name, b.board_id, b.usb, b.port, b.flash.get("method", "-"), power,
                     "yes" if b.present() else "no"))
        run, skip = selectors[b.name]
        if run is not None:
            print("    run_tests:  " + (", ".join(run) or "(empty: every test deselected)"))
        if skip:
            print("    skip_tests: " + ", ".join(skip))
    for w in warnings:
        print("warning: " + w)
    return 0


def pytest_generate_tests(metafunc):
    """Every test that takes `board` runs once per selected board, grouped by module."""
    if "board" in metafunc.fixturenames:
        names = metafunc.config.cpr_selected
        metafunc.parametrize("board", names, indirect=True, scope="module", ids=names)


def item_tier(item):
    """The tier marker on a test or its module; unmarked tests count as nightly."""
    for t in TIERS:
        if item.get_closest_marker(t):
            return t
    return "nightly"


def _test_selectors(entry, key):
    """The board entry's run_tests/skip_tests list, normalized, or None if it isn't set.

    After Brent's _test_selectors (conftest.py:130 @ cf64e03). The type check is in
    validate_board_entry, which load_farm has already run, so this only normalizes.
    """
    from fixtures.farm import normalize_selector  # pylint: disable=import-outside-toplevel

    selectors = entry.get(key)
    if selectors is None:
        return None
    return [normalize_selector(s) for s in selectors]


def _matches(test_id, selectors):
    """Whether test_id (relative to test/) is in, or is, one of selectors: a folder, a file, or
    a test with or without its params. Brent's _matches, conftest.py:144 @ cf64e03."""
    return any(test_id == s or test_id.startswith((s + "/", s + "::", s + "["))
               for s in selectors)


def _test_id(item):
    """item's path relative to test/ plus the rest of its nodeid, or None outside test/."""
    try:
        test_id = Path(item.path).relative_to(TEST_DIR).as_posix()
    except ValueError:
        return None
    if "::" in item.nodeid:
        test_id += "::" + item.nodeid.split("::", 1)[1]
    return test_id


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Drop tests above --tier, apply each board's run_tests/skip_tests, and pin each board's
    tests to one xdist worker.

    tryfirst: xdist reads xdist_group markers in its own hook, so ours must run before it, or
    one board's tests get spread over several workers that then take turns on its REPL. It also
    puts the hardware marker on before -m deselects.

    run_tests/skip_tests are judged per item against the board it is parametrized with, so a
    module can run on one board and be skipped on another. Tests that take no board are never
    touched by them, like Brent's tests outside FIRMWARE_DIR (conftest.py:164 @ cf64e03).
    """
    max_tier = TIERS.index(config.getoption("tier"))
    selectors = getattr(config, "cpr_selectors", {})
    keep, drop = [], []
    for item in items:
        if TIERS.index(item_tier(item)) > max_tier:
            drop.append(item)
            continue
        callspec = getattr(item, "callspec", None)
        if callspec is None or "board" not in callspec.params:
            keep.append(item)
            continue
        name = callspec.params["board"]
        item.add_marker(pytest.mark.hardware)
        run, skip = selectors.get(name, (None, None))
        test_id = _test_id(item)
        if test_id is not None and run is not None and not _matches(test_id, run):
            drop.append(item)
            continue
        if test_id is not None and skip and _matches(test_id, skip):
            item.add_marker(pytest.mark.skip(reason="in skip_tests for board '%s'" % name))
        # with `-n 8 --dist loadgroup` each board gets its own worker, like run.py --jobs
        item.add_marker(pytest.mark.xdist_group(name))
        keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
