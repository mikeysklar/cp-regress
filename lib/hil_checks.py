# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Console checks shared by the hardware tests: crash and reset signatures, banner, identity.

After Brent's hil_checks.py (hil/tests_hil_board_utils/hil_checks.py @ cf64e03). His names are
kept so a test reads the same in both repos:

    mon.expect(ROM_RESET_RE, timeout=5)        # ESP only, and only on a debug UART
    expect_banner(mon)
    expect_identity(mon, board)
    mon.assert_none(CRASH_RE, duration=CRASH_WINDOW)

He watches one ESP32 over a USB-UART bridge, where the ROM and ESP-IDF panic handler print to
the same port as the firmware. CircuitPython on our farm talks over native USB CDC, so what a
crash looks like depends on the port. The table below (SIGNATURES) lists each console line per
port family with the source line that prints it. Sources are CircuitPython 29e9b60846
(bravo:~/wt-tusb-repin) and its ESP-IDF 6.1.0 submodule (ports/espressif/esp-idf).

How a crash reaches the USB console:

* Every port: a fault calls reset_into_safe_mode(), the chip resets (USB drops, so the monitor
  sees the port vanish), and on the next boot the reason is printed when the host is connected,
  just before "Press any key" (main.c:668-669 -> safe_mode.c:153-239). With a monitor reopened
  after the drop, CRASH_RE then matches.
* espressif release builds: ESP-IDF's own panic output is off (sdkconfig-opt.defaults:30-31,
  PANIC_SILENT_REBOOT and CONSOLE_NONE). A panic reboots silently and comes back as
  "Hard fault" safe mode (supervisor/port.c:311-312, ESP_RST_PANIC). "Guru Meditation",
  "abort() was called", "Backtrace:" and ROM "rst:0x" lines only appear on UART0 of a DEBUG=1
  build (sdkconfig-debug.defaults:31) or from the ROM, so they need a debug-UART monitor.
* A Python exception is not a crash: the traceback prints and the VM carries on. Tests that
  want those treated as failures pass python_errors=True.

