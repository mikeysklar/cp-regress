# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Deep sleep and light sleep alarm cycles.

Tests alarm.sleep_memory persistence across deep sleep, TimeAlarm wakeups,
and light sleep timeout precision. Targets regressions in sleep modes,
wake-up timing, and sleep memory preservation.
"""

import statistics
import time

META = {
    "id": "sleep_alarm",
    "title": "Deep sleep and light sleep with TimeAlarm",
    "category": "Power",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": ["feather_stm32f405"],  # STM32 has no reliable deep sleep with USB
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 120,
    "targets": [5462, 2875, 10573, 9464, 10934, 6484, 2855, 5839],
}

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


def run(board, ctx):
    import farmlib

    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    # ===== Light sleep (raw REPL), also probes for the alarm module =====
    try:
        res, _, _ = ctx.device(DEVICE_LIGHT, timeout=30)
    except Exception as e:  # noqa: BLE001 - FarmError timeouts, serial errors
        ctx.check("light_sleep_device", False, "%s: %s" % (type(e).__name__, e))
        return
    if not any(r.get("name") == "alarm_import" and r.get("ok") for r in res):
        ctx.skip("deep_sleep", "alarm module not available")
        return

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
