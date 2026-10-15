# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Brent's fixture chain on every board: board -> flashed_firmware -> flashed_fs -> serial.

New with the be-more-like-brent fixtures; there is no tests/fixture_chain.py. It proves the
chain itself: the firmware step reported what it did, flashed_fs put the farm's idle code.py and
a settings.toml from base_secrets on the drive, and after a soft reboot the console prints what
that code.py prints.
"""
import os

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.destructive,
]


def test_firmware_step(request, flashed_firmware):
    """flashed / unchanged with --firmware, none without it."""
    want = ("flashed", "unchanged") if request.config.getoption("firmware") else ("none",)
    assert flashed_firmware.get("action") in want, flashed_firmware


def test_firmware_version(board, flashed_firmware):
    """After a flash or a skip, boot_out.txt reports the version that was recorded."""
    if flashed_firmware.get("action") == "none":
        pytest.skip("no --firmware")
    assert board.version() == flashed_firmware.get("version")


def test_fs_settings(board, flashed_fs, secrets):  # pylint: disable=unused-argument
    """settings.toml on the drive carries the broker address from base_secrets."""
    text = board.read_file("settings.toml") or ""
    assert 'CPR_BROKER_HOST = "%s"' % secrets["CPR_BROKER_HOST"] in text


def test_fs_code(board, flashed_fs):  # pylint: disable=unused-argument
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, "data", "idle_code.py"), "rb") as f:
        assert board.read_file("code.py", binary=True) == f.read()


def test_serial_reset(board, flashed_fs, serial):  # pylint: disable=unused-argument
    """Soft reboot, then the idle code.py's first lines on the console."""
    serial.reset()
    serial.expect(r"hello world", timeout=20)
    serial.expect(r"board: " + board.board_id, timeout=5)


def test_fs_copied(flashed_fs, flashed_firmware, record_property):
    """Records what this run copied and what the firmware step did (in --junitxml)."""
    record_property("fs_copied", ",".join(flashed_fs) or "nothing")
    record_property("firmware", "%s %s" % (flashed_firmware.get("action"),
                                           flashed_firmware.get("seconds", "")))
