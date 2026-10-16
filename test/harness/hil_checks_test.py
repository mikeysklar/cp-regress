# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/hil_checks.py against recorded CircuitPython console output: crash and safe-mode
signatures, the banner, boot_out.txt, and the checks Brent's tests call (expect_banner,
assert_none(CRASH_RE, ...), hil/tests_hil_board_utils/hil_checks.py @ cf64e03) run on a pty."""
import re

import pytest

import hil_checks as hc
from serialmon import ANSI_ESCAPE_RE, SerialMonitor

pytestmark = pytest.mark.smoke

# --- recorded on the farm -----------------------------------------------------------------------

# Every banner the farm printed, from bravo:~/cp-regress/results/20261014-*/results.jsonl:
# (line, board_name, mcu)
BANNERS = [
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Metro ESP32S2 "
     "with ESP32S2", "Adafruit Metro ESP32S2", "ESP32S2"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Metro M4 "
     "Airlift Lite with samd51j19", "Adafruit Metro M4 Airlift Lite", "samd51j19"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Feather "
     "STM32F405 Express with STM32F405RG", "Adafruit Feather STM32F405 Express", "STM32F405RG"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Metro RP2040 "
     "with rp2040", "Adafruit Metro RP2040", "rp2040"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Metro ESP32S3 "
     "with ESP32S3", "Adafruit Metro ESP32S3", "ESP32S3"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be on 2026-10-12; Adafruit Feather "
     "nRF52840 Express with nRF52840", "Adafruit Feather nRF52840 Express", "nRF52840"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-86-g55488150be-dirty on 2026-10-13; Adafruit Metro "
     "RP2350 with rp2350b", "Adafruit Metro RP2350", "rp2350b"),
    ("Adafruit CircuitPython 11.0.0-alpha.1-48-g37c207ccaf on 2026-10-08; Adafruit Metro M0 "
     "Express with samd21g18", "Adafruit Metro M0 Express", "samd21g18"),
]

# feather_stm32f405, bravo:~/cp-regress/results/20261014-162746/results.jsonl, the console tail
# of a stack overflow (the record starts mid-line; "Running " is main.c:465's).
SAFE_MODE_STM32 = (
    "Running in safe mode! Not running saved code.\r\n\r\nYou are in safe mode because:\r\n"
    "Stack overflow. Increase stack size.\r\nPress reset to exit safe mode.\r\n\r\n"
    "Press any key to enter the REPL. Use CTRL-D to reload.\r\n")

# metro_esp32s3 boot_out.txt, seen on bravo 2026-10-16 (hil_checks.py's docstring).
BOOT_OUT_S3 = (
    "Adafruit CircuitPython 11.0.0-alpha.1-87-g29e9b60846 on 2026-10-12; Adafruit Metro ESP32S3 "
    "with ESP32S3\r\nBoard ID:adafruit_metro_esp32s3\r\narchitecture:xtensawin\r\n"
    "UID:14C19F2FAD04\r\nMAC:14:C1:9F:2F:AD:04\r\n")

# --- built from CircuitPython's own strings (not recorded; the source line is cited) -------------

# main.c:536-538 and the auto-reload line; a normal boot that must never count as a crash.
CLEAN_BOOT = (
    "Auto-reload is on. Simply save files over USB to run them or enter REPL to disable.\r\n"
    "code.py output:\r\nhello from code.py\r\nsafe to say: hard fault tolerant\r\n\r\n"
    "Code done running.\r\n\r\nPress any key to enter the REPL. Use CTRL-D to reload.\r\n")

# safe_mode.c:153, :207, :216 for a HardFault (every Cortex-M port's port.c HardFault handler).
HARD_FAULT = (
    "\x1b]0;\xf0\x9f\x90\x8dSafe mode | 11.0.0\x1b\\Auto-reload is off.\r\n"
    "Running in safe mode! Not running saved code.\r\n\r\nYou are in safe mode because:\r\n"
    "CircuitPython core code crashed hard. Whoops!\r\n"
    "Hard fault: memory access or instruction error.\r\n"
    "Please file an issue with your program at github.com/adafruit/circuitpython/issues.\r\n"
    "Press reset to exit safe mode.\r\n")

TRACEBACK = (
    "code.py output:\r\nTraceback (most recent call last):\r\n"
    '  File "code.py", line 3, in <module>\r\nMemoryError: memory allocation failed\r\n')

# ESP ROM lines: Brent's esp32 (hil_checks.py:8 @ cf64e03), and the S2/S3 form.
ROM_ESP32 = "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"
ROM_S3 = "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)"


def lines(text):
    """Console text as the monitor's lines: escapes stripped, split on line endings."""
    return ANSI_ESCAPE_RE.sub("", text).splitlines()


