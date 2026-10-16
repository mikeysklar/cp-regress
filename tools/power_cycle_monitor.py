#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Power-cycle farm boards, attach to the console tty the moment it comes back, and check boot.

After Brent's hil_power_cycle_monitor.py (hil/tests_hil_board_utils/hil_power_cycle_monitor.py
@ cf64e03), for eight native-USB CircuitPython boards instead of one ESP32 behind a CP2102:

    python3 tools/power_cycle_monitor.py --boards metro_rp2040
    python3 tools/power_cycle_monitor.py --boards all --timeout 40
    python3 tools/power_cycle_monitor.py --boards metro_esp32s3 --reset   # also a Ctrl-D boot
    python3 tools/power_cycle_monitor.py --boards metro_m0 --passive      # never type a key

Per board:
  1. take the board's run lock (/tmp/cp-regress-<usb>.lock, the one run.py and pytest take);
     a busy board is reported and left alone
  2. Board.power_cycle(wait=False): flock-serialized, unprivileged uhubctl with the board's own
     -r/-w from farm.toml. A watcher thread timestamps the tty leaving and coming back.
  3. resolve the tty from the board's sysfs USB path again (never a cached ttyACM number) and
     open it as soon as it is readable; every line is timestamped from the first byte. The
     port timeout starts after uhubctl returns. A SAMD board whose tty never comes back gets one
     SWD rescue (openocd init, reset run, exit at its speed) and is recorded as swd_rescue. The
     first write waits WRITE_SETTLE_S after the tty reappears
  4. unless --passive, interrupt to the REPL at once and press Ctrl-B, which prints the banner
     (shared/runtime/pyexec.c friendly_repl_reset); with --passive, type nothing and only
     watch. Either way the first CRASH_WINDOW s from the open are checked for a crash or the
     safe-mode reason; a code.py traceback before any key is a warning
  5. ask the board for time.monotonic() and reset_reason: an uptime longer than the time since
     the cut means VBUS cycling re-enumerated USB without resetting the MCU (nRF52840 excepted:
     its monotonic clock is not an uptime across resets, so devnum and reset_reason decide)
     A BROWNOUT reset_reason or the "Power dipped" safe-mode line is outcome "brownout", not a
     failure; the board is reset out of safe mode with microcontroller.reset()
  6. Ctrl-D, so code.py runs again, as after any boot
It prints per-board timings (port gone, port back, opened, first byte, banner) and writes
results/powermon/<stamp>/<board>.serial.log ("[HH:MM:SS.mmm] text", Brent's dump format) and
summary.json. Exit status 0 when every board passed, 1 otherwise.

What a native-USB console can and cannot show (supervisor/shared/serial.c serial_write_substring:
output goes to CDC only while tud_cdc_connected(), i.e. while a host holds DTR):
  * Not capturable: anything printed before the tty enumerates and we open it. That is the ROM
    and second-stage bootloader output, early port init, and boot.py (which goes to
    boot_out.txt instead). Brent's -reset gets those through the CP2102's RTS->EN line; native
    USB has no such line, so there is no board_reset() here.
  * Capturable: everything from our open on. When code.py has already finished, CircuitPython
    reprints the "Auto-reload" status, the safe-mode reason and "Press any key" when a host
    connects (main.c run_code_py, printed_press_any_key), so a safe-mode boot is still seen.
  * --reset adds a Ctrl-D soft reboot once attached: the whole soft boot from its first line
    ("soft reboot", "Auto-reload is ...", "code.py output:"). It is not a hard boot.

Metro ESP32-S2 (and S3): the console is CircuitPython's own TinyUSB CDC (interfaces 1.0/1.1 at
3-3.3.4.1, alongside MSC and HID). The farm's CP2102 (3-3.4.5) is not wired to its debug UART,
so the ESP32-S2 ROM banner ("ESP-ROM:esp32s2-...", "rst:0x1 (POWERON)"), the IDF bootloader log,
and any panic printed before USB is up (Guru Meditation, abort(), Backtrace:) go to UART0 and
are lost. What remains: CircuitPython's own output after enumeration, the safe-mode reason
(printed on connect, and supervisor.runtime.safe_mode_reason), microcontroller.cpu.reset_reason
(POWER_ON vs BROWNOUT/WATCHDOG/SOFTWARE, queried at step 5), and boot_out.txt. To see the ROM
lines, wire the CP2102 RX to the S2's TX (GPIO43 on UART0) and add its by-path tty to farm.toml.

The banner and status lines are translated (MP_ERROR_TEXT); the patterns here are for English
builds.