The serial monitor interface (package A's lib/serialmon.py, after Brent's SerialMonitor,
hil_board_utils.py:757 @ cf64e03): mon.expect(pattern, timeout), mon.mark(),
mon.assert_none(pattern, since=None, duration=0). The `serial` fixture (fixtures/console.py)
now hands out that monitor. The older byte-stream monitor (expect only, mon.repl) is still
accepted; see assert_no_crash().
"""

import collections
import re
import time
from dataclasses import dataclass

from cpstrings import to_english

# --- Brent's constants, his names and values (hil_checks.py:7-15 @ cf64e03) ---------------------

# ESP32 ROM line after an EN-pin reset, booting from flash (not download mode). ESP32 classic
# prints "POWERON_RESET"; see ESP_ROM_POWERON_RE for the S2/S3 form.
ROM_RESET_RE = r"^rst:0x1 \(POWERON_RESET\),boot:0x[0-9a-f]+ \(SPI_FAST_FLASH_BOOT\)$"
ROM_ANY_RESET_RE = r"^rst:0x"

# How long a fresh boot is watched for crashes. His is 5 s, kept: a crash that takes longer
# than that is a soak test's job (memory_soak), not a boot check's.
CRASH_WINDOW = 5

# --- Signature table ------------------------------------------------------------------------

CP = "circuitpython@29e9b60846 "
IDF = CP + "ports/espressif/esp-idf/components/"

CONSOLE = "console"      # the USB CDC REPL tty every farm board has
DEBUG_UART = "debug_uart"  # ESP UART0 / ROM output; needs a USB-UART on the debug pins


@dataclass(frozen=True)
class Signature:
    """One console line that means something went wrong."""
    name: str
    pattern: str   # regex, searched one line at a time
    kind: str      # "crash", "safe_mode", "python", "reset"
    where: str     # CONSOLE or DEBUG_UART
    source: str    # file:line that prints it


def _sm(name, text, line, kind="safe_mode"):
    """A safe_mode.c message, printed on the console on the boot after the fault."""
    return Signature(name, re.escape(text), kind, CONSOLE,
                     CP + "supervisor/shared/safe_mode.c:%d" % line)


# Every port prints these: they come from shared code.
COMMON = (
    _sm("safe_mode", "You are in safe mode because:", 153),
    Signature("running_safe_mode", re.escape("Running in safe mode! Not running saved code."),
              "safe_mode", CONSOLE, CP + "main.c:465"),
    _sm("core_crash", "CircuitPython core code crashed hard. Whoops!", 207, "crash"),
    _sm("hard_fault", "Hard fault: memory access or instruction error.", 216, "crash"),
    _sm("gc_outside_vm", "Heap allocation when VM not running.", 210, "crash"),
    _sm("flash_write_fail", "Failed to write internal flash.", 213, "crash"),
    _sm("interrupt_error", "Interrupt error.", 219, "crash"),
    _sm("nlr_jump_fail", "NLR jump failed. Likely memory corruption.", 222, "crash"),
    _sm("no_heap", "Unable to allocate to the heap.", 225, "crash"),
    _sm("sdk_fatal", "Third-party firmware fatal error.", 228, "crash"),
    _sm("brownout", "Power dipped. Make sure you are providing enough power.", 161),
    _sm("no_circuitpy", "CIRCUITPY drive could not be found or created.", 173),
    _sm("safemode_py", "Error in safemode.py.", 180),
    _sm("stack_overflow", "Stack overflow. Increase stack size.", 184),
    _sm("usb_endpoints", "USB devices need more endpoints than are available.", 187),
    _sm("usb_iface_names", "USB devices specify too many interface names.", 190),
    _sm("usb_boot_iface", "Boot device must be first (interface #0).", 193),
    _sm("watchdog", "Internal watchdog timer expired.", 196),
)

_C = {s.name: s.pattern for s in COMMON}

# Python-level errors. Not crashes: only checked with python_errors=True.
PYTHON = (
    Signature("traceback", re.escape("Traceback (most recent call last):"), "python", CONSOLE,
              CP + "py/obj.c:241"),
    Signature("memory_error", r"MemoryError", "python", CONSOLE,
              CP + "py/runtime.c:1790,1794"),
)

# Per port family (farm.toml `port`). For the non-ESP ports these name which shared message
# the port's own fault paths lead to; the console text is the COMMON entry's.
SIGNATURES = {
    "atmel-samd": (
        Signature("hard_fault", _C["hard_fault"], "crash", CONSOLE,
                  CP + "ports/atmel-samd/supervisor/port.c:695 (HardFault -> safe_mode.c:216)"),
        Signature("brownout", _C["brownout"], "safe_mode", CONSOLE,
                  CP + "ports/atmel-samd/supervisor/port.c:347 SAMD21, :352 SAMD51"),
    ),
    "nordic": (
        Signature("hard_fault", _C["hard_fault"], "crash", CONSOLE,
                  CP + "ports/nordic/supervisor/port.c:380-381"),
        Signature("brownout", _C["brownout"], "safe_mode", CONSOLE,
                  CP + "ports/nordic/supervisor/port.c:67"),
        Signature("watchdog", _C["watchdog"], "safe_mode", CONSOLE,
                  CP + "ports/nordic/supervisor/port.c:197 (only with VBUS present)"),
        Signature("sdk_fatal", _C["sdk_fatal"], "crash", CONSOLE,
                  CP + "ports/nordic/common-hal/_bleio/Adapter.c:69 (SoftDevice assert)"),
    ),
    "raspberrypi": (
        Signature("hard_fault", _C["hard_fault"], "crash", CONSOLE,
                  CP + "ports/raspberrypi/supervisor/port.c:698 (core 0 only; a core 1 "
                  "fault hangs core 1 and prints nothing)"),
    ),
    "stm": (
        Signature("hard_fault", _C["hard_fault"], "crash", CONSOLE,
                  CP + "ports/stm/supervisor/port.c:454,461,468,475 "
                  "(MemManage, BusFault, UsageFault, HardFault)"),
    ),
    "espressif": (
        Signature("hard_fault", _C["hard_fault"], "crash", CONSOLE,
                  CP + "ports/espressif/supervisor/port.c:311-312 (ESP_RST_PANIC)"),
        Signature("watchdog", _C["watchdog"], "safe_mode", CONSOLE,
                  CP + "ports/espressif/supervisor/port.c:313-316 (ESP_RST_INT_WDT)"),
        Signature("brownout", _C["brownout"], "safe_mode", CONSOLE,
                  CP + "ports/espressif/supervisor/port.c:309-310"),
        # Brent's CRASH_RE, split up (hil_checks.py:10 @ cf64e03). DEBUG=1 builds only.
        Signature("guru_meditation", r"Guru Meditation", "crash", DEBUG_UART,
                  IDF + "esp_system/panic.c:361"),
        Signature("abort", r"abort\(\) was called", "crash", DEBUG_UART,
                  IDF + "esp_libc/src/abort.c:16"),
        Signature("backtrace", r"^Backtrace:", "crash", DEBUG_UART,
                  IDF + "esp_system/port/arch/xtensa/debug_helpers.c:71"),
        Signature("task_wdt", r"task_wdt: Task watchdog", "crash", DEBUG_UART,
                  IDF + "esp_system/task_wdt/task_wdt.c:87,790 (TWDT not started by "
                  "default: sdkconfig.defaults:46)"),
        Signature("rom_reset", ROM_ANY_RESET_RE, "reset", DEBUG_UART,
                  "ESP ROM (closed source); pattern from Brent's hil_checks.py:9"),
    ),
}
SIGNATURES["common"] = COMMON
SIGNATURES["python"] = PYTHON

PORTS = ("atmel-samd", "nordic", "raspberrypi", "stm", "espressif")

# Seen on bravo: "rst:0x1 (POWERON_RESET)" is ESP32 classic; the S2/S3 ROM is documented as
# "(POWERON)". Unverified on the farm: no debug UART is wired yet (farm.toml wiring = {}).
ESP_ROM_POWERON_RE = (r"^rst:0x1 \(POWERON(?:_RESET)?\),boot:0x[0-9a-f]+ "
                      r"\(SPI_FAST_FLASH_BOOT\)$")

# Not failures, but useful to tell a reload from a reset (main.c:1173, :536, :538).
SOFT_REBOOT_RE = r"soft reboot"
AUTO_RELOAD_RE = r"Code stopped by auto-reload"
CODE_DONE_RE = r"Code done running\."


def signatures(port=None, python_errors=False, where=(CONSOLE, DEBUG_UART),
               kinds=("crash", "safe_mode")):
    """The Signatures that apply to a port family (None: every port), de-duplicated by pattern."""
    groups = [COMMON]
    groups += [SIGNATURES[p] for p in (PORTS if port is None else (port,)) if p in SIGNATURES]
    if python_errors:
        groups.append(PYTHON)
        kinds = tuple(kinds) + ("python",)
    seen, out = set(), []
    for group in groups:
        for s in group:
            if s.where in where and s.kind in kinds and s.pattern not in seen:
                seen.add(s.pattern)
                out.append(s)
    return out


def crash_re(port=None, python_errors=False, where=(CONSOLE, DEBUG_UART)):
    """One alternation regex (str) of the crash and safe-mode lines for a port family."""
    return "|".join("(?:%s)" % s.pattern for s in signatures(port, python_errors, where))


# Brent's name: every crash line on any port, console and debug UART.
CRASH_RE = crash_re()


def classify(line, port=None, python_errors=True):
    """The first Signature a console line matches, or None. For failure messages."""
    for s in signatures(port, python_errors, kinds=("crash", "safe_mode", "reset")):
        if re.search(s.pattern, line):
            return s
    return None


# --- Banner ---------------------------------------------------------------------------------

# "Adafruit CircuitPython <tag> on <date>; <board name> with <mcu>"
# py/makeversionhdr.py:62 (MICROPY_FULL_VERSION_INFO), py/mpconfig.h:2358 (BANNER_MACHINE).
# Printed on entering the friendly REPL (shared/runtime/pyexec.c:705), on Ctrl-B at the
# prompt (pyexec.c:526), and as the first line of boot_out.txt (main.c:911). Board names can
# hold " with " (greedy board, lazy mcu: the last one splits). Not anchored with ^: the console
# sends an OSC title ("\x1b]0;...\x1b\\") just before it.
_BANNER = (r"Adafruit CircuitPython (?P<version>\S+) on (?P<date>\d{4}-\d{2}-\d{2}); "
           r"(?P<board_name>[^\r\n]+) with (?P<mcu>[^\r\n]+?)")
BANNER_RE = _BANNER + r"[ \t\r]*$"      # one line at a time (lines without their ending)
BANNER_STREAM_RE = _BANNER + r"\r?\n"   # a raw byte stream: wait for the line to end


@dataclass(frozen=True)
class Banner:
    version: str      # "11.0.0-alpha.1-87-g29e9b60846"
    date: str         # build date, "2026-10-12"
    board_name: str   # MICROPY_HW_BOARD_NAME, "Adafruit Metro RP2040"
    mcu: str          # MICROPY_HW_MCU_NAME, "rp2040"


def _text(s):
    """str in English: a translated build's supervisor lines are mapped back (cpstrings)."""
    return to_english(s.decode(errors="replace") if isinstance(s, (bytes, bytearray)) else s)


