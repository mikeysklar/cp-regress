# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""USB CDC data loopback and storage recovery fixture.

Tests usb_cdc.data echo functionality and recovery from disable_usb_drive.
Targets regressions in USB CDC secondary interfaces and storage state transitions.

boot.py only runs on a hard reset, so every USB configuration change here is applied with
microcontroller.reset() (reset_ok boards) or a hub power cycle, never a soft reboot.
"""

import time

import farmlib  # same module object run.py uses, so farmlib.FarmError matches

META = {
    "id": "usb_cdc_data",
    "title": "USB CDC data echo with storage recovery",
    "category": "USB",
    "tier": "nightly",
    "needs": ["native_usb", "reset_ok"],
    "wiring": [],
    "skip_boards": [],
    # console + data CDC + MSC needs 6 IN endpoints incl. EP0 (ESP32-S2/S3 allow 5) and
    # 6 endpoint numbers (STM32F405 has 4): those boot into safe mode, see usb_desc.c.
    "skip_ports": ["espressif", "stm"],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 420,
    "targets": [4364, 5381, 6018, 7456, 8417, 10396, 4417, 5640, 6471, 4986],
}

# boot.py for phase 1. HID and MIDI are dropped so console + data + MSC fit in the
# endpoint budget; otherwise boot ends in safe mode.
BOOT_CDC = (
    "import usb_cdc\n"
    "try:\n"
    "    import usb_hid\n"
    "    usb_hid.disable()\n"
    "except ImportError:\n"
    "    pass\n"
    "try:\n"
    "    import usb_midi\n"
    "    usb_midi.disable()\n"
    "except ImportError:\n"
    "    pass\n"
    "usb_cdc.enable(console=True, data=True)\n"
)

# code.py for phase 1: echo everything that arrives on usb_cdc.data. No READY banner: a
# write before the host opens the port may or may not be delivered, so the host syncs by
# pinging instead. Ctrl-C on the console ends the loop.
CODE_ECHO = (
    "import usb_cdc\n"
    "import time\n"
    "\n"
    "d = usb_cdc.data\n"
    "if d is None:\n"
    "    print('usb_cdc.data not available')\n"
    "else:\n"
    "    t_last = time.monotonic()\n"
    "    while time.monotonic() - t_last < 180:\n"
    "        n = d.in_waiting\n"
    "        if n:\n"
    "            d.write(d.read(n))\n"
    "            t_last = time.monotonic()\n"
    "        else:\n"
    "            time.sleep(0.001)\n"
)

# Run from the console REPL after the echo phase (Ctrl-C has ended code.py).
DEVICE_CDC_STATE = r'''
import usb_cdc
check("usb_cdc_data_available", usb_cdc.data is not None, repr(usb_cdc.data))
check("usb_cdc_console_available", usb_cdc.console is not None, repr(usb_cdc.console))
'''

# Run with the USB drive disabled. CIRCUITPY should be writable from Python then; removing
# boot.py is also the recovery step that brings MSC back on the next hard reset.
DEVICE_DISABLED = r'''
import os
import storage
try:
    with open("/cpr_probe.txt", "w") as f:
        f.write("x")
    os.remove("/cpr_probe.txt")
    check("fs_writable_when_drive_disabled", True)
except OSError as e:
    check("fs_writable_when_drive_disabled", False, repr(e))
    try:
        storage.remount("/", readonly=False)
    except Exception as e2:
        check("remount_when_drive_disabled", False, repr(e2))
try:
    os.remove("/boot.py")
    check("boot_py_removed_on_device", True)
except OSError as e:
    check("boot_py_removed_on_device", False, repr(e))
'''


def _hard_reset(board, need_mount=True, safe_mode=False):
    """Re-run boot.py. Returns wait_ready() seconds or None. Never raises FarmError/OSError."""
    if "reset_ok" not in board.caps:
        try:
            t = board.power_cycle()
        except farmlib.FarmError:
            return None
        return t if (t is not None or need_mount) else board.wait_ready(need_mount=False)
    cmd = b"import microcontroller; microcontroller.reset()\r"
    if safe_mode:
        cmd = (b"import microcontroller; "
               b"microcontroller.on_next_reset(microcontroller.RunMode.SAFE_MODE); "
               b"microcontroller.reset()\r")
    sent = False
    try:
        with board.repl() as r:
            r.prompt()
            sent = True
            r.write(cmd)
            time.sleep(0.5)
    except (farmlib.FarmError, OSError):
        pass  # the port vanishing mid-write is expected
    if not sent:  # never reached '>>> ': the reset was not issued, fall back to the hub
        try:
            t = board.power_cycle()
        except farmlib.FarmError:
            return None
        return t if (t is not None or need_mount) else board.wait_ready(need_mount=False)
    time.sleep(3)  # let the old ttys disappear before wait_ready polls for new ones
    return board.wait_ready(need_mount=need_mount)


def _wait_ttys(board, n, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if len(board.ttys()) >= n:
            break
        time.sleep(0.25)
    return board.ttys()


def _echo(board, ctx, data_tty):
    s = farmlib.serial.Serial(data_tty, 115200, timeout=0.1, write_timeout=5)
    try:
        # Sync: ping until the echo loop answers.
        ready = False
        end = time.time() + 15
        buf = b""
        while time.time() < end and not ready:
            s.write(b"PING")
            s.flush()
            t = time.time() + 0.5
            while time.time() < t:
                buf += s.read(256)
                if b"PING" in buf:
                    ready = True
                    break
        ctx.check("device_ready", ready, "no echo of PING on data tty: %r" % buf[-60:])
        if not ready:
            return
        time.sleep(0.3)
        s.read(4096)  # extra PING echoes
        s.reset_input_buffer()

        # 64 KB in 512-byte chunks, each echo collected before the next write.
        test_data = bytes(range(256)) * 256
        chunk_size = 512
        recv_data = bytearray()
        t0 = time.time()
        for i in range(0, len(test_data), chunk_size):
            s.write(test_data[i:i + chunk_size])
            s.flush()
            want = i + chunk_size
            deadline = time.time() + 5
            while len(recv_data) < want and time.time() < deadline:
                recv_data.extend(s.read(4096))
            if len(recv_data) < want:
                break
        t_elapsed = time.time() - t0
        ctx.check("echo_received", len(recv_data) == len(test_data),
                  "expected %d bytes, got %d" % (len(test_data), len(recv_data)))
        match = bytes(recv_data) == test_data
        first_bad = next((j for j in range(min(len(recv_data), len(test_data)))
                          if recv_data[j] != test_data[j]), None)
        ctx.check("echo_data_match", match,
                  "" if match else "first mismatch at byte %s" % first_bad)
        if match:
            kbps = (len(test_data) / 1024) / (t_elapsed if t_elapsed > 0 else 1)
            ctx.metric("data_echo_kBps", kbps, "KB/s", better="higher")
    finally:
        s.close()


def _phase_cdc(board, ctx):
    board.write_file("boot.py", BOOT_CDC)
    board.write_file("code.py", CODE_ECHO)
    t = _hard_reset(board)
    ctx.check("cdc_boot_ready", t is not None, "board did not come back after reset")
    if t is None:
        return
    ttys = _wait_ttys(board, 2)
    ctx.check("ttys_count", len(ttys) == 2, "expected 2 ttys, got %s" % ttys)
    data_tty = board.data_tty()
    ctx.check("data_tty_exists", data_tty is not None, str(data_tty))
    if data_tty:
        try:
            _echo(board, ctx, data_tty)
        except (farmlib.FarmError, OSError) as e:
            ctx.check("data_tty_test", False, "%s: %s" % (type(e).__name__, e))

    # Console still works after data traffic; ctx.device Ctrl-Cs code.py first.
    try:
        res, out, err = ctx.device(DEVICE_CDC_STATE, timeout=20)
        ctx.check("console_responsive", len(res) == 2, "out=%r err=%r" % (out[-100:], err[-100:]))
    except (farmlib.FarmError, OSError) as e:
        ctx.check("console_responsive", False, "%s: %s" % (type(e).__name__, e))


def _recover_drive(board, ctx):
    """Bring MSC back so preserved() can restore files over CIRCUITPY."""
    if board.mount(timeout=5):
        return True
    try:
        ctx.device(DEVICE_DISABLED, timeout=20)
    except (farmlib.FarmError, OSError) as e:
        ctx.check("device_recovery_step", False, "%s: %s" % (type(e).__name__, e))
    t = _hard_reset(board, need_mount=True)
    ctx.check("recovery_success", t is not None, "CIRCUITPY back after removing boot.py on device")
    if t is not None:
        return True
    # Last resort: safe mode skips boot.py, so MSC is presented again.
    t = _hard_reset(board, need_mount=True, safe_mode=True)
    ctx.check("recovery_safe_mode", t is not None, "CIRCUITPY back in safe mode")
    return t is not None


def _phase_disabled(board, ctx):
    board.write_file("boot.py", "import storage\nstorage.disable_usb_drive()\n")
    board.write_file("code.py", "print('Storage disabled')\n")
    try:
        t = _hard_reset(board, need_mount=False)
        ctx.check("disabled_boot_ready", t is not None, "console tty did not come back")
        if t is not None:
            time.sleep(2)  # interfaces and block devices settle
            ifaces = board.interfaces()
            msc_present = any(v == "08" for v in ifaces.values())
            ctx.check("msc_disabled", bool(ifaces) and not msc_present,
                      "MSC interface should be absent, found: %s" % ifaces)
            ctx.check("no_block_dev", not board.block_devs(), str(board.block_devs()))
            try:
                with board.repl() as r:
                    out, err = r.exec_raw("print(1+1)", timeout=10)
                ctx.check("console_after_disable", out.strip() == "2", "out=%r err=%r" % (out[:100], err[:100]))
            except (farmlib.FarmError, OSError) as e:
                ctx.check("console_after_disable", False, "%s: %s" % (type(e).__name__, e))
    finally:
        recovered = _recover_drive(board, ctx)
    ifaces = board.interfaces()
    ctx.check("msc_restored", recovered and any(v == "08" for v in ifaces.values()),
              "MSC should be present after recovery: %s" % ifaces)


def run(board, ctx):
    # Basic presence checks
    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))
    n_ttys0 = len(board.ttys())

    # ===== Phase 1: USB CDC data echo test =====
    try:
        with board.preserved():
            _phase_cdc(board, ctx)
    except (farmlib.FarmError, OSError) as e:
        ctx.check("cdc_phase", False, "%s: %s" % (type(e).__name__, e))
    t = _hard_reset(board)  # restored boot.py only takes effect on a hard reset
    ctx.check("cdc_restore_ready", t is not None, "board not ready after restore reset")
    if t is None:
        return
    ttys = _wait_ttys(board, n_ttys0)
    ctx.check("ttys_restored", len(ttys) == n_ttys0, "expected %d ttys, got %s" % (n_ttys0, ttys))

    # ===== Phase 2: disable_usb_drive test =====
    try:
        with board.preserved():
            _phase_disabled(board, ctx)
    except (farmlib.FarmError, OSError) as e:
        ctx.check("disable_phase", False, "%s: %s" % (type(e).__name__, e))
    t = _hard_reset(board)  # leaves safe mode too, if recovery needed it
    ctx.check("final_ready", t is not None, "board not ready after final reset")
