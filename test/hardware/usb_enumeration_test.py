# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""USB enumeration: host sees correct VID/PID, interfaces, and mountpoint.

pytest version of tests/usb_enumeration.py. Each check there is one test here, in the same
order. The host reads sysfs (presence, VID/PID, interface classes, tty), mounts CIRCUITPY once
per board, reads boot_out.txt, writes and deletes a test file, then runs the device code.
Where the original gives up with skip("rest", ...) (board absent, CIRCUITPY not mounted), the
tests after that point skip with the same reason.

Targets: #9888 (USB bus reset disconnects), #9956 (VID/PID wrong), #10390 (MSC interface
missing), #8134 (mount races enumeration).
"""

import os
import time

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(9888, 9956, 10390, 8134),
]

# Device-side code: read and report the board ID and CircuitPython version
DEVICE = r'''
import board
import sys
check("board_id", True, board.board_id, board.board_id)
check("cp_version", sys.implementation.version[0] >= 9, str(sys.implementation.version))
'''


@pytest.fixture(scope="module", autouse=True)
def t_start(board):
    """When the module started on this board, for enumeration_checks_s.

    Depends on board so it restarts for each board (a module fixture without it would be shared
    across boards in a serial run), and autouse so it starts at this board's first test.
    """
    return board.name, time.time()


# ----- presence and identity -----

def test_usb_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


@pytest.fixture(scope="module")
def vid_pid(board):
    """(vid, pid) from sysfs, or None."""
    return board.vid_pid()


def test_vid_pid_read(vid_pid):
    """The host can read the board's USB VID:PID."""
    assert vid_pid is not None, str(vid_pid)


def test_vid_not_empty(vid_pid):
    """VID is non-empty and not 0000."""
    if not vid_pid:
        pytest.skip("not reached: VID/PID not readable")
    vid = vid_pid[0]
    assert vid and vid != "0000", vid


def test_pid_not_empty(vid_pid):
    """PID is non-empty and not 0000."""
    if not vid_pid:
        pytest.skip("not reached: VID/PID not readable")
    pid = vid_pid[1]
    assert pid and pid != "0000", pid


# ----- interface classes (CDC 02/0a, MSC 08) -----

@pytest.fixture(scope="module")
def classes(board):
    """(interfaces dict, set of interface class codes)."""
    ifaces = board.interfaces()
    return ifaces, set(ifaces.values())


def test_interfaces_exist(classes):
    """The device has at least one interface."""
    ifaces = classes[0]
    assert len(ifaces) > 0, "%d interfaces" % len(ifaces)


def test_cdc_present(classes):
    """A CDC (02) interface is present."""
    cls = classes[1]
    has_cdc = any(c.startswith("02") or c == "02" for c in cls)
    assert has_cdc, "CDC interfaces: %s" % [c for c in cls if c.startswith("02")]


def test_msc_present(classes):
    """An MSC (08) interface is present."""
    has_msc = "08" in classes[1]
    assert has_msc, "MSC present: %s" % has_msc


def test_other_interfaces(classes):
    """Other classes (HID, audio) are informational; recorded only when there are some."""
    other = [c for c in classes[1] if not (c.startswith("02") or c == "08")]
    if not other:
        pytest.skip("no other interface classes")
    assert True, "HID/Audio: %s" % other


def test_tty_exists(board):
    """The console tty exists."""
    tty = board.tty()
    assert tty is not None, tty


# ----- CIRCUITPY mount -----

@pytest.fixture(scope="module")
def mounted(board):
    """Mount CIRCUITPY once: (mount point or None, seconds taken, error or None).

    board.mount raises FarmError when the drive belongs to a different board. The original
    charges that once (run.py's "harness" check) and stops, so it is kept here and failed by
    test_mounted only; the tests after it skip as not reached.
    """
    t_mount = time.time()
    try:
        mp = board.mount(timeout=30, check_id=True)
    except Exception as e:  # pylint: disable=broad-except
        return None, time.time() - t_mount, "%s: %s" % (type(e).__name__, e)
    return mp, time.time() - t_mount, None


