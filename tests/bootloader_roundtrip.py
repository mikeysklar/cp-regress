# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Test bootloader entry and exit via microcontroller.on_next_reset()."""

import os
import re
import subprocess
import time

import farmlib

META = {
    "id": "bootloader_roundtrip",
    "title": "Enter and exit bootloader via on_next_reset()",
    "category": "USB",
    "tier": "release",
    "needs": ["uf2", "reset_ok"],
    "wiring": [],
    # metro_m0: reset() is hazardous. metro_m4_airlift: a VBUS cycle does not reset it,
    # so it would be left sitting in the UF2 bootloader with no automated way out.
    "skip_boards": ["metro_m0", "metro_m4_airlift"],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 180,
    "targets": [4728, 7578, 7060, 739, 11097, 5354, 9047],
}

# RunMode.UF2, not BOOTLOADER: on espressif BOOTLOADER is the ROM download mode with no
# UF2 volume. On atmel-samd, raspberrypi and nordic both values do the same thing.
ENTER_UF2 = "import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.UF2)\nmicrocontroller.reset()\n"


def _read_info_uf2(mount_path):
    """Read INFO_UF2.TXT from a UF2 volume."""
    info_path = os.path.join(mount_path, "INFO_UF2.TXT")
    try:
        with open(info_path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _mount_uf2_volume(board, timeout=20):
    """Find or mount the UF2 bootloader volume (no boot_out.txt there). Returns (dev, mp, info)."""
    end = time.time() + timeout
    while time.time() < end:
        for dev in board.block_devs():
            mp = subprocess.run(["findmnt", "-n", "-o", "TARGET", dev],
                                capture_output=True, text=True).stdout.strip().splitlines()
            mp = mp[0] if mp else ""
            if not mp:
                r = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                                   capture_output=True, text=True)
                m = re.search(r" at (.+?)\.?$", r.stdout.strip())
                mp = m.group(1) if m else ""
            if mp:
                info = _read_info_uf2(mp)
                if info:
                    return (dev, mp, info)
        time.sleep(0.5)
    return (None, None, None)


def _power_cycle(board):
    try:
        return board.power_cycle()
    except farmlib.FarmError:
        return None


def run(board, ctx):
    ctx.check("present", board.present(), board.usb)

    # Record initial state
    v_before = board.version()
    ctx.check("version_before", v_before is not None, "CircuitPython version: %s" % v_before)
    old_vid_pid = board.vid_pid()
    old_devnum = board.devnum()

    # Enter the UF2 bootloader. When it works the port vanishes mid-read and exec_raw raises.
    t0 = time.time()
    returned = None
    try:
        with board.repl() as repl:
            returned = repl.exec_raw(ENTER_UF2, timeout=5)
    except Exception:  # noqa: BLE001 - FarmError or SerialException, expected here
        pass
    if returned is not None:
        ctx.check("reset_issued", False,
                  "reset() returned: %s" % (returned[1].strip()[-300:] or "no error"))

    # Poll for re-enumeration with a different VID/PID (bootloaders use their own PID).
    poll_timeout = 40
    new_vid_pid = None
    while time.time() - t0 < poll_timeout:
        cur = board.vid_pid()
        if cur and cur != old_vid_pid and board.devnum() != old_devnum:
            new_vid_pid = cur
            break
        time.sleep(0.25)

    entered = new_vid_pid is not None
    elapsed = time.time() - t0
    ctx.check("vid_pid_changed", entered,
              "VID/PID %s -> %s, devnum %s -> %s" % (old_vid_pid, board.vid_pid(), old_devnum, board.devnum()))

    if entered:
        dev, mp, info = _mount_uf2_volume(board, timeout=20)
        ctx.check("uf2_volume_mounted", info is not None,
                  "INFO_UF2.TXT at %s" % mp if info else "no volume with INFO_UF2.TXT on %s" % board.block_devs())
        if info:
            elapsed = time.time() - t0
            ctx.metric("bootloader_entry_s", elapsed, "s", better="lower")
            ctx.check("info_uf2_content", "UF2 Bootloader" in info, info.strip()[:200])
            # Leave nothing mounted on a device that is about to vanish.
            subprocess.run(["udisksctl", "unmount", "-b", dev, "--no-user-interaction"],
                           capture_output=True, text=True)
        ctx.check("entered_bootloader", info is not None, "%.1f s" % elapsed)
    else:
        ctx.check("entered_bootloader", False,
                  "no bootloader detected within %d s" % poll_timeout)

    # Power cycle to return to CircuitPython (on_next_reset does not survive power loss).
    wait_result = _power_cycle(board)
    if wait_result is not None:
        ctx.check("returned_to_cp", True, "circuitpython booted after power cycle (%.1f s)" % wait_result)
        ctx.check("vid_pid_restored", board.vid_pid() == old_vid_pid,
                  "VID/PID %s -> %s" % (old_vid_pid, board.vid_pid()))
        v_after = board.version()
        ctx.check("version_unchanged", v_after is not None and v_before == v_after,
                  "version %s -> %s" % (v_before, v_after))
    else:
        ctx.check("returned_to_cp", False, "timeout waiting for board after power cycle")
        recovery = _power_cycle(board)
        ctx.check("recovery", recovery is not None, "second power cycle %s" % ("worked" if recovery else "failed"))