SerialMonitor below is a minimal local port of Brent's (hil_board_utils.py:757 @ cf64e03).
lib/serialmon.py has landed but is not used here yet: it opens from a Board, not a port, keeps
pyserial's input flush on open (the first boot bytes after a power cycle), and has no
opened/first_byte/last_line/uptime. monitor_class() keeps the local class until it does.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))
import farmlib  # noqa: E402

SERIAL_BAUD = 115200
# Read timeout: how long the reader thread blocks before checking for close().
SERIAL_READ_TIMEOUT = 0.05
# A run of bytes this long with no newline is split off as its own line.
SERIAL_MAX_LINE = 4096
# ANSI codes, stripped so they cannot break anchors (CircuitPython sends line_clear and the
# status-bar title escape).
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x1b\x07]*(\x07|\x1b\\)")

# MICROPY_FULL_VERSION_INFO, e.g.
# "Adafruit CircuitPython 10.0.3 on 2025-10-17; Adafruit Metro ESP32S2 with ESP32S2"
BANNER_RE = r"^Adafruit CircuitPython (\S+) on (\S+); (.+) with (\S+)$"
# Firmware crashes and safe mode (supervisor/shared/safe_mode.c print_safe_mode_message), plus
# the ESP-IDF panic lines in case a port routes them to the console.
CRASH_RE = (r"You are in safe mode because:|Running in safe mode!|crashed hard|Hard fault"
            r"|NLR jump failed|MicroPython fatal error|Guru Meditation|abort\(\) was called"
            r"|^Backtrace:|task_wdt: Task watchdog")
# A user-code traceback is reported, not failed: the board is fine, code.py is not.
TRACEBACK_RE = r"^Traceback \(most recent call last\):"
# Lines that only a new boot prints (main.c): "soft reboot" from the REPL, "<file>.py output:"
# as code.py starts. "Auto-reload is ..." is not one: it is reprinted when a host connects.
BOOT_RE = r"^soft reboot$|^\S+\.py output:$"
# Seconds from the open that a fresh boot is watched for a crash (Brent: CRASH_WINDOW).
CRASH_WINDOW = 5
# Seconds after a tty reappears before the first byte is written to it. A CDC port written to
# sooner can block the write (farm rule: 10 to 12 s; the ESP32-S3 banner write timed out at less).
WRITE_SETTLE_S = 12
# How long the tty may take to come back after an SWD rescue (flash.py: wait_ready(timeout=45)).
RESCUE_PORT_S = 45
# Safe mode after a power dip (supervisor/shared/safe_mode.c, SAFE_MODE_BROWNOUT): the message
# printed after "You are in safe mode because:". A BROWNOUT reset_reason means the same.
BROWNOUT_RE = r"^Power dipped\. Make sure you are providing enough power\.$"

serial_log = logging.getLogger("cpr.serial")


class PortError(farmlib.FarmError):
    """The board's tty did not come back (Brent: PortError)."""


class SerialError(farmlib.FarmError):
    """The monitor cannot read: not opened, port lost, or closed (Brent: SerialError)."""


class SerialTimeout(SerialError, AssertionError):
    pass


class SerialUnexpectedError(SerialError, AssertionError):
    pass


class SerialLine:
    """One complete console line (Brent: SerialLine dataclass)."""

    __slots__ = ("index", "time", "text")

    def __init__(self, index, t, text):
        self.index = index
        self.time = t      # time.time() when the line's first byte arrived
        self.text = text   # decoded, without the line ending or ANSI codes

    def __repr__(self):
        return "SerialLine(%d, %r)" % (self.index, self.text)


# ---------- port and power (Board resolves everything from its sysfs USB path) ----------

def wait_for_port(board, timeout=15, interval=0.05):
    """Poll until the board's console tty exists and is readable and writable; return its path.

    After Brent's wait_for_port, hil_board_utils.py:716 @ cf64e03: the tty appears before udev
    has given it its group, so wait for access too. The path is looked up again on every poll.
    """
    deadline = time.monotonic() + timeout
    while True:
        tty = board.tty()
        if tty and os.access(tty, os.R_OK | os.W_OK):
            return tty
        if time.monotonic() >= deadline:
            if tty:
                raise PortError("%s: no permission to open %s" % (board.name, tty))
            raise PortError("%s: no tty at %s after %.3g s" % (board.name, board.usb, timeout))
        time.sleep(interval)


def swd_rescue_speed(board):
    """kHz for the SWD rescue of a board whose tty did not come back after a VBUS cut, or None.

    Only SWD boards qualify: the SAMD family (Metro M4 at its speed 500, Metro M0 at its
    rescue_speed 400), where a VBUS cut does not re-enumerate the MCU and the farm's skill says
    to attach over SWD and reset it. RP2040, nRF52840, STM32 and ESP32 boards get None.
    """
    cfg = board.flash
    if cfg.get("method") != "swd":
        return None
    if cfg.get("rescue_speed"):
        return int(cfg["rescue_speed"])
    if board.port == "atmel-samd" and cfg.get("speed"):
        return int(cfg["speed"])
    return None


