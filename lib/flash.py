# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Flash a CircuitPython image onto one farm board, by the path that board is known to take.

Ported from ~/farm-tools/fastcp.sh, one board at a time instead of all eight in one script.
farm.toml `flash.method` picks the path:

  uf2      1200-baud touch, wait for the bootloader drive, copy the .uf2 (RP2040, RP2350)
  esp_uf2  1200-baud touch; if the chip lands in ROM rather than tinyuf2, write tinyuf2 with ONE
           esptool call, then copy the .uf2 to the bootloader drive (ESP32-S2, S3)
  swd      openocd through the board's CMSIS-DAP probe; a .uf2 is split into its contiguous
           runs first, because the nRF52840 image has a gap (M0, M4, nRF52840, STM32F405)

Every path ends the same way: wait for CIRCUITPY, then confirm the board id from boot_out.txt.
flash() returns a FlashResult with per-phase timings, after Brent's FlashState
(firmware/conftest.py:367-405 @ cf64e03), which times erase, build and flash separately.

Before any of it, flash() refuses a board whose tty another process (or this one) holds open,
after Brent's test_flash_refuses_open_port (tests_hil_board_utils/test_filesystem_flash.py:138
@ cf64e03). His SerialMonitor opens the port exclusive so esptool fails; ours looks first.

erase=True (pytest --flash-erase, off by default) wipes the application area before the write,
only where that is known to be safe. See erase_refusal() for which boards and why.
"""
import glob
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from farmlib import FarmError

OPENOCD = "/usr/bin/openocd"
ESPTOOL = os.path.expanduser("~/esptool-venv/bin/esptool")
ESP_ROM_PIDS = ("0002", "0009", "1001")  # S2 ROM, S3 DFU, S3 USB-JTAG
OCD_QUIET = ["-c", "gdb_port disabled", "-c", "tcl_port disabled", "-c", "telnet_port disabled"]
UF2_MAGIC0, UF2_MAGIC1, UF2_MAGICEND = 0x0A324655, 0x9E5D5157, 0x0AB16F30
UF2_FLAG_NOFLASH, UF2_FLAG_FAMILY = 0x00001000, 0x00002000
UF2_FAMILIES = {  # microsoft/uf2 utils/uf2families.json
    "esp32s2": (0xBFDD4EEE,),
    "esp32s3": (0xC47E5767,),
    "rp2040": (0xE48BFF56,),
    "rp2350": (0xE48BFF57, 0xE48BFF59, 0xE48BFF5A),  # absolute, ARM-S, RISC-V
}

# Phases every FlashResult times, in order. Retries add to the same phase.
PHASES = ("enter_bootloader_s", "erase_s", "write_s", "ready_s")

# swd erase: the lowest address an erase may start at, per openocd target. Below it lives a
# bootloader we never touch (CircuitPython's atmel-samd BOOTLOADER_SIZE, ports/atmel-samd/
# mpconfigport.h). The STM32F405's bootloader is in ROM (system memory), so its whole bank is
# the app. nRF52 is absent on purpose: see erase_refusal().
SWD_APP_BASE = {
    "target/at91samdXX.cfg": 0x2000,     # SAMD21, 8 KB UF2 bootloader
    "target/atsame5x.cfg": 0x4000,       # SAMD51, 16 KB UF2 bootloader
    "target/stm32f4x.cfg": 0x08000000,   # STM32F4, ROM bootloader
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def find_image(fw_dir, board):
    """The image for this board in fw_dir: the file whose name carries its board_id.

    Names from the Adafruit S3 bucket look like
    adafruit-circuitpython-<board_id>-en_US-<version>.uf2. SWD boards with a .bin take it
    over the .uf2 (the STM32F405 has no UF2 at all).
    """
    hits = [p for p in glob.glob(os.path.join(fw_dir, "*"))
            if re.search(r"(^|-)%s-" % re.escape(board.board_id), os.path.basename(p))]
    by_ext = {os.path.splitext(p)[1]: p for p in sorted(hits)}
    order = (".bin", ".uf2") if board.flash.get("bin_base") else (".uf2", ".bin")
    for ext in order:
        if ext in by_ext:
            return by_ext[ext]
    return None


def expected_commit(image):
    """The short git hash in an S3 per-commit build name (...-20261013-main-PR11569-5548815)."""
    m = re.search(r"-([0-9a-f]{7,})\.(uf2|bin)$", os.path.basename(image))
    return m.group(1) if m else None


def _uf2_blocks(path):
    with open(path, "rb") as f:
        blob = f.read()
    for off in range(0, len(blob) - 511, 512):
        b = blob[off:off + 512]
        m0, m1, flags, addr, size = struct.unpack("<5I", b[:20])
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or struct.unpack("<I", b[508:512])[0] != UF2_MAGICEND:
            continue
        yield flags, addr, b[32:32 + size], struct.unpack("<I", b[28:32])[0]


def uf2_runs(path):
    """Contiguous (address, bytes) runs of a UF2's main-flash blocks, in address order."""
    blocks = [(addr, data) for flags, addr, data, _ in _uf2_blocks(path)
              if not flags & UF2_FLAG_NOFLASH]
    blocks.sort(key=lambda x: x[0])
    runs, cur_addr, cur = [], None, bytearray()
    for addr, data in blocks:
        if cur_addr is not None and addr == cur_addr + len(cur):
            cur += data
        else:
            if cur_addr is not None:
                runs.append((cur_addr, bytes(cur)))
            cur_addr, cur = addr, bytearray(data)
    if cur_addr is not None:
        runs.append((cur_addr, bytes(cur)))
    return runs


