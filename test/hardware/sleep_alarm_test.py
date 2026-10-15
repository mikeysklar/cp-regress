# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Deep sleep and light sleep alarm cycles.

pytest version of tests/sleep_alarm.py. The device code (DEVICE_LIGHT, CODE_PY) and the host
helpers (_collect_wakes, _back_to_normal) are copied from the original unchanged. Light sleep
runs once per board in the raw REPL (the `light` fixture); the deep sleep loop (write code.py,
soft reboot, listen for wake lines, restore, reboot back) runs once per board in the `deep`
fixture, which records every check the original would have made. Each check is then one test
here, in the original's order.

Targets: #5462, #2875, #10573, #9464, #10934, #6484, #2855, #5839.
"""

import logging
import statistics
import time

import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.skip_boards("feather_stm32f405"),  # STM32 has no reliable deep sleep with USB
    pytest.mark.targets(5462, 2875, 10573, 9464, 10934, 6484, 2855, 5839),
    pytest.mark.destructive,
]

DEEP_SLEEP_S = 5
CAPTURE_S = 27

# Light sleep in the raw REPL. Also tells the host whether alarm exists at all.
DEVICE_LIGHT = r'''
import time
try:
    import alarm
except ImportError:
    alarm = None

if alarm is None:
    skip("alarm_import", "no alarm module on this build")
else:
    check("alarm_import", True)
    try:
        t0 = time.monotonic_ns()
        ta = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 2)
        woke = alarm.light_sleep_until_alarms(ta)
        elapsed = (time.monotonic_ns() - t0) / 1e9
        metric("light_sleep_s", elapsed, "s")
        check("light_sleep_woke", isinstance(woke, alarm.time.TimeAlarm), "type=%s" % type(woke).__name__)
        check("light_sleep_wake_alarm", alarm.wake_alarm is woke, "wake_alarm=%r" % (alarm.wake_alarm,))
        check("light_sleep_timing", 1.9 <= elapsed <= 3.0, "elapsed=%.3f s for a 2 s alarm" % elapsed)
    except Exception as e:
        check("light_sleep_error", False, "%s: %s" % (type(e).__name__, e))
    try:
        alarm.time.TimeAlarm(monotonic_time=time.monotonic() - 1)
        check("past_alarm_raises", False, "TimeAlarm in the past did not raise")
    except ValueError as e:
        check("past_alarm_raises", True, str(e))
    except Exception as e:
        check("past_alarm_raises", False, "%s: %s" % (type(e).__name__, e))
'''

# code.py for the deep sleep loop. With USB attached CircuitPython pretends to deep
# sleep: the VM restarts on the alarm and sleep_memory survives.
CODE_PY = r'''
import alarm
import time

count = (alarm.sleep_memory[0] + 1) & 0xFF
alarm.sleep_memory[0] = count
wake_type = type(alarm.wake_alarm).__name__ if alarm.wake_alarm else "None"
print("CPR wake %d %s" % (count, wake_type))
alarm.exit_and_deep_sleep_until_alarms(alarm.time.TimeAlarm(monotonic_time=time.monotonic() + @@SLEEP@@))
'''


def _collect_wakes(repl, seconds):
    """Return [(host_time, line)] for each 'CPR wake' line seen within seconds."""
    import farmlib

    wakes = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            repl.read_until(b"CPR wake", timeout=max(0.1, end - time.time()))
            t = time.time()
            rest = repl.read_until(b"\n", timeout=2)
        except farmlib.FarmError:
            break
        wakes.append((t, "CPR wake " + rest.decode(errors="replace").strip()))
    return wakes


def _back_to_normal(board, ctx):
    """Files are restored: soft reboot so the user's code.py runs, then wait for the board."""
    try:
        with board.repl() as repl:
            repl.soft_reboot()
        ok = board.wait_ready() is not None
    except Exception as e:  # noqa: BLE001
        ctx.log("soft reboot after restore: %s" % e)
        ok = False
    if not ok:
        try:
            ok = board.power_cycle() is not None
        except Exception as e:  # noqa: BLE001
            ctx.log("power_cycle: %s" % e)
    ctx.check("ready_after_restore", ok, "board back after restoring code.py")