def parse_banner(text):
    """The first banner in text as a Banner, or None."""
    m = re.search(_BANNER + r"[ \t\r]*(?:\n|$)", _text(text), re.M)
    if not m:
        return None
    return Banner(m.group("version"), m.group("date"), m.group("board_name"), m.group("mcu"))


def _is_line_monitor(mon):
    """Package A's monitor (and Brent's) match one decoded line at a time."""
    return hasattr(mon, "assert_none")


def _write(mon, data):
    if hasattr(mon, "write"):
        mon.write(data)
    else:
        mon.repl.write(data)  # fixtures.console.SerialMonitor


def expect_banner(mon, timeout=20, poke=False):
    """Expect the CircuitPython banner on the console and return it as a Banner.

    After Brent's expect_banner (hil_checks.py:18 @ cf64e03), which waits for WipperSnapper's
    boot lines. CircuitPython prints its banner only on entering the friendly REPL, not at
    boot, so with poke=True this sends Ctrl-C (stop code.py), CR (the "Press any key" that is
    eaten, main.c:672) and Ctrl-B (reprint at the prompt, pyexec.c:522-526) first. Without poke
    the test's own Act step must lead to the REPL.
    """
    __tracebackhide__ = True  # pylint: disable=unused-variable
    if poke:
        _write(mon, b"\x03")
        time.sleep(0.3)
        _write(mon, b"\r")
        time.sleep(0.3)
        _write(mon, b"\x02")
    pattern = BANNER_RE if _is_line_monitor(mon) else BANNER_STREAM_RE
    m = mon.expect(pattern, timeout=timeout)
    banner = parse_banner(_text(m.group(0)))
    assert banner is not None, "banner matched but did not parse: %r" % m.group(0)
    return banner


