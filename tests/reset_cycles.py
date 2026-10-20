# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Test soft reboots, hard resets, and power cycles."""

import statistics
import time

import farmlib

META = {
    "id": "reset_cycles",
    "title": "Soft reboot, hard reset, and power cycle recovery",
    "category": "Core",
    "tier": "nightly",
    "needs": [],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 300,
    "targets": [10934, 11380],
}

# Deterministic code.py for the soft reboot loop: finishes at once, so every reboot
# ends at "Press any key to enter the REPL" whatever the user's code.py does.
SOFT_CODE = 'print("cpr-reset-cycles")\n'
DONE_TOKEN = b"Press any key to enter the REPL"
RESET_REASON = "import microcontroller\nprint('@@RR', microcontroller.cpu.reset_reason)\n"


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


def run(board, ctx):
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