# --- signatures ---------------------------------------------------------------------------------


def test_every_signature_compiles_and_cites_a_source():
    rows = hc.table()
    assert rows
    for port, name, kind, where, pattern, source in rows:
        re.compile(pattern)
        assert kind in ("crash", "safe_mode", "python", "reset"), (port, name)
        assert where in (hc.CONSOLE, hc.DEBUG_UART), (port, name)
        assert (source.startswith(hc.CP) and re.search(r":\d+", source)
                or source.startswith("ESP ROM")), (port, name, source)
    for port in hc.PORTS + (None,):
        re.compile(hc.crash_re(port))
        re.compile(hc.crash_re(port, python_errors=True, where=(hc.CONSOLE,)))


def test_recorded_safe_mode_is_a_crash():
    found = [l for l in lines(SAFE_MODE_STM32) if re.search(hc.CRASH_RE, l)]
    assert found == ["Running in safe mode! Not running saved code.",
                     "You are in safe mode because:", "Stack overflow. Increase stack size."]
    names = [hc.classify(l, "stm").name for l in found]
    assert names == ["running_safe_mode", "safe_mode", "stack_overflow"]
    explained = hc.explain(lines(SAFE_MODE_STM32), "stm")
    assert any(e.startswith("stack_overflow ") and "safe_mode.c:184" in e for e in explained)


@pytest.mark.parametrize("port", hc.PORTS)
def test_hard_fault_on_every_port(port):
    found = [l for l in lines(HARD_FAULT) if re.search(hc.crash_re(port, where=(hc.CONSOLE,)), l)]
    assert "Hard fault: memory access or instruction error." in found
    assert "CircuitPython core code crashed hard. Whoops!" in found
    assert hc.classify("Hard fault: memory access or instruction error.", port).kind == "crash"


@pytest.mark.parametrize("line", lines(CLEAN_BOOT) + [b[0] for b in BANNERS])
def test_clean_boot_lines_are_not_crashes(line):
    assert not re.search(hc.CRASH_RE, line), line
    assert hc.classify(line, python_errors=False) is None


def test_python_errors_only_when_asked():
    assert not any(re.search(hc.CRASH_RE, l) for l in lines(TRACEBACK))
    pattern = hc.crash_re(python_errors=True)
    hits = [l for l in lines(TRACEBACK) if re.search(pattern, l)]
    assert hits == ["Traceback (most recent call last):", "MemoryError: memory allocation failed"]


def test_debug_uart_lines_stay_off_the_console_pattern():
    console = hc.crash_re("espressif", where=(hc.CONSOLE,))
    for line in ("Guru Meditation Error: Core  1 panic'ed (LoadProhibited)",
                 "abort() was called at PC 0x400d1234 on core 0", "Backtrace: 0x4008 0x4009",
                 ROM_ESP32):
        assert not re.search(console, line), line
        assert re.search(hc.crash_re("espressif") + "|" + hc.ROM_ANY_RESET_RE, line), line


def test_rom_reset_lines():
    assert re.search(hc.ROM_RESET_RE, ROM_ESP32)
    assert not re.search(hc.ROM_RESET_RE, ROM_S3)
    assert re.search(hc.ESP_ROM_POWERON_RE, ROM_ESP32) and re.search(hc.ESP_ROM_POWERON_RE, ROM_S3)
    assert re.search(hc.ROM_ANY_RESET_RE,
                     "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)")


# --- banner and boot_out.txt --------------------------------------------------------------------


@pytest.mark.parametrize("line,board_name,mcu", BANNERS)
def test_recorded_banners_parse(line, board_name, mcu):
    assert re.search(hc.BANNER_RE, line)
    assert re.search(hc.BANNER_STREAM_RE, line + "\r\n")
    b = hc.parse_banner(">>> \r\n" + line + "\r\n>>> ")
    assert (b.board_name, b.mcu, b.date) == (board_name, mcu, line.split(" on ")[1][:10])
    assert b.version.startswith("11.0.0-alpha.1-")