# --- boot_out.txt ---------------------------------------------------------------------------

# main.c:911 "Board ID:<id>", main.c:913 "architecture:<arch>" (native .mpy builds),
# main.c:918-922 "UID:" + %02X per byte (translatable text; our builds are English),
# then port_boot_info() (espressif adds "MAC:"). Seen on bravo 2026-10-16:
#   Adafruit CircuitPython 11.0.0-alpha.1-87-g29e9b60846 on 2026-10-12; Adafruit Metro ESP32S3 \
#       with ESP32S3
#   Board ID:adafruit_metro_esp32s3
#   architecture:xtensawin
#   UID:14C19F2FAD04
#   MAC:14:C1:9F:2F:AD:04
BOARD_ID_RE = re.compile(r"^Board ID:\s*(\S+)\s*$", re.M)
UID_RE = re.compile(r"^UID:\s*([0-9A-Fa-f]+)\s*$", re.M)
ARCH_RE = re.compile(r"^architecture:\s*(\S+)\s*$", re.M)

# COMMON_HAL_MCU_PROCESSOR_UID_LENGTH per port, in hex digits
# (ports/<port>/common-hal/microcontroller/Processor.h:9-11).
UID_HEX_LEN = {"atmel-samd": 32, "nordic": 16, "raspberrypi": 16, "stm": 24, "espressif": 12}


@dataclass(frozen=True)
class BootOut:
    banner: Banner        # None if the first line is not a banner
    board_id: str
    uid: str              # upper-case hex, None if absent or all zeros
    architecture: str
    text: str