def _need_mount(mounted):
    if mounted[2]:
        pytest.skip("not reached: mount raised (%s)" % mounted[2][:200])
    if not mounted[0]:
        pytest.skip("CIRCUITPY not mounted")
    return mounted[0]


def test_mounted(mounted):
    """CIRCUITPY mounts within 30 s."""
    assert mounted[2] is None, mounted[2]
    mp = mounted[0]
    assert mp is not None, mp


def test_mount_s(mounted, metric):
    """Seconds to mount, compared with this board's history."""
    _need_mount(mounted)
    metric("mount_s", mounted[1], "s", better="lower")


@pytest.fixture(scope="module")
def boot_out(board, mounted):
    """(boot_out.txt contents, error or None); skips when CIRCUITPY is not mounted.

    board.boot_out() mounts again and can raise; that is charged once, by
    test_boot_out_exists, and the steps after it skip as not reached, as in the original.
    """
    _need_mount(mounted)
    try:
        return board.boot_out(), None
    except Exception as e:  # pylint: disable=broad-except
        return "", "%s: %s" % (type(e).__name__, e)


def _need_boot(boot_out):
    if boot_out[1]:
        pytest.skip("not reached: reading boot_out.txt raised (%s)" % boot_out[1][:200])
    return boot_out[0]


def test_boot_out_exists(boot_out):
    """boot_out.txt exists and is not empty."""
    assert boot_out[1] is None, boot_out[1]
    text = boot_out[0]
    assert len(text) > 0, "length %d" % len(text)


def test_board_id_in_boot(board, boot_out):
    """boot_out.txt names the expected board_id."""
    assert board.board_id in _need_boot(boot_out), "expected %s" % board.board_id


def test_circuitpython_in_boot(boot_out):
    """boot_out.txt has the Adafruit CircuitPython version line."""
    assert "Adafruit CircuitPython" in _need_boot(boot_out), "version line present"


@pytest.fixture(scope="module")
def host_write(mounted, boot_out):
    """Write a tiny file to CIRCUITPY and delete it. Records name -> [(ok, detail)]."""
    mp = _need_mount(mounted)
    _need_boot(boot_out)
    rec = {}
    test_path = os.path.join(mp, ".cpr_test.txt")
    try:
        with open(test_path, "w") as f:
            f.write("test")
            f.flush()
            os.fsync(f.fileno())
        os.sync()
        exists = os.path.exists(test_path)
        rec.setdefault("write_file", []).append((exists, test_path))

        if exists:
            os.remove(test_path)
            os.sync()
            gone = not os.path.exists(test_path)
            rec.setdefault("delete_file", []).append((gone, "deleted"))
    except OSError as e:
        rec.setdefault("write_file", []).append((False, str(e)))
    finally:
        try:  # never leave the test file behind
            if os.path.exists(test_path):
                os.remove(test_path)
                os.sync()
        except OSError:
            pass
    return rec


def _expect(rec, name):
    rs = rec.get(name)
    if not rs:
        pytest.skip("not reached: %s was not recorded on this run" % name)
    bad = [d for ok, d in rs if not ok]
    assert not bad, "%s: %s" % (name, "; ".join(bad)[:500])


def test_write_file(host_write):
    """The host can write a file to CIRCUITPY."""
    _expect(host_write, "write_file")


def test_delete_file(host_write):
    """The host can delete that file again."""
    _expect(host_write, "delete_file")


# ----- device side -----

@pytest.fixture(scope="module")
def results(device, mounted, boot_out):
    """Run DEVICE once per board (the original only gets here with CIRCUITPY mounted)."""
    _need_mount(mounted)
    _need_boot(boot_out)
    return device.run(DEVICE, timeout=30)


def test_board_id(results):
    """board.board_id is readable on the device."""
    results.expect("board_id")


def test_cp_version(results):
    """CircuitPython major version is 9 or later."""
    results.expect("cp_version")


def test_device_exception(results):
    """The device code raised nothing (Ctx.device records device_exception if it did)."""
    results.expect_no_exception()


def test_enumeration_checks_s(results, t_start, metric):  # pylint: disable=unused-argument
    """Seconds for the whole module on this board, compared with its history."""
    metric("enumeration_checks_s", time.time() - t_start[1], "s", better="lower")
