# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Host-side helpers for the cp-regress farm harness.

Rules this file enforces (from the bravo farm reference):
  * every board is resolved from its USB hub path at the moment of use;
  * identity is confirmed from boot_out.txt before anything is written;
  * serial ports always get write_timeout=5;
  * the REPL eats the first keystroke, so sync on the prompt before sending code;
  * uhubctl runs unprivileged, never through sudo.
"""

import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import termios
import time
from contextlib import contextmanager

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - reported by run.py
    serial = None

SYS_USB = "/sys/bus/usb/devices"
DEVTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devtest.py")
RESULT_TAG = "@@R "
UHUBCTL_LOCK = "/tmp/uhubctl.lock"  # the lock farm-helpers.sh cycle() takes


class FarmError(Exception):
    pass


def _read(path, default=None):
    """Text of a sysfs or drive file. Bytes that are not UTF-8 are replaced, not raised: after
    an ESP32-S2 reset with no re-mount the host can keep boot_out.txt's old, longer size, and
    the read runs past the new end of file into whatever the cluster holds."""
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return default


def _read_fresh(path, default=""):
    """Read a file on a mounted drive with this file's cached pages dropped first.

    The host page cache can hold boot_out.txt from before a reset. An ESP32-S2 can reset with
    only a USB bus reset (same devnum, no disconnect), so nothing re-mounts the drive. The drop
    is POSIX_FADV_DONTNEED, which needs no root; without os.posix_fadvise (macOS) it does nothing.
    """
    try:
        with open(path, "rb") as f:
            if hasattr(os, "posix_fadvise"):
                try:
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
            return f.read().decode(errors="replace").strip()
    except OSError:
        return default


# Runs on the board: sha256 of each path in PATHS, one "CPRH <path> <hex>" line each.
_BOARD_SHA256 = """\
import hashlib
for p in PATHS:
    try:
        h = hashlib.new('sha256')
        with open(p, 'rb') as f:
            while True:
                b = f.read(256)
                if not b:
                    break
                h.update(b)
        print('CPRH', p, ''.join('%02x' % c for c in h.digest()))
    except OSError:
        print('CPRH', p, 'MISSING')