def parse_boot_out(text):
    """boot_out.txt contents as a BootOut. Missing fields are None."""
    text = _text(text or "")
    bid = BOARD_ID_RE.search(text)
    uid = UID_RE.search(text)
    arch = ARCH_RE.search(text)
    uid = uid.group(1).upper() if uid else None
    if uid and not uid.strip("0"):
        uid = None  # seen on an AT-START-F435: a port without a real UID prints zeros
    return BootOut(parse_banner(text), bid.group(1) if bid else None, uid,
                   arch.group(1) if arch else None, text)


def check_uid(uid, port):
    """Why uid is not a plausible UID for this port family, or None."""
    if not uid or not re.fullmatch(r"[0-9A-F]+", uid) or not uid.strip("0"):
        return "no UID (%r)" % (uid,)
    want = UID_HEX_LEN.get(port)
    if want and len(uid) != want:
        return "UID %s has %d hex digits, %s gives %d" % (uid, len(uid), port, want)
    return None


# --- Identity -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    board_id: str
    version: str
    banner: Banner    # from boot_out.txt
    console: Banner   # from the console, or None when only boot_out.txt was read
    uid: str


def expect_identity(mon_or_board, board, expect_version=None, timeout=20, poke=False):
    """Check that the board answering is the farm.toml board, running the expected firmware.

    After Brent's expect_identity (hil_checks.py:22 @ cf64e03), which checks the IO user and
    SSID the firmware read from secrets.json. Ours checks what CircuitPython reports about
    itself:

    * boot_out.txt "Board ID:" equals farm.toml board_id exactly (Board.mount() only checks a
      substring), and its first line is a banner;
    * if expect_version is given (--expect-version, or the version flashed_firmware wrote),
      the boot_out.txt version contains it;
    * if mon_or_board is a serial monitor, the console banner matches boot_out.txt's version,
      board name and MCU. A mismatch means boot_out.txt is stale (written by an older image
      and not rewritten, main.c:938-944) or the tty and the drive are two different boards.

    boot_out.txt is read through board.boot_out(), which resolves the drive from the sysfs USB
    path at the moment of use. Returns an Identity; fails the test with pytest.fail otherwise.
    """
    __tracebackhide__ = True  # pylint: disable=unused-variable
    import pytest  # pylint: disable=import-outside-toplevel

    console = None
    if mon_or_board is not board and hasattr(mon_or_board, "expect"):
        console = expect_banner(mon_or_board, timeout=timeout, poke=poke)
    try:
        boot = parse_boot_out(board.boot_out())
    except Exception as e:  # pylint: disable=broad-except
        # farmlib.FarmError when the drive holds a different board's boot_out.txt
        pytest.fail("%s: boot_out.txt: %s: %s" % (board.name, type(e).__name__, e),
                    pytrace=False)
    problems = []
    if not boot.text:
        problems.append("boot_out.txt empty or CIRCUITPY not mounted")
    if boot.banner is None:
        problems.append("boot_out.txt has no banner line")
    if boot.board_id != board.board_id:
        problems.append("Board ID %r, farm.toml says %r" % (boot.board_id, board.board_id))
    version = boot.banner.version if boot.banner else None
    if expect_version and (not version or expect_version not in version):
        problems.append("version %r does not contain %r" % (version, expect_version))
    if console is not None and boot.banner is not None and console != boot.banner:
        problems.append("console banner %s differs from boot_out.txt %s" % (console, boot.banner))
    if problems:
        pytest.fail("%s: %s" % (board.name, "; ".join(problems)), pytrace=False)
    return Identity(boot.board_id, version, boot.banner, console, boot.uid)


# --- Crash watch ----------------------------------------------------------------------------

class StreamMark(collections.namedtuple("StreamMark", "repl offset")):
    """A mark on fixtures.console.SerialMonitor: the Repl it was taken on and the byte offset
    into that Repl's transcript. SerialMonitor.expect() replaces mon.repl when the tty drops,
    and the new Repl's transcript starts empty, so an offset alone would point past its end."""


def mark(mon):
    """Where the next output starts: mon.mark() (a line index) on a line monitor, else a
    StreamMark into fixtures.console.SerialMonitor's transcript."""
    if hasattr(mon, "mark"):
        return mon.mark()
    return StreamMark(mon.repl, len(mon.repl.transcript))