def test_banner_board_name_may_hold_with():
    b = hc.parse_banner("Adafruit CircuitPython 10.0.3 on 2025-10-17; Feather with Wings with "
                        "nRF52840")
    assert (b.board_name, b.mcu) == ("Feather with Wings", "nRF52840")
    assert hc.parse_banner("Adafruit CircuitPython 11.0.0 on 2026-10-13; Fake Board") is None


def test_boot_out():
    boot = hc.parse_boot_out(BOOT_OUT_S3)
    assert (boot.board_id, boot.uid, boot.architecture) == (
        "adafruit_metro_esp32s3", "14C19F2FAD04", "xtensawin")
    assert boot.banner.mcu == "ESP32S3"
    assert hc.check_uid(boot.uid, "espressif") is None
    assert "hex digits" in hc.check_uid(boot.uid, "raspberrypi")
    zeros = hc.parse_boot_out(BOOT_OUT_S3.replace("14C19F2FAD04", "000000000000"))
    assert zeros.uid is None and hc.check_uid(zeros.uid, "espressif").startswith("no UID")
    empty = hc.parse_boot_out("")
    assert empty.banner is None and empty.board_id is None


# --- the same checks on a live (pty) monitor ----------------------------------------------------


@pytest.fixture
def mon(pty_board):
    pytest.importorskip("serial", reason="pyserial is not installed")
    m = SerialMonitor(pty_board)
    yield m
    m.close()


def test_expect_banner_through_the_osc_title(pty_board, mon):
    pty_board.write("\r\n\x1b]0;\xf0\x9f\x90\x8dDone | 11.0.0\x1b\\" + BANNERS[3][0] + "\r\n>>> ")
    banner = hc.expect_banner(mon, timeout=5)
    assert (banner.board_name, banner.mcu) == ("Adafruit Metro RP2040", "rp2040")


def test_assert_no_crash_names_the_recorded_reason(pty_board, mon):
    start = hc.mark(mon)
    pty_board.write(SAFE_MODE_STM32)
    with pytest.raises(pytest.fail.Exception) as exc:
        hc.assert_no_crash(mon, since=start, duration=1, port="stm")
    msg = str(exc.value)
    assert "stack_overflow" in msg and "safe_mode.c:184" in msg and mon.board.name in msg


def test_assert_no_crash_passes_a_clean_boot(pty_board, mon):
    pty_board.write(CLEAN_BOOT + TRACEBACK)
    hc.assert_no_crash(mon, duration=0.3, port="raspberrypi")
    with pytest.raises(pytest.fail.Exception, match="traceback"):
        hc.assert_no_crash(mon, duration=0.1, port="raspberrypi", python_errors=True)


def test_assert_no_crash_reports_a_lost_port_as_a_reset(pty_board):
    pytest.importorskip("serial", reason="pyserial is not installed")
    m = SerialMonitor(pty_board, reopen=False)
    try:
        pty_board.unplug()
        with pytest.raises(pytest.fail.Exception, match="console lost during"):
            hc.assert_no_crash(m, duration=2)
    finally:
        m.close()


class BootOutBoard:
    name = "metro_esp32s3"
    board_id = "adafruit_metro_esp32s3"

    def __init__(self, text):
        self.text = text

    def boot_out(self):
        return self.text


def test_expect_identity(pty_board, mon):
    board = BootOutBoard(BOOT_OUT_S3)
    ident = hc.expect_identity(board, board, expect_version="11.0.0-alpha.1")
    assert (ident.board_id, ident.uid, ident.console) == (board.board_id, "14C19F2FAD04", None)

    # The console banner must agree with boot_out.txt (same board, same image).
    pty_board.write(BOOT_OUT_S3.splitlines()[0] + "\r\n")
    assert hc.expect_identity(mon, board, timeout=5).console.board_name == "Adafruit Metro ESP32S3"
    pty_board.write(BANNERS[4][0] + "\r\n")  # the 86-g build: boot_out.txt is stale
    with pytest.raises(pytest.fail.Exception, match="differs from boot_out.txt"):
        hc.expect_identity(mon, board, timeout=5)

    other = BootOutBoard(BOOT_OUT_S3.replace("Board ID:adafruit_metro_esp32s3",
                                             "Board ID:adafruit_metro_esp32s2"))
    with pytest.raises(pytest.fail.Exception, match="farm.toml says"):
        hc.expect_identity(other, other)
    with pytest.raises(pytest.fail.Exception, match="does not contain"):
        hc.expect_identity(board, board, expect_version="10.")