def uf2_families(path):
    """The family IDs a UF2's blocks declare (empty when none carry the family flag)."""
    return {fam for flags, _, _, fam in _uf2_blocks(path) if flags & UF2_FLAG_FAMILY}


def check_uf2_family(path, chip):
    """Raise FarmError if the UF2 declares families and none of them is chip's."""
    want = UF2_FAMILIES.get(chip)
    have = uf2_families(path)
    if want and have and not have & set(want):
        raise FarmError("%s: UF2 family %s is not %s (%s)"
                        % (os.path.basename(path), ",".join("0x%08x" % f for f in sorted(have)),
                           chip, ",".join("0x%08x" % f for f in want)))


# ---------------------------------------------------------------- results


@dataclass
class EraseResult:
    """What an erase did, after Brent's EraseResult (hil_board_utils.py:1114 @ cf64e03), which
    keeps the esptool command and its output. done=False with refused set means the board was
    flashed without an erase, and why."""
    done: bool = False
    refused: str = ""
    method: str = ""
    region: str = ""        # "0x2000-0x40000", or "whole chip"
    command: list = field(default_factory=list)
    output: str = ""        # last lines of the tool's output


@dataclass
class FlashResult:
    """What flash() did, after Brent's FlashResult (hil_board_utils.py:1107 @ cf64e03) and
    FlashState (firmware/conftest.py:367-376 @ cf64e03). His times erase / build / flash of
    one esp32; ours times the phases every flash method shares:

      enter_bootloader_s  1200-baud touch until the bootloader drive (uf2, esp_uf2); on esp_uf2
                          also the tinyuf2 esptool write when the chip lands in ROM
      erase_s             --flash-erase only: openocd erase, flash_nuke copy, or the erase part
                          of the esptool call that writes the 0xFF images (from its log)
      write_s             the image copy (uf2) or openocd write (swd)
      ready_s             until CIRCUITPY is mounted and boot_out.txt is read
      total_s             the whole call, retries and rescue included
    """
    board: str
    image: str
    method: str
    version: str = None
    erase: EraseResult = None
    enter_bootloader_s: float = 0.0
    erase_s: float = 0.0
    write_s: float = 0.0
    ready_s: float = 0.0
    total_s: float = 0.0
    passes: int = 1          # swd: 2 when the second pass ran
    rescued: bool = False    # swd: the slow re-attach ran

    def timings(self):
        """{phase: seconds} rounded for the record, total_s included."""
        return {k: round(getattr(self, k), 2) for k in PHASES + ("total_s",)}

    def as_record(self):
        d = asdict(self)
        d.update(self.timings())
        return d


class _Phases:
    """Adds wall time to FlashResult phases. `with ph("write_s"):` around each step."""

    def __init__(self, result):
        self.result = result

    @contextmanager
    def __call__(self, name):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.add(name, time.monotonic() - t0)

    def add(self, name, seconds):
        setattr(self.result, name, getattr(self.result, name) + seconds)


# ---------------------------------------------------------------- open-port guard


def _comm(pid):
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        r = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True)
        return r.stdout.strip() or "?"