def _stream_start(repl, since):
    """Transcript offset to search from. A mark taken on an earlier Repl (the tty was reopened
    after a reset) means everything the current one has read is new, so start at 0."""
    if since is None:
        return 0
    if isinstance(since, StreamMark):
        return since.offset if since.repl is repl else 0
    return since if since <= len(repl.transcript) else 0


def _port_lost(e):
    """A monitor error that means the tty went away. On native USB that is a reset."""
    return isinstance(e, OSError) or type(e).__name__ in ("SerialError", "SerialException")


def explain(lines, port=None, python_errors=False):
    """Every signature found in lines, in order, as "name (source)" strings. A safe-mode crash
    prints a header, then its reason, so both are listed."""
    found = []
    for line in lines:
        s = classify(line, port, python_errors)
        if s is not None:
            found.append("%s %r (%s)" % (s.name, line.strip()[:120], s.source))
    return found


def _fail_window(pytest, name, duration, e):
    pytest.fail("%s: console lost during the %.3g s crash window (reset or USB drop): %s"
                % (name, duration, e), pytrace=False)


def assert_no_crash(mon, since=None, duration=CRASH_WINDOW, port=None, python_errors=False):
    """Fail if the console shows a crash or safe-mode line from `since` on, watching `duration`
    more seconds. Brent's call is mon.assert_none(CRASH_RE, duration=CRASH_WINDOW)
    (test_end_to_end.py:47 @ cf64e03); this is the same with the pattern picked per port.

    port: farm.toml port family ("espressif", ...); None checks every port's lines.
    since: mark(mon) from before the Act step. None means the monitor's default: the last
    reset's boot start on a line monitor (Brent's assert_none), everything this monitor has
    read on fixtures.console.SerialMonitor.

    On native USB a hard reset drops the tty, so a port lost during the window fails as a
    reset, not as a harness error. Only CONSOLE signatures are used: the farm has no debug UART
    monitor yet; use crash_re(port) directly on one.
    """
    __tracebackhide__ = True  # pylint: disable=unused-variable
    import pytest  # pylint: disable=import-outside-toplevel

    pattern = crash_re(port, python_errors, where=(CONSOLE,))
    name = getattr(getattr(mon, "board", None), "name", None) or getattr(mon, "port", "?")
    if hasattr(mon, "assert_none"):
        try:
            mon.assert_none(pattern, since=since, duration=duration)
        except AssertionError as e:
            # The header matched first; give the reason line a moment to arrive, then name both.
            time.sleep(0.5)
            found = []
            if hasattr(mon, "lines"):
                start = getattr(mon, "boot", 0) if since is None else since
                found = explain([l.text for l in mon.lines(start)], port, python_errors)
            pytest.fail("%s: %s" % (name, "; ".join(found) or str(e)), pytrace=False)
        except Exception as e:  # pylint: disable=broad-except
            if not _port_lost(e):
                raise
            _fail_window(pytest, name, duration, e)
        return
    # fixtures.console.SerialMonitor: read the byte stream for `duration`, then search it.
    # read_for() returns what was pending plus what arrived; keep it pending for later expects.
    # Everything read is also in the transcript, which `since` indexes (see StreamMark).
    repl = mon.repl
    try:
        repl.pending = bytearray(repl.read_for(duration))
    except Exception as e:  # pylint: disable=broad-except
        if not _port_lost(e):
            raise
        _fail_window(pytest, name, duration, e)
    lines = _text(bytes(repl.transcript[_stream_start(repl, since):])).splitlines()
    bad = [l for l in lines if re.search(pattern, l)]
    if bad:
        pytest.fail("%s: %s" % (name, "; ".join(explain(bad, port, python_errors))),
                    pytrace=False)


def table():
    """The signature table as rows (port, name, kind, where, pattern, source), for docs."""
    rows = []
    for port in ("common",) + PORTS + ("python",):
        for s in SIGNATURES[port]:
            rows.append((port, s.name, s.kind, s.where, s.pattern, s.source))
    return rows


if __name__ == "__main__":
    for row in table():
        print("\t".join(row))
    print("CRASH_RE has %d alternatives" % CRASH_RE.count("(?:"))
