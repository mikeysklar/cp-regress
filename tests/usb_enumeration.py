# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""USB enumeration: host sees correct VID/PID, interfaces, and mountpoint."""

import os
import time

META = {
    "id": "usb_enumeration",
    "title": "USB enumeration: VID/PID, interfaces, mount, version",
    "category": "USB",
    "tier": "smoke",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 60,
    "targets": [9888, 9956, 10390, 8134],
}

# Device-side code: read and report the board ID and CircuitPython version
DEVICE = r'''
import board
import sys
check("board_id", True, board.board_id, board.board_id)
check("cp_version", sys.implementation.version[0] >= 9, str(sys.implementation.version))
'''


def run(board, ctx):
    """Host-side USB enumeration checks."""
    t0 = time.time()

    # Check USB presence
    ctx.check("usb_present", board.present(), board.usb)
    if not board.present():
        ctx.skip("rest", "board not present")
        return

    # Check VID/PID
    vp = board.vid_pid()
    ctx.check("vid_pid_read", vp is not None, str(vp))
    if vp:
        vid, pid = vp
        ctx.check("vid_not_empty", vid and vid != "0000", vid)
        ctx.check("pid_not_empty", pid and pid != "0000", pid)

    # Check interface classes (CDC 02/0a, MSC 08)
    ifaces = board.interfaces()
    ctx.check("interfaces_exist", len(ifaces) > 0, "%d interfaces" % len(ifaces))

    # Look for CDC (02) and MSC (08) interfaces
    classes = set(ifaces.values())
    has_cdc = any(c.startswith("02") or c == "02" for c in classes)
    has_msc = "08" in classes

    ctx.check("cdc_present", has_cdc, "CDC interfaces: %s" % [c for c in classes if c.startswith("02")])
    ctx.check("msc_present", has_msc, "MSC present: %s" % has_msc)

    # Report other interface classes as info
    other = [c for c in classes if not (c.startswith("02") or c == "08")]
    if other:
        ctx.check("other_interfaces", True, "HID/Audio: %s" % other, value=len(other))

    # Check console TTY exists
    tty = board.tty()
    ctx.check("tty_exists", tty is not None, tty)

    # Try to mount and read boot_out.txt
    t_mount = time.time()
    mp = board.mount(timeout=30, check_id=True)
    ctx.check("mounted", mp is not None, mp)
    if mp:
        ctx.metric("mount_s", time.time() - t_mount, "s", better="lower")
    if not mp:
        ctx.skip("rest", "CIRCUITPY not mounted")
        return

    # Check boot_out.txt exists and has expected content
    boot_out = board.boot_out()
    ctx.check("boot_out_exists", len(boot_out) > 0, "length %d" % len(boot_out))
    ctx.check("board_id_in_boot", board.board_id in boot_out, "expected %s" % board.board_id)
    ctx.check("circuitpython_in_boot", "Adafruit CircuitPython" in boot_out, "version line present")

    # Check mount is writable: write a tiny file
    try:
        test_path = os.path.join(mp, ".cpr_test.txt")
        with open(test_path, "w") as f:
            f.write("test")
            f.flush()
            os.fsync(f.fileno())
        os.sync()
        exists = os.path.exists(test_path)
        ctx.check("write_file", exists, test_path)

        if exists:
            os.remove(test_path)
            os.sync()
            gone = not os.path.exists(test_path)
            ctx.check("delete_file", gone, "deleted")
    except OSError as e:
        ctx.check("write_file", False, str(e))

    # Check device-side info: board.board_id and CircuitPython version
    ctx.device(DEVICE, timeout=30)

    ctx.metric("enumeration_checks_s", time.time() - t0, "s", better="lower")