def _pids_holding(dev):
    """PIDs with dev open: fuser, else lsof -t, else a /proc scan. Read-only; sees this user's
    processes (and others' where /proc allows), which is everyone who can open the tty here."""
    if shutil.which("fuser"):
        r = subprocess.run(["fuser", dev], capture_output=True, text=True, timeout=10)
        return {int(p) for p in re.findall(r"\d+", r.stdout)}  # the name goes to stderr
    if shutil.which("lsof"):
        r = subprocess.run(["lsof", "-t", dev], capture_output=True, text=True, timeout=10)
        return {int(p) for p in r.stdout.split() if p.isdigit()}
    pids = set()
    for pid in filter(str.isdigit, os.listdir("/proc") if os.path.isdir("/proc") else []):
        try:
            fds = os.listdir("/proc/%s/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == dev:
                    pids.add(int(pid))
                    break
            except OSError:
                continue
    return pids


def tty_holders(board):
    """[(by-path tty, pid, command)] for every process with one of board's ttys open.

    Each tty is resolved from the board's USB path now, then to its /dev node for the lookup.
    """
    out = []
    for link in board.ttys():
        dev = os.path.realpath(link)
        if not os.path.exists(dev):
            continue
        for pid in sorted(_pids_holding(dev)):
            out.append((link, pid, _comm(pid)))
    return out


def refuse_open_port(board, log, tries=3, gap=0.5):
    """Raise FarmError if board's tty stays held open by any process, ours included.

    A flash with the console held open races the holder: the 1200-baud touch, esptool and the
    re-enumeration after the write all fight it. Checked a few times, so a probe that opens a
    new tty for a moment (udev, ModemManager) does not fail the flash.
    """
    held = []
    for i in range(tries):
        held = tty_holders(board)
        if not held:
            return
        if i + 1 < tries:
            time.sleep(gap)
    who = ", ".join("%s pid %d (%s%s)" % (os.path.basename(t), pid, comm,
                                          ", this pytest process" if pid == os.getpid() else "")
                    for t, pid, comm in held)
    log("refusing to flash: tty held open by " + who)
    raise FarmError("%s: refusing to flash with the console port held open by %s; close the "
                    "serial monitor / REPL first" % (board.name, who))


# ---------------------------------------------------------------- esp image sanity
# After Brent's partition parser and image checks (hil_board_utils.py:52-81, 136-211, 466-485
# @ cf64e03). Ours writes tinyuf2's own bootloader.bin, partition-table.bin and tinyuf2.bin
# rather than one merged image, so the checks run on those, before the one esptool call.

ESP_ENTRY_SIZE = 32
ESP_ENTRY_MAGIC = b"\xaa\x50"
ESP_MD5_MAGIC = b"\xeb\xeb"
ESP_MAX_TABLE_SIZE = 0xC00
ESP_PARTITION_TABLE_OFFSET = 0x8000
ESP_IMAGE_MAGIC = 0xE9
ESP_TYPE_APP = 0x00
ESP_SUBTYPE_FACTORY = 0x00  # tinyuf2's "uf2" partition; CircuitPython goes in ota_0
ESP_BOOTLOADER_OFFSETS = {"esp32": 0x1000, "esp32s2": 0x1000, "esp32s3": 0x0, "esp32c3": 0x0,
                          "esp32c6": 0x0, "esp32h2": 0x0, "esp32c5": 0x2000, "esp32p4": 0x2000}


@dataclass(frozen=True)
class EspPartition:
    index: int
    type: int
    subtype: int
    offset: int
    size: int
    label: str
    flags: int


def esp_parse_partitions(data):
    """Parse and validate an ESP32 binary partition table (Brent's esp_parse_partitions,
    hil_board_utils.py:192 @ cf64e03): aa50 entries, an optional ebeb MD5 entry that must
    match, 0xFF padding to the end; at least one entry, no zero sizes, no overlaps."""
    if not data or len(data) % ESP_ENTRY_SIZE or len(data) > ESP_MAX_TABLE_SIZE:
        raise FarmError("partition table length %d is not a non-zero multiple of %d up to %#x"
                        % (len(data), ESP_ENTRY_SIZE, ESP_MAX_TABLE_SIZE))
    parts = []
    for pos in range(0, len(data), ESP_ENTRY_SIZE):
        raw = data[pos:pos + ESP_ENTRY_SIZE]
        if raw[:2] == ESP_ENTRY_MAGIC:
            ptype, subtype, offset, size = struct.unpack_from("<BBII", raw, 2)
            label = raw[12:28].split(b"\x00", 1)[0].decode("ascii", errors="replace")
            parts.append(EspPartition(pos // ESP_ENTRY_SIZE, ptype, subtype, offset, size, label,
                                      struct.unpack_from("<I", raw, 28)[0]))
        elif raw[:2] == ESP_MD5_MAGIC:
            if raw[16:32] != hashlib.md5(data[:pos]).digest():
                raise FarmError("partition table MD5 mismatch")
        elif raw == b"\xff" * ESP_ENTRY_SIZE:
            break
        else:
            raise FarmError("invalid partition entry at %#x: magic %s" % (pos, raw[:2].hex()))
    if not parts:
        raise FarmError("no partition entries (missing aa50 magic)")
    ordered = sorted(parts, key=lambda p: p.offset)
    for p in ordered:
        if p.size == 0:
            raise FarmError("partition '%s' has zero size" % p.label)
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.size > b.offset:
            raise FarmError("partitions '%s' and '%s' overlap" % (a.label, b.label))
    return parts


def esp_blank_images(parts, cfg, tmp):
    """[(offset, path)] of 0xFF images covering every partition the tinyuf2 write does not
    already fill (all but tinyuf2's and otadata): app slots, NVS, ffat (CIRCUITPY), the rest.

    This is the esp erase. It replaces Brent's esptool erase-flash (hil_board_utils.py:1120 @
    cf64e03) on purpose: erase-flash (or write-flash --erase-all) leaves the chip with no
    second-stage bootloader and no tinyuf2 for the whole erase, and a native-USB S2/S3 that
    drops off the bus then needs a hand on its BOOT button. Here esptool rewrites the
    bootloader first, as the plain ROM path does, and nothing below 0x9000 is blanked.
    """
    keep = {int(cfg["tinyuf2_offset"], 0), 0xE000}
    out = []
    for p in sorted(parts, key=lambda p: p.offset):
        if p.offset in keep:
            continue
        if p.offset < ESP_PARTITION_TABLE_OFFSET + ESP_MAX_TABLE_SIZE:
            raise FarmError("partition '%s' at %#x overlaps the bootloader or partition table"
                            % (p.label, p.offset))
        path = os.path.join(tmp, "blank_%x.bin" % p.offset)
        chunk = b"\xff" * 65536
        with open(path, "wb") as f:
            for i in range(0, p.size, len(chunk)):
                f.write(chunk[:min(len(chunk), p.size - i)])
        out.append((p.offset, path))
    return out


def blanks_sizes(parts, blanks):
    """[(offset, size)] of the partitions esp_blank_images covered."""
    offs = {off for off, _ in blanks}
    return [(p.offset, p.size) for p in parts if p.offset in offs]


def _flash_bytes(size):
    m = re.fullmatch(r"(\d+)\s*([KM])B", str(size).strip(), re.I)
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).upper() == "K" else 1024 * 1024)