"""


class Board:
    def __init__(self, cfg, host_cfg):
        self.name = cfg["name"]
        self.board_id = cfg["board_id"]
        self.usb = cfg["usb"]
        self.port = cfg.get("port", "")
        self.caps = set(cfg.get("caps", []))
        self.wiring = dict(cfg.get("wiring", {}))
        self.power = dict(cfg.get("power", {}))
        self.flash = dict(cfg.get("flash", {}))
        self.notes = cfg.get("notes", "")
        self.uhubctl = host_cfg.get("uhubctl", "uhubctl")

    def __repr__(self):
        return "<Board %s @%s>" % (self.name, self.usb)

    # ---------- USB identity ----------
    def sysfs(self):
        return os.path.join(SYS_USB, self.usb)

    def present(self):
        return os.path.isdir(self.sysfs())

    def vid_pid(self):
        v = _read(os.path.join(self.sysfs(), "idVendor"))
        p = _read(os.path.join(self.sysfs(), "idProduct"))
        return (v, p) if v and p else None

    def devnum(self):
        return _read(os.path.join(self.sysfs(), "devnum"))

    def interfaces(self):
        """Interface class codes keyed by interface name, e.g. {'3-3.2:1.0': '02'}."""
        out = {}
        for d in glob.glob(os.path.join(self.sysfs(), self.usb + ":1.*")):
            out[os.path.basename(d)] = _read(os.path.join(d, "bInterfaceClass"), "")
        return out

    # ---------- serial ----------
    def _bypath(self, iface):
        tail = self.usb.split("-", 1)[1]
        hits = glob.glob("/dev/serial/by-path/*usb-0:%s:1.%d" % (tail, iface))
        return hits[0] if hits else None

    def ttys(self):
        """All CDC ttys for this board, console first."""
        tail = self.usb.split("-", 1)[1]
        hits = sorted(glob.glob("/dev/serial/by-path/*usb-0:%s:1.*" % tail),
                      key=lambda p: int(p.rsplit(".", 1)[1]))
        return hits

    def tty(self):
        t = self.ttys()
        return t[0] if t else None

    def data_tty(self):
        t = self.ttys()
        return t[1] if len(t) > 1 else None

    # ---------- mass storage ----------
    def block_devs(self):
        devs = []
        for b in glob.glob(os.path.join(self.sysfs(), self.usb + ":1.*", "host*", "target*", "*", "block", "*")):
            disk = os.path.basename(b)
            for dev in (disk + "1", disk):
                if os.path.exists("/dev/" + dev):
                    devs.append("/dev/" + dev)
                    break
        return devs

    def mount(self, timeout=20, check_id=True):
        """Return the CIRCUITPY mount point, mounting through udisks if needed."""
        end = time.time() + timeout
        while time.time() < end:
            for dev in self.block_devs():
                mp = subprocess.run(["findmnt", "-n", "-o", "TARGET", dev],
                                    capture_output=True, text=True).stdout.strip().splitlines()
                mp = mp[0] if mp else ""
                if not mp:
                    r = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                                       capture_output=True, text=True)
                    m = re.search(r" at (.+?)\.?$", r.stdout.strip())
                    mp = m.group(1) if m else ""
                if mp and os.path.isfile(os.path.join(mp, "boot_out.txt")):
                    if check_id and self.board_id not in (_read(os.path.join(mp, "boot_out.txt"), "")):
                        raise FarmError("%s: %s is a different board" % (self.name, mp))
                    return mp
            time.sleep(0.5)
        return None

    def boot_volume(self, timeout=30):
        """Mount point of this board's UF2 bootloader drive (INFO_UF2.TXT, no boot_out.txt).

        A bootloader drive with a size of 0 is stale MSC media (left sitting in UF2 mode); it
        is skipped, and the caller re-enters the bootloader.
        """
        end = time.time() + timeout
        while time.time() < end:
            for dev in self.block_devs():
                size = _read("/sys/class/block/%s/size" % os.path.basename(dev), "0")
                if size == "0":
                    continue
                mp = subprocess.run(["findmnt", "-n", "-o", "TARGET", dev],
                                    capture_output=True, text=True).stdout.strip().splitlines()
                mp = mp[0] if mp else ""
                if not mp:
                    r = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                                       capture_output=True, text=True)
                    m = re.search(r" at (.+?)\.?$", r.stdout.strip())
                    mp = m.group(1) if m else ""
                if mp and os.path.isfile(os.path.join(mp, "INFO_UF2.TXT")) \
                        and not os.path.isfile(os.path.join(mp, "boot_out.txt")):
                    return mp
            time.sleep(0.3)
        return None

    def touch1200(self):
        """1200-baud touch on the console tty. False when there is no tty to touch."""
        tty = self.tty()
        if not tty or serial is None:
            return False
        try:
            s = serial.Serial(tty, 1200, write_timeout=5)
            time.sleep(0.25)
            s.close()
        except (OSError, serial.SerialException):
            pass  # the port vanishing under us is the point
        return True

    def _drop_block_cache(self):
        """Drop the host cache of this drive's block device (FAT directory entries live there).
        Best effort: a device this user cannot open for reading is skipped."""
        for dev in self.block_devs():
            try:
                fd = os.open(dev, os.O_RDONLY)
            except OSError:
                continue
            try:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
            finally:
                os.close(fd)

    def boot_out(self):
        """boot_out.txt as the host reads it from the mount, cache dropped first. Can be stale
        right after a reset; board_boot_out() is the board's own view."""
        mp = self.mount()
        if not mp:
            return ""
        self._drop_block_cache()
        return _read_fresh(os.path.join(mp, "boot_out.txt"), "")

    def board_boot_out(self, timeout=5):
        """boot_out.txt as the board reads it over the REPL: it reads its own flash, so no host
        cache is involved. None when the REPL cannot answer."""
        if serial is None:
            return None
        try:
            with self.repl() as r:
                r.prompt(timeout=timeout)
                outs, err = r.exec_raw_pieces(["print(open('/boot_out.txt').read(), end='')\n"],
                                              timeout=timeout + 2)
        except (FarmError, OSError):
            return None
        return "" if err.strip() else outs[0]

    def fresh_boot_out(self):
        """boot_out.txt from the board's REPL when it answers, else the host mount
        with the cache dropped."""
        text = self.board_boot_out()
        return text if text is not None else self.boot_out()

    def wait_after_reset(self, needle, prev_devnum=None, timeout=None):
        """After microcontroller.reset(), wait until boot_out.txt shows needle.

        Returns (seconds, found). The evidence of a reboot is needle in boot_out.txt as read by
        fresh_boot_out(), so a stale host cache cannot pass for a new boot. When the device
        re-enumerates (devnum differs from prev_devnum), the board counts as back once its drive
        mounts and the settle has run; found is still only True when needle shows up.
        (None, False) means no sign of a reboot within timeout.
        """
        timeout = timeout or 60 + self.power.get("settle", 12)
        t0 = time.time()
        end = t0 + timeout
        back = None
        while time.time() < end:
            if needle in self.fresh_boot_out():
                return (time.time() - t0 if back is None else back), True
            if back is None and prev_devnum is not None and self.present() \
                    and self.devnum() not in (None, prev_devnum):
                if self.wait_ready(timeout=max(1.0, end - time.time())) is None:
                    break
                back = time.time() - t0
            time.sleep(0.5)
        return back, False

    def version(self):
        m = re.search(r"Adafruit CircuitPython (\S+)", self.boot_out())
        return m.group(1) if m else None

    def write_file(self, rel, data, sync=True):
        mp = self.mount()
        if not mp:
            raise FarmError("%s: CIRCUITPY not mounted" % self.name)
        path = os.path.join(mp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
        with open(path, mode) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if sync:
            os.sync()
        return path

    def read_file(self, rel, binary=False):
        mp = self.mount()
        path = os.path.join(mp, rel)
        if not os.path.exists(path):
            return None
        with open(path, "rb" if binary else "r") as f:
            return f.read()

    def remove_file(self, rel):
        mp = self.mount()
        path = os.path.join(mp, rel)
        if os.path.exists(path):
            os.remove(path)
            os.sync()

    # ---------- power / readiness ----------
    def power_cycle(self, off_s=3, wait=True):
        """Cut and restore this port. With wait, return wait_ready()'s result; else 0."""
        hub, port = self.usb.rsplit(".", 1)
        r = str(self.power.get("r", 30))
        w = str(self.power.get("w", 500))
        # Serialised with the farm's shell helpers: hub_activate() re-asserts power across the
        # whole hub, so two cycles on one hub at once can undo each other.
        with open(UHUBCTL_LOCK, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                res = subprocess.run([self.uhubctl, "-l", hub, "-p", port, "-a", "cycle", "-d",
                                      str(off_s), "-r", r, "-w", w], capture_output=True, text=True)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
        if res.returncode != 0:
            raise FarmError("uhubctl failed: %s" % res.stderr.strip())
        return self.wait_ready() if wait else 0

    def wait_ready(self, timeout=None, need_mount=True):
        """Wait for tty (+ mount), then the settle time. Returns seconds taken or None."""
        timeout = timeout or 60 + self.power.get("settle", 12)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.present() and self.tty() and (not need_mount or self.block_devs()):
                if need_mount and not self.mount(timeout=5):
                    time.sleep(0.5)
                    continue
                return time.time() - t0
            time.sleep(0.25)
        return None

    def repl(self, iface=0):
        return Repl(self, iface)

    # ---------- preserve user files ----------
    @contextmanager
    def preserved(self, files=("code.py", "boot.py", "settings.toml")):
        """Back up files, yield, restore, then verify each restored file. Missing files are
        removed again.

        The check is sha256 on the board, over the REPL, because a host read can come from a
        stale cache and pass for a restore that did not stick (ESP32-S2 after a USB bus reset).
        Only when the board cannot answer (no REPL, or no hashlib) is the host md5 used instead.
        A difference on the board raises FarmError.
        """
        saved = {}
        for rel in files:
            data = self.read_file(rel, binary=True)
            saved[rel] = data
        try:
            yield saved
        finally:
            for rel, data in saved.items():
                if data is None:
                    self.remove_file(rel)
                else:
                    self.write_file(rel, data)
            self._verify_restored(saved)

    def _verify_restored(self, saved):
        want = {rel: hashlib.sha256(data).hexdigest() for rel, data in saved.items()
                if data is not None}
        if not want:
            return
        got = self.board_sha256(sorted(want))
        if got is not None:
            bad = ["%s (board sha256 %s, want %s)" % (rel, got[rel], want[rel])
                   for rel in sorted(want) if got[rel] != want[rel]]
            if bad:
                raise FarmError("%s: restore did not stick on the board: %s"
                                % (self.name, "; ".join(bad)))
            return
        # The board cannot say: fall back to the host read, with the cache dropped first.
        mp = self.mount()
        if not mp:
            raise FarmError("%s: CIRCUITPY not mounted, cannot verify the restore" % self.name)
        self._drop_block_cache()
        for rel, data in saved.items():
            if data is None:
                continue
            try:
                with open(os.path.join(mp, rel), "rb") as f:
                    back = f.read()
            except OSError:
                back = b""
            if hashlib.md5(back).hexdigest() != hashlib.md5(data).hexdigest():
                raise FarmError("%s: restore of %s failed md5 check (board unreachable)"
                                % (self.name, rel))

    def board_sha256(self, rels, timeout=5):
        """sha256 hex of each file in rels (names relative to the drive), as the board reads
        them over the REPL. None when the REPL cannot answer or a file is not reported (e.g. no
        hashlib on this build)."""
        if serial is None:
            return None
        paths = ["/" + rel for rel in rels]
        code = "PATHS = %r\n" % (paths,) + _BOARD_SHA256
        try:
            with self.repl() as r:
                r.prompt(timeout=timeout)
                outs, err = r.exec_raw_pieces([code], timeout=timeout + 5)
        except (FarmError, OSError):
            return None
        if err.strip():
            return None
        got = {}
        for line in outs[0].splitlines():
            parts = line.strip().split(" ")
            if len(parts) == 3 and parts[0] == "CPRH":
                got[parts[1].lstrip("/")] = parts[2]
        if set(got) != set(rels):
            return None
        return got


class Repl:
    """Serial REPL driver. Raw REPL for code, friendly REPL for interactive checks."""

    def __init__(self, board, iface=0):
        if serial is None:
            raise FarmError("pyserial is not installed (pip install pyserial)")
        self.board = board
        path = board.ttys()[iface] if len(board.ttys()) > iface else None
        if not path:
            raise FarmError("%s: no tty at interface %d" % (board.name, iface))
        self.s = serial.Serial(path, 115200, timeout=0.1, write_timeout=5)
        self.transcript = bytearray()
        self.pending = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def write(self, b):
        if isinstance(b, str):
            b = b.encode()
        try:
            self.s.write(b)
            self.s.flush()
        except termios.error as e:
            # pyserial's flush() is a bare tcdrain(): a board resetting under us gives
            # termios.error (EIO), which is not an OSError, so callers that catch
            # (FarmError, OSError) would miss it.
            raise FarmError("%s: write failed: %s" % (self.board.name, e)) from e

    def _pull(self):
        chunk = self.s.read(4096)
        if chunk:
            self.transcript += chunk
        return chunk

    def read_until(self, token, timeout=10):
        """Return everything up to and including token; keep the rest buffered."""
        if isinstance(token, str):
            token = token.encode()
        buf = self.pending
        end = time.time() + timeout
        while True:
            i = buf.find(token)
            if i >= 0:
                cut = i + len(token)
                self.pending = bytearray(buf[cut:])
                return bytes(buf[:cut])
            if time.time() >= end:
                self.pending = bytearray()
                raise FarmError("timeout waiting for %r; got tail %r" % (token, bytes(buf[-200:])))
            buf += self._pull()

    def read_for(self, seconds):
        buf = self.pending
        self.pending = bytearray()
        end = time.time() + seconds
        while time.time() < end:
            buf += self._pull()
        return bytes(buf)

    def drain(self, quiet=0.3, limit=3):
        self.pending = bytearray()
        end = time.time() + limit
        last = time.time()
        while time.time() < end and time.time() - last < quiet:
            if self._pull():
                last = time.time()

    def prompt(self, timeout=20):
        """Interrupt whatever runs and get to a stable friendly '>>> ' prompt."""
        # Keep poking until the prompt shows and stays: a host write to CIRCUITPY can trigger an
        # auto-reload after our keys went out, leaving the board at "Press any key" with nothing
        # left to wake it. The first keystroke there is eaten, so each poke sends several.
        end = time.time() + timeout
        tail = b""
        while time.time() < end:
            self.write(b"\r\x03\x03")
            try:
                self.read_until(b">>> ", timeout=1.5)
            except FarmError as e:
                tail = str(e).encode()[-200:]
                continue
            start = len(self.transcript)
            self.drain(quiet=0.5, limit=2)
            later = bytes(self.transcript[start:])
            if b"soft reboot" in later or b"Press any key" in later or b"Code done" in later:
                continue
            return
        raise FarmError("timeout waiting for b'>>> '; last %r" % tail)

    def exec_raw(self, code, timeout=60):
        """Run code in the raw REPL. Returns (stdout, stderr) as str."""
        outs, err = self.exec_raw_pieces([code], timeout=timeout)
        return outs[0], err

    def exec_raw_pieces(self, pieces, timeout=60):
        """Run pieces in order inside ONE raw REPL session, so globals carry over (leaving the
        raw REPL drops them). Stops at the first piece with stderr. Returns ([stdout...], stderr)."""
        self.prompt()
        self.write(b"\x01")
        self.read_until(b"raw REPL; CTRL-B to exit\r\n>", timeout=5)
        end = time.time() + timeout
        outs, err = [], ""
        for code in pieces:
            data = code.encode() if isinstance(code, str) else code
            for i in range(0, len(data), 256):
                self.write(data[i:i + 256])
                time.sleep(0.01)
            self.write(b"\x04")
            self.read_until(b"OK", timeout=10)
            out = self.read_until(b"\x04", timeout=max(5, end - time.time()))
            e = self.read_until(b"\x04", timeout=10)
            try:
                self.read_until(b">", timeout=2)
            except FarmError:
                pass
            outs.append(out[:-1].decode(errors="replace"))
            err = e[:-1].decode(errors="replace")
            if err.strip():
                break
        self.write(b"\x02")  # back to friendly REPL
        self.drain()
        return outs, err

    def run_device(self, body, timeout=60):
        """exec_raw with devtest prepended. Returns (results, stdout, stderr).

        On boards with the "small" cap (SAMD21) the source is sent in pieces of whole
        top-level statements within one raw REPL session: buffering and compiling 7 KB at once
        does not fit the heap.
        """
        if "small" not in getattr(self.board, "caps", ()):
            out, err = self.exec_raw(device_source(body), timeout=timeout)
            return parse_results(out), out, err
        pieces = [device_source("")] + ["import gc\ngc.collect()\n" + c for c in split_source(body, limit=600)]
        outs, err = self.exec_raw_pieces(pieces, timeout=timeout)
        out = "".join(outs)
        return parse_results(out), out, err

    def soft_reboot(self, timeout=20):
        self.prompt()
        self.write(b"\x04")
        return self.read_until(b"soft reboot", timeout=timeout)


def device_source(body):
    """Prepend the on-device result helper to a test body."""
    with open(DEVTEST) as f:
        return f.read() + "\n" + body


def split_source(body, limit=1200):
    """Split device code into chunks of whole top-level statements, each about `limit` bytes.
    A single statement larger than the limit stays whole. Unparseable code is one chunk."""
    import ast
    try:
        nodes = ast.parse(body).body
    except SyntaxError:
        return [body]
    lines = body.splitlines(keepends=True)
    chunks, cur = [], ""
    for i, node in enumerate(nodes):
        start = node.lineno - 1 if i else 0
        stop = nodes[i + 1].lineno - 1 if i + 1 < len(nodes) else len(lines)
        stmt = "".join(lines[start:stop])
        if cur and len(cur) + len(stmt) > limit:
            chunks.append(cur)
            cur = ""
        cur += stmt
    if cur:
        chunks.append(cur)
    return chunks


def parse_results(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(RESULT_TAG):
            try:
                out.append(json.loads(line[len(RESULT_TAG):]))
            except ValueError:
                out.append({"name": "parse_error", "ok": False, "detail": line[:200]})
    return out


def host_ip():
    env = os.environ.get("CPR_HOST_IP")
    if env:
        return env
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def which(cmd):
    return shutil.which(cmd)
