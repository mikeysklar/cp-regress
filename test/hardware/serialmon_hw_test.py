# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""SerialMonitor (lib/serialmon.py) against real boards, after Brent's
hil/tests_hil_board_utils/test_serial_monitor_hw.py @ 90cd5af. The pty-only behaviour is
test/harness/serialmon_test.py.

His tests reset through a USB-UART auto-reset circuit (DTR/RTS), which no farm board has. Ours
reset with Ctrl-D (soft_reset) and microcontroller.reset() (hard_reset, reset_ok boards only, so
never the Metro M0). No VBUS cut, so SAMD boards keep their USB and need no SWD rescue.

  test_second_reset_starts_a_new_boot   his, with soft_reset: boot indices rise, assert_none
                                        only looks back past the latest boot when asked
  test_open_does_not_reset              his: opening the console prints no new banner
  test_timestamps_and_dump              his: times rise, indices are contiguous, dump() lines
                                        are "[HH:MM:SS.mmm] text"
  test_hard_reset_reopens               his power-cycle test, with microcontroller.reset():
                                        the monitor follows the board off USB and back, then
                                        refuses to reset once closed
"""
import re
import time

import pytest

import hil_checks
from serialmon import SerialError, SerialMonitor, SerialUnexpectedError

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.destructive,
]

SOFT_REBOOT_RE = r"soft reboot"


def test_second_reset_starts_a_new_boot(serial):
    first = serial.soft_reset()
    hil_checks.expect_banner(serial, poke=True)
    second = serial.soft_reset()
    assert second > first

    hil_checks.expect_banner(serial, poke=True)
    reboots = [l for l in serial.lines(second) if re.search(SOFT_REBOOT_RE, l.text)]
    assert len(reboots) == 1, [l.text for l in reboots]
    # assert_none defaults to the latest boot, so the first boot's line only matches when
    # asked to look back that far.
    with pytest.raises(SerialUnexpectedError):
        serial.assert_none(SOFT_REBOOT_RE, since=first)


def test_open_does_not_reset(board, serial):
    serial.soft_reset()
    hil_checks.expect_banner(serial, poke=True)
    serial.close()

    mon = SerialMonitor(board)
    try:
        mon.assert_none(hil_checks.BANNER_RE, since=0, duration=3)
        mon.assert_none(SOFT_REBOOT_RE, since=0, duration=0)
    finally:
        mon.close()


def test_timestamps_and_dump(serial, tmp_path):
    boot = serial.soft_reset()
    hil_checks.expect_banner(serial, poke=True)
    serial.close()

    lines = serial.lines(boot)
    assert lines, "nothing captured after the reset"
    times = [l.time for l in lines]
    assert times == sorted(times)
    assert [l.index for l in lines] == list(range(boot, boot + len(lines)))
    assert "Adafruit CircuitPython" in serial.output

    dumped = serial.dump(tmp_path / "boot.serial.log").read_text().splitlines()
    text = [l for l in dumped if " --- cpr: " not in l]  # our action markers, not lines
    assert len(text) == serial.mark()
    assert all(re.match(r"^\[\d\d:\d\d:\d\d\.\d{3}\] ", l) for l in dumped)


def test_hard_reset_reopens(board, serial):
    if "reset_ok" not in board.caps:
        pytest.skip("%s: microcontroller.reset() is not safe here (no reset_ok)" % board.name)
    t0 = time.monotonic()
    boot = serial.hard_reset()
    took = time.monotonic() - t0
    assert boot == serial.boot
    hil_checks.expect_banner(serial, poke=True)
    serial.assert_none(hil_checks.CRASH_RE, duration=hil_checks.CRASH_WINDOW)
    assert took < 60 + board.power.get("settle", 12), "reopen took %.1f s" % took

    serial.close()
    with pytest.raises(SerialError, match="closed"):
        serial.soft_reset()