def esp_check_combined_image(path, chip):
    """0xFF padding up to the chip's bootloader offset, then the 0xE9 image magic: a merged
    image meant for 0x0 (Brent's esp_check_combined_image, hil_board_utils.py:466 @ cf64e03)."""
    offset = ESP_BOOTLOADER_OFFSETS.get(chip)
    if offset is None:
        raise FarmError("unknown esp chip '%s'" % chip)
    with open(path, "rb") as f:
        head = f.read(offset + 1)
    if len(head) <= offset or head[:offset] != b"\xff" * offset or head[offset] != ESP_IMAGE_MAGIC:
        raise FarmError("%s: not a merged %s image (want 0xFF padding then 0xe9 at %#x)"
                        % (os.path.basename(path), chip, offset))


def esp_check_tinyuf2(cfg):
    """Check the tinyuf2 files and farm.toml offsets agree before esptool writes them.

    bootloader.bin and tinyuf2.bin carry the 0xE9 image magic; the bootloader sits at the
    chip's offset and ends before the partition table; the table parses, fits flash_size, and
    has a factory app partition at tinyuf2_offset big enough for tinyuf2.bin. Returns the
    partitions.
    """
    chip = cfg["chip"]
    tdir = os.path.expanduser(cfg["tinyuf2_dir"])
    boot = os.path.join(tdir, "bootloader.bin")
    tuf2 = os.path.join(tdir, "tinyuf2.bin")
    want_off = ESP_BOOTLOADER_OFFSETS.get(chip)
    boot_off = int(cfg["bootloader_offset"], 0)
    if want_off is None or boot_off != want_off:
        raise FarmError("bootloader_offset %#x is not %s's (%s)" % (boot_off, chip, want_off))
    for p in (boot, tuf2):
        with open(p, "rb") as f:
            if f.read(1) != bytes([ESP_IMAGE_MAGIC]):
                raise FarmError("%s: no 0xe9 image magic" % p)
    if boot_off + os.path.getsize(boot) > ESP_PARTITION_TABLE_OFFSET:
        raise FarmError("%s runs into the partition table at %#x"
                        % (boot, ESP_PARTITION_TABLE_OFFSET))
    with open(os.path.join(tdir, "partition-table.bin"), "rb") as f:
        parts = esp_parse_partitions(f.read())
    end = _flash_bytes(cfg.get("flash_size", ""))
    if end:
        for p in parts:
            if p.offset + p.size > end:
                raise FarmError("partition '%s' ends past %s" % (p.label, cfg["flash_size"]))
    off = int(cfg["tinyuf2_offset"], 0)
    hit = [p for p in parts
           if p.offset == off and p.type == ESP_TYPE_APP and p.subtype == ESP_SUBTYPE_FACTORY]
    if not hit:
        raise FarmError("no factory app partition at tinyuf2_offset %#x in %s" % (off, tdir))
    if os.path.getsize(tuf2) > hit[0].size:
        raise FarmError("tinyuf2.bin is larger than partition '%s'" % hit[0].label)
    return parts


# ---------------------------------------------------------------- shared steps


def _run(cmd, timeout, log):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
    for line in tail:
        log("  " + line)
    return r


def _tail(r, n=12):
    return "\n".join((r.stdout + r.stderr).strip().splitlines()[-n:])


def _idproduct(board):
    try:
        with open(os.path.join(board.sysfs(), "idProduct")) as f:
            return f.read().strip()
    except OSError:
        return None


