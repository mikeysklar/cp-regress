# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""serial (function scope, resets), after Brent's SerialMonitor: expect() on console output.

    def test_boots(serial):
        serial.reset()
        serial.expect("code.py output:")

For running code and asserting on check() results, use `device`; this is for tests that only
watch what the board prints. The REPL eats the first keystroke, so anything sent to the board
goes through Repl.prompt(), which syncs on ">>> " first.
"""
import re
import time

import pytest

import farmlib


class SerialMonitor:
    """The board's console tty, opened for one test."""

    def __init__(self, board):
        self.board = board
        self.repl = board.repl()

    def close(self):
        self.repl.close()

    def reset(self, timeout=20):
        """Soft reboot (Ctrl-D at a synced prompt). Output from here on is fresh."""
        self.repl.soft_reboot(timeout=timeout)

    def expect(self, pattern, timeout=10):
        """Read until the regex (str or bytes) matches. Returns the match; fails the test on
        timeout with the last of the output, so the failure says what the board did print."""
        if isinstance(pattern, str):
            pattern = pattern.encode()
        rx = re.compile(pattern)
        buf = bytearray(self.repl.pending)
        self.repl.pending = bytearray()
        end = time.time() + timeout
        while True:
            m = rx.search(buf)
            if m:
                self.repl.pending = bytearray(buf[m.end():])
                return m
            if time.time() >= end:
                pytest.fail("%s: no %r within %d s; last output %r"
                            % (self.board.name, pattern, timeout, bytes(buf[-300:])), pytrace=False)
            try:
                buf += self.repl._pull()  # pylint: disable=protected-access
            except Exception as e:  # pylint: disable=broad-except
                # USB dropping mid-read (a reset) is reopened rather than charged to the test
                self.repl.close()
                if self.board.wait_ready(timeout=max(5, end - time.time()), need_mount=False) is None:
                    pytest.fail("%s: console lost (%s)" % (self.board.name, e), pytrace=False)
                self.repl = self.board.repl()

    def send(self, line):
        """Type a line at a synced friendly prompt."""
        self.repl.prompt()
        self.repl.write(line.rstrip("\r\n").encode() + b"\r\n")


@pytest.fixture
def serial(board):
    """SerialMonitor on the board's console, closed after the test."""
    try:
        mon = SerialMonitor(board)
    except farmlib.FarmError as e:
        pytest.fail(str(e), pytrace=False)
    yield mon
    mon.close()