class _Record:
    """What the deep sleep flow saw, in the shape the original's helpers expect from ctx.

    check() and skip() store results by name; metric() keeps the raw number so the test can
    hand it to the metric fixture; log() goes to the logging module.
    """

    def __init__(self):
        self.checks = {}      # name -> (ok, detail); ok is None for a skip
        self.metrics = {}     # name -> (value, unit, better)
        self.stopped = None   # why the flow returned early, for "not reached" skips
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


def _run_deep(board, ctx):
    """The original run() after the light sleep part, with ctx a _Record."""
    import farmlib

    # ===== Deep sleep cycles with sleep_memory counter =====
    wakes = []
    try:
        with board.preserved():
            board.write_file("code.py", CODE_PY.replace("@@SLEEP@@", str(DEEP_SLEEP_S)))
            time.sleep(2)  # let the MSC write's autoreload settle before we interrupt it
            with board.repl() as repl:
                repl.soft_reboot()  # Ctrl-C to the REPL, then Ctrl-D: first run has no wake_alarm
                wakes = _collect_wakes(repl, CAPTURE_S)
                repl.prompt()  # any key ends the pretend deep sleep; park at the REPL for the restore
    except farmlib.FarmError as e:
        ctx.check("deep_sleep_harness", False, str(e))
    finally:
        _back_to_normal(board, ctx)

    lines = [w[1] for w in wakes]
    ctx.log("wake lines: %s" % lines)
    expect = CAPTURE_S // (DEEP_SLEEP_S + 1)
    ctx.check("wake_count", len(lines) >= expect, "%d wakes in %d s, expected >= %d" % (len(lines), CAPTURE_S, expect))
    if len(lines) < 2:
        ctx.stopped = "fewer than 2 wake lines (see wake_count)"
        return

    counters = []
    types = []
    for line in lines:
        parts = line.split()
        try:
            counters.append(int(parts[2]))
        except (IndexError, ValueError):
            counters.append(None)
        types.append(parts[3] if len(parts) >= 4 else "?")

    steps = [(b - a) & 0xFF if a is not None and b is not None else None for a, b in zip(counters, counters[1:])]
    ctx.check("sleep_memory_persists", all(s == 1 for s in steps), "counters=%s" % counters)
    ctx.check("first_wake_type", types[0] == "None", "after soft reboot wake_alarm=%s" % types[0])
    ctx.check("later_wake_types", all(t == "TimeAlarm" for t in types[1:]), "types=%s" % types[1:])

    intervals = [b[0] - a[0] for a, b in zip(wakes, wakes[1:])]
    med = statistics.median(intervals)
    ctx.metric("deep_sleep_interval_s", med, "s")
    ctx.check("deep_sleep_interval", DEEP_SLEEP_S - 0.5 <= med <= DEEP_SLEEP_S + 3,
              "median %.2f s for a %d s alarm, intervals=%s" % (med, DEEP_SLEEP_S, ["%.2f" % i for i in intervals]))


@pytest.fixture(scope="module")
def light(device):
    """DEVICE_LIGHT once per board (raw REPL, 30 s), as DeviceResults."""
    return device.run(DEVICE_LIGHT, timeout=30)


@pytest.fixture(scope="module")
def deep(board, light):
    """The deep sleep loop once per board, after light sleep found the alarm module."""
    rec = _Record()
    if light.crashed:
        rec.stopped = "the light sleep device run failed (see light_sleep_device)"
        return rec
    if not any(r.get("name") == "alarm_import" and r.get("ok") for r in light.records):
        rec.skip("deep_sleep", "alarm module not available")
        rec.stopped = "alarm module not available"
        return rec
    try:
        _run_deep(board, rec)
    except Exception as e:  # pylint: disable=broad-except
        rec.error = "%s: %s" % (type(e).__name__, e)
        rec.stopped = "host-side exception (see deep_sleep_harness): " + rec.error
    return rec


def _expect(deep, name):
    """Assert the check the flow recorded under `name`, skip if it was skipped or never made."""
    if name not in deep.checks:
        pytest.skip("not reached: %s" % (deep.stopped or "the original makes this check only on another branch"))
    ok, detail = deep.checks[name]
    if ok is None:
        pytest.skip(detail or "skipped")
    assert ok, detail


