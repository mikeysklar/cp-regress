# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/serialmon.py against a pty pair: what Brent's test_serial_monitor_hw.py checks on his
esp32 (tests_hil_board_utils/test_serial_monitor_hw.py @ cf64e03), with the board replaced by
the master end of os.openpty(). Nothing here opens a farm tty."""
import re
import threading
import time

import pytest

from harness.fakes import wait_for
from serialmon import (SERIAL_MAX_LINE, SerialError, SerialMonitor, SerialTimeout,
                       SerialUnexpectedError)

pytestmark = pytest.mark.smoke
pytest.importorskip("serial", reason="pyserial is not installed")

# Seen on bravo (cp-regress/results/20261014-*), with the OSC title CircuitPython's status bar
# sends in front of the banner (supervisor/shared/status_bar.c).
BANNER = ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; "
          "Adafruit Metro RP2040 with rp2040")


@pytest.fixture
def open_monitor(pty_board):
    """open_monitor(**kw) -> a SerialMonitor on pty_board, closed at teardown (Brent's
    open_monitor fixture, tests_hil_board_utils/conftest.py @ cf64e03)."""
    mons = []

    def open_(**kw):
        mon = SerialMonitor(pty_board, **kw)
        mons.append(mon)
        return mon

    yield open_
    for mon in mons:
        mon.close()


def later(delay, fn, *args):
    """Run fn(*args) on a thread after delay seconds, while the test blocks in the monitor."""
    t = threading.Timer(delay, fn, args)
    t.daemon = True
    t.start()
    return t


def test_expect_moves_cursor_and_checks_order(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write("boot\r\nalpha 1\r\nbeta 2\r\nalpha 3\r\n")
    assert mon.expect(r"^alpha (\d)$", timeout=5).group(1) == "1"
    assert mon.expect(r"^beta", timeout=5).string == "beta 2"
    assert mon.expect(r"^alpha (\d)$", timeout=5).group(1) == "3"
    # The cursor is past "beta 2", so it is not found again.
    with pytest.raises(SerialTimeout, match="no line matched"):
        mon.expect(r"^beta", timeout=0.3)
    assert [l.text for l in mon.lines()] == ["boot", "alpha 1", "beta 2", "alpha 3"]
    assert mon.mark() == 4


def test_line_counts_once_its_newline_arrives(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write("partial")
    with pytest.raises(SerialTimeout, match=r"\[partial\] partial"):
        mon.expect("partial", timeout=0.3)
    later(0.1, pty_board.write, " line\r\n")
    assert mon.expect(r"^partial line$", timeout=5)


def test_bytes_and_compiled_patterns(pty_board, open_monitor):
    """Tests written against the old byte-stream expect() keep working."""
    mon = open_monitor()
    pty_board.write("one\r\ntwo\r\nthree\r\n")
    assert mon.expect(b"one", timeout=5)
    assert mon.expect(re.compile(rb"^t(w)o$"), timeout=5).group(1) == "w"
    assert mon.expect(re.compile(r"THREE", re.I), timeout=5)


def test_timeout_is_a_test_failure_and_says_what_it_saw(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write("".join("line %d\r\n" % i for i in range(30)))
    wait_for(lambda: mon.mark() == 30)
    t0 = time.monotonic()
    with pytest.raises(SerialTimeout) as exc:
        mon.expect("never printed", timeout=0.5)
    assert 0.4 < time.monotonic() - t0 < 3
    assert isinstance(exc.value, AssertionError)  # pytest counts it as a failure
    assert isinstance(exc.value, SerialError)
    msg = str(exc.value)
    assert "... 10 earlier lines" in msg and "[29] line 29" in msg and "[9] line 9" not in msg


def test_assert_none(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write("Auto-reload is off.\r\ncode.py output:\r\n")
    wait_for(lambda: mon.mark() == 2)
    mon.assert_none(r"Hard fault")
    mon.assert_none(r"^code\.py", since=2)  # since skips what came before
    with pytest.raises(SerialUnexpectedError, match=r"line 1 matched") as exc:
        mon.assert_none(r"^code\.py")
    assert isinstance(exc.value, AssertionError)

    # Watching for `duration` sees a line that arrives during the window.
    later(0.2, pty_board.write, "Hard fault: memory access or instruction error.\r\n")
    t0 = time.monotonic()
    with pytest.raises(SerialUnexpectedError, match="Hard fault"):
        mon.assert_none(r"Hard fault", duration=3)
    assert time.monotonic() - t0 < 2.5  # fails when the line lands, not at the end
    # assert_none never moves the cursor: expect still finds the first line.
    assert mon.expect(r"^Auto-reload", timeout=1)


def test_assert_none_waits_out_a_quiet_window(pty_board, open_monitor):
    mon = open_monitor()
    t0 = time.monotonic()
    mon.assert_none(r"Hard fault", duration=0.4)
    assert time.monotonic() - t0 >= 0.4


def test_ansi_and_osc_are_stripped(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write("\x1b]0;\xf0\x9f\x90\x8dDone | 11.0.0\x1b\\" + BANNER + "\r\n")
    pty_board.write("\x1b[32mI (312) wifi: connected\x1b[0m\r\n")
    pty_board.write("\x1b]0;title with bel\x07>>> x\r\n")
    pty_board.write("\x1b[?25lcursor hidden\x1b[2K\r\n")
    assert mon.expect(r"^Adafruit CircuitPython \S+ on [\d-]+; .+ with rp2040$", timeout=5)
    assert mon.expect(r"^I \(312\) wifi: connected$", timeout=5)
    assert mon.expect(r"^>>> x$", timeout=5)
    assert mon.expect(r"^cursor hidden$", timeout=5)
    assert all("\x1b" not in l.text for l in mon.lines())


def test_crlf_and_bad_utf8(pty_board, open_monitor):
    mon = open_monitor()
    pty_board.write(b"crlf\r\nlf only\n\xff\xfe raw\r\n")
    assert mon.expect(r"^crlf$", timeout=5)
    assert mon.expect(r"^lf only$", timeout=5)
    assert mon.expect("\ufffd\ufffd raw$", timeout=5)


def test_long_lines_are_split(pty_board, open_monitor):
    """A run with no newline is split off at SERIAL_MAX_LINE (Brent's 4096), so garbage can't
    grow one line forever, however the bytes arrive."""
    mon = open_monitor()
    junk = "".join(chr(ord("a") + i % 26) for i in range(3 * SERIAL_MAX_LINE + 100))
    pty_board.write(junk + "\r\ntail\r\n")
    assert mon.expect(r"^tail$", timeout=5)
    lines = [l.text for l in mon.lines()]
    assert "".join(lines[:-1]) == junk
    assert max(len(t) for t in lines) <= SERIAL_MAX_LINE


def test_feed_splits_one_big_read():
    """The same split when one read returns many lines' worth (pyserial reads in_waiting)."""

    class Bare(SerialMonitor):
        def _open(self, path=None):  # no port: drive _feed by hand
            pass

    mon = Bare(type("B", (), {"name": "bare", "power": {}})())
    mon._feed(b"x" * (2 * SERIAL_MAX_LINE + 5), time.time(), time.monotonic())
    assert [len(l.text) for l in mon.lines()] == [SERIAL_MAX_LINE, SERIAL_MAX_LINE]
    mon._feed(b"\n", time.time(), time.monotonic())
    assert [len(l.text) for l in mon.lines()] == [SERIAL_MAX_LINE, SERIAL_MAX_LINE, 5]


def test_timestamps(pty_board, open_monitor):
    mon = open_monitor()
    before = time.time()
    pty_board.write("first\r\n")
    wait_for(lambda: mon.mark() == 1)
    time.sleep(0.3)
    pty_board.write("second\r\n")
    wait_for(lambda: mon.mark() == 2)
    a, b = mon.lines()
    assert (a.index, b.index) == (0, 1)
    assert before - 0.1 <= a.time <= b.time <= time.time()
    assert b.mono - a.mono >= 0.25
    assert 0.25 <= b.time - a.time < 2


def test_dump(pty_board, open_monitor, tmp_path):
    mon = open_monitor()
    pty_board.write("".join("line %d\r\n" % i for i in range(5)) + "no newline")
    wait_for(lambda: mon.mark() == 5)
    assert mon.output == "".join("line %d\n" % i for i in range(5))
    mon.close()  # the trailing partial line is kept as a line
    dumped = mon.dump(tmp_path / "sub" / "x.serial.log").read_text().splitlines()
    assert len(dumped) == mon.mark() == 6
    assert all(re.match(r"^\[\d\d:\d\d:\d\d\.\d{3}\] ", l) for l in dumped)
    assert dumped[-1].endswith("] no newline")


def test_port_lost_without_reopen_is_an_error_not_a_failure(pty_board, open_monitor):
    """Brent's test_power_cycle_drops_the_port (test_serial_monitor_hw.py:76 @ cf64e03): a lost
    port is SerialError, which is not an AssertionError, so pytest reports an error."""
    mon = open_monitor(reopen=False)
    pty_board.write("before\r\n")
    assert mon.expect("before", timeout=5)
    pty_board.unplug()
    with pytest.raises(SerialError, match="lost") as exc:
        mon.expect("never printed", timeout=10)
    assert not isinstance(exc.value, AssertionError)
    with pytest.raises(SerialError, match="lost") as exc:
        mon.assert_none("anything", duration=1)
    assert not isinstance(exc.value, AssertionError)
    mon.close()
    with pytest.raises(SerialError, match="closed"):
        mon.soft_reset()
    with pytest.raises(SerialError, match="closed"):
        mon.write(b"x")


def test_port_lost_and_not_back_is_an_error(pty_board, open_monitor):
    mon = open_monitor()  # reopen=True, the farm default
    pty_board.comes_back = False
    pty_board.unplug()
    t0 = time.monotonic()
    with pytest.raises(SerialError, match="did not come back") as exc:
        mon.expect("never printed", timeout=10)
    assert not isinstance(exc.value, AssertionError)
    assert time.monotonic() - t0 < 5


def test_port_lost_and_back_is_reopened(pty_board, open_monitor, tmp_path):
    """A board that resets drops off USB and comes back on a new tty: the capture carries on,
    resolving the tty again from the board (never the cached name)."""
    mon = open_monitor()
    old = pty_board.path
    pty_board.write("before reset\r\n")
    assert mon.expect("before reset", timeout=5)

    def reset():
        pty_board.unplug()
        new = pty_board.replug()
        wait_for(lambda: mon.port == new)
        time.sleep(0.2)  # pyserial flushes input on open
        pty_board.write("after reset\r\n")

    later(0.1, reset)
    assert mon.expect("after reset", timeout=10)
    assert mon.port == pty_board.path != old
    text = mon.dump(tmp_path / "r.log").read_text()
    assert "--- cpr: port lost" in text and "--- cpr: reopened %s" % pty_board.path in text


def test_port_is_exclusive(pty_board, open_monitor):
    """A second opener fails cleanly instead of splitting the reads (the open-port guard in
    lib/flash.py relies on holders being visible, Brent's test_flash_refuses_open_port)."""
    open_monitor()
    with pytest.raises(SerialError, match="cannot open"):
        open_monitor()


def test_no_tty_is_an_error(pty_board):
    pty_board.unplug()
    with pytest.raises(SerialError, match="no tty"):
        SerialMonitor(pty_board)


def test_soft_reset_against_a_fake_repl(pty_board, open_monitor):
    """prompt() syncs on '>>> ' through the eaten first key, Ctrl-D reboots, and the boot start
    moves: expect and assert_none only see the new boot (Brent's
    test_second_reset_starts_a_new_boot, test_serial_monitor_hw.py:33 @ cf64e03)."""
    selftest = pytest.importorskip("tools.selftest")
    stop = threading.Event()
    mon = open_monitor()
    repl = threading.Thread(target=selftest.fake_repl, args=(pty_board.master, stop),
                            daemon=True)
    repl.start()
    try:
        first = mon.soft_reset(timeout=10)
        assert mon.lines()[first].text == "soft reboot"
        assert mon.boot == first
        assert mon.expect(r"^Code done running\.$", timeout=5)
        second = mon.soft_reset(timeout=10)
        assert second > first
        resets = [l for l in mon.lines(second) if l.text == "soft reboot"]
        assert len(resets) == 1
        mon.assert_none(r"^soft reboot$", since=second + 1)
        with pytest.raises(SerialUnexpectedError):
            mon.assert_none(r"^soft reboot$", since=first + 1)
    finally:
        # Wake the fake REPL out of its blocking read before the fixture closes the master:
        # on macOS close() waits for a read another thread is blocked in.
        stop.set()
        mon.write(b"\r")
        repl.join(timeout=5)