def swd_rescue(board, speed, log=None):
    """openocd init, reset run, exit at speed kHz on the board's own probe (flash.py _openocd,
    the same command line as flash_swd). Returns True when openocd exited 0, False when it
    exited non-zero, hung past its 60 s timeout, or could not be started (no openocd). Never
    raises: the caller then waits for the tty, and a missing tty raises PortError (a FarmError).
    Used here, not flash.rescue_swd, which only runs for boards that set rescue_speed (the M4
    does not)."""
    import flash  # pylint: disable=import-outside-toplevel  (lib/flash.py, the farm's flasher)
    log = log or (lambda m: print(m, file=sys.stderr))
    log("%s: no tty after the power cycle; SWD rescue at %d kHz" % (board.name, speed))
    try:
        # pylint: disable=protected-access
        r = flash._openocd(board, speed, ["init", "reset run", "exit"], log, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        log("%s: SWD rescue failed: %s" % (board.name, e))
        return False
    return r.returncode == 0


def wait_for_port_or_rescue(board, timeout, rescue=True):
    """wait_for_port; if the tty never came back and the board is an SWD rescue board, one SWD
    rescue, then wait again. Returns (tty, rescued). Callers start the timeout only after the
    uhubctl cut has returned, so uhubctl's flock wait is never charged to the port."""
    try:
        return wait_for_port(board, timeout=timeout), False
    except PortError:
        speed = swd_rescue_speed(board) if rescue else None
        if speed is None or board.tty() is not None:  # a permission error is not a lost tty
            raise
    swd_rescue(board, speed)
    return wait_for_port(board, timeout=RESCUE_PORT_S), True


def settle_remaining(ready_at, now):
    """Seconds still to wait before the first write to a tty that appeared at ready_at."""
    return max(0.0, ready_at + WRITE_SETTLE_S - now)


def settle_before_write(ready_at):
    """Sleep out WRITE_SETTLE_S from the moment the tty appeared (ready_at, time.time())."""
    time.sleep(settle_remaining(ready_at, time.time()))


def reset_name(text):
    """'ResetReason.BROWNOUT' -> 'BROWNOUT'; None stays None."""
    return text.rsplit(".", 1)[-1] if text else None


def reboot_verdict(board, up, reason, since_cut, devnum_before, devnum_after):
    """(rebooted, why): did the cut reset the MCU? up is the board's time.monotonic() and reason
    its microcontroller.cpu.reset_reason text, both from uptime().

    SAMD, RP2 and the rest: monotonic uptime longer than the time since the cut means USB was
    re-enumerated without an MCU reset (the original check).
    nRF52840 (port nordic): time.monotonic() is not an uptime across resets, so it is not used.
    The evidence is the re-enumeration (devnum changed) and a reset_reason that is not SOFTWARE.
    """
    name = reset_name(reason)
    if board.port == "nordic":
        if devnum_before == devnum_after:
            return False, "devnum unchanged: USB never re-enumerated"
        if name is None:
            return False, "no reset_reason to judge the reset by"
        if name == "SOFTWARE":
            return False, "reset_reason SOFTWARE: a software reset, not the power cut"
        return True, "devnum changed and reset_reason %s" % name
    if up is None:
        return False, "no uptime from the board"
    if up <= since_cut + 1.0:
        return True, "uptime %.1f s, cut %.1f s ago" % (up, since_cut)
    return False, "MCU not reset: uptime %.1f s, cut %.1f s ago" % (up, since_cut)


class _Watch(threading.Thread):
    """Timestamps the tty leaving and coming back while uhubctl runs."""

    def __init__(self, board, interval=0.02):
        super().__init__(daemon=True, name="watch-%s" % board.name)
        self.board = board
        self.interval = interval
        self.gone = None
        self.back = None
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            tty = self.board.tty()
            now = time.time()
            if tty is None and self.gone is None:
                self.gone = now
            elif tty is not None and self.gone is not None and self.back is None:
                self.back = now
                return
            time.sleep(self.interval)


def power_cycle_board(board, delay=3):
    """Cut and restore the board's hub port; return {t0, uhubctl_s, devnum_before, watch}.

    After Brent's power_cycle_board, hil_board_utils.py:708 @ cf64e03, but through
    farmlib Board.power_cycle: the farm's flock on /tmp/uhubctl.lock, unprivileged uhubctl, and
    the board's own -r/-w. watch.gone / watch.back are time.time() stamps of the tty leaving
    and returning, or None if the watcher missed them (a cycle faster than its poll;
    devnum_before still shows a re-enumeration). The caller sets watch.stop when done.
    """
    devnum = board.devnum()
    watch = _Watch(board)
    t0 = time.time()
    watch.start()
    try:
        board.power_cycle(off_s=delay, wait=False)
    except BaseException:
        watch.stop.set()
        raise
    t1 = time.time()
    # the watcher keeps going until the port is back, or wait_for_port gives up
    return {"t0": t0, "uhubctl_s": t1 - t0, "devnum_before": devnum, "watch": watch}


# ---------- serial monitor ----------

def monitor_class():
    """lib/serialmon.SerialMonitor once it fits this tool, else the local one below.

    SWAP-LATER: serialmon.SerialMonitor(board, iface, baud) does not take (port, baud) and has
    no uptime/opened/first_byte/last_line, so it is used only once it says it does
    (PORT_ARGS = True plus those names). Until then the local class is returned.
    """
    try:
        import serialmon  # pylint: disable=import-outside-toplevel
        cls = getattr(serialmon, "SerialMonitor", None)
        need = ("expect", "assert_none", "mark", "dump", "soft_reset", "prompt", "uptime",
                "write")
        if cls and getattr(cls, "PORT_ARGS", False) and all(hasattr(cls, a) for a in need):
            return cls
    except ImportError:
        pass
    return SerialMonitor


class SerialMonitor:
    """A console tty captured as timestamped lines on a background thread.

    Minimal local port of Brent's SerialMonitor (hil_board_utils.py:757 @ cf64e03) with the
    same matching rules: str regexes searched one complete line at a time, a cursor that only
    moves forward, expect() raising SerialTimeout and assert_none() SerialUnexpectedError
    (both AssertionErrors), SerialError when the port drops. Differences for native USB:
      * no board_reset(): there is no RTS->EN line. soft_reset() (Ctrl-D at a synced prompt)
        is the "new boot under observation" this farm has; port_reset() power-cycles.
      * first_byte: time.time() of the first byte read, for the boot timings.
      * write()/prompt()/uptime() so a test can type without opening a second handle.
    SWAP-LATER for lib/serialmon.py.
    """

    def __init__(self, port, baud=SERIAL_BAUD):
        if farmlib.serial is None:
            raise SerialError("pyserial is required (pip install pyserial)")
        self.port = str(port)
        self._lines = []
        self._partial = bytearray()
        self._partial_time = 0.0
        self._cursor = 0
        self._boot = 0
        self._error = None
        self._closed = False
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._baud = baud
        self.opened = None
        self.first_byte = None
        self.last_line = None  # the SerialLine the last expect() matched
        self._open()

    def _open(self):
        ser = farmlib.serial.Serial()
        ser.port = self.port
        ser.baudrate = self._baud
        ser.timeout = SERIAL_READ_TIMEOUT
        ser.write_timeout = 5  # farm rule: every serial port gets write_timeout=5
        ser.exclusive = True   # a second opener fails cleanly instead of sharing reads
        # DTR up is what tud_cdc_connected() tests: CircuitPython only writes to a host that
        # holds it. Neither line resets a native-USB board (only a 1200-baud open does).
        ser.dtr = True
        ser.rts = True
        # pyserial 3.5's posix open() ends with _reset_input_buffer() (tcflush), after the
        # kernel has already raised DTR, so the board's first bytes could be flushed. Skip that
        # one call: the tty is new, nothing stale can be in it.
        ser._reset_input_buffer = lambda: None  # pylint: disable=protected-access
        try:
            ser.open()
        except (OSError, ValueError) as e:
            raise SerialError("cannot open %s: %s" % (self.port, e)) from e
        finally:
            del ser._reset_input_buffer
        self._serial = ser
        self.opened = time.time()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="SerialMonitor(%s)" % self.port)
        self._reader.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- capture --

    def _read_loop(self):
        ser = self._serial
        while not self._stop.is_set():
            try:
                data = ser.read(ser.in_waiting or 1)
            except (OSError, TypeError) as e:  # SerialException is an OSError
                with self._cond:
                    self._error = e
                    self._cond.notify_all()
                return
            if not data:
                continue
            with self._cond:
                if self.first_byte is None:
                    self.first_byte = time.time()
                self._feed(data, time.time())
                self._cond.notify_all()

    def _feed(self, data, now):
        if not self._partial:
            self._partial_time = now
        self._partial += data
        i = self._partial.find(b"\n")
        while i >= 0:
            self._emit(self._partial[:i], self._partial_time)
            del self._partial[:i + 1]
            self._partial_time = now
            i = self._partial.find(b"\n")
        if len(self._partial) >= SERIAL_MAX_LINE:
            self._emit(self._partial, self._partial_time)
            self._partial.clear()

    def _emit(self, raw, t):
        text = ANSI_ESCAPE_RE.sub("", raw.decode(errors="replace")).rstrip("\r")
        self._lines.append(SerialLine(len(self._lines), t, text))
        serial_log.info("%s | %s", self.port, text)

    def close(self):
        """Stop capturing and release the port. A trailing partial line is kept."""
        if self._closed:
            return
        self._stop.set()
        self._reader.join()
        try:
            self._serial.close()
        except (OSError, ValueError):
            pass
        with self._cond:
            self._closed = True
            if self._partial:
                self._emit(self._partial, self._partial_time)
                self._partial.clear()
            self._cond.notify_all()

    # -- board control --

    def write(self, data):
        if self._closed:
            raise SerialError("%s: monitor is closed" % self.port)
        if isinstance(data, str):
            data = data.encode()
        try:
            self._serial.write(data)
            self._serial.flush()
        except (OSError, ValueError) as e:
            raise SerialError("%s: write failed: %s" % (self.port, e)) from e

    def _partial_text(self):
        with self._cond:
            return ANSI_ESCAPE_RE.sub("", self._partial.decode(errors="replace"))

    def prompt(self, timeout=20):
        """Interrupt whatever runs and wait for a friendly ">>> " (it never ends in a newline,
        so it is looked for in the partial line). Like Repl.prompt, keys go out in threes: the
        first keystroke at "Press any key" is eaten."""
        if self._closed:
            raise SerialError("%s: monitor is closed" % self.port)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.write(b"\r\x03\x03")
            poke_end = min(end, time.monotonic() + 1.5)
            while time.monotonic() < poke_end:
                if self._partial_text().endswith(">>> "):
                    return
                if self._error:
                    raise self._port_error()
                time.sleep(0.05)
        raise SerialTimeout("%s: no >>> prompt within %.3g s:\n%s"
                            % (self.port, timeout, self._tail(max(0, self.mark() - 20))))

    def soft_reset(self, timeout=20):
        """Ctrl-D at a synced prompt; return the boot start (the "soft reboot" line's index).
        The cursor moves there, so the next expect() only sees this boot. The farm's stand-in
        for Brent's board_reset() (hil_board_utils.py:900 @ cf64e03)."""
        self.prompt(timeout=timeout)
        start = self.mark()
        self.write(b"\x04")
        with self._cond:
            self._cursor = start
        self.expect(r"^soft reboot$", timeout=timeout)
        with self._cond:
            self._boot = self._cursor - 1
            return self._boot

    def port_reset(self, board, delay=3, timeout=60):
        """Power-cycle the board, wait for its tty, reopen; return the boot start.

        After Brent's port_reset, hil_board_utils.py:934 @ cf64e03. The tty may come back under
        another name; self.port is updated. The first lines of the boot are lost (see the module
        docstring). Raises FarmError, PortError, or SerialError.
        """
        if self._closed:
            raise SerialError("%s: monitor is closed" % self.port)
        self._stop.set()
        self._reader.join()
        self._serial.close()
        with self._cond:
            if self._partial:
                self._emit(self._partial, self._partial_time)
                self._partial.clear()
        try:
            cyc = power_cycle_board(board, delay)
            port, _ = wait_for_port_or_rescue(board, timeout)
            ready = time.time()
            cyc["watch"].stop.set()
            with self._cond:
                self.port = str(port)
                self._error = None
                self._boot = self._cursor = len(self._lines)
                self.first_byte = None
            self._open()
            settle_before_write(ready)  # the caller's first write comes after the settle
        except farmlib.FarmError as e:
            with self._cond:
                self._error = e
                self._cond.notify_all()
            raise
        return self._boot

    def uptime(self, timeout=10):
        """(time.monotonic(), reset_reason) from the board, typed at a synced prompt."""
        self.prompt()
        self.write("import time,microcontroller as m;"
                   "print('@@UP',time.monotonic(),getattr(m.cpu,'reset_reason',None))\r")
        m = self.expect(r"^@@UP ([0-9.]+) (\S+)", timeout=timeout)
        return float(m.group(1)), m.group(2)

    # -- matching --

    def mark(self):
        """The index the next complete line will get."""
        with self._cond:
            return len(self._lines)

    @property
    def boot(self):
        """Index of the first line of the last soft_reset()/port_reset() boot (0 if none)."""
        return self._boot

    @staticmethod
    def _compile(pattern):
        return pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)

    def _search(self, regex, start):
        for line in self._lines[start:]:
            m = regex.search(line.text)
            if m:
                return line, m
        return None, None

    def _tail(self, start, count=20):
        lines = self._lines[start:]
        shown = ["  [%d] %s" % (ln.index, ln.text) for ln in lines[-count:]]
        if len(lines) > count:
            shown.insert(0, "  ... %d earlier lines" % (len(lines) - count))
        return "\n".join(shown) if shown else "  (no lines)"

    def _port_error(self):
        return SerialError("%s: serial port lost: %s" % (self.port, self._error))

    def expect(self, pattern, timeout=10):
        """Wait for the first line at or after the cursor matching pattern, move the cursor past
        it and return the re.Match. Raises SerialTimeout, or SerialError if the port drops."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        regex = self._compile(pattern)
        deadline = time.monotonic() + timeout
        with self._cond:
            start = pos = self._cursor
            while True:
                line, m = self._search(regex, pos)
                if m:
                    self._cursor = line.index + 1
                    self.last_line = line
                    return m
                pos = len(self._lines)
                if self._error:
                    raise self._port_error()
                remaining = deadline - time.monotonic()
                if self._closed or remaining <= 0:
                    why = "monitor is closed" if self._closed else "within %.3g s" % timeout
                    raise SerialTimeout("%s: no line matched %r (%s); lines from %d:\n%s"
                                        % (self.port, regex.pattern, why, start,
                                           self._tail(start)))
                self._cond.wait(remaining)

    def assert_none(self, pattern, since=None, duration=0):
        """Fail if a line from since (default: the boot start) matches, watching new lines for
        duration more seconds. Does not move the cursor."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        regex = self._compile(pattern)
        deadline = time.monotonic() + duration
        with self._cond:
            pos = self._boot if since is None else since
            while True:
                line, m = self._search(regex, pos)
                if m:
                    raise SerialUnexpectedError(
                        "%s: line %d matched %r: %r"
                        % (self.port, line.index, regex.pattern, line.text))
                pos = max(pos, len(self._lines))
                if self._error:
                    raise self._port_error()
                remaining = deadline - time.monotonic()
                if self._closed or remaining <= 0:
                    return
                self._cond.wait(remaining)

    # -- output --

    def lines(self, since=0):
        with self._cond:
            return self._lines[since:]

    @property
    def output(self):
        with self._cond:
            return "".join("%s\n" % ln.text for ln in self._lines)

    def dump(self, path):
        """Write every line, prefixed with the time its first byte arrived. Returns the path."""
        with self._cond:
            lines = list(self._lines)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for ln in lines:
                stamp = datetime.fromtimestamp(ln.time).strftime("%H:%M:%S.%f")[:-3]
                f.write("[%s] %s\n" % (stamp, ln.text))
        return path


