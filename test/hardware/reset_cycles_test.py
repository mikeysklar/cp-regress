# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Soft reboots, hard resets, and power cycles, and recovery from each.

pytest version of tests/reset_cycles.py. The device code constants and the host helpers
(_wait_reenumerated, _recover) are copied from the original unchanged. The whole sequence
(10 soft reboots with a known code.py inside board.preserved(), restore, 3 hard resets on
reset_ok boards, 2 power cycles) runs once per board in the module-scoped `flow` fixture, which
records every check and metric the original would have made. Each check is then one test here,
in the original's order; the per-iteration families are parametrized over the original's names.

Targets: #10934 (soft reboot instability), #11380 (reset recovery timing).
"""

import logging
import statistics
import time

import pytest

import farmlib

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(10934, 11380),
    pytest.mark.destructive,
]

# Deterministic code.py for the soft reboot loop: finishes at once, so every reboot
# ends at "Press any key to enter the REPL" whatever the user's code.py does.
SOFT_CODE = 'print("cpr-reset-cycles")\n'
DONE_TOKEN = b"Press any key to enter the REPL"
RESET_REASON = "import microcontroller\nprint('@@RR', microcontroller.cpu.reset_reason)\n"

# Check names the original builds in its loops, in the order it makes them.
SOFT_REBOOT_CHECKS = ["soft_reboot_%d" % i for i in range(10)]
HARD_RESET_CHECKS = [n % i for i in range(3)
                     for n in ("hard_reset_%d_returned", "hard_reset_%d_recovery", "hard_reset_%d_version")]
POWER_CYCLE_CHECKS = [n % i for i in range(2)
                      for n in ("power_cycle_%d_ready", "power_cycle_%d_recovery", "power_cycle_%d_version",
                                "power_cycle_%d_reset_reason")]


def _wait_reenumerated(board, old_devnum, timeout=20):
    """True once the board has left the bus (devnum changed or gone)."""
    end = time.time() + timeout
    while time.time() < end:
        if board.devnum() != old_devnum:
            return True
        time.sleep(0.1)
    return False


def _recover(board, ctx, name):
    """Bring the board back with a power cycle; record the outcome."""
    try:
        ok = board.power_cycle() is not None
        ctx.check(name, ok, "recovery power cycle")
    except farmlib.FarmError as e:
        ctx.check(name, False, str(e)[:200])


class _Record:
    """What the flow saw, in the shape the original's code expects from ctx.

    check() and skip() store results by name, in order; metric() keeps the raw number so the
    test can hand it to the metric fixture; log() goes to the logging module.
    """

    def __init__(self):
        self.checks = {}      # name -> (ok, detail); ok is None for a skip
        self.metrics = {}     # name -> (value, unit, better)
        self.reached = set()  # stages the flow got past
        self.error = None     # an unexpected host-side exception

    def check(self, name, ok, detail="", value=None):  # pylint: disable=unused-argument
        self.checks[name] = (bool(ok), str(detail)[:500])
        return bool(ok)

    def skip(self, name, why=""):
        self.checks[name] = (None, why)

    def metric(self, name, value, unit="", better="lower", tolerance=0.25):  # pylint: disable=unused-argument
        self.metrics[name] = (value, unit, better)

    def log(self, msg):
        logging.getLogger(__name__).info(msg)


def _run_flow(board, ctx):
    """The original run(), with ctx a _Record. Stage marks are the only additions."""
    ctx.check("present", board.present(), board.usb)
    v_start = board.version()
    ctx.check("version_before", v_start is not None, "version %s" % v_start)

    # Soft reboots via Ctrl-D in the REPL, with a known code.py.
    soft_reboot_times = []
    try:
        with board.preserved(files=("code.py",)):
            board.write_file("code.py", SOFT_CODE)
            time.sleep(2)  # let the autoreload from the write settle before we connect
            with board.repl() as repl:
                for i in range(10):
                    try:
                        repl.soft_reboot(timeout=20)
                        t0 = time.time()
                        text = repl.read_until(DONE_TOKEN, timeout=20)
                        soft_reboot_times.append(time.time() - t0)
                        ctx.check("soft_reboot_%d" % i, b"cpr-reset-cycles" in text,
                                  "code.py output %s" % ("seen" if b"cpr-reset-cycles" in text else "missing"))
                    except Exception as e:  # noqa: BLE001 - FarmError or SerialException
                        ctx.check("soft_reboot_%d" % i, False, str(e)[:200])
                        break
    except Exception as e:  # noqa: BLE001
        ctx.check("soft_reboot_restore", False, "%s: %s" % (type(e).__name__, str(e)[:200]))
    ctx.reached.add("soft_reboot_restore")

    if soft_reboot_times:
        median_ms = statistics.median(soft_reboot_times) * 1000
        ctx.metric("soft_reboot_ms", median_ms, "ms", better="lower")

    # The restored code.py runs on the next soft reboot; then make sure the board is ready.
    try:
        with board.repl() as repl:
            repl.soft_reboot(timeout=20)
        ctx.check("soft_restore_ready", board.wait_ready() is not None, "after restoring code.py")
    except Exception as e:  # noqa: BLE001
        ctx.check("soft_restore_ready", False, str(e)[:200])
        _recover(board, ctx, "soft_restore_recovery")
    ctx.reached.add("soft_restore")

    # Hard resets via microcontroller.reset() - only on reset_ok boards.
    if "reset_ok" in board.caps:
        hard_reset_times = []
        for i in range(3):
            old_devnum = board.devnum()
            returned = None
            try:
                with board.repl() as repl:
                    returned = repl.exec_raw("import microcontroller\nmicrocontroller.reset()\n", timeout=10)
            except Exception:  # noqa: BLE001 - the port vanishes mid-read when reset works
                pass
            if returned is not None:
                ctx.check("hard_reset_%d_returned" % i, False,
                          "reset() returned: %s" % (returned[1].strip()[-200:] or "no error"))
                break

            gone = _wait_reenumerated(board, old_devnum)
            wait_result = board.wait_ready(timeout=60, need_mount=True)
            ctx.check("hard_reset_%d_returned" % i, gone and wait_result is not None,
                      "devnum %s -> %s, ready %s" % (old_devnum, board.devnum(), wait_result))
            if wait_result is None:
                _recover(board, ctx, "hard_reset_%d_recovery" % i)
                break
            v = board.version()
            ctx.check("hard_reset_%d_version" % i, v is not None and v == v_start,
                      "version %s -> %s" % (v_start, v))
            hard_reset_times.append(wait_result)
        if hard_reset_times:
            ctx.metric("hard_reset_to_tty_s", statistics.median(hard_reset_times), "s", better="lower")
    else:
        ctx.skip("hard_reset", "board lacks reset_ok capability")
    ctx.reached.add("hard_reset")

    # Power cycles via board.power_cycle().
    power_cycle_times = []
    for i in range(2):
        v_before = board.version()
        try:
            wait_result = board.power_cycle()
        except farmlib.FarmError as e:
            ctx.check("power_cycle_%d_ready" % i, False, str(e)[:200])
            break

        ctx.check("power_cycle_%d_ready" % i, wait_result is not None,
                  "ready after %s s" % wait_result)
        if wait_result is None:
            _recover(board, ctx, "power_cycle_%d_recovery" % i)
            break

        v_after = board.version()
        ctx.check("power_cycle_%d_version" % i, v_after is not None and v_before == v_after,
                  "version %s -> %s" % (v_before, v_after))
        power_cycle_times.append(wait_result)

        # Did the VBUS cycle really reset the MCU? Some farm ports do not cut power.
        try:
            with board.repl() as repl:
                out, err = repl.exec_raw(RESET_REASON, timeout=10)
            if "POWER_ON" in out:
                ctx.check("power_cycle_%d_reset_reason" % i, True, out.strip()[-100:])
            else:
                ctx.skip("power_cycle_%d_reset_reason" % i,
                         "not POWER_ON (VBUS cycle may not reset this board): %s" % (out.strip() or err.strip())[-150:])
        except Exception as e:  # noqa: BLE001
            ctx.check("power_cycle_%d_reset_reason" % i, False, str(e)[:200])

    if power_cycle_times:
        ctx.metric("power_cycle_ready_s", statistics.median(power_cycle_times), "s", better="lower")
    ctx.reached.add("power_cycle")


@pytest.fixture(scope="module")
def flow(board):
    """Run the whole reset sequence once per board and return its _Record.

    The code.py restore is board.preserved() inside the flow, as in the original; the board
    fixture's teardown health check power-cycles a board the flow left unhealthy.
    """
    rec = _Record()
    try:
        _run_flow(board, rec)
    except Exception as e:  # pylint: disable=broad-except
        rec.error = "%s: %s" % (type(e).__name__, str(e)[:300])
    return rec


def _why_missing(flow):
    if flow.error:
        return "not reached: host-side exception (see test_no_host_error): " + flow.error
    failed = [n for n, (ok, _) in flow.checks.items() if ok is False]
    if failed:
        return "not reached: the original stops this loop after a failure (first failure %s)" % failed[0]
    return "not made: the original makes this check only on another branch"


def _expect(flow, name):
    """Assert the check the flow recorded under `name`, skip if it was skipped or never made."""
    if name not in flow.checks:
        pytest.skip(_why_missing(flow))
    ok, detail = flow.checks[name]
    if ok is None:
        pytest.skip(detail or "skipped")
    assert ok, detail


def _skip_unneeded_recovery(flow, name, main_suffix):
    """Skip a *_recovery check whose iteration's main check passed (the original makes it only
    when that check's wait_ready came back None). Any other missing recovery falls to _expect,
    which says the loop never got there."""
    if not name.endswith("_recovery") or name in flow.checks:
        return
    main = name[:-len("_recovery")] + main_suffix
    if flow.checks.get(main, (None, ""))[0] is True:
        pytest.skip("no recovery needed")


def _metric(flow, metric, name, why):
    if name not in flow.metrics:
        pytest.skip(flow.error and _why_missing(flow) or why)
    value, unit, better = flow.metrics[name]
    metric(name, value, unit, better)


def test_present(flow):
    """The board is on the bus at its hub port."""
    _expect(flow, "present")


def test_version_before(flow):
    """boot_out.txt version is readable before any reset."""
    _expect(flow, "version_before")


@pytest.mark.parametrize("name", SOFT_REBOOT_CHECKS)
def test_soft_reboot(flow, name):
    """Ctrl-D soft reboot runs the one-line code.py and reaches "Press any key"."""
    _expect(flow, name)


def test_soft_reboot_restore(flow):
    """board.preserved() put the user's code.py back (the original checks only on an exception)."""
    if "soft_reboot_restore" not in flow.reached:
        pytest.skip(_why_missing(flow))
    if "soft_reboot_restore" in flow.checks:
        _expect(flow, "soft_reboot_restore")


