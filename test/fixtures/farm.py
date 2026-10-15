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
"""
import fcntl
import os
import time
from contextlib import contextmanager

import pytest

import run as cpr_run  # board_health, recover: the same checks run.py uses

LOCK_WAIT_S = 900


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
    if b.name in config.cpr_preflight:
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
            pytest.fail("%s did not survive this module: %s. Power-cycled: %s"
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
            pytest.skip("not reached: the board stopped answering earlier (%s)" % why[:160])
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
