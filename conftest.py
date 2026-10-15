# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""
pytest entry point for cp-regress.

This file is loaded for every test because it sits in the project root. It puts lib/ and test/
on the path, loads every fixture module in test/fixtures as a plugin (the same layout as
Adafruit_Wippersnapper_Python), adds the farm command-line options, and turns farm.toml boards
into a "board" parameter on every test that asks for one.
"""
import os
import sys

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

pytest_plugins = ALL_FIXTURES

TIERS = ["smoke", "nightly", "release"]


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


def pytest_configure(config):
    """Load farm.toml once and pick the boards this run uses."""
    import farmlib  # pylint: disable=import-outside-toplevel

    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore  # pylint: disable=import-outside-toplevel

    with open(config.getoption("farm"), "rb") as f:
        cfg = tomllib.load(f)
    boards = [farmlib.Board(b, cfg.get("host", {})) for b in cfg.get("board", [])]
    want = config.getoption("boards")
    selected = [b.name for b in boards] if want == "all" else [n for n in want.split(",") if n]
    unknown = [n for n in selected if n not in {b.name for b in boards}]
    if unknown:
        raise pytest.UsageError("unknown board(s) %s; farm.toml has %s"
                                % (",".join(unknown), ",".join(b.name for b in boards)))
    config.cpr_cfg = cfg
    config.cpr_boards = boards
    config.cpr_selected = selected
    config.cpr_down = {}        # board -> why it is unusable for the rest of this run
    config.cpr_preflight = {}   # board -> (ok, why) from its first health check


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


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Drop tests above --tier, and pin each board's tests to one xdist worker.

    tryfirst: xdist reads xdist_group markers in its own hook, so ours must run before it, or
    one board's tests get spread over several workers that then take turns on its REPL.
    """
    max_tier = TIERS.index(config.getoption("tier"))
    keep, drop = [], []
    for item in items:
        (keep if TIERS.index(item_tier(item)) <= max_tier else drop).append(item)
        callspec = getattr(item, "callspec", None)
        if callspec is not None and "board" in callspec.params:
            # with `-n 8 --dist loadgroup` each board gets its own worker, like run.py --jobs
            item.add_marker(pytest.mark.xdist_group(callspec.params["board"]))
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