def _copy_uf2(image, vol, log):
    """Copy to the bootloader drive. The board resets as the last block lands, so the copy can
    end in an I/O error on a write that already succeeded; the boot_out.txt check decides."""
    log("copy %s -> %s" % (os.path.basename(image), vol))
    try:
        shutil.copyfile(image, os.path.join(vol, os.path.basename(image)))
        os.sync()
    except OSError as e:
        log("  copy ended with %s (expected when the board resets mid-sync)" % e)


def _enter_uf2(board, log, tries=2):
    for attempt in range(tries):
        if not board.touch1200():
            raise FarmError("%s: no tty for the 1200-baud touch" % board.name)
        vol = board.boot_volume(timeout=30)
        if vol:
            return vol
        log("no bootloader drive after touch %d" % (attempt + 1))
    return None


# ---------------------------------------------------------------- erase


def _rp_chip(board):
    return "rp2350" if "rp2350" in board.board_id else "rp2040"


def erase_refusal(board, image):
    """Why --flash-erase must not run on this board with this image, or None when it may.

    Allowed, application area only, bootloader kept:
      esp_uf2   ROM download mode, then the ONE esptool write-flash of the ROM path: tinyuf2,
                plus 0xFF over every other partition in the same call (esp_blank_images; the
                one-call-per-download-mode-entry rule holds). Needs reset_ok, to reach ROM
                with microcontroller.RunMode.BOOTLOADER. Wipes the app slots, NVS and the
                ffat partition, which is CIRCUITPY.
      swd SAMD  openocd erase from the image's own start (0x2000 SAMD21, 0x4000 SAMD51) to
                the end of the bank, at the board's speed; refused at erase time unless that
                start is an openocd sector boundary and nothing in range is protected.
                CIRCUITPY is on the external SPI/QSPI flash and survives; nvm does not.
      swd STM32 openocd erase of bank 0 from 0x08000000; its bootloader is in ROM.
                CIRCUITPY is on the external SPI flash and survives.
      uf2 RP    only with flash.nuke_uf2 set in farm.toml: Raspberry Pi's flash_nuke.uf2,
                copied to the BOOTSEL drive, erases all flash and returns to BOOTSEL. The
                bootloader is in ROM, so nothing can be lost that the copy does not restore,
                but the path is not proven on the farm yet, so it stays off until configured.
                Wipes CIRCUITPY (internal flash).
    Refused:
      nRF52840  the SoftDevice and the UF2 bootloader share internal flash with the app, and
                the only whole-chip erase (nrf5 mass_erase) takes both. Never.
      anything else not listed above.
    """
    cfg = board.flash
    method = cfg.get("method")
    if board.port == "nordic" or "nrf52" in cfg.get("target", ""):
        return "nRF52840: never erased (nrf5 mass_erase would take the SoftDevice and bootloader)"
    if method == "esp_uf2":
        if "reset_ok" not in board.caps:
            return "esp: needs reset_ok to reach ROM download mode"
        if cfg.get("chip") not in ESP_BOOTLOADER_OFFSETS:
            return "esp: unknown chip %r" % cfg.get("chip")
        return None
    if method == "swd":
        base = cfg.get("app_base")
        base = int(base, 0) if base else SWD_APP_BASE.get(cfg.get("target"))
        if base is None:
            return "swd: no known application base for %s" % cfg.get("target")
        start = _image_start(board, image)
        if start != base:
            return ("swd: image starts at %s, not the application base %#x; refusing to guess "
                    "where the bootloader ends" % ("?" if start is None else "%#x" % start, base))
        return None
    if method == "uf2":
        nuke = cfg.get("nuke_uf2")
        if not nuke:
            return ("RP2040/RP2350: no flash.nuke_uf2 in farm.toml (safe: the bootloader is in "
                    "ROM, but the nuke path is not configured)")
        nuke = os.path.expanduser(nuke)
        if not os.path.isfile(nuke):
            return "nuke_uf2 %s not found" % nuke
        fams = uf2_families(nuke)
        if fams and not fams & set(UF2_FAMILIES[_rp_chip(board)]):
            return "nuke_uf2 %s has no %s family block" % (nuke, _rp_chip(board))
        return None
    return "no erase path for flash method %r" % method


def _image_start(board, image):
    if image.endswith(".bin"):
        base = board.flash.get("bin_base")
        return int(base, 0) if base else None
    runs = uf2_runs(image)
    return runs[0][0] if runs else None