# ---------- the check ----------

def expect_banner(mon, timeout=20, passive=False):
    """Expect the CircuitPython banner; return its match. Unless passive, interrupt to the
    REPL and press Ctrl-B, which reprints it (pyexec.c friendly_repl_reset). Brent's
    check_banner (hil_power_cycle_monitor.py:59) checks a fixed list; ours is one regex."""
    if passive:
        return mon.expect(BANNER_RE, timeout=timeout)
    deadline = time.monotonic() + timeout
    mon.prompt(timeout=timeout)
    mon.write(b"\x02")
    return mon.expect(BANNER_RE, timeout=max(1, deadline - time.monotonic()))


class BootReport:
    """What one power-cycled boot looked like. Times are seconds after the uhubctl call."""

    FIELDS = ("board", "ok", "problems", "warnings", "uhubctl_s", "gone_s", "back_s", "open_s",
              "first_byte_s", "banner_s", "version", "uptime_s", "reset_reason", "rebooted",
              "devnum", "lines", "log", "swd_rescue", "brownout", "recovered")

    def __init__(self, board):
        for f in self.FIELDS:
            setattr(self, f, None)
        self.board = board.name
        self.problems = []
        self.warnings = []
        self.swd_rescue = False  # the tty needed the SWD rescue (SAMD); a note, not a failure
        self.brownout = False    # came back in safe mode, "Power dipped": its own outcome

    @property
    def outcome(self):
        """brownout (safe mode after a power dip, not a harness failure), pass, or fail."""
        if self.brownout:
            return "brownout"
        return "pass" if self.ok else "fail"

    def as_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d["outcome"] = self.outcome
        return d