def test_soft_reboot_ms(flow, metric):
    """Median time from "soft reboot" to "Press any key", against this board's history."""
    _metric(flow, metric, "soft_reboot_ms", "no soft reboot completed")


def test_soft_restore_ready(flow):
    """After restoring code.py, a soft reboot and wait_ready succeed."""
    _expect(flow, "soft_restore_ready")


def test_soft_restore_recovery(flow):
    """Recovery power cycle, made only when soft_restore_ready failed."""
    if "soft_restore" in flow.reached and "soft_restore_recovery" not in flow.checks:
        pytest.skip("no recovery needed")
    _expect(flow, "soft_restore_recovery")


def test_hard_reset(flow):
    """Skipped on boards without reset_ok, as in the original."""
    if "hard_reset" in flow.reached and "hard_reset" not in flow.checks:
        pytest.skip("board has reset_ok: hard resets ran, see test_hard_reset_cycle")
    _expect(flow, "hard_reset")


@pytest.mark.parametrize("name", HARD_RESET_CHECKS)
def test_hard_reset_cycle(flow, name):
    """microcontroller.reset() does not return, the board re-enumerates, same version after."""
    if "hard_reset" in flow.checks:
        pytest.skip(flow.checks["hard_reset"][1])
    _skip_unneeded_recovery(flow, name, "_returned")
    _expect(flow, name)


def test_hard_reset_to_tty_s(flow, metric):
    """Median time from disconnect to tty + mount after a hard reset."""
    if "hard_reset" in flow.checks:
        pytest.skip(flow.checks["hard_reset"][1])
    _metric(flow, metric, "hard_reset_to_tty_s", "no hard reset completed")


@pytest.mark.parametrize("name", POWER_CYCLE_CHECKS)
def test_power_cycle(flow, name):
    """board.power_cycle() comes back with the same version; reset_reason is POWER_ON."""
    _skip_unneeded_recovery(flow, name, "_ready")
    _expect(flow, name)


def test_power_cycle_ready_s(flow, metric):
    """Median time to tty + mount after the uhubctl cycle."""
    _metric(flow, metric, "power_cycle_ready_s", "no power cycle completed")


def test_no_host_error(flow):
    """The flow ran to the end without an unexpected host-side exception."""
    assert flow.error is None, flow.error