def _ocd_flash_info(board, log):
    """Read-only openocd `flash info 0`: (bank base, bank size, [(start, size, protected)])."""
    cfg = board.flash
    cmds = ["init"]
    if cfg.get("fast_speed"):
        cmds.append("adapter speed %d" % cfg["fast_speed"])
    cmds += [cfg.get("connect", "halt"), "flash info 0", "shutdown"]
    r = _openocd(board, cfg.get("speed", 1000), cmds, log, timeout=60)
    text = r.stdout + r.stderr
    m = re.search(r"#\d+\s*:\s*\S+ at 0x([0-9a-fA-F]+), size 0x([0-9a-fA-F]+)", text)
    if r.returncode != 0 or not m:
        return None
    base, size = int(m.group(1), 16), int(m.group(2), 16)
    sectors = []
    sector_re = r"#\s*\d+:\s*0x([0-9a-fA-F]+)\s*\(0x([0-9a-fA-F]+)[^)]*\)\s*([a-z ]*)"
    for s in re.finditer(sector_re, text):
        sectors.append((base + int(s.group(1), 16), int(s.group(2), 16),
                        s.group(3).strip() == "protected"))
    return base, size, sectors


def erase_swd(board, image, log, ph):
    """openocd erase from the image's start to the end of flash bank 0. Returns EraseResult.

    The bank layout comes from openocd itself first (read-only): the start must be a sector
    boundary (openocd would otherwise round down into the bootloader with pad, or refuse) and
    no sector in range may be protected. Anything off refuses the erase, and the write goes on
    as without --flash-erase.
    """
    cfg = board.flash
    start = _image_start(board, image)
    with ph("erase_s"):
        info = _ocd_flash_info(board, log)
        if not info:
            return EraseResult(refused="openocd flash info failed; not erasing")
        base, size, sectors = info
        end = base + size
        starts = {a for a, _, _ in sectors}
        if start not in starts:
            return EraseResult(refused="%#x is not an openocd sector boundary on %s; an erase "
                                       "there would reach below the application"
                                       % (start, board.name))
        prot = [a for a, n, p in sectors if p and a + n > start]
        if prot:
            return EraseResult(refused="protected sector(s) at "
                                       + ",".join("%#x" % a for a in prot))
        cmds = ["init"]
        if cfg.get("fast_speed"):
            cmds.append("adapter speed %d" % cfg["fast_speed"])
        cmds += [cfg.get("connect", "halt"),
                 "flash erase_address 0x%x 0x%x" % (start, end - start), "shutdown"]
        r = _openocd(board, cfg.get("speed", 1000), cmds, log, timeout=240)
    res = EraseResult(done=r.returncode == 0, method="openocd flash erase_address",
                      region="%#x-%#x" % (start, end), command=r.args, output=_tail(r))
    if not res.done:
        # The write that follows erases what it writes, as without --flash-erase.
        res.refused = "openocd erase failed: %s" % (r.stderr.strip().splitlines() or ["?"])[-1]
    return res


def erase_rp_nuke(board, vol, log, ph):
    """Copy flash.nuke_uf2 to the BOOTSEL drive; it erases all flash and resets back to BOOTSEL.
    Returns (EraseResult, the new bootloader drive)."""
    nuke = os.path.expanduser(board.flash["nuke_uf2"])
    res = EraseResult(method="flash_nuke.uf2", region="whole chip", command=["copy", nuke, vol])
    with ph("erase_s"):
        _copy_uf2(nuke, vol, log)
        end = time.time() + 15
        while time.time() < end and board.block_devs():  # the drive leaves while it erases
            time.sleep(0.2)
        left = not board.block_devs()
        vol = board.boot_volume(timeout=60)
    if not vol:
        raise FarmError("%s: BOOTSEL drive did not come back after flash_nuke" % board.name)
    res.done = left
    if not left:
        res.refused = "BOOTSEL drive never left after the flash_nuke copy; erase not confirmed"
    return res, vol


def _enter_esp_rom(board, log, timeout=30):
    """Reset into ROM download mode through CircuitPython: RunMode.BOOTLOADER on S2/S3 sets
    the USB DFU persist flag (ports/espressif/common-hal/microcontroller/__init__.c). Returns
    None, or why not."""
    if _idproduct(board) in ESP_ROM_PIDS:
        return None
    try:
        r = board.repl()
    except FarmError as e:
        return "no REPL to reset into ROM: %s" % e
    try:
        r.prompt()
    except FarmError as e:
        r.close()
        return "no REPL prompt to reset into ROM: %s" % e
    try:
        r.write(b"import microcontroller\r\n"
                b"microcontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\r\n"
                b"microcontroller.reset()\r\n")
        time.sleep(1)
    except Exception as e:  # pylint: disable=broad-except
        log("  REPL during ROM reset: %s (the port vanishing is the reset working)" % e)
    finally:
        r.close()
    end = time.time() + timeout
    while time.time() < end:
        if _idproduct(board) in ESP_ROM_PIDS:
            return None
        time.sleep(0.25)
    return "not in ROM download mode %d s after RunMode.BOOTLOADER" % timeout


# ---------------------------------------------------------------- methods