def _rel(t, t0):
    return None if t is None else round(t - t0, 3)


def _browned_out(mon):
    """A line of the power-dip safe-mode message has been captured."""
    return any(re.search(BROWNOUT_RE, ln.text) for ln in mon.lines())


def _reset_out_of_safe_mode(board, mon, timeout):
    """After a power dip the board sits in safe mode. microcontroller.reset() leaves it (the
    farm's skill recovery table). Returns True when the tty came back after the reset."""
    mon.prompt()
    mon.write("import microcontroller as m;m.reset()\r")
    mon.close()  # the reset drops the port under the monitor; nothing more is read from it
    try:
        wait_for_port(board, timeout=timeout)
        return True
    except farmlib.FarmError:
        return False


def capture_power_boot(board, delay=3, port_timeout=None, timeout=20, passive=False,
                       reset=False, log_path=None):
    """Power-cycle board and check its boot. Returns (BootReport, monitor or None).

    Takes no lock: the CLI holds the board's run lock, and under pytest the board fixture does
    (flock is per open file, so taking it again here would deadlock). The monitor is closed.
    port_timeout is a duration for the tty to return. It starts when the cut has returned, so
    uhubctl's own flock wait (in rep.uhubctl_s) is never charged to the port.
    """
    rep = BootReport(board)
    port_timeout = port_timeout or 60 + board.power.get("settle", 12)
    mon = None
    try:
        cyc = power_cycle_board(board, delay)
    except farmlib.FarmError as e:
        rep.problems.append("power cycle: %s" % e)
        rep.ok = False
        return rep, None
    t0 = cyc["t0"]
    devnum_before = cyc["devnum_before"]
    rep.uhubctl_s = round(cyc["uhubctl_s"], 3)
    found = None
    try:
        # The clock for the tty starts here, after uhubctl returned. An SWD rescue (SAMD only)
        # runs inside this call if the tty never came back.
        tty, rep.swd_rescue = wait_for_port_or_rescue(board, timeout=port_timeout)
        found = time.time()
        if rep.swd_rescue:
            rep.warnings.append("no tty after the cut: SWD rescue brought it back")
        # Open as soon as it is readable; permission can lag the link by a few ms.
        end = time.monotonic() + 5
        while True:
            try:
                mon = monitor_class()(tty, SERIAL_BAUD)
                break
            except SerialError:
                if time.monotonic() >= end:
                    raise
                time.sleep(0.02)
                tty = wait_for_port(board, timeout=5)
    except farmlib.FarmError as e:
        rep.problems.append(str(e))
    finally:
        cyc["watch"].stop.set()
    watch = cyc["watch"]
    rep.gone_s = _rel(watch.gone, t0)
    # the watcher polls faster, but this thread may still see the tty first
    rep.back_s = _rel(watch.back or found, t0)
    rep.devnum = "%s -> %s" % (devnum_before, board.devnum())
    if watch.gone is None and board.devnum() == devnum_before:
        rep.problems.append("the tty never left: the port was not power-cycled")
    if mon is None:
        rep.ok = False
        return rep, None

    rep.open_s = _rel(getattr(mon, "opened", None), t0)
    try:
        if passive:
            # Nothing typed: watch the whole window for a crash. The banner prints only on
            # entering the REPL, so with code.py still running it is not expected here.
            try:
                mon.assert_none(CRASH_RE, since=0, duration=timeout)
            except SerialUnexpectedError as e:
                if _browned_out(mon):
                    rep.brownout = True
                else:
                    rep.problems.append("crash: %s" % e)
            if any(re.search(BANNER_RE, ln.text) for ln in mon.lines()):
                m = mon.expect(BANNER_RE, timeout=0)
            else:
                m = None
                rep.warnings.append("no banner without a key (normal while code.py runs)")
        else:
            # A CDC port written to too soon can block the write: settle first.
            settle_before_write(found)
            typed = mon.mark()  # everything before this arrived with no key pressed
            m = expect_banner(mon, timeout=timeout)
            if any(re.search(TRACEBACK_RE, ln.text) for ln in mon.lines()[:typed]):
                rep.warnings.append("code.py raised before any key was pressed")
        if m:
            rep.version = m.group(1)
            line = getattr(mon, "last_line", None)
            rep.banner_s = _rel(line.time if line else time.time(), t0)
        if not passive:
            up, why = mon.uptime()
            rep.uptime_s, rep.reset_reason = round(up, 3), why
            since_cut = time.time() - t0
            rep.rebooted, verdict = reboot_verdict(board, up, why, since_cut, devnum_before,
                                                   board.devnum())
            # A brownout is its own outcome: the safe-mode crash lines are not a crash.
            rep.brownout = reset_name(why) == "BROWNOUT" or _browned_out(mon)
            if rep.brownout:
                rep.rebooted = True
                rep.warnings.append("back in safe mode after the cut: power dipped (BROWNOUT)")
            elif not rep.rebooted:
                # farm.toml power.resets = false marks a board that VBUS cycling is known not to
                # reset (it has another supply); there it is a note, not a failure.
                (rep.warnings if board.power.get("resets", True) is False else rep.problems) \
                    .append("MCU not reset: %s" % verdict)
            if rep.brownout:
                rep.recovered = _reset_out_of_safe_mode(board, mon, port_timeout)
            else:
                # The banner is fetched at once so banner_s is a boot time, not our wait; the
                # crash window (from the open) is watched after it, over everything captured.
                opened = getattr(mon, "opened", None) or time.time()
                try:
                    mon.assert_none(CRASH_RE, since=0,
                                    duration=max(0, CRASH_WINDOW - (time.time() - opened)))
                except SerialUnexpectedError as e:
                    rep.problems.append("crash: %s" % e)
                if reset:
                    boot = mon.soft_reset()
                    mon.expect(r"^\S+\.py output:$|^Auto-reload is", timeout=timeout)
                    try:
                        mon.assert_none(CRASH_RE, since=boot, duration=CRASH_WINDOW)
                    except SerialUnexpectedError as e:
                        rep.problems.append("crash after Ctrl-D: %s" % e)
                else:
                    mon.prompt()
                    mon.write(b"\x04")  # run code.py again, as after any boot
                    time.sleep(0.5)
    except farmlib.FarmError as e:
        rep.problems.append(str(e).splitlines()[0][:300])
    finally:
        mon.close()
    rep.first_byte_s = _rel(getattr(mon, "first_byte", None), t0)
    rep.lines = mon.mark()
    if log_path:
        rep.log = str(mon.dump(log_path))
    rep.ok = not rep.problems
    return rep, mon


