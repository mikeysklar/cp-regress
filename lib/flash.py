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

from farmlib import FarmError

OPENOCD = "/usr/bin/openocd"
ESPTOOL = os.path.expanduser("~/esptool-venv/bin/esptool")
ESP_ROM_PIDS = ("0002", "0009", "1001")  # S2 ROM, S3 DFU, S3 USB-JTAG
OCD_QUIET = ["-c", "gdb_port disabled", "-c", "tcl_port disabled", "-c", "telnet_port disabled"]
UF2_MAGIC0, UF2_MAGIC1, UF2_MAGICEND = 0x0A324655, 0x9E5D5157, 0x0AB16F30


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


def uf2_runs(path):
    """Contiguous (address, bytes) runs of a UF2's main-flash blocks, in address order."""
    with open(path, "rb") as f:
        blob = f.read()
    blocks = []
    for off in range(0, len(blob) - 511, 512):
        b = blob[off:off + 512]
        m0, m1, flags, addr, size = struct.unpack("<5I", b[:20])
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or struct.unpack("<I", b[508:512])[0] != UF2_MAGICEND:
            continue
        if flags & 0x00001000:  # not main flash
            continue
        blocks.append((addr, b[32:32 + size]))
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


def _run(cmd, timeout, log):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
    for line in tail:
        log("  " + line)
    return r


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


def flash_uf2(board, image, log):
    vol = _enter_uf2(board, log)
    if not vol:
        raise FarmError("%s: UF2 bootloader drive never appeared" % board.name)
    _copy_uf2(image, vol, log)


def flash_esp_uf2(board, image, log):
    cfg = board.flash
    if not board.touch1200():
        raise FarmError("%s: no tty for the 1200-baud touch" % board.name)
    end = time.time() + 30
    vol = None
    while time.time() < end:
        if _idproduct(board) in ESP_ROM_PIDS:
            break
        vol = board.boot_volume(timeout=0.5)
        if vol:
            break
    if not vol:
        if _idproduct(board) not in ESP_ROM_PIDS:
            raise FarmError("%s: neither tinyuf2 nor ROM download mode after touch" % board.name)
        # The tty symlink appears after idProduct flips; esptool with an empty port fails fast.
        t_end = time.time() + 15
        while time.time() < t_end and not board.tty():
            time.sleep(0.2)
        tdir = os.path.expanduser(cfg["tinyuf2_dir"])
        # ONE esptool call per download-mode entry: a second one talks to CircuitPython.
        r = _run([ESPTOOL, "--chip", cfg["chip"], "--port", board.tty() or "", "--baud", "921600",
                  "--before", "no-reset", "--after", "hard-reset", "write-flash",
                  "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", cfg["flash_size"],
                  cfg["bootloader_offset"], os.path.join(tdir, "bootloader.bin"),
                  "0x8000", os.path.join(tdir, "partition-table.bin"),
                  "0xe000", os.path.join(tdir, "ota_data_initial.bin"),
                  cfg["tinyuf2_offset"], os.path.join(tdir, "tinyuf2.bin")], 120, log)
        if r.returncode != 0:
            raise FarmError("%s: esptool tinyuf2 write failed" % board.name)
        vol = board.boot_volume(timeout=30)
        if not vol:
            log("tinyuf2 drive not up after esptool; power cycling (native USB has no RTS reset)")
            board.power_cycle(wait=False)
            vol = board.boot_volume(timeout=30)
    if not vol:
        raise FarmError("%s: tinyuf2 bootloader drive never appeared" % board.name)
    _copy_uf2(image, vol, log)


def _openocd(board, speed, cmds, log, timeout=180):
    cfg = board.flash
    cmd = [OPENOCD, "-f", "interface/cmsis-dap.cfg", "-c", "adapter serial %s" % cfg["probe"],
           "-c", "transport select swd", "-c", "adapter speed %d" % speed] + OCD_QUIET + \
          ["-f", cfg["target"]]
    for c in cmds:
        cmd += ["-c", c]
    return _run(cmd, timeout, log)


def flash_swd(board, image, log):
    cfg = board.flash
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
        r = _openocd(board, cfg.get("speed", 1000), cmds, log)
        if r.returncode != 0:
            raise FarmError("%s: openocd write failed: %s"
                            % (board.name, (r.stderr.strip().splitlines() or ["?"])[-1]))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def rescue_swd(board, log):
    """M0: flashed fine but USB never came up. A slow re-attach and reset run brings it back."""
    if board.flash.get("method") != "swd" or not board.flash.get("rescue_speed"):
        return
    log("USB did not come up; SWD reset at %d kHz" % board.flash["rescue_speed"])
    _openocd(board, board.flash["rescue_speed"], ["init", "reset run", "exit"], log, timeout=60)


METHODS = {"uf2": flash_uf2, "esp_uf2": flash_esp_uf2, "swd": flash_swd}


def flash(board, image, log=print):
    """Write image, wait for CIRCUITPY, confirm the board id. Returns boot_out.txt's version."""
    method = board.flash.get("method")
    if method not in METHODS:
        raise FarmError("%s: no flash method in farm.toml (got %r)" % (board.name, method))
    t0 = time.time()
    try:
        METHODS[method](board, image, log)
        ready = board.wait_ready(timeout=60 + board.power.get("settle", 12))
    except FarmError as e:
        if method != "swd":
            raise
        log("first SWD pass failed: %s" % e)
        ready = None
    if ready is None and method == "swd":
        # Seen on the M4 in a parallel run: the erase landed, the write did not, and the
        # board sat in its bootloader with an empty app region. A running CircuitPython can
        # also hold SWDIO on the first attach. The second pass attaches to the bootloader.
        rescue_swd(board, log)
        ready = board.wait_ready(timeout=45)
        if ready is None:
            log("USB still not up; second SWD pass")
            METHODS[method](board, image, log)
            ready = board.wait_ready(timeout=60 + board.power.get("settle", 12))
    if ready is None:
        raise FarmError("%s: CIRCUITPY did not come back after flashing" % board.name)
    v = board.version()  # mount() already refused a drive with a different board id
    log("%s: %s in %.1f s" % (board.name, v, time.time() - t0))
    return v
