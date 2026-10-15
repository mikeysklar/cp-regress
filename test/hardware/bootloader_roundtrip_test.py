# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Enter and exit the UF2 bootloader via microcontroller.on_next_reset().

pytest version of tests/bootloader_roundtrip.py. The round trip (reset into the UF2 bootloader,
find INFO_UF2.TXT, power cycle back to CircuitPython) runs once per board in a module-scoped
fixture that always power-cycles the board back in its finally block; each check there is one
test here, in the same order, asserting on what the fixture recorded.
Targets: #4728, #7578, #7060, #739, #11097, #5354, #9047.
"""
import os
import re
import subprocess
import time

import pytest

import farmlib

pytestmark = [
    pytest.mark.release,
    pytest.mark.needs("uf2", "reset_ok"),
    # metro_m0: reset() is hazardous. metro_m4_airlift: a VBUS cycle does not reset it,
    # so it would be left sitting in the UF2 bootloader with no automated way out.
    pytest.mark.skip_boards("metro_m0", "metro_m4_airlift"),
    pytest.mark.targets(4728, 7578, 7060, 739, 11097, 5354, 9047),
    pytest.mark.destructive,
]

# RunMode.UF2, not BOOTLOADER: on espressif BOOTLOADER is the ROM download mode with no
# UF2 volume. On atmel-samd, raspberrypi and nordic both values do the same thing.
ENTER_UF2 = "import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.UF2)\nmicrocontroller.reset()\n"

POLL_TIMEOUT = 40


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


def _enter_bootloader(board, rec):
    """Everything up to and including entered_bootloader; fills rec["checks"] in order."""
    checks = rec["checks"]
    checks["present"] = (board.present(), board.usb)

    # Record initial state
    v_before = board.version()
    rec["v_before"] = v_before
    checks["version_before"] = (v_before is not None, "CircuitPython version: %s" % v_before)
    old_vid_pid = board.vid_pid()
    old_devnum = board.devnum()
    rec["old_vid_pid"] = old_vid_pid

    # Enter the UF2 bootloader. When it works the port vanishes mid-read and exec_raw raises.
    t0 = time.time()
    returned = None
    try:
        with board.repl() as repl:
            returned = repl.exec_raw(ENTER_UF2, timeout=5)
    except Exception:  # noqa: BLE001 - FarmError or SerialException, expected here
        pass
    rec["reset_sent"] = True
    if returned is not None:
        checks["reset_issued"] = (False, "reset() returned: %s"
                                  % (returned[1].strip()[-300:] or "no error"))

    # Poll for re-enumeration with a different VID/PID (bootloaders use their own PID).
    poll_timeout = POLL_TIMEOUT
    new_vid_pid = None
    while time.time() - t0 < poll_timeout:
        cur = board.vid_pid()
        if cur and cur != old_vid_pid and board.devnum() != old_devnum:
            new_vid_pid = cur
            break
        time.sleep(0.25)

    entered = new_vid_pid is not None
    elapsed = time.time() - t0
    checks["vid_pid_changed"] = (entered, "VID/PID %s -> %s, devnum %s -> %s"
                                 % (old_vid_pid, board.vid_pid(), old_devnum, board.devnum()))

    if entered:
        dev, mp, info = _mount_uf2_volume(board, timeout=20)
        checks["uf2_volume_mounted"] = (
            info is not None,
            "INFO_UF2.TXT at %s" % mp if info else "no volume with INFO_UF2.TXT on %s" % board.block_devs())
        if info:
            elapsed = time.time() - t0
            rec["bootloader_entry_s"] = elapsed
            checks["info_uf2_content"] = ("UF2 Bootloader" in info, info.strip()[:200])
            # Leave nothing mounted on a device that is about to vanish.
            subprocess.run(["udisksctl", "unmount", "-b", dev, "--no-user-interaction"],
                           capture_output=True, text=True)
        checks["entered_bootloader"] = (info is not None, "%.1f s" % elapsed)
    else:
        checks["entered_bootloader"] = (False,
                                        "no bootloader detected within %d s" % poll_timeout)


def _return_to_cp(board, rec):
    """Power cycle back to CircuitPython (on_next_reset does not survive power loss)."""
    checks = rec["checks"]
    wait_result = _power_cycle(board)
    if wait_result is not None:
        checks["returned_to_cp"] = (True, "circuitpython booted after power cycle (%.1f s)"
                                    % wait_result)
        # Compare only against a starting value the entry phase actually captured; if it
        # raised before that, the original never got here and these checks skip.
        if "old_vid_pid" in rec:
            old_vid_pid = rec["old_vid_pid"]
            checks["vid_pid_restored"] = (board.vid_pid() == old_vid_pid,
                                          "VID/PID %s -> %s" % (old_vid_pid, board.vid_pid()))
        if "v_before" in rec:
            v_before = rec["v_before"]
            v_after = board.version()
            checks["version_unchanged"] = (v_after is not None and v_before == v_after,
                                           "version %s -> %s" % (v_before, v_after))
    else:
        checks["returned_to_cp"] = (False, "timeout waiting for board after power cycle")
        recovery = _power_cycle(board)
        checks["recovery"] = (recovery is not None,
                              "second power cycle %s" % ("worked" if recovery else "failed"))


@pytest.fixture(scope="module")
def roundtrip(board):
    """Reset into the UF2 bootloader, then power cycle back. Returns the record.

    record["checks"] maps check name -> (ok, detail) for every check the original would have
    reported; record["error"] holds an unexpected host-side exception, if any. The power cycle
    back to CircuitPython always runs, in finally.
    """
    rec = {"checks": {}, "error": None}
    try:
        _enter_bootloader(board, rec)
    except Exception as e:  # noqa: BLE001 - keep it in the record, fail one test
        rec["error"] = "entering bootloader: %r" % e
        # charge it once, to the check that sums up the entry phase
        rec["checks"].setdefault("entered_bootloader", (False, rec["error"]))
    finally:
        try:
            _return_to_cp(board, rec)
        except Exception as e:  # noqa: BLE001
            why = "power cycle back: %r" % e
            rec["error"] = (rec["error"] + "; " if rec["error"] else "") + why
            # charge it once, to the first return-phase check it kept from being recorded
            checks = rec["checks"]
            if "returned_to_cp" not in checks:
                name = "returned_to_cp"
            elif checks["returned_to_cp"][0]:
                name = "vid_pid_restored" if "vid_pid_restored" not in checks else "version_unchanged"
            else:
                name = "recovery"
            checks.setdefault(name, (False, why))
    return rec


def _expect(rec, name):
    """Assert a recorded check; a check the round trip never reached skips."""
    if name not in rec["checks"]:
        pytest.skip("not reached" + (": %s" % rec["error"][:160] if rec["error"] else ""))
    ok, detail = rec["checks"][name]
    assert ok, detail


def test_present(roundtrip):
    """The board is on the bus at its hub port."""
    _expect(roundtrip, "present")


def test_version_before(roundtrip):
    """boot_out.txt version is readable before the round trip."""
    _expect(roundtrip, "version_before")


def test_reset_issued(roundtrip):
    """on_next_reset(RunMode.UF2) + reset() did not return (the original reports only a failure)."""
    if "reset_issued" in roundtrip["checks"] or not roundtrip.get("reset_sent"):
        _expect(roundtrip, "reset_issued")


def test_vid_pid_changed(roundtrip):
    """Within 40 s the board re-enumerates with a new devnum and a different VID/PID."""
    _expect(roundtrip, "vid_pid_changed")


def test_uf2_volume_mounted(roundtrip):
    """A block device on the same hub port has INFO_UF2.TXT."""
    if ("uf2_volume_mounted" not in roundtrip["checks"]
            and roundtrip["checks"].get("vid_pid_changed", (True,))[0] is False):
        pytest.skip("not reached: bootloader did not enumerate")
    _expect(roundtrip, "uf2_volume_mounted")


def test_bootloader_entry_s(roundtrip, metric):
    """Time from sending reset() to a readable INFO_UF2.TXT, compared with this board's history."""
    if "bootloader_entry_s" not in roundtrip:
        pytest.skip("not reached: no INFO_UF2.TXT read")
    metric("bootloader_entry_s", roundtrip["bootloader_entry_s"], "s", better="lower")