# ---------- CLI ----------

def load_boards(config, names):
    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore  # pylint: disable=import-outside-toplevel
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    boards = [farmlib.Board(b, cfg.get("host", {})) for b in cfg.get("board", [])]
    if names == "all":
        return boards
    by = {b.name: b for b in boards}
    unknown = [n for n in names.split(",") if n and n not in by]
    if unknown:
        raise SystemExit("unknown board(s) %s; farm.toml has %s"
                         % (",".join(unknown), ",".join(by)))
    return [by[n] for n in names.split(",") if n]


def _fmt(v):
    return "-" if v is None else ("%.2f" % v if isinstance(v, float) else str(v))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "farm.toml"))
    ap.add_argument("--boards", default="all", help="comma-separated farm.toml names, or all")
    ap.add_argument("--timeout", type=float, default=20,
                    help="seconds after attaching for the banner (default 20)")
    ap.add_argument("--delay", type=float, default=3, help="seconds off (default 3)")
    ap.add_argument("--port-timeout", type=float, default=None,
                    help="seconds to wait for the tty (default 60 + the board's settle)")
    ap.add_argument("--reset", action="store_true",
                    help="also Ctrl-D once attached, to capture a whole soft boot")
    ap.add_argument("--passive", action="store_true",
                    help="never type: only what the board prints on its own after the open")
    ap.add_argument("-o", "--out", default="", help="log folder (default results/powermon/<time>)")
    a = ap.parse_args(argv)
    if farmlib.serial is None:
        print("pyserial missing: pip install --user pyserial", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    boards = load_boards(a.config, a.boards)
    out = Path(a.out or os.path.join(HERE, "results", "powermon", time.strftime("%Y%m%d-%H%M%S")))
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    # One board at a time: cycles are serialized on /tmp/uhubctl.lock anyway, and a cycle that
    # waits for the lock would add the wait to its timings.
    for b in boards:
        if not b.present():
            rep = BootReport(b)
            rep.ok, rep.problems = False, ["not on the bus at %s" % b.usb]
            reports.append(rep)
            continue
        lock_path = "/tmp/cp-regress-%s.lock" % b.usb
        with open(lock_path, "w") as lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                rep = BootReport(b)
                rep.ok, rep.problems = False, ["busy: %s is held by another run" % lock_path]
                reports.append(rep)
                continue
            try:
                print("%s: cycling %s (%.3g s off)" % (b.name, b.usb, a.delay), file=sys.stderr)
                rep, _ = capture_power_boot(b, delay=a.delay, port_timeout=a.port_timeout,
                                            timeout=a.timeout, passive=a.passive, reset=a.reset,
                                            log_path=out / ("%s.serial.log" % b.name))
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
        reports.append(rep)

    cols = ("board", "outcome", "swd_rescue", "uhubctl_s", "gone_s", "back_s", "open_s",
            "first_byte_s", "banner_s", "uptime_s", "reset_reason", "version")
    rows = [[_fmt(getattr(r, c)) for c in cols] for r in reports]
    widths = [max(len(c), *(len(row[i]) for row in rows)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for row in rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    for r in reports:
        for p in r.problems:
            print("FAIL %s: %s" % (r.board, p))
        for w in r.warnings:
            print("warn %s: %s" % (r.board, w))
    with open(out / "summary.json", "w") as f:
        json.dump([r.as_dict() for r in reports], f, indent=1)
    print("logs: %s" % out)
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
