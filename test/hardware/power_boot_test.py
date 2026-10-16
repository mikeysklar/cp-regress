# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Power-cycle boot monitor tests: the console across uhubctl cycles and soft reboots.

After Brent's test_power.py, test_serial_monitor_hw.py and test_end_to_end.py
(hil/tests_hil_board_utils/ @ cf64e03), over every farm.toml board, using the monitor in
tools/power_cycle_monitor.py. Every board-facing test power-cycles or soft-reboots the board, so
the module runs only when asked:

    pytest test/hardware/power_boot_test.py --tier release -m power_boot
    pytest test/hardware/power_boot_test.py --tier release -m power_boot --boards metro_rp2040
    CPR_POWER_BOOT=1 pytest test/hardware/power_boot_test.py --tier release

To register the marker (and silence PytestUnknownMarkWarning) add this line to the markers list
in pyproject.toml:
    "power_boot: power-cycles boards with uhubctl; runs only with -m power_boot",

What differs from Brent: his board_reset() pulls EN through the CP2102's RTS line; native-USB
CircuitPython has no such line and prints nothing until a host opens the tty, so "the whole boot
from the first byte" is the Ctrl-D soft boot here, and a power-cycled boot is checked from the
open on (see the tools/power_cycle_monitor.py docstring for what is lost, ESP32-S2 included).
"""

import os
import re
import sys
import time

import pytest

import farmlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import power_cycle_monitor as pcm  # noqa: E402  pylint: disable=wrong-import-position


def _asked(config):
    """-m names power_boot, or CPR_POWER_BOOT=1."""
    return "power_boot" in (config.option.markexpr or "") \
        or os.environ.get("CPR_POWER_BOOT") == "1"


pytestmark = [
    pytest.mark.release,
    pytest.mark.power_boot,
    pytest.mark.destructive,
    pytest.mark.needs("native_usb"),
    # A string condition is evaluated at setup, before the board fixture opens anything.
    pytest.mark.skipif("not _asked(config)", reason="power-cycles boards: run with -m power_boot"),
]

OFF_S = 3  # farmlib Board.power_cycle's default off time


@pytest.fixture
def open_monitor(request, board, tmp_path):
    """open_monitor(port=None) -> a SerialMonitor on the board's console, resolved from its USB
    path now. Every monitor is closed and dumped to tmp_path after the test (Brent's
    open_monitor fixture, hil/firmware/conftest.py)."""
    opened = []

    def get(port=None):
        try:
            mon = pcm.monitor_class()(port or pcm.wait_for_port(board, timeout=5), pcm.SERIAL_BAUD)
        except farmlib.FarmError as e:
            pytest.fail(str(e), pytrace=False)
        opened.append(mon)
        return mon

    yield get
    for i, mon in enumerate(opened):
        mon.close()
        mon.dump(tmp_path / ("%s-%s-%d.serial.log" % (board.name, request.node.name, i)))


def _port_timeout(board):
    return 60 + board.power.get("settle", 12)


# --- no hardware ---


def test_wait_for_port_times_out():
    """wait_for_port gives up on time on a USB path with nothing there (Brent's
    test_wait_for_port_times_out, test_power.py:110)."""
    nowhere = farmlib.Board({"name": "nowhere", "board_id": "none", "usb": "9-9.9"}, {})
    start = time.monotonic()
    with pytest.raises(pcm.PortError, match="no tty"):
        pcm.wait_for_port(nowhere, timeout=1, interval=0.2)
    assert 1 <= time.monotonic() - start < 3


# --- offline checks of the recovery rules (no board; they run with the module's marker) ---

FARM_TOML = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "farm.toml")


def _farm():
    return {b.name: b for b in pcm.load_boards(FARM_TOML, "all")}


def test_swd_rescue_only_on_samd_swd_boards():
    """The SWD rescue applies to the SAMD boards that stay off the bus after a VBUS cut: the M4
    at its speed 500, the M0 at its rescue_speed 400. No other board gets one."""
    farm = _farm()
    assert pcm.swd_rescue_speed(farm["metro_m4_airlift"]) == 500
    assert pcm.swd_rescue_speed(farm["metro_m0"]) == 400
    for name in ("metro_rp2040", "feather_nrf52840", "metro_esp32s2", "metro_esp32s3",
                 "metro_rp2350", "feather_stm32f405"):
        assert pcm.swd_rescue_speed(farm[name]) is None, name


def test_s3_power_keys_are_the_long_repeats():
    """The ESP32-S3 needs -r 80 -w 300 to power off (farm.toml), not the default 30/500."""
    p = _farm()["metro_esp32s3"].power
    assert p["r"] >= 80 and p["w"] >= 300, p
    assert pcm.WRITE_SETTLE_S >= 10


def test_nrf_reboot_is_not_judged_by_monotonic():
    """nRF52840: a monotonic clock longer than the time since the cut is no evidence that the
    MCU was not reset, so the verdict comes from devnum and reset_reason."""
    nrf = _farm()["feather_nrf52840"]
    rebooted, why = pcm.reboot_verdict(nrf, 900.0, "ResetReason.POWER_ON", 5.0, "7", "8")
    assert rebooted, why
    assert not pcm.reboot_verdict(nrf, 900.0, "ResetReason.SOFTWARE", 5.0, "7", "8")[0]
    assert not pcm.reboot_verdict(nrf, 900.0, "ResetReason.POWER_ON", 5.0, "7", "7")[0]


def test_samd_and_rp_reboot_is_judged_by_uptime():
    rp = _farm()["metro_rp2040"]
    assert pcm.reboot_verdict(rp, 2.0, "ResetReason.POWER_ON", 5.0, "7", "8")[0]
    assert not pcm.reboot_verdict(rp, 500.0, "ResetReason.POWER_ON", 5.0, "7", "8")[0]


def test_brownout_is_its_own_outcome():
    """The power-dip safe-mode line and the BROWNOUT reset_reason name one outcome that is not
    a harness failure."""
    line = "Power dipped. Make sure you are providing enough power."
    assert re.search(pcm.BROWNOUT_RE, line)
    assert not re.search(pcm.BROWNOUT_RE, "You are in safe mode because:")
    assert pcm.reset_name("ResetReason.BROWNOUT") == "BROWNOUT"
    assert pcm.reset_name(None) is None
    rep = pcm.BootReport(_farm()["feather_nrf52840"])
    rep.ok, rep.brownout = False, True
    assert rep.outcome == "brownout"
    rep.brownout = False
    assert rep.outcome == "fail"


def test_write_settle_counts_from_the_tty_reappearing():
    assert pcm.settle_remaining(100.0, 103.0) == pytest.approx(pcm.WRITE_SETTLE_S - 3.0)
    assert pcm.settle_remaining(100.0, 500.0) == 0.0


# --- soft boots (Ctrl-D), the farm's board_reset() ---


def test_reset_captures_whole_boot(open_monitor):
    """A Ctrl-D boot is captured from its first line, in order, with no crash
    (test_serial_monitor_hw.py:16)."""
    mon = open_monitor()
    boot = mon.soft_reset()
    assert boot == mon.boot
    # CircuitPython 11 with a code.py that runs long prints "soft reboot" then "<file>.py
    # output:" (main.c). The "Auto-reload is" status is reprinted only once code.py has
    # finished and a host is connected, so it is not required here.
    mon.expect(r"^\S+\.py output:$", timeout=10)
    mon.assert_none(pcm.CRASH_RE, duration=pcm.CRASH_WINDOW)
    # Nothing from before the reset leaked into this boot.
    first = mon.lines(boot)[:2]
    assert len(first) == 2 and first[0].text == "soft reboot", first
    assert re.search(r"^\S+\.py output:$", first[1].text), first
    pcm.expect_banner(mon)


def test_second_reset_starts_a_new_boot(open_monitor):
    """Each soft_reset() moves the boot start; assert_none looks back only when asked
    (test_serial_monitor_hw.py:30)."""
    mon = open_monitor()
    first = mon.soft_reset()
    mon.expect(r"^\S+\.py output:$", timeout=10)
    second = mon.soft_reset()
    assert second > first
    mon.expect(r"^\S+\.py output:$", timeout=10)
    boots = [ln for ln in mon.lines(second) if re.search(r"^soft reboot$", ln.text)]
    assert len(boots) == 1, boots
    with pytest.raises(pcm.SerialUnexpectedError):
        mon.assert_none(r"^soft reboot$", since=first)


def test_open_does_not_reset(board, open_monitor):
    """Closing and reopening the console neither re-enumerates nor reboots the board
    (test_serial_monitor_hw.py:46). An ESP32 on USB-Serial-JTAG would fail here."""
    mon = open_monitor()
    up1, _ = mon.uptime()
    devnum = board.devnum()
    mon.close()

    t = time.time()
    mon = open_monitor()
    mon.assert_none(pcm.BOOT_RE, since=0, duration=3)
    up2, _ = mon.uptime()
    assert board.devnum() == devnum, "re-enumerated on open"
    assert up2 >= up1 + (time.time() - t) - 1.0, "uptime went %.1f -> %.1f s" % (up1, up2)


def test_timestamps_and_dump(open_monitor, tmp_path):
    """Lines carry ordered timestamps and contiguous indices; dump() writes one stamped line per
    captured line (test_serial_monitor_hw.py:56)."""
    mon = open_monitor()
    boot = mon.soft_reset()
    mon.expect(r"^\S+\.py output:$", timeout=10)
    mon.close()

    lines = mon.lines(boot)
    times = [ln.time for ln in lines]
    assert times == sorted(times)
    assert [ln.index for ln in lines] == list(range(boot, boot + len(lines)))
    assert "soft reboot\n" in mon.output

    dumped = mon.dump(tmp_path / "boot.serial.log").read_text().splitlines()
    assert len(dumped) == mon.mark()
    assert all(re.match(r"^\[\d\d:\d\d:\d\d\.\d{3}\] ", ln) for ln in dumped)


# --- power cycles ---


def test_power_cycle_drops_the_port(board, open_monitor):
    """An open monitor reports the port lost (an error, not a test failure), refuses to act
    once closed, and a new monitor on the re-enumerated tty sees the board
    (test_serial_monitor_hw.py:73)."""
    mon = open_monitor()
    devnum = board.devnum()
    cyc = pcm.power_cycle_board(board, OFF_S)
    try:
        with pytest.raises(pcm.SerialError, match="lost") as exc:
            mon.expect("never printed", timeout=10)
        assert not isinstance(exc.value, AssertionError)
        mon.close()
        with pytest.raises(pcm.SerialError, match="closed"):
            mon.soft_reset()

        # The port clock starts now, after the cut has returned (uhubctl's flock wait is before).
        port, _ = pcm.wait_for_port_or_rescue(board, timeout=_port_timeout(board))
        ready = time.time()
    finally:
        cyc["watch"].stop.set()
    assert board.devnum() != devnum, "devnum unchanged: the board never left the bus"
    pcm.settle_before_write(ready)  # the first write comes 12 s after the tty reappears
    mon = open_monitor(port)
    pcm.expect_banner(mon)


def test_power_cycle_boot(board, tmp_path):
    """The tool's whole check: port gone, port back, banner, no crash, MCU really reset
    (hil_power_cycle_monitor.py main and test_end_to_end.py, without the flashing)."""
    rep, _ = pcm.capture_power_boot(board, delay=OFF_S, port_timeout=_port_timeout(board),
                                    log_path=tmp_path / ("%s.serial.log" % board.name))
    print("timings: %s" % {k: v for k, v in rep.as_dict().items() if k.endswith("_s")})
    if rep.swd_rescue:
        print("note: no tty after the cut; SWD rescue needed (%s)" % rep.warnings)
    if rep.brownout:
        # Safe mode after a power dip (BROWNOUT) is a board behaviour on a VBUS cut, not a harness
        # failure: the outcome is recorded, and the board was reset out of safe mode.
        pytest.xfail("BROWNOUT after the VBUS cut (reset_reason %s, recovered %s): %s"
                     % (rep.reset_reason, rep.recovered, "; ".join(rep.warnings)))
    assert rep.back_s is not None or rep.open_s is not None, rep.problems
    assert rep.version, "no banner: %s" % rep.problems
    assert rep.ok, "; ".join(rep.problems)
    if not rep.rebooted:
        # only reached when farm.toml says power.resets = false for this board
        pytest.xfail("VBUS cycle does not reset this board: %s" % rep.warnings)