def _gate_light(light, name):
    """Skip a light sleep check the device did not reach (no alarm module, or light sleep raised)."""
    light.expect("alarm_import")  # skips when the build has no alarm, or the run never got there
    if "light_sleep_error" in light.checks:
        pytest.skip("not reached: light sleep raised (see light_sleep_error)")
    if light.err:  # charged once by test_device_exception
        pytest.skip("not reached: the device code raised (%s)" % light.err[-200:])
    pytest.fail("device never reported %r" % name, pytrace=False)


def _expect_light(light, name):
    if name not in light.checks:
        _gate_light(light, name)
    light.expect(name)


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_light_sleep_device(light):
    """DEVICE_LIGHT ran without the board dropping off (FarmError timeouts, serial errors)."""
    assert not light.crashed, light.crashed


def test_alarm_import(light):
    """import alarm works (a skip when the build has no alarm module)."""
    light.expect("alarm_import")


def test_light_sleep_s(light, metric):
    """light_sleep_until_alarms() time for a 2 s TimeAlarm, compared with this board's history."""
    if "light_sleep_s" not in light.metrics:
        _gate_light(light, "light_sleep_s")
    rec = light.metric_record("light_sleep_s")
    metric("light_sleep_s", rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def test_light_sleep_woke(light):
    """The returned alarm is a TimeAlarm (#10573)."""
    _expect_light(light, "light_sleep_woke")


def test_light_sleep_wake_alarm(light):
    """alarm.wake_alarm is the returned alarm."""
    _expect_light(light, "light_sleep_wake_alarm")


def test_light_sleep_timing(light):
    """A 2 s light sleep takes 1.9 to 3.0 s."""
    _expect_light(light, "light_sleep_timing")


def test_light_sleep_error(light):
    """No unexpected exception during light sleep."""
    if "light_sleep_error" not in light.checks:
        light.expect("alarm_import")  # skips when there is no alarm module or no run
        return
    light.expect("light_sleep_error")


def test_past_alarm_raises(light):
    """TimeAlarm(monotonic_time=<past>) raises ValueError (#9464)."""
    if "past_alarm_raises" not in light.checks:
        light.expect("alarm_import")
    light.expect("past_alarm_raises")


def test_device_exception(light):
    """DEVICE_LIGHT raised nothing on the board (run.py's device_exception)."""
    if light.crashed:
        pytest.skip("not reached: the device run failed (see light_sleep_device)")
    assert not light.err, light.err[-400:]


def test_deep_sleep(deep):
    """The alarm module exists, so the deep sleep loop runs (a skip otherwise)."""
    if "deep_sleep" in deep.checks:
        _expect(deep, "deep_sleep")
    elif deep.stopped and not deep.checks:
        pytest.skip("not reached: %s" % deep.stopped)


def test_deep_sleep_harness(deep):
    """No farmlib.FarmError (or other host-side exception) during the deep sleep loop."""
    if "deep_sleep" in deep.checks:
        _expect(deep, "deep_sleep")
    if deep.stopped and not deep.checks and deep.error is None:
        pytest.skip("not reached: %s" % deep.stopped)
    assert deep.error is None, deep.error
    if "deep_sleep_harness" in deep.checks:
        _expect(deep, "deep_sleep_harness")


def test_ready_after_restore(deep):
    """The board is back after code.py is restored and soft rebooted (power cycle fallback)."""
    _expect(deep, "ready_after_restore")


def test_wake_count(deep):
    """At least 4 wake lines in 27 s."""
    _expect(deep, "wake_count")


def test_sleep_memory_persists(deep):
    """Each count is the previous one + 1 (mod 256) (#5462, #6484)."""
    _expect(deep, "sleep_memory_persists")


def test_first_wake_type(deep):
    """The first run after the soft reboot has wake_alarm None."""
    _expect(deep, "first_wake_type")


def test_later_wake_types(deep):
    """Every later run has wake_alarm of type TimeAlarm (#10934)."""
    _expect(deep, "later_wake_types")


def test_deep_sleep_interval_s(deep, metric):
    """Median host-timed gap between wake lines, compared with this board's history."""
    if "deep_sleep_interval_s" not in deep.metrics:
        pytest.skip("not reached: %s" % (deep.stopped or "no wake intervals measured"))
    value, unit, better = deep.metrics["deep_sleep_interval_s"]
    metric("deep_sleep_interval_s", value, unit, better)


def test_deep_sleep_interval(deep):
    """The median gap between wake lines is 4.5 to 8 s for a 5 s alarm (#2875)."""
    _expect(deep, "deep_sleep_interval")