def flash_uf2(board, image, log, ph, erase=False):
    with ph("enter_bootloader_s"):
        vol = _enter_uf2(board, log)
    if not vol:
        raise FarmError("%s: UF2 bootloader drive never appeared" % board.name)
    er = None
    if erase:
        er, vol = erase_rp_nuke(board, vol, log, ph)
    with ph("write_s"):
        _copy_uf2(image, vol, log)
    return er


def flash_esp_uf2(board, image, log, ph, erase=False):
    cfg = board.flash
    if image.endswith(".bin"):
        try:
            esp_check_combined_image(image, cfg["chip"])
            why = "a merged .bin; esp_uf2 writes the .uf2"
        except FarmError as e:
            why = str(e)
        raise FarmError("%s: %s (put the board's .uf2 in the firmware folder)" % (board.name, why))
    check_uf2_family(image, cfg["chip"])
    er = None
    vol = None
    with ph("enter_bootloader_s"):
        if erase:
            why = _enter_esp_rom(board, log)
            if why:
                log("erase refused: " + why)
                er = EraseResult(refused=why)
                erase = False
        if not erase:
            if not board.touch1200():
                raise FarmError("%s: no tty for the 1200-baud touch" % board.name)
            end = time.time() + 30
            while time.time() < end:
                if _idproduct(board) in ESP_ROM_PIDS:
                    break
                vol = board.boot_volume(timeout=0.5)
                if vol:
                    break
        if not vol:
            if _idproduct(board) not in ESP_ROM_PIDS:
                raise FarmError("%s: neither tinyuf2 nor ROM download mode after touch"
                                % board.name)
            # The tty symlink appears after idProduct flips; esptool with an empty port fails fast.
            t_end = time.time() + 15
            while time.time() < t_end and not board.tty():
                time.sleep(0.2)
            parts = esp_check_tinyuf2(cfg)
            tdir = os.path.expanduser(cfg["tinyuf2_dir"])
            # ONE esptool call per download-mode entry: a second one talks to CircuitPython.
            # With erase, the same call also writes 0xFF over every other partition.
            cmd = [ESPTOOL, "--chip", cfg["chip"], "--port", board.tty() or "", "--baud", "921600",
                   "--before", "no-reset", "--after", "hard-reset", "write-flash",
                   "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", cfg["flash_size"],
                   cfg["bootloader_offset"], os.path.join(tdir, "bootloader.bin"),
                   "0x8000", os.path.join(tdir, "partition-table.bin"),
                   "0xe000", os.path.join(tdir, "ota_data_initial.bin"),
                   cfg["tinyuf2_offset"], os.path.join(tdir, "tinyuf2.bin")]
            tmp = tempfile.mkdtemp(prefix="cpr-esp-erase-") if erase else None
            blanks = []
            try:
                if erase:
                    blanks = esp_blank_images(parts, cfg, tmp)
                for off, path in blanks:
                    cmd += ["%#x" % off, path]
                r = _run(cmd, 600 if erase else 120, log)
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
            if erase:
                # esptool 5: "Wrote 12320768 bytes (12034 compressed) at 0x00450000 in 25.3
                # seconds". The blank images' writes are the erase.
                sizes = dict(blanks_sizes(parts, blanks))
                wrote = re.findall(r"at 0x([0-9a-fA-F]+) in ([\d.]+) seconds",
                                   r.stdout + r.stderr)
                secs = sum(float(t) for a, t in wrote if int(a, 16) in sizes)
                ph.add("erase_s", secs)
                ph.add("enter_bootloader_s", -secs)
                er = EraseResult(done=r.returncode == 0, method="esptool write-flash, 0xFF images",
                                 region=",".join("%#x+%#x" % kv for kv in sorted(sizes.items())),
                                 command=cmd, output=_tail(r))
            if r.returncode != 0:
                raise FarmError("%s: esptool tinyuf2 write failed" % board.name)
            vol = board.boot_volume(timeout=30)
            if not vol:
                log("tinyuf2 drive not up after esptool; power cycling "
                    "(native USB has no RTS reset)")
                board.power_cycle(wait=False)
                vol = board.boot_volume(timeout=30)
    if not vol:
        raise FarmError("%s: tinyuf2 bootloader drive never appeared" % board.name)
    with ph("write_s"):
        _copy_uf2(image, vol, log)
    return er


def _openocd(board, speed, cmds, log, timeout=180):
    cfg = board.flash
    cmd = [OPENOCD, "-f", "interface/cmsis-dap.cfg", "-c", "adapter serial %s" % cfg["probe"],
           "-c", "transport select swd", "-c", "adapter speed %d" % speed] + OCD_QUIET + \
          ["-f", cfg["target"]]
    for c in cmds:
        cmd += ["-c", c]
    return _run(cmd, timeout, log)


