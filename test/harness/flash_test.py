# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/flash.py with the board and every subprocess mocked: FlashResult timing records (after
Brent's FlashResult / FlashState, hil_board_utils.py:1107 and firmware/conftest.py:367-405 @
cf64e03), the --flash-erase refusal table, the open-port refusal (his
test_flash_refuses_open_port, test_filesystem_flash.py:138 @ cf64e03), and what the erase
paths would hand openocd and esptool. No tool runs and no board is touched."""
import json
import os
import struct
import subprocess

import pytest

import flash
from farmlib import Board, FarmError
from flash import FlashResult, erase_refusal

pytestmark = pytest.mark.smoke

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def uf2_block(addr, data, family=None, flags=0):
    if family is not None:
        flags |= flash.UF2_FLAG_FAMILY
    hdr = struct.pack("<8I", flash.UF2_MAGIC0, flash.UF2_MAGIC1, flags, addr, len(data), 0, 1,
                      family or 0)
    return hdr + data.ljust(476, b"\0") + struct.pack("<I", flash.UF2_MAGICEND)


def write_uf2(path, start, nblocks=2, family=None):
    with open(path, "wb") as f:
        for i in range(nblocks):
            f.write(uf2_block(start + 256 * i, bytes([i]) * 256, family))
    return str(path)


class FakeBoard:
    """The parts of farmlib.Board that flash() and erase_refusal() use, all in memory."""

    def __init__(self, name, port, flash_cfg, caps=("native_usb", "reset_ok"),
                 board_id="adafruit_fake", vol=None):
        self.name, self.port, self.flash, self.caps = name, port, dict(flash_cfg), set(caps)
        self.board_id = board_id
        self.power = {"settle": 0}
        self.vol = vol
        self.calls = []

    def ttys(self):
        return []

    def tty(self):
        return "/dev/serial/by-path/fake"

    def wait_ready(self, timeout=None, need_mount=True):  # pylint: disable=unused-argument
        self.calls.append("wait_ready")
        return 0.01

    def version(self):
        return "11.0.0-alpha.1-87-g29e9b60846"

    def touch1200(self):
        self.calls.append("touch1200")
        return True

    def boot_volume(self, timeout=30):  # pylint: disable=unused-argument
        return self.vol

    def block_devs(self):
        return []

    def power_cycle(self, off_s=3, wait=True):  # pylint: disable=unused-argument
        self.calls.append("power_cycle")


@pytest.fixture
def no_tools(monkeypatch):
    """Every subprocess.run in lib/flash.py goes through here; nothing real ever runs.
    Set .reply(cmd) -> CompletedProcess to answer; .cmds holds what would have run."""

    class Tools:
        cmds = []

        @staticmethod
        def reply(cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")

    def run(cmd, **kw):  # pylint: disable=unused-argument
        Tools.cmds.append(list(cmd))
        return Tools.reply(cmd)

    monkeypatch.setattr(flash.subprocess, "run", run)
    monkeypatch.setattr(flash.time, "sleep", lambda s: None)
    monkeypatch.setattr(flash, "tty_holders", lambda board: [])
    return Tools


# --- the refusal table --------------------------------------------------------------------------


def real_boards():
    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore  # pylint: disable=import-outside-toplevel
    with open(os.path.join(HERE, "farm.toml"), "rb") as f:
        cfg = tomllib.load(f)
    return {e["name"]: Board(e, cfg.get("host", {})) for e in cfg["board"]}


# farm.toml board -> (image start, whether --flash-erase may run). The image starts where the
# real build does: after the UF2 bootloader on SAMD, at 0x08000000 (a .bin) on the STM32.
FARM_TABLE = {
    "metro_rp2040": (0x10000000, False),       # no flash.nuke_uf2 configured
    "metro_m4_airlift": (0x4000, True),        # SAMD51 after its 16 KB bootloader
    "metro_m0": (0x2000, True),                # SAMD21 after its 8 KB bootloader
    "feather_nrf52840": (0x26000, False),      # never: SoftDevice and bootloader
    "metro_esp32s2": (None, True),             # ROM path, tinyuf2 rewritten in the same call
    "metro_rp2350": (0x10000000, False),
    "metro_esp32s3": (None, True),
    "feather_stm32f405": ("bin", True),        # ROM bootloader; bank 0 from 0x08000000
    # dev hub boards: no flash method yet, so never erased
    "feather_esp32c6": (None, False),
    "esp32c5_devkitc": (None, False),
    "korvo_esp32s31": (None, False),
    "p4gpio": (None, False),
    "nrf54lm20dk": (None, False),
    "siwx917_dk2605a": (None, False),
}


def test_refusal_table_covers_the_farm():
    assert set(real_boards()) == set(FARM_TABLE), "farm.toml changed: extend FARM_TABLE"


@pytest.mark.parametrize("name", sorted(FARM_TABLE))
def test_refusal_table_on_the_real_farm(name, tmp_path):
    board = real_boards()[name]
    start, allowed = FARM_TABLE[name]
    if start == "bin":
        image = str(tmp_path / "x.bin")
        open(image, "wb").close()
    else:
        image = write_uf2(tmp_path / "x.uf2", start or 0)
    why = erase_refusal(board, image)
    assert (why is None) is allowed, why
    if name == "feather_nrf52840":
        assert why.startswith("nRF52840: never erased")


@pytest.mark.parametrize("port,cfg,caps,start,refused", [
    ("nordic", {"method": "swd", "target": "target/nrf52.cfg"}, (), 0x26000, "nRF52840"),
    ("nordic", {"method": "uf2"}, (), 0x26000, "nRF52840"),
    ("", {"method": "swd", "target": "target/nrf52.cfg"}, (), 0x26000, "nRF52840"),
    ("espressif", {"method": "esp_uf2", "chip": "esp32s3"}, ("native_usb",), None, "reset_ok"),
    ("espressif", {"method": "esp_uf2", "chip": "esp8266"}, ("reset_ok",), None, "unknown chip"),
    ("atmel-samd", {"method": "swd", "target": "target/at91samdXX.cfg"}, (), 0x0,
     "not the application base 0x2000"),
    ("atmel-samd", {"method": "swd", "target": "target/atsame5x.cfg"}, (), 0x2000,
     "not the application base 0x4000"),
    ("atmel-samd", {"method": "swd", "target": "target/at91samdXX.cfg", "app_base": "0x4000"},
     (), 0x4000, None),
    ("x", {"method": "swd", "target": "target/lpc17xx.cfg"}, (), 0x0, "no known application"),
    ("raspberrypi", {"method": "uf2"}, (), 0x10000000, "no flash.nuke_uf2"),
    ("raspberrypi", {"method": "uf2", "nuke_uf2": "/nonexistent/flash_nuke.uf2"}, (),
     0x10000000, "not found"),
    ("x", {"method": "dfu"}, (), 0x0, "no erase path"),
    ("x", {}, (), 0x0, "no erase path"),
])
def test_refusal_table(tmp_path, port, cfg, caps, start, refused):
    board = FakeBoard("b", port, cfg, caps)
    why = erase_refusal(board, write_uf2(tmp_path / "x.uf2", start or 0))
    if refused is None:
        assert why is None
    else:
        assert why and refused in why, why


def test_rp_nuke_must_be_for_the_right_chip(tmp_path):
    nuke = write_uf2(tmp_path / "nuke.uf2", 0x20000000, family=flash.UF2_FAMILIES["rp2350"][0])
    image = write_uf2(tmp_path / "x.uf2", 0x10000000)
    rp2040 = FakeBoard("b", "raspberrypi", {"method": "uf2", "nuke_uf2": nuke},
                       board_id="adafruit_metro_rp2040")
    assert "no rp2040 family" in erase_refusal(rp2040, image)
    rp2350 = FakeBoard("b", "raspberrypi", {"method": "uf2", "nuke_uf2": nuke},
                       board_id="adafruit_metro_rp2350")
    assert erase_refusal(rp2350, image) is None


# --- flash(): refusals, records -----------------------------------------------------------------


def test_flash_refuses_an_open_port(monkeypatch, no_tools):
    monkeypatch.setattr(flash, "tty_holders",
                        lambda b: [("/dev/serial/by-path/x-if00", 4242, "screen")])
    monkeypatch.setitem(flash.METHODS, "uf2", lambda *a, **k: pytest.fail("flashed anyway"))
    board = FakeBoard("metro_rp2040", "raspberrypi", {"method": "uf2"})
    logged = []
    with pytest.raises(FarmError, match="held open by x-if00 pid 4242 \\(screen\\)"):
        flash.flash(board, "x.uf2", log=logged.append)
    assert board.calls == [] and no_tools.cmds == []
    assert logged and logged[-1].startswith("refusing to flash")


def test_flash_without_a_method_refuses():
    with pytest.raises(FarmError, match="no flash method"):
        flash.flash(FakeBoard("b", "x", {}), "x.uf2", log=lambda m: None)


def test_refused_erase_still_flashes_without_it(monkeypatch, no_tools, tmp_path):
    seen = {}

    def fake_swd(board, image, log, ph, erase=False):  # pylint: disable=unused-argument
        seen["erase"] = erase
        with ph("write_s"):
            pass
        return None

    monkeypatch.setitem(flash.METHODS, "swd", fake_swd)
    board = FakeBoard("feather_nrf52840", "nordic",
                      {"method": "swd", "target": "target/nrf52.cfg", "probe": "P"})
    res = flash.flash(board, write_uf2(tmp_path / "x.uf2", 0x26000), log=lambda m: None,
                      erase=True)
    assert seen["erase"] is False
    assert res.erase.done is False and res.erase.refused.startswith("nRF52840")
    assert res.version == board.version()
    rec = res.as_record()
    json.dumps(rec)  # goes into history/flashed.json as is
    assert rec["erase"]["refused"].startswith("nRF52840") and rec["method"] == "swd"
    assert set(flash.PHASES) | {"total_s"} <= set(rec)
    assert rec["total_s"] >= rec["write_s"] >= 0


def test_flash_result_timings():
    res = FlashResult(board="b", image="x.uf2", method="uf2")
    ph = flash._Phases(res)  # pylint: disable=protected-access
    ph.add("write_s", 1.234)
    ph.add("write_s", 2.0)   # retries add to the same phase
    with ph("ready_s"):
        pass
    res.total_s = 9.876
    t = res.timings()
    assert list(t) == list(flash.PHASES) + ["total_s"]
    assert t["write_s"] == 3.23 and t["total_s"] == 9.88 and t["erase_s"] == 0.0
    rec = res.as_record()
    assert rec["passes"] == 1 and rec["rescued"] is False and rec["erase"] is None


# --- what the erase paths would run -------------------------------------------------------------

SAMD51_INFO = """#0 : atsame5 at 0x00000000, size 0x00080000, buswidth 4, chipwidth 0
\t#  0: 0x00000000 (0x2000 8kB) protected
\t#  1: 0x00002000 (0x2000 8kB) protected
\t#  2: 0x00004000 (0x2000 8kB) not protected
\t#  3: 0x00006000 (0x7a000 488kB) not protected
"""


def openocd_reply(info):
    def reply(cmd):
        if "flash info 0" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", info)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return reply


def m4():
    return FakeBoard("metro_m4_airlift", "atmel-samd",
                     {"method": "swd", "probe": "E6647C740349682C",
                      "target": "target/atsame5x.cfg", "speed": 500, "fast_speed": 2000,
                      "connect": "halt"})


def test_swd_erase_starts_at_the_application(no_tools, tmp_path):
    no_tools.reply = openocd_reply(SAMD51_INFO)
    res = FlashResult(board="m4", image="x.uf2", method="swd")
    er = flash.erase_swd(m4(), write_uf2(tmp_path / "x.uf2", 0x4000), lambda m: None,
                         flash._Phases(res))  # pylint: disable=protected-access
    assert er.done and not er.refused and er.region == "0x4000-0x80000"
    erase_cmd = no_tools.cmds[-1]
    assert "flash erase_address 0x4000 0x7c000" in erase_cmd
    assert "adapter serial E6647C740349682C" in erase_cmd and "adapter speed 2000" in erase_cmd
    for cmd in no_tools.cmds:
        assert "sudo" not in cmd and not any("mass_erase" in c for c in cmd)


@pytest.mark.parametrize("start,info,why", [
    (0x5000, SAMD51_INFO, "not an openocd sector boundary"),
    (0x4000, SAMD51_INFO.replace("0x00006000 (0x7a000 488kB) not protected",
                                 "0x00006000 (0x7a000 488kB) protected"), "protected sector"),
    (0x4000, "Error: no flash bank", "flash info failed"),
])
def test_swd_erase_refuses_at_erase_time(no_tools, tmp_path, start, info, why):
    no_tools.reply = openocd_reply(info)
    res = FlashResult(board="m4", image="x.uf2", method="swd")
    er = flash.erase_swd(m4(), write_uf2(tmp_path / "x.uf2", start), lambda m: None,
                         flash._Phases(res))  # pylint: disable=protected-access
    assert not er.done and why in er.refused
    assert not any("erase_address" in c for cmd in no_tools.cmds for c in cmd)


def test_nrf_flash_never_erases(no_tools, tmp_path):
    """flash(erase=True) on the nRF52840 writes the image (openocd erases only what it writes)
    and never runs an erase of its own."""
    board = real_boards()["feather_nrf52840"]
    monkey_ready = FakeBoard("x", "x", {})
    board.wait_ready = monkey_ready.wait_ready
    board.version = monkey_ready.version
    board.ttys = lambda: []
    res = flash.flash(board, write_uf2(tmp_path / "x.uf2", 0x26000, nblocks=4),
                      log=lambda m: None, erase=True)
    assert res.erase.refused.startswith("nRF52840")
    assert len(no_tools.cmds) == 1
    cmd = " ".join(no_tools.cmds[0])
    assert "flash write_image erase" in cmd and "0x26000 bin" in cmd
    assert "erase_address" not in cmd and "mass_erase" not in cmd and "sudo" not in cmd
    assert "adapter serial E6647C74034E732F" in cmd


def partition_table(entries):
    """ESP32 partition table bytes: (label, type, subtype, offset, size), 0xFF padded."""
    out = b""
    for label, ptype, subtype, offset, size in entries:
        out += struct.pack("<2sBBII16sI", b"\xaa\x50", ptype, subtype, offset, size,
                           label.encode(), 0)
    return out + b"\xff" * (flash.ESP_MAX_TABLE_SIZE - len(out))


S2_PARTS = [("nvs", 1, 0x02, 0x9000, 0x5000), ("otadata", 1, 0x00, 0xE000, 0x2000),
            ("ota_0", 0, 0x10, 0x10000, 0x160000), ("ota_1", 0, 0x11, 0x170000, 0x160000),
            ("uf2", 0, 0x00, 0x2D0000, 0x40000), ("ffat", 1, 0x81, 0x310000, 0xF0000)]


def test_esp_erase_is_one_esptool_call_that_keeps_the_bootloader(monkeypatch, no_tools,
                                                                    tmp_path):
    tdir = tmp_path / "tinyuf2"
    tdir.mkdir()
    (tdir / "bootloader.bin").write_bytes(b"\xe9" + b"\0" * 1000)
    (tdir / "tinyuf2.bin").write_bytes(b"\xe9" + b"\0" * 1000)
    (tdir / "ota_data_initial.bin").write_bytes(b"\xff" * 0x2000)
    (tdir / "partition-table.bin").write_bytes(partition_table(S2_PARTS))
    vol = tmp_path / "S2BOOT"
    vol.mkdir()
    cfg = {"method": "esp_uf2", "chip": "esp32s2", "flash_size": "4MB",
           "bootloader_offset": "0x1000", "tinyuf2_offset": "0x2d0000", "tinyuf2_dir": str(tdir)}
    board = FakeBoard("metro_esp32s2", "espressif", cfg, vol=str(vol))
    monkeypatch.setattr(flash, "_idproduct", lambda b: "0002")  # already in ROM download mode
    blanks = {0x9000: 1.5, 0x10000: 2.0, 0x170000: 2.0, 0x310000: 1.0}
    out = "".join("Wrote %d bytes at 0x%08x in %.1f seconds\n" % (1, a, s)
                  for a, s in list(blanks.items()) + [(0x2D0000, 0.7)])
    no_tools.reply = lambda cmd: subprocess.CompletedProcess(cmd, 0, out, "")
    image = write_uf2(tmp_path / "adafruit-circuitpython-x.uf2", 0, family=0xBFDD4EEE)

    res = FlashResult(board=board.name, image="x.uf2", method="esp_uf2")
    er = flash.flash_esp_uf2(board, image, lambda m: None,
                             flash._Phases(res), erase=True)  # pylint: disable=protected-access
    assert len(no_tools.cmds) == 1, "one esptool call per download-mode entry"
    cmd = no_tools.cmds[0]
    assert "write-flash" in cmd and "erase-flash" not in cmd and "--erase-all" not in cmd
    pairs = dict(zip(cmd[cmd.index("--flash-size") + 2::2], cmd[cmd.index("--flash-size") + 3::2]))
    assert pairs["0x1000"].endswith("bootloader.bin")  # rewritten in the same call
    assert pairs["0x2d0000"].endswith("tinyuf2.bin")
    blanked = sorted(int(k, 0) for k, v in pairs.items()
                     if os.path.basename(v).startswith("blank"))
    assert blanked == sorted(blanks)  # app slots, NVS, ffat; never below 0x9000 or otadata
    assert er.done and er.method.startswith("esptool")
    assert res.erase_s == pytest.approx(sum(blanks.values()))
    assert res.enter_bootloader_s + res.erase_s >= 0
    assert os.listdir(vol) == [os.path.basename(image)]
    assert not os.path.exists(pairs["0x9000"])  # the 0xFF images are cleaned up
