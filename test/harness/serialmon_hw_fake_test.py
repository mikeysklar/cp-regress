# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""test/hardware/serialmon_hw_test.py run against a fake CircuitPython console on a pty, so its
four tests are seen to pass on a board that behaves, before they go to the farm.

FakeConsole answers the keys those tests send, as shared/runtime/pyexec.c and main.c @ 2d299b0
do: Ctrl-C gives '>>> ', Ctrl-B reprints the banner, Ctrl-D soft reboots into code.py and then
"Press any key", and 'microcontroller.reset()' drops the port and comes back on a new tty.
"""
import importlib.util
import os
import pathlib
import select
import threading
import time

import pytest

from harness.fakes import PtyBoard
from serialmon import SerialMonitor

pytestmark = pytest.mark.smoke

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "serialmon_hw", ROOT / "test" / "hardware" / "serialmon_hw_test.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)

BANNER = ("Adafruit CircuitPython 11.0.0-alpha.1-87-g29e9b60846 on 2026-10-12; "
          "Adafruit Metro ESP32S3 with ESP32S3")
BOOT = ("Auto-reload is off.\r\ncode.py output:\r\nhello world\r\n\r\nCode done running.\r\n\r\n"
        "Press any key to enter the REPL. Use CTRL-D to reload.\r\n")


class FakeConsole:
    def __init__(self, board):
        self.b = board
        self.waiting = False  # at "Press any key": the next byte is eaten (main.c:672)
        self.line = b""
        self.reboot_text = "\r\nsoft reboot\r\n\r\n"
        self.stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def out(self, s):
        if self.b.master is not None:
            self.b.write(s)

    def _repl(self, banner=False):
        self.waiting = False
        self.out(("\r\n" + BANNER + "\r\n" if banner else "\r\n") + ">>> ")

    def _key(self, c):
        if self.waiting:
            self._repl(banner=True)
            return
        if c == b"\x03":
            self.line = b""
            self.out("\r\n>>> ")
        elif c == b"\x02":
            self._repl(banner=True)
        elif c == b"\x04":
            self.out(self.reboot_text + BOOT)
            self.waiting = True
        elif c in (b"\r", b"\n"):
            if b"microcontroller.reset()" in self.line:
                self.line = b""
                threading.Thread(target=self._hard_reset, daemon=True).start()
                return
            self.line = b""
            self.out("\r\n>>> ")
        else:
            self.line += c

    def _hard_reset(self):
        time.sleep(0.05)
        self.b.unplug()
        time.sleep(0.3)
        self.b.replug()
        time.sleep(0.3)  # the host misses the first lines, as on a real board
        self.out(BOOT)
        self.waiting = True

    def _run(self):
        while not self.stop:
            m = self.b.master
            if m is None:
                time.sleep(0.01)
                continue
            try:
                r, _, _ = select.select([m], [], [], 0.05)
                data = os.read(m, 256) if r else b""
            except OSError:
                time.sleep(0.01)
                continue
            for i in range(len(data)):
                self._key(data[i:i + 1])


@pytest.fixture
def fake():
    b = PtyBoard("fake_cp")
    b.caps = ["native_usb", "reset_ok"]
    con = FakeConsole(b)
    b.console = con
    yield b
    con.stop = True
    b.close()


@pytest.fixture
def mon(fake):
    m = SerialMonitor(fake)
    yield m
    m.close()


def test_second_reset_starts_a_new_boot(mon):
    hw.test_second_reset_starts_a_new_boot(mon)


def test_open_does_not_reset(fake, mon):
    hw.test_open_does_not_reset(fake, mon)


def test_timestamps_and_dump(mon, tmp_path):
    hw.test_timestamps_and_dump(mon, tmp_path)


def test_hard_reset_reopens(fake, mon):
    hw.test_hard_reset_reopens(fake, mon)


def test_hard_reset_skips_without_reset_ok(fake, mon):
    fake.caps = ["native_usb"]
    with pytest.raises(pytest.skip.Exception, match="reset_ok"):
        hw.test_hard_reset_reopens(fake, mon)


def test_second_reset_catches_a_double_reboot(fake, mon):
    """Test the test: a console that reboots twice per Ctrl-D must fail it."""
    fake.console.reboot_text = "\r\nsoft reboot\r\n\r\nsoft reboot\r\n\r\n"
    with pytest.raises(AssertionError, match="soft reboot"):
        hw.test_second_reset_starts_a_new_boot(mon)