def test_info_uf2_content(roundtrip):
    """INFO_UF2.TXT contains "UF2 Bootloader"."""
    if ("info_uf2_content" not in roundtrip["checks"]
            and roundtrip["checks"].get("uf2_volume_mounted", (True,))[0] is False):
        pytest.skip("not reached: no INFO_UF2.TXT read")
    _expect(roundtrip, "info_uf2_content")


def test_entered_bootloader(roundtrip):
    """The UF2 volume was reached."""
    _expect(roundtrip, "entered_bootloader")


def test_returned_to_cp(roundtrip):
    """A power cycle brings back CircuitPython (tty + CIRCUITPY)."""
    _expect(roundtrip, "returned_to_cp")


def test_vid_pid_restored(roundtrip):
    """VID/PID is back to the CircuitPython one."""
    if ("vid_pid_restored" not in roundtrip["checks"]
            and roundtrip["checks"].get("returned_to_cp", (True,))[0] is False):
        pytest.skip("not reached: board did not return to CircuitPython")
    _expect(roundtrip, "vid_pid_restored")


def test_version_unchanged(roundtrip):
    """boot_out.txt version equals the starting version."""
    if ("version_unchanged" not in roundtrip["checks"]
            and roundtrip["checks"].get("returned_to_cp", (True,))[0] is False):
        pytest.skip("not reached: board did not return to CircuitPython")
    _expect(roundtrip, "version_unchanged")


def test_recovery(roundtrip):
    """Only after returned_to_cp fails: a second power cycle brought the board back."""
    if "recovery" not in roundtrip["checks"]:
        if roundtrip["checks"].get("returned_to_cp", (False,))[0]:
            pytest.skip("first power cycle worked; no recovery needed")
    _expect(roundtrip, "recovery")