def flash_swd(board, image, log, ph, erase=False):
    cfg = board.flash
    er = erase_swd(board, image, log, ph) if erase else None
    if er and er.refused:
        log("erase refused: " + er.refused)
    if image.endswith(".bin"):
        if "bin_base" not in cfg:
            raise FarmError("%s: a .bin needs flash.bin_base in farm.toml" % board.name)
        runs = [(int(cfg["bin_base"], 0), image)]
        tmp = None
    else:
        tmp = tempfile.mkdtemp(prefix="cpr-swd-")
        runs = []
        for addr, data in uf2_runs(image):
            p = os.path.join(tmp, "run_0x%x.bin" % addr)
            with open(p, "wb") as f:
                f.write(data)
            runs.append((addr, p))
        log("uf2 runs: " + ", ".join("0x%x" % a for a, _ in runs))
    try:
        cmds = ["init"]
        if cfg.get("fast_speed"):  # M4: attach at 500, write at 2000 (26.6 s -> 12.4 s)
            cmds.append("adapter speed %d" % cfg["fast_speed"])
        cmds.append(cfg.get("connect", "halt"))
        for addr, p in runs:
            cmds.append("flash write_image erase %s 0x%x bin" % (p, addr))
        cmds += ["reset run", "shutdown"]
        with ph("write_s"):
            r = _openocd(board, cfg.get("speed", 1000), cmds, log)
        if r.returncode != 0:
            e = FarmError("%s: openocd write failed: %s"
                          % (board.name, (r.stderr.strip().splitlines() or ["?"])[-1]))
            e.erase = er  # flash() keeps it on the result before the second pass
            raise e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return er


def rescue_swd(board, log):
    """M0: flashed fine but USB never came up. A slow re-attach and reset run brings it back."""
    if board.flash.get("method") != "swd" or not board.flash.get("rescue_speed"):
        return
    log("USB did not come up; SWD reset at %d kHz" % board.flash["rescue_speed"])
    _openocd(board, board.flash["rescue_speed"], ["init", "reset run", "exit"], log, timeout=60)


def swd_reset(board, log=lambda s: None):
    """Reset over the board's probe, for a board a VBUS cycle does not bring back (the M4
    AirLift was off the bus until init; reset run, 2026-10-20). True when openocd ran clean.
    Never erases or writes, so it is safe on every swd board, nRF52840 included."""
    if board.flash.get("method") != "swd":
        return False
    speed = board.flash.get("rescue_speed") or board.flash.get("speed", 1000)
    return _openocd(board, speed, ["init", "reset run", "exit"], log, timeout=60).returncode == 0


METHODS = {"uf2": flash_uf2, "esp_uf2": flash_esp_uf2, "swd": flash_swd}


def flash(board, image, log=print, erase=False):
    """Write image, wait for CIRCUITPY, confirm the board id. Returns a FlashResult; its
    .version is boot_out.txt's version.

    erase=True erases first where erase_refusal() allows it; where it does not, the board is
    flashed as without it and FlashResult.erase.refused says why. Refuses (FarmError) to start
    while any process holds the board's tty open.
    """
    method = board.flash.get("method")
    if method not in METHODS:
        raise FarmError("%s: no flash method in farm.toml (got %r)" % (board.name, method))
    res = FlashResult(board=board.name, image=os.path.basename(image), method=method)
    ph = _Phases(res)
    t0 = time.monotonic()
    refuse_open_port(board, log)
    if erase:
        why = erase_refusal(board, image)
        if why:
            log("erase refused: " + why)
            res.erase = EraseResult(refused=why)
            erase = False
    try:
        try:
            er = METHODS[method](board, image, log, ph, erase=erase)
            if er is not None:
                res.erase = er
            with ph("ready_s"):
                ready = board.wait_ready(timeout=60 + board.power.get("settle", 12))
        except FarmError as e:
            if method != "swd":
                raise
            res.erase = getattr(e, "erase", None) or res.erase
            log("first SWD pass failed: %s" % e)
            ready = None
        if ready is None and method == "swd":
            # Seen on the M4 in a parallel run: the erase landed, the write did not, and the
            # board sat in its bootloader with an empty app region. A running CircuitPython can
            # also hold SWDIO on the first attach. The second pass attaches to the bootloader.
            res.rescued = bool(board.flash.get("rescue_speed"))
            with ph("ready_s"):
                rescue_swd(board, log)
                ready = board.wait_ready(timeout=45)
            if ready is None:
                log("USB still not up; second SWD pass")
                res.passes = 2
                METHODS[method](board, image, log, ph)
                with ph("ready_s"):
                    ready = board.wait_ready(timeout=60 + board.power.get("settle", 12))
        if ready is None:
            raise FarmError("%s: CIRCUITPY did not come back after flashing" % board.name)
        with ph("ready_s"):
            res.version = board.version()  # mount() already refused a drive with a different id
    finally:
        res.total_s = time.monotonic() - t0
    log("%s: %s in %.1f s (%s)" % (board.name, res.version, res.total_s,
                                   ", ".join("%s %.1f" % (k[:-2], v)
                                             for k, v in res.timings().items() if k != "total_s")))
    return res
