# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Console capture for one board: lines on a background thread, expect() / assert_none() on them.

After Brent's SerialMonitor (hil_board_utils.py:738-1083 @ cf64e03, from his 9c5e6ac6 and
3ca8a507). Same names and shapes, so his tests read the same here: SerialLine, mark(), boot,
expect(), assert_none(), lines(), output, dump(), and his error split (SerialError for a lost
port; SerialTimeout / SerialUnexpectedError are AssertionErrors, so pytest counts them as test
failures).

What this farm adds to his:
  * the port is resolved from the board's sysfs USB path (/dev/serial/by-path) every time it is
    opened, never cached, since boards re-enumerate on every reset;
  * write_timeout=5 on every open;
  * a dropped port is reopened when the board comes back (reopen=True, the default), instead of
    failing the test; Brent's behaviour is reopen=False;
  * prompt() syncs on '>>> ' before any keys go out, because the REPL eats the first keystroke;
  * resets for native USB boards: soft_reset() (Ctrl-D), hard_reset() (microcontroller.reset()
    then wait for re-enumeration) and port_reset() (Board.power_cycle(), the flock-serialized,
    unprivileged uhubctl). His board_reset() (DTR/RTS into EN/IO0) is not here: our native USB
    boards do not wire those lines.
  * OSC sequences (CircuitPython's status bar title) are stripped along with ANSI colours.
  * SerialMonitor(tty_path, baud) takes a bare tty as well as a board, for tools/
    power_cycle_monitor.py (PORT_ARGS). A bare tty has no USB devnum and no hub port, so
    hard_reset() and port_reset() raise FarmError there; soft_reset() and uptime() work.

Each line is logged at INFO on the 'cpr.serial' logger, so pytest shows it under "Captured log"
on failure (or live with --log-cli-level=INFO).
"""

import logging
import os
import re
import termios
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cpstrings import to_english
from farmlib import FarmError

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - reported by run.py
    serial = None

SERIAL_BAUD = 115200
# Read timeout: how long the reader thread blocks before checking for close().
SERIAL_READ_TIMEOUT = 0.05
SERIAL_WRITE_TIMEOUT = 5
# A run of bytes this long with no newline is split off as its own line, so garbage can't grow
# the pending line forever (Brent's SERIAL_MAX_LINE).
SERIAL_MAX_LINE = 4096
# Raw bytes kept for prompt syncing (">>> " never ends in a newline, so it is not a line).
RAW_KEEP = 64 * 1024
# CSI (colours, cursor) and OSC (CircuitPython's status bar sets the terminal title with
# ESC ] 0; ... ESC \) sequences, stripped so they can't break anchors.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
TAIL_LINES = 20

serial_log = logging.getLogger("cpr.serial")


class SerialError(FarmError):
    """The port could not be opened, or dropped and did not come back. A test error."""


class SerialWriteTimeout(SerialError):
    """A write did not go out within write_timeout. The port is still there: an ESP32 USB-JTAG
    console takes no input while the chip boots (Feather ESP32-C6: 2.6 s to 10 s after a
    reset, 3 of 3), so the caller may poke again."""


class SerialTimeout(SerialError, AssertionError):
    """expect() saw no matching line (or the board gave no prompt); a test failure."""


class SerialUnexpectedError(SerialError, AssertionError):
    """assert_none() saw a matching line; a test failure."""


class _TtyPath:
    """Stands in for farmlib.Board when SerialMonitor is given a tty path (see __init__).

    The path is the console and nothing else: there is no USB device behind it to re-enumerate
    (devnum() refuses) and no hub port to cycle (power_cycle() refuses).
    """

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path) or path
        self.power = {}

    def ttys(self):
        return [self.path] if os.path.exists(self.path) else []

    def present(self):
        return os.path.exists(self.path)

    def devnum(self):
        raise FarmError("%s: a bare tty has no USB devnum; use a board for hard_reset()"
                        % self.name)

    def power_cycle(self, **_kw):
        raise FarmError("%s: a bare tty has no hub port to power-cycle; use a board for "
                        "port_reset()" % self.name)

    def wait_ready(self, timeout=None, need_mount=False):  # pylint: disable=unused-argument
        """Seconds until the path exists, or None if it does not within timeout."""
        t0 = time.monotonic()
        end = t0 + (timeout or 10)
        while not os.path.exists(self.path):
            if time.monotonic() >= end:
                return None
            time.sleep(0.05)
        return time.monotonic() - t0


@dataclass(frozen=True)
class SerialLine:
    """One console line. Field order matches Brent's (index, time, text); mono is ours."""

    index: int
    time: float  # time.time() when the line's first byte arrived
    text: str    # decoded, without the line ending or ANSI/OSC sequences
    mono: float = 0.0  # time.monotonic() at the same moment, for intervals


class SerialMonitor:
    """Capture a board's console as lines on a background thread.

    In a function-scoped fixture:

        mon = SerialMonitor(board)   # opens the console; the board keeps running
        mon.soft_reset()             # optional: boot under observation
        yield mon                    # the test calls expect / mark / assert_none
        mon.close()
        mon.dump(path)

    Matching (as Brent's): patterns are str regexes (or compiled), searched one complete line at
    a time with re.search, so ^ and $ anchor to the line. A line counts once its '\\n' arrives.
    bytes patterns are decoded, for tests written against the older byte-stream expect().

    Positions are line indices. mark() is the index the next complete line will get. expect()
    searches forward from a cursor that only moves forward: past each match, and to the boot
    start on any reset. assert_none() defaults to the boot start.
    """

    # power_cycle_monitor.py builds its monitor as cls(tty, baud) and checks PORT_ARGS for that.
    PORT_ARGS = True

    def __init__(self, board, iface=0, baud=SERIAL_BAUD, reopen=True, keep_input=False):
        """board is a farmlib.Board (the console is its interface iface), or a tty path, as
        power_cycle_monitor passes it. With a path the second positional argument is the baud
        rate, so SerialMonitor(path, 115200) and SerialMonitor(path, baud=115200) both work.
        A path has no board behind it, so hard_reset() and port_reset() raise FarmError.
        keep_input=True keeps the bytes the board sent before this open: pyserial's open
        otherwise flushes the tty's input, which can drop a boot's first lines."""
        if serial is None:
            raise SerialError("pyserial is not installed (pip install pyserial)")
        if isinstance(board, (str, os.PathLike)):
            if iface:
                baud = iface
            board, iface = _TtyPath(os.fspath(board)), 0
        self.board = board
        self.iface = iface
        self.reopen = reopen
        self._keep_input = keep_input
        self.port = None  # the by-path name of the current open, for messages only
        self.opened = None  # time.time() of the last successful open
        self.first_byte = None  # time.time() of the first byte since the last open
        self.last_line = None  # the SerialLine the last expect() matched
        self._baud = baud
        self._lines = []
        self._events = []  # (wall time, monotonic time, text): resets and reopens, for dump()
        self._partial = bytearray()
        self._partial_time = (0.0, 0.0)
        self._raw = bytearray()
        self._raw_base = 0  # stream offset of self._raw[0]
        self._last_rx = time.monotonic()
        self._cursor = 0
        self._boot = 0
        self._error = None
        self._closed = False
        self._serial = None
        self._reader = None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._open()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _name(self):
        return "%s (%s)" % (self.board.name, self.port)

    # -- port -----------------------------------------------------------------

    def _resolve(self):
        """The console's by-path tty, looked up now from the board's USB path."""
        ttys = self.board.ttys()
        return ttys[self.iface] if len(ttys) > self.iface else None

    def _open(self, path=None):
        """Open the port (resolved now unless given) and start the reader thread."""
        path = path or self._resolve()
        if not path:
            raise SerialError("%s: no tty at interface %d" % (self.board.name, self.iface))
        ser = serial.Serial()
        ser.port = path
        ser.baudrate = self._baud
        ser.timeout = SERIAL_READ_TIMEOUT
        ser.write_timeout = SERIAL_WRITE_TIMEOUT
        ser.exclusive = True  # another exclusive opener fails cleanly instead of sharing reads
        # pyserial 3.5's posix open() ends with _reset_input_buffer() (tcflush). keep_input
        # skips that one call, so bytes the board sent before this open are read.
        if self._keep_input:
            ser._reset_input_buffer = lambda: None  # pylint: disable=protected-access
        try:
            ser.open()
        except (OSError, ValueError) as e:
            raise SerialError("%s: cannot open %s: %s" % (self.board.name, path, e)) from e
        finally:
            if self._keep_input:
                del ser._reset_input_buffer  # pylint: disable=protected-access
        with self._cond:
            self.port = path
            self._serial = ser
            self._error = None
            self._last_rx = time.monotonic()
            self.opened = time.time()
            self.first_byte = None
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="SerialMonitor(%s)" % self.board.name)
        self._reader.start()

    def _shutdown(self):
        """Stop the reader and close the port. A trailing partial line is kept as a line."""
        self._stop.set()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join()
        self._reader = None
        if self._serial is not None:
            try:
                self._serial.close()
            except OSError:  # closing a port that already vanished
                pass
        with self._cond:
            if self._partial:  # last words before the port went away
                self._emit(self._partial, *self._partial_time)
                self._partial.clear()
            self._cond.notify_all()

    def _event(self, text):
        """Note a harness action in the capture, placed in dump() by monotonic time; never
        matched. Call it when the action happens: lines whose bytes arrived before that moment
        come before it in the dump, even if the reader thread stored them after."""
        with self._cond:
            self._events.append((time.time(), time.monotonic(), text))
        serial_log.info("%s | --- %s", self.board.name, text)

    def _wait_port(self, timeout=None):
        """Wait for the console to re-enumerate and be openable; return its by-path name.

        Like Brent's wait_for_port (hil_board_utils.py:716): the tty can appear before udev has
        given it its group, so wait until it is readable and writable too.
        """
        timeout = timeout or 60 + self.board.power.get("settle", 12)
        deadline = time.monotonic() + timeout
        if self.board.wait_ready(timeout=timeout, need_mount=False) is None:
            raise SerialError("%s: console did not come back within %.0f s"
                              % (self.board.name, timeout))
        while True:
            path = self._resolve()
            if path and os.access(path, os.R_OK | os.W_OK):
                return path
            if time.monotonic() >= deadline:
                raise SerialError("%s: no permission to open %s" % (self.board.name, path))
            time.sleep(0.25)

    def _reopen(self, deadline):
        """Wait for the console to come back and open it; return its by-path name.

        The open is retried until deadline. Right after a reset the by-path link can outlive its
        device (ENOENT or ENODEV), or the tty can exist before it is readable, so one failed open
        is not the board being gone. Raises SerialError at the deadline.
        """
        while True:
            path = self._wait_port(max(0.1, deadline - time.monotonic()))
            try:
                self._open(path)
                return path
            except SerialError:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(0.25)

    def _recover(self, deadline):
        """The reader lost the port. Reopen it once the board is back (reopen=True), else raise.

        Boards drop off USB whenever they reset, so a test that resets a board some other way
        keeps its capture. Waits at least 5 s, even past the caller's deadline.
        """
        err = self._error
        if not self.reopen or self._closed:
            raise SerialError("%s: serial port lost: %s" % (self._name(), err))
        self._shutdown()
        self._event("port lost (%s), waiting for it" % err)
        try:
            path = self._reopen(max(deadline, time.monotonic() + 5))
        except SerialError as e:
            with self._cond:
                self._error = e
            raise SerialError("%s: serial port lost: %s; %s" % (self._name(), err, e)) from e
        self._event("reopened %s" % path)

    def _check_open(self):
        if self._closed:
            raise SerialError("%s: monitor is closed" % self._name())

    def close(self):
        """Stop capturing and release the port. The capture stays readable."""
        if self._closed:
            return
        self._shutdown()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # -- capture --------------------------------------------------------------

    def _read_loop(self):
        ser = self._serial
        while not self._stop.is_set():
            try:
                data = ser.read(ser.in_waiting or 1)
            except (OSError, TypeError, AttributeError) as e:  # SerialException is an OSError
                with self._cond:
                    if not self._stop.is_set():
                        self._error = e
                    self._cond.notify_all()
                return
            if not data:
                continue
            with self._cond:
                self._feed(data, time.time(), time.monotonic())
                self._cond.notify_all()

    def _feed(self, data, now, mono):
        self._raw += data
        self._last_rx = mono
        if self.first_byte is None:
            self.first_byte = now
        if len(self._raw) > RAW_KEEP:
            cut = len(self._raw) - RAW_KEEP
            del self._raw[:cut]
            self._raw_base += cut
        if not self._partial:
            self._partial_time = (now, mono)
        self._partial += data
        # Split at each newline, and at SERIAL_MAX_LINE bytes when no newline comes first.
        # Brent's (hil_board_utils.py:870-876 @ cf64e03) checks the limit once, after the
        # newlines: one read can bring several limits' worth (pyserial reads all of
        # in_waiting), and a long run that ends in a newline is never split, so a line could
        # grow well past the limit.
        while True:
            i = self._partial.find(b"\n", 0, SERIAL_MAX_LINE + 1)
            if i < 0 and len(self._partial) < SERIAL_MAX_LINE:
                break
            cut, skip = (i, 1) if i >= 0 else (SERIAL_MAX_LINE, 0)
            self._emit(self._partial[:cut], *self._partial_time)
            del self._partial[:cut + skip]
            self._partial_time = (now, mono)

    def _emit(self, raw, now, mono):
        text = ANSI_ESCAPE_RE.sub("", raw.decode(errors="replace")).rstrip("\r")
        # Lines are matched in English; the log keeps what a translated build printed.
        self._lines.append(SerialLine(len(self._lines), now, to_english(text), mono))
        serial_log.info("%s | %s", self.board.name, text)

    # -- sending ----------------------------------------------------------------

    def write(self, data):
        """Send bytes as they are. Most callers want send(), which syncs on the prompt first."""
        self._check_open()
        if isinstance(data, str):
            data = data.encode()
        try:
            self._serial.write(data)
            self._serial.flush()
        # write_timeout raises SerialTimeoutException; flush() is a bare tcdrain(), which raises
        # termios.error (not an OSError) when the board resets under it
        except (OSError, AttributeError, termios.error) as e:
            kind = (SerialWriteTimeout if serial is not None
                    and isinstance(e, serial.SerialTimeoutException) else SerialError)
            raise kind("%s: write failed: %s" % (self._name(), e)) from e

    def _raw_mark(self):
        with self._cond:
            return self._raw_base + len(self._raw)

    def _raw_since(self, offset):
        with self._cond:
            return to_english(bytes(self._raw[max(0, offset - self._raw_base):]))

    def _wait_raw(self, token, offset, timeout):
        """True once token shows in the raw stream after offset, False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                if token in to_english(self._raw[max(0, offset - self._raw_base):]):
                    return True
                remaining = deadline - time.monotonic()
                if not self._error and not self._closed and remaining > 0:
                    self._cond.wait(remaining)
                    continue
            if self._error and not self._closed:
                self._recover(deadline)
                continue
            return False

    def _wait_quiet(self, quiet=0.3, limit=3):
        """Wait until nothing has arrived for quiet seconds (or limit seconds pass)."""
        end = time.monotonic() + limit
        with self._cond:
            while True:
                now = time.monotonic()
                left = min(end - now, self._last_rx + quiet - now)
                if left <= 0 or self._error or self._closed:
                    return
                self._cond.wait(left)

    def prompt(self, timeout=20):
        """Interrupt whatever runs and get to a stable friendly '>>> ' prompt.

        Same loop as farmlib.Repl.prompt(): keep poking until the prompt shows and stays. A host
        write to CIRCUITPY can trigger an auto-reload after our keys went out, leaving the board
        at "Press any key" with nothing left to wake it, and that first key is eaten, so each
        poke sends several.
        """
        __tracebackhide__ = True  # pylint: disable=unused-variable
        self._check_open()
        start = self.mark()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sent = self._raw_mark()
            try:
                self.write(b"\r\x03\x03")
            except SerialWriteTimeout:
                time.sleep(0.5)  # booting; it takes input again once the console runs
                continue
            except SerialError:
                if not self.reopen:
                    raise
                self._recover(deadline)  # the board was resetting; poke the new port
                continue
            if not self._wait_raw(b">>> ", sent, min(1.5, max(0.1, deadline - time.monotonic()))):
                continue
            after = self._raw_mark()
            self._wait_quiet(quiet=0.5, limit=2)
            later = self._raw_since(after)
            if b"soft reboot" in later or b"Press any key" in later or b"Code done" in later:
                continue
            return
        raise SerialTimeout("%s: no stable '>>> ' prompt within %.3g s; lines from %d:\n%s"
                            % (self._name(), timeout, start, self._tail(start)))

    def send(self, line):
        """Type a line at a synced friendly prompt."""
        self.prompt()
        if isinstance(line, bytes):
            line = line.decode()
        self.write(line.rstrip("\r\n").encode() + b"\r\n")

    # -- board control ----------------------------------------------------------

    def soft_reset(self, timeout=20):
        """Soft reboot (Ctrl-D at a synced prompt) and return the boot start: the index of the
        'soft reboot' line. The cursor moves there, so the next expect() only sees this boot."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        self._check_open()
        self.prompt()
        start = self.mark()
        self._event("soft reset (Ctrl-D)")
        self.write(b"\x04")
        line, _ = self._wait_line(re.compile(r"soft reboot"), start, timeout)
        with self._cond:
            self._boot = self._cursor = line.index
        return self._boot

    reset = soft_reset  # the name console tests used before the port

    def hard_reset(self, timeout=None):
        """microcontroller.reset() from the REPL, wait for the board to drop off USB and come
        back, reopen the console, and return the boot start (the cursor moves there).

        The board boots while the host is still enumerating it, so the first lines of the boot
        (e.g. code.py's first prints) can be missed, as with Brent's port_reset.
        """
        self._check_open()
        self.prompt()  # may recover the port, and with it the board's devnum
        devnum = self.board.devnum()
        self._event("hard reset (microcontroller.reset())")
        with self._cond:
            before = len(self._lines)
        try:
            self.write(b"import microcontroller; microcontroller.reset()\r\n")
        except SerialError:
            pass  # the port can vanish under the write; the wait below decides
        # Without CircuitPython's own USB the console is a USB-JTAG unit, a probe's CDC or a
        # UART bridge, and it stays on the bus through the reset (Feather ESP32-C6: ESP-ROM
        # banner 36 ms after the write, same devnum). There the boot is the first line after
        # the echoed command.
        stays = "native_usb" not in getattr(self.board, "caps", {"native_usb"})
        end = time.monotonic() + 10
        while True:
            with self._cond:
                gone = self._error is not None
                after = [l for l in self._lines[before:]
                         if l.text.strip() and "microcontroller.reset()" not in l.text]
            if gone or not self.board.present() or self.board.devnum() != devnum:
                break
            if stays and after:
                with self._cond:
                    self._boot = self._cursor = after[0].index
                self._event("reset seen on the open console (it stays on USB)")
                return self._boot
            if time.monotonic() >= end:
                raise SerialError("%s: still on USB 10 s after microcontroller.reset()"
                                  % self._name())
            time.sleep(0.05)
        return self._reopen_after_reset(timeout)

    def port_reset(self, delay=3, timeout=None):
        """Power-cycle the board's hub port, wait for the console to re-enumerate, reopen it, and
        return the boot start (the cursor moves there). delay is how long power stays off.

        After Brent's port_reset (hil_board_utils.py:934 @ cf64e03), but through
        Board.power_cycle(): unprivileged uhubctl, serialized with the farm's other cycles by
        flock, with this board's -r/-w from farm.toml. Never sudo, no port cache. As with his,
        the first lines of the boot can be missed.
        """
        self._check_open()
        self._shutdown()
        self._event("port reset (uhubctl cycle, %s s off)" % delay)
        try:
            self.board.power_cycle(off_s=delay, wait=False)
        except FarmError as e:
            with self._cond:  # expect()/assert_none() try to reopen instead of waiting
                self._error = e
                self._cond.notify_all()
            raise
        return self._reopen_after_reset(timeout)

    def _reopen_after_reset(self, timeout):
        self._shutdown()
        timeout = timeout or 60 + self.board.power.get("settle", 12)
        with self._cond:  # no reader runs until the reopen, so no line can arrive before this
            start = len(self._lines)
        try:
            path = self._reopen(time.monotonic() + timeout)
        except SerialError as e:
            with self._cond:
                self._error = e
                self._cond.notify_all()
            raise
        with self._cond:
            self._boot = self._cursor = start
        self._event("reopened %s" % path)
        return self._boot

    # -- matching ---------------------------------------------------------------

    def mark(self):
        """Return the index the next complete line will get."""
        with self._cond:
            return len(self._lines)

    @property
    def boot(self):
        """Index of the first line of the last reset's boot (0 if none)."""
        return self._boot

    @staticmethod
    def _compile(pattern):
        if isinstance(pattern, re.Pattern):
            if isinstance(pattern.pattern, bytes):
                return re.compile(pattern.pattern.decode(errors="replace"), pattern.flags)
            return pattern
        if isinstance(pattern, (bytes, bytearray)):
            pattern = bytes(pattern).decode(errors="replace")
        return re.compile(pattern)

    def _search(self, regex, start):
        for line in self._lines[start:]:
            m = regex.search(line.text)
            if m:
                return line, m
        return None, None

    def _tail(self, start, count=TAIL_LINES):
        with self._cond:
            lines = self._lines[start:]
            partial = bytes(self._partial)
        shown = ["  [%d] %s" % (l.index, l.text) for l in lines[-count:]]
        if len(lines) > count:
            shown.insert(0, "  ... %d earlier lines" % (len(lines) - count))
        if partial:
            text = to_english(ANSI_ESCAPE_RE.sub("", partial.decode(errors="replace")))
            shown.append("  [partial] %s" % text)
        return "\n".join(shown) if shown else "  (no lines)"

    def _wait_line(self, regex, start, timeout):
        """(line, match) for the first line from start that matches, or SerialTimeout."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        deadline = time.monotonic() + timeout
        pos = start
        while True:
            with self._cond:
                line, m = self._search(regex, pos)
                if m:
                    return line, m
                pos = len(self._lines)  # don't rescan lines already checked
                remaining = deadline - time.monotonic()
                if not self._error and not self._closed and remaining > 0:
                    self._cond.wait(remaining)
                    continue
            if self._error and not self._closed:
                self._recover(deadline)
                continue
            why = "monitor is closed" if self._closed else "within %.3g s" % timeout
            raise SerialTimeout("%s: no line matched %r (%s); lines from %d:\n%s"
                                % (self._name(), regex.pattern, why, start, self._tail(start)))

    def expect(self, pattern, timeout=10):
        """Wait for the first line at or after the cursor that matches pattern, move the cursor
        past that line, and return the re.Match (the line is m.string). Consecutive expect()
        calls therefore check order. Raises SerialTimeout, or SerialError if the port drops and
        does not come back.
        """
        __tracebackhide__ = True  # pylint: disable=unused-variable
        with self._cond:
            start = self._cursor
        line, m = self._wait_line(self._compile(pattern), start, timeout)
        with self._cond:
            self._cursor = max(self._cursor, line.index + 1)
            self.last_line = line
        return m

    def uptime(self, timeout=10):
        """(time.monotonic(), reset_reason) from the board, typed at a synced prompt. The
        board's uptime since its last reset, so a test can tell a real reset from a reopen."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        self.prompt()
        self.write(b"import time,microcontroller as m;"
                   b"print('@@UP',time.monotonic(),getattr(m.cpu,'reset_reason',None))\r")
        m = self.expect(r"^@@UP ([0-9.]+) (\S+)", timeout=timeout)
        return float(m.group(1)), m.group(2)

    def assert_none(self, pattern, since=None, duration=0):
        """Fail if any line from since (default: the boot start) matches pattern, watching new
        lines for duration more seconds. Doesn't move the cursor. Raises SerialUnexpectedError,
        or SerialError if the port drops and does not come back.
        """
        __tracebackhide__ = True  # pylint: disable=unused-variable
        regex = self._compile(pattern)
        deadline = time.monotonic() + duration
        first = pos = self._boot if since is None else since
        while True:
            with self._cond:
                line, m = self._search(regex, pos)
                if m:
                    raise SerialUnexpectedError(
                        "%s: line %d matched %r: %r; lines from %d:\n%s"
                        % (self._name(), line.index, regex.pattern, line.text, first,
                           self._tail_to(first, line.index)))
                pos = max(pos, len(self._lines))
                remaining = deadline - time.monotonic()
                if not self._error and not self._closed and remaining > 0:
                    self._cond.wait(remaining)
                    continue
            if self._error and not self._closed:
                if remaining > 0:
                    self._recover(deadline)  # raises if reopen is off or the board is not back
                    continue
                # Out of time with the port gone: nothing was watched to the end, so it is not
                # a pass (as Brent's _port_error() in assert_none, hil_board_utils.py:1052).
                raise SerialError("%s: serial port lost: %s" % (self._name(), self._error))
            return

    def _tail_to(self, start, last, count=TAIL_LINES):
        """Up to count lines ending at index last (the offending line)."""
        with self._cond:
            lines = self._lines[start:last + 1]
        shown = ["  [%d] %s" % (l.index, l.text) for l in lines[-count:]]
        if len(lines) > count:
            shown.insert(0, "  ... %d earlier lines" % (len(lines) - count))
        return "\n".join(shown)

    # -- output -----------------------------------------------------------------

    def lines(self, since=0):
        """Return the complete lines from index since on."""
        with self._cond:
            return self._lines[since:]

    @property
    def output(self):
        """All complete lines, joined with newlines."""
        with self._cond:
            return "".join("%s\n" % l.text for l in self._lines)

    def dump(self, path):
        """Write every line to path as '[HH:MM:SS.mmm] text', the time its first byte arrived,
        with resets and reopens as '[HH:MM:SS.mmm] --- cpr: ...' at the moment they were issued.

        Markers are merged in by monotonic time, not by line index: the reader thread can store
        a line after a marker was made even though its bytes arrived before it, and that line
        belongs before the marker. So the dump is in time order, and each marker's stamp is the
        time it was made. Returns the path."""
        with self._cond:
            lines = list(self._lines)
            events = list(self._events)  # appended in time order by _event()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def stamp(t):
            return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]

        def marker(ev):
            f.write("[%s] --- cpr: %s\n" % (stamp(ev[0]), ev[2]))

        ev = 0
        with path.open("w") as f:
            for l in lines:  # lines' monotonic times rise with their index
                while ev < len(events) and events[ev][1] <= l.mono:
                    marker(events[ev])
                    ev += 1
                f.write("[%s] %s\n" % (stamp(l.time), l.text))
            for e in events[ev:]:
                marker(e)
        return path
