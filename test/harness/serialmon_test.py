# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/serialmon.py against a pty pair: what Brent's test_serial_monitor_hw.py checks on his
esp32 (tests_hil_board_utils/test_serial_monitor_hw.py @ cf64e03), with the board replaced by
the master end of os.openpty(). Nothing here opens a farm tty."""
import os
import re
import select
import threading
import time

import pytest

from farmlib import FarmError
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


def settling_ttys(board, tmp_path, bad_opens=2):
    """board.ttys() that first names a by-path link which exists and is readable but is not a
    tty, so open() fails the way a link whose device is still going away does, then the real
    tty. Returns (the link's path, a list of the calls)."""
    link = tmp_path / "by-path-link"
    link.write_text("")
    calls, real = [], board.ttys

    def ttys():
        calls.append(1)
        return [str(link)] if len(calls) <= bad_opens else real()

    board.ttys = ttys
    return link, calls


def test_reopen_retries_an_open_that_fails_while_the_tty_settles(pty_board, open_monitor,
                                                                 tmp_path):
    """After a reset the by-path name can still resolve to a link whose open fails. That is not
    a lost board: the reopen is retried inside the wait, and the capture carries on."""
    mon = open_monitor()
    pty_board.write("before\r\n")
    assert mon.expect("before", timeout=5)
    pty_board.unplug()
    pty_board.replug()
    _, calls = settling_ttys(pty_board, tmp_path)

    def back():
        wait_for(lambda: mon.port == pty_board.path)
        time.sleep(0.2)  # pyserial flushes input on open
        pty_board.write("after\r\n")

    later(0.1, back)
    assert mon.expect("after", timeout=10)
    assert len(calls) >= 3 and mon.port == pty_board.path


def test_port_reset_retries_an_open_that_fails_while_the_tty_settles(pty_board, open_monitor,
                                                                     tmp_path, monkeypatch):
    """port_reset() goes through the same retried reopen as the recovery path."""
    mon = open_monitor()
    pty_board.write("before\r\n")
    assert mon.expect("before", timeout=5)

    def power_cycle(**_kw):  # the board drops off USB and comes back on a new tty
        pty_board.unplug()
        pty_board.replug()

    monkeypatch.setattr(pty_board, "power_cycle", power_cycle, raising=False)
    _, calls = settling_ttys(pty_board, tmp_path)
    boot = mon.port_reset(delay=0, timeout=10)
    assert len(calls) >= 3 and mon.port == pty_board.path
    assert mon.boot == boot
    time.sleep(0.2)  # pyserial flushes input on open
    pty_board.write("after\r\n")
    assert mon.expect("after", timeout=5)


def test_hard_reset_reads_devnum_after_the_prompt(pty_board, open_monitor, monkeypatch):
    """prompt() can recover the port, and a re-enumerated board has a new devnum. hard_reset()
    must wait for the change its own reset makes, not stop at the change prompt() already made.
    """
    mon = open_monitor()
    state = {"dev": "old"}
    monkeypatch.setattr(pty_board, "devnum", lambda: state["dev"])

    def prompt(timeout=20):  # the port was recovered while syncing: a new enumeration
        state["dev"] = "new"

    monkeypatch.setattr(mon, "prompt", prompt)

    def reset_takes_effect():  # the board resets half a second after the write
        pty_board.unplug()
        pty_board.replug()
        state["dev"] = "reset"  # the new enumeration is visible only once its tty is

    later(0.5, reset_takes_effect)
    t0 = time.monotonic()
    mon.hard_reset(timeout=10)
    assert time.monotonic() - t0 >= 0.4
    assert mon.port == pty_board.path


def test_assert_none_out_of_time_with_the_port_gone_is_an_error(pty_board, open_monitor):
    """With the deadline passed and the port lost, nothing was watched to the end: not a pass."""
    mon = open_monitor()  # reopen=True
    pty_board.comes_back = False
    pty_board.unplug()
    assert wait_for(lambda: mon._error is not None)  # pylint: disable=protected-access
    with pytest.raises(SerialError, match="lost") as exc:
        mon.assert_none("anything", duration=0)
    assert not isinstance(exc.value, AssertionError)


def test_dump_puts_a_late_stored_line_before_the_marker_it_predates(tmp_path):
    """A line whose bytes arrived before a marker can be stored after it. The dump is in time
    order, so the line comes first. (Placed by line index, the marker came first.)"""

    class Bare(SerialMonitor):
        def _open(self, path=None):  # no port: feed the capture by hand
            pass

    mon = Bare(type("B", (), {"name": "bare", "power": {}})())
    now, mono = time.time(), time.monotonic()
    mon._feed(b"first\n", now - 2, mono - 2)
    mon._event("soft reset (Ctrl-D)")
    mon._feed(b"late\n", now - 1, mono - 1)
    mon._feed(b"after\n", time.time(), time.monotonic())
    dumped = mon.dump(tmp_path / "d.log").read_text().splitlines()
    assert [d.split("] ", 1)[1] for d in dumped] == [
        "first", "late", "--- cpr: soft reset (Ctrl-D)", "after"]
    stamps = [d[1:13] for d in dumped]
    assert stamps == sorted(stamps)


def test_dump_orders_a_soft_reset_marker_before_its_boot(pty_board, open_monitor, tmp_path):
    selftest = pytest.importorskip("tools.selftest")
    stop = threading.Event()
    mon = open_monitor()
    repl = threading.Thread(target=selftest.fake_repl, args=(pty_board.master, stop),
                            daemon=True)
    repl.start()
    try:
        mon.soft_reset(timeout=10)
        dumped = mon.dump(tmp_path / "s.log").read_text().splitlines()
    finally:
        stop.set()
        mon.write(b"\r")
        repl.join(timeout=5)
    marker = next(i for i, d in enumerate(dumped) if "--- cpr: soft reset" in d)
    boot = next(i for i, d in enumerate(dumped) if d.endswith("] soft reboot"))
    assert marker < boot
    stamps = [d[1:13] for d in dumped]
    assert stamps == sorted(stamps)


def test_a_tty_path_is_the_console_power_cycle_monitor_builds(pty_board):
    """power_cycle_monitor.py calls cls(tty, baud) and reads these names; PORT_ARGS says the
    class takes that form."""
    mon = SerialMonitor(pty_board.path, 115200)  # the baud is the second positional argument
    try:
        assert SerialMonitor.PORT_ARGS is True
        for name in ("uptime", "opened", "first_byte", "last_line", "expect", "assert_none",
                     "mark", "dump", "soft_reset", "prompt", "write"):
            assert hasattr(mon, name), name
        assert mon.port == pty_board.path and mon.opened is not None
        assert mon._serial.baudrate == 115200  # pylint: disable=protected-access
        assert mon.first_byte is None and mon.last_line is None
        pty_board.write("hello\r\n")
        assert mon.expect("hello", timeout=5)
        assert mon.last_line.text == "hello"
        assert mon.first_byte == mon.last_line.time
        with pytest.raises(FarmError, match="no USB devnum"):
            mon.board.devnum()
    finally:
        mon.close()


def test_uptime_reads_the_boards_clock(pty_board, monkeypatch):
    """uptime() types the clock query at a synced prompt and reads its one line back."""
    mon = SerialMonitor(pty_board.path)
    monkeypatch.setattr(mon, "prompt", lambda timeout=20: None)  # the fake board is not a REPL
    stop = threading.Event()

    def board():  # answers the query as a board that has run for 12.5 s after a power-on
        buf = b""
        while not stop.is_set():
            ready, _, _ = select.select([pty_board.master], [], [], 0.05)
            if not ready:
                continue
            buf += os.read(pty_board.master, 1024)
            if b"microcontroller as m" in buf:
                os.write(pty_board.master, b"@@UP 12.5 POWER_ON\r\n")
                return

    t = threading.Thread(target=board, daemon=True)
    t.start()
    try:
        assert mon.uptime(timeout=5) == (12.5, "POWER_ON")
    finally:
        stop.set()
        t.join(timeout=2)
        mon.close()


def test_power_cycle_monitor_uses_this_class():
    """Once PORT_ARGS and the names are there, the power tool picks this class up (not its own
    local port of it)."""
    pcm = pytest.importorskip("tools.power_cycle_monitor")
    assert pcm.monitor_class() is SerialMonitor


def test_keep_input_reads_the_bytes_sent_before_the_open(pty_board):
    """A boot's first lines can arrive before the monitor opens. pyserial's open flushes the
    tty's input unless keep_input says not to (power_cycle_monitor.py passes keep_input=True)."""
    pty_board.write("boot line\r\nsecond\r\n")
    time.sleep(0.2)  # the bytes are in the tty before the open
    mon = SerialMonitor(pty_board.path, 115200, keep_input=True)
    try:
        assert mon.expect("^boot line$", timeout=5)
        assert mon.expect("^second$", timeout=5)
    finally:
        mon.close()


def test_power_cycle_monitor_retries_serialmons_own_open_error(pty_board, monkeypatch):
    """serialmon raises its own SerialError when the tty is not yet openable. capture_power_boot
    must retry that, as it retries its local SerialError, not fail the board check."""
    pcm = pytest.importorskip("tools.power_cycle_monitor")
    calls = []

    def factory(port, baud, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise SerialError("%s: cannot open: still settling" % port)
        return SerialMonitor(port, baud, **kw)

    class Watch:  # the stop/gone/back fields capture_power_boot reads
        stop = threading.Event()
        gone = time.time()
        back = time.time()

    cyc = {"t0": time.time(), "uhubctl_s": 0.0, "devnum_before": pty_board.devnum(),
           "watch": Watch()}
    monkeypatch.setattr(pcm, "monitor_class", lambda: factory)
    monkeypatch.setattr(pcm, "power_cycle_board", lambda board, delay=3: cyc)
    monkeypatch.setattr(pcm, "wait_for_port", lambda board, timeout=15: pty_board.path)
    rep, mon = pcm.capture_power_boot(pty_board, passive=True, timeout=0.2, port_timeout=5)
    try:
        assert len(calls) == 2 and all(kw == {"keep_input": True} for kw in calls)
        assert mon is not None and mon.port == pty_board.path
        assert not any("cannot open" in p for p in rep.problems), rep.problems
    finally:
        if mon is not None:
            mon.close()
