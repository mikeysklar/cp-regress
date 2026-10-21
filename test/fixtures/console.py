# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""serial (function scope), after Brent's serial fixture: a SerialMonitor on the console.

    def test_boots(serial):
        serial.soft_reset()                    # or serial.reset(), the older name
        serial.expect("code.py output:")
        serial.assert_none(r"Traceback", duration=2)

The monitor (lib/serialmon.py) reads the console on a background thread from the moment the
fixture opens it, so nothing printed during the test is lost between expect() calls. It does not
reset the board; the test does, as its Act step: soft_reset() (Ctrl-D), hard_reset()
(microcontroller.reset()) or port_reset() (uhubctl power cycle). For running code and asserting
on check() results, use `device`; this is for tests that watch what the board prints.

Every test's capture is written on teardown, pass or fail, to
<serial log dir>/<board>/<test node id>.serial.log, with lines stamped HH:MM:SS.mmm. The
directory is --serial-log-dir (Brent's option name, hil/firmware/conftest.py:125 @ 242a162) or
results/pytest-<stamp>/serial, one per run, shared by all xdist workers. The path goes into the
report (user_properties "serial_log", shown in --junitxml, and a "serial log" section on
failures).
"""
import hashlib
import logging
import os
import re
import time

import pytest

import farmlib
import run as cpr_run  # HERE: our logs go under results/, beside run.py's results/<stamp>/
from serialmon import SerialMonitor
from serialmon import SerialError, SerialLine, SerialTimeout, SerialUnexpectedError  # noqa: F401

log = logging.getLogger("cpr.serial")


def pytest_addoption(parser):
    parser.getgroup("cp-regress").addoption(
        "--serial-log-dir", default="",
        help="write each serial test's console capture under this folder "
             "(default results/pytest-<stamp>/serial)")


def pytest_configure(config):
    """Pick the run's serial log folder once; xdist workers get the controller's."""
    win = getattr(config, "workerinput", None)
    if win and win.get("cpr_serial_log_dir"):
        config.cpr_serial_log_dir = win["cpr_serial_log_dir"]
        return
    d = config.getoption("serial_log_dir")
    if not d:
        d = os.path.join(cpr_run.HERE, "results", "pytest-" + time.strftime("%Y%m%d-%H%M%S"),
                         "serial")
    config.cpr_serial_log_dir = os.path.abspath(d)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    """xdist controller -> worker: the same folder for every worker's logs."""
    node.workerinput["cpr_serial_log_dir"] = node.config.cpr_serial_log_dir


def log_name(nodeid):
    """A file name from a test node id; long ids keep a hash so they stay unique."""
    nodeid = re.sub(r"@[^/:\[\]]*$", "", nodeid)  # xdist loadgroup's "@<board>" suffix
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid).strip("_")
    if len(name) > 180:
        name = name[:160] + "_" + hashlib.sha1(nodeid.encode()).hexdigest()[:12]
    return name + ".serial.log"


def serial_log_path(config, board, nodeid):
    return os.path.join(config.cpr_serial_log_dir, board.name, log_name(nodeid))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Put the capture's path on a failing test's report, next to the captured log."""
    outcome = yield
    path = getattr(item, "cpr_serial_log", None)
    if path and call.when == "call":
        outcome.get_result().sections.append(("serial log", path))


@pytest.fixture
def serial(board, request):
    """SerialMonitor on the board's console; closed and dumped after the test."""
    path = serial_log_path(request.config, board, request.node.nodeid)
    try:
        mon = SerialMonitor(board)
    except farmlib.FarmError as e:
        # still leave a log, so every serial test has one
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[%s] --- cpr: console did not open: %s\n" % (time.strftime("%H:%M:%S"), e))
        request.node.user_properties.append(("serial_log", path))
        pytest.fail(str(e), pytrace=False)
    request.node.cpr_serial_log = path
    try:
        yield mon
    finally:
        mon.close()
        mon.dump(path)
        request.node.user_properties.append(("serial_log", path))
        log.info("serial log: %s", path)
