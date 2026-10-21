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

import base64
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
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

import cpstrings

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - reported by run.py
    serial = None

SYS_USB = "/sys/bus/usb/devices"
DEVTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devtest.py")
RESULT_TAG = "@@R "
UHUBCTL_LOCK = "/tmp/uhubctl.lock"  # the lock farm-helpers.sh cycle() takes
# CIRCUITPY_WEB_API_PASSWORD as test/fixtures/secrets.py writes it into settings.toml.
WEB_API_PASSWORD = "cpr-test"


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

# Runs on the board, first piece of Repl.put_file: make the parent folders of PATH and open
# PATH + '.cprtmp' for writing. The following pieces are f.write(a2b_base64(...)) lines.
_PUT_OPEN = """\
import binascii, os
d = ''
for part in PATH.split('/')[1:-1]:
    d += '/' + part
    try:
        os.mkdir(d)
    except OSError:
        pass
f = open(PATH + '.cprtmp', 'wb')
"""

# Last piece of Repl.put_file: close, swap the temp file in (FAT rename will not overwrite),
# report the size. A failed transfer leaves PATH as it was.
_PUT_CLOSE = """\
f.close()
try:
    os.remove(PATH)
except OSError:
    pass
os.rename(PATH + '.cprtmp', PATH)
print('CPRP', os.stat(PATH)[6])
"""

# Runs on the board: PATH as base64 lines between CPRG BEGIN / END, or CPRG MISSING.
_GET_FILE = """\
import binascii
try:
    f = open(PATH, 'rb')
except OSError:
    print('CPRG MISSING')
else:
    print('CPRG BEGIN')
    while True:
        b = f.read(384)
        if not b:
            break
        print(binascii.b2a_base64(b, newline=False).decode())
    f.close()
    print('CPRG END')
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
        # Where files go: "drive" (CIRCUITPY over USB MSC), "repl" (raw REPL, for boards with
        # no native USB) or "web" (web workflow /fs/, REPL until it answers). Everything that reads or writes board files goes through
        # write_file / read_file / remove_file, which pick the transport from this.
        self.fs = cfg.get("fs", "drive")
        # Extra settings.toml keys this board always needs, merged under the module's own
        # (the S31 Korvo-1 needs CIRCUITPY_SAFE_MODE_DELAY = 0 or every reset is safe mode).
        self.settings = dict(cfg.get("settings", {}))
        # A real UART behind the tty (a CP2102N, a probe's CDC). run_device sends such a board
        # its code in pieces, as it does for "small" boards. None: a USB console.
        self.console_baud = cfg.get("console_baud")
        # "rts": EN is wired to the UART bridge's RTS (esptool's auto-reset circuit), the one
        # reset that reaches a board its USB port does not power (the S31 Korvo-1).
        self.reset = cfg.get("reset")
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
        """All ttys for this board, console first: CDC (...:1.0) or a USB-UART bridge
        (...:1.0-port0, a CP2102N on a board with no native USB)."""
        tail = self.usb.split("-", 1)[1]
        hits = glob.glob("/dev/serial/by-path/*usb-0:%s:1.*" % tail)
        return sorted(hits, key=lambda p: [int(n) for n in re.findall(r"\d+", p.rsplit(":1.", 1)[1])])

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

    def mount_readonly(self):
        """True when this board's drive is mounted and the mount is read-only."""
        devs = set(self.block_devs())
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    dev, _, _, opts = line.split()[:4]
                    if dev in devs:
                        return "ro" in opts.split(",")
        except OSError:
            pass
        return False

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
        right after a reset; board_boot_out() is the board's own view. A board with no drive
        answers with board_boot_out()."""
        if self.fs != "drive":
            return self.board_boot_out() or ""
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

    # ---------- web workflow ----------
    def mac(self):
        """The 12 hex digits of this board's MAC when its USB serial is one (ESP32 USB-JTAG
        units and Adafruit ESP32 boards report it, e.g. 10:BD:A3:DF:20:14), else None."""
        sn = re.sub(r"[:-]", "", _read(os.path.join(self.sysfs(), "serial"), "") or "").lower()
        return sn if re.fullmatch(r"[0-9a-f]{12}", sn) else None

    def web(self, password=WEB_API_PASSWORD):
        """A WebFs for this board, resolved now, or None when its web workflow does not answer.

        The host comes from mDNS: espressif's default hostname ends in the STA MAC
        (ports/espressif/common-hal/wifi/__init__.c:538), which matches the USB serial. Without
        an mDNS match the board is asked for wifi.radio.ipv4_address over the REPL. Either way
        /cp/version.json must name this board_id before anything is written, and the board
        must take WEB_API_PASSWORD (it will not before flashed_fs writes its settings.toml)."""
        cands = []
        mac = self.mac()
        if mac:
            cands = ["http://%s:%s" % (ip, port) for host, ip, port in _avahi_circuitpython()
                     if host.endswith("-%s.local" % mac)]
        if not cands and serial is not None:
            try:
                with self.repl() as r:
                    out, err = r.exec_raw("import wifi\nprint('CPRIP', wifi.radio.ipv4_address)\n",
                                          timeout=10)
                m = re.search(r"CPRIP (\d+\.\d+\.\d+\.\d+)", out) if not err.strip() else None
                if m:
                    cands = ["http://%s" % m.group(1)]
            except (FarmError, OSError):
                pass
        for base in cands:
            try:
                with urllib.request.urlopen(base + "/cp/version.json", timeout=5) as resp:
                    info = json.loads(resp.read().decode())
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if info.get("board_id") == self.board_id:
                w = WebFs(self, base, password=password)
                return w if w.authorized() else None
        return None

    def _web_files(self):
        """The WebFs to use on an fs = "web" board, or None for the REPL. A web board whose
        workflow does not answer (no settings.toml yet, WiFi down) falls back to the REPL,
        which is how its first settings.toml gets there."""
        return self.web() if self.fs == "web" else None

    def write_file(self, rel, data, sync=True):
        if self.fs != "drive":
            w = self._web_files()
            if w:
                w.put(rel, data)
                return "/" + rel
            with self.repl() as r:
                r.put_file(rel, data)
            return "/" + rel
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
        if self.fs != "drive":
            w = self._web_files()
            if w:
                data = w.get(rel)
            else:
                with self.repl() as r:
                    data = r.get_file(rel)
            return data if data is None or binary else data.decode(errors="replace")
        mp = self.mount()
        path = os.path.join(mp, rel)
        if not os.path.exists(path):
            return None
        with open(path, "rb" if binary else "r") as f:
            return f.read()

    def remove_file(self, rel):
        if self.fs != "drive":
            w = self._web_files()
            if w:
                w.delete(rel)
                return
            with self.repl() as r:
                r.del_file(rel)
            return
        mp = self.mount()
        path = os.path.join(mp, rel)
        if os.path.exists(path):
            os.remove(path)
            os.sync()

    # ---------- power / readiness ----------
    def power_cycle(self, off_s=3, wait=True, confirm_off=False, off_timeout=10):
        """Cut and restore this port, and the power.also ports with it (a board fed by two
        USB ports, like the ESP32-C5 DevKitC, only resets when both go). With wait, return
        wait_ready()'s result; else 0.

        With confirm_off, the board must drop off the bus within off_timeout seconds of the
        ports going off, or FarmError (after switching them back on): some hubs report a port
        off while VBUS stays up (3-6.3 and 3-6 here), and a board on a battery stays up too.
        After Brent's power_cycle_board(confirm_off=True), hil_board_utils.py:751 @ 242a162.
        """
        hubs, stuck = {}, False
        for path in [self.usb] + list(self.power.get("also", [])):
            hub, port = path.rsplit(".", 1)
            hubs.setdefault(hub, []).append(port)
        r = str(self.power.get("r", 30))
        w = str(self.power.get("w", 500))
        # Serialised with the farm's shell helpers: hub_activate() re-asserts power across the
        # whole hub, so two cycles on one hub at once can undo each other.
        with open(UHUBCTL_LOCK, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                if len(hubs) == 1 and not confirm_off:
                    (hub, ports), = hubs.items()
                    steps = [(hub, ports, "cycle")]
                else:  # every port off, then every port on: no window with one side fed
                    steps = [(h, p, "off") for h, p in hubs.items()] + \
                            [(h, p, "on") for h, p in hubs.items()]
                for i, (hub, ports, action) in enumerate(steps):
                    cmd = [self.uhubctl, "-l", hub, "-p", ",".join(ports), "-a", action,
                           "-r", r, "-w", w]
                    if action == "cycle":
                        cmd += ["-d", str(off_s)]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        break
                    if action == "off" and steps[i + 1][2] == "on":
                        if confirm_off and not self._wait_gone(off_timeout):
                            stuck = True
                        time.sleep(off_s)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
        if res.returncode != 0:
            raise FarmError("uhubctl failed: %s" % res.stderr.strip())
        if stuck:
            raise FarmError("%s: still on the bus %g s after its port went off (the hub does "
                            "not cut VBUS, or the board has another supply)"
                            % (self.name, off_timeout))
        return self.wait_ready() if wait else 0

    def _wait_gone(self, timeout, interval=0.2):
        """True once the board has left the bus (no sysfs device at its USB path)."""
        deadline = time.monotonic() + timeout
        while self.present():
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)
        return True

    def wait_ready(self, timeout=None, need_mount=True):
        """Wait for tty (+ mount), then the settle time. Returns seconds taken or None."""
        timeout = timeout or 60 + self.power.get("settle", 12)
        need_mount = need_mount and self.fs == "drive"
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

    def line_reset(self):
        """Pulse EN through the console's RTS with DTR (IO0) released: a clean boot, not the
        ROM bootloader. Opening the port asserts both, which the auto-reset circuit ignores."""
        path = self.tty()
        if not path:
            raise FarmError("%s: no tty for an RTS reset" % self.name)
        s = serial.Serial(path, 115200, timeout=0.1, write_timeout=5)
        try:
            s.dtr = False
            s.rts = True
            time.sleep(0.2)
            s.rts = False
        finally:
            s.close()

    def repl_reset(self):
        """microcontroller.reset() from a synced friendly prompt; returns once it is sent."""
        with self.repl() as r:
            r.prompt()
            r.write(b"import microcontroller; microcontroller.reset()\r\n")
            time.sleep(0.5)

    def add_missing_settings(self):
        """Append the farm.toml settings keys settings.toml does not set. Returns the keys
        added. Only adds: a key the file already has is left as it is."""
        if not getattr(self, "settings", None):
            return []
        old = self.read_file("settings.toml", binary=True) or b""
        text = old.decode(errors="replace")
        missing = [k for k in sorted(self.settings)
                   if not re.search(r"^\s*%s\s*=" % re.escape(k), text, re.M)]
        if not missing:
            return []
        lines = []
        for k in missing:
            v = self.settings[k]
            lines.append("%s = %s" % (k, ("true" if v else "false") if isinstance(v, bool)
                                       else str(v) if isinstance(v, int)
                                       else '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')))
        sep = b"" if not old or old.endswith(b"\n") else b"\n"
        self.write_file("settings.toml", old + sep + ("\n".join(lines) + "\n").encode())
        return missing

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
        if self.fs != "drive":
            raise FarmError("%s: board cannot hash files (no REPL answer or no hashlib), "
                            "cannot verify the restore" % self.name)
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
            buf = bytearray(cpstrings.normalize(buf))  # translated lines, extra CRs
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
        return cpstrings.normalize(bytes(buf))

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
        extended = False
        while time.time() < end:
            try:
                self.write(b"\r\x03\x03")
            except serial.SerialTimeoutException as e:
                # An ESP32 USB-JTAG console takes no input while the chip boots (Feather
                # ESP32-C6: 2.6 s to 10 s after a reset) or sits in a blocking WiFi join (the
                # ESP32-C5 took 18 s from reset to code.py): poke again, with 25 s more once.
                tail = str(e).encode()
                if not extended:
                    end, extended = end + 25, True
                time.sleep(0.5)
                continue
            try:
                self.read_until(b">>> ", timeout=1.5)
            except FarmError as e:
                tail = str(e).encode()[-200:]
                continue
            start = len(self.transcript)
            self.drain(quiet=0.5, limit=2)
            later = cpstrings.normalize(bytes(self.transcript[start:]))
            if b"soft reboot" in later or b"Press any key" in later or b"Code done" in later:
                continue
            return
        raise FarmError("timeout waiting for b'>>> '; last %r" % tail)

    def raw_repl_wait(self):
        """Seconds to wait for the raw REPL banner after Ctrl-A. A REPL mode switch ends one VM
        and starts the next (main.c run_repl), and both ends call supervisor_workflow_reset()
        (main.c:166, :453), whose web workflow does a blocking connect() with an 8 s timeout
        (web_workflow.c:329). When settings.toml names a network the board cannot join, Ctrl-A
        therefore takes two failed joins: 19.3 s on the S3 on bravo, 2026-10-17."""
        return 30 if "wifi" in getattr(self.board, "caps", ()) else 5

    def exec_raw(self, code, timeout=60):
        """Run code in the raw REPL. Returns (stdout, stderr) as str."""
        outs, err = self.exec_raw_pieces([code], timeout=timeout)
        return outs[0], err

    def exec_raw_pieces(self, pieces, timeout=60):
        """Run pieces in order inside ONE raw REPL session, so globals carry over (leaving the
        raw REPL drops them). Stops at the first piece with stderr. Returns ([stdout...], stderr)."""
        self.prompt()
        self.write(b"\x01")
        self.read_until(b"raw REPL; CTRL-B to exit\r\n>", timeout=self.raw_repl_wait())
        end = time.time() + timeout
        outs, err = [], ""
        # A UART console has no flow control, and the board drains its receive FIFO only now
        # and then: the S31 Korvo-1 garbled a 421-byte script 2 of 6 times at 4 KB/s and at
        # 2 KB/s, never at 1 KB/s (6 of 6). So console_baud boards get about baud/144 bytes a
        # second (800 at 115200), in 32-byte writes.
        baud = getattr(self.board, "console_baud", None)
        step, pause = (32, 32 * 144.0 / baud) if baud else (256, 0.01)
        for code in pieces:
            data = code.encode() if isinstance(code, str) else code
            for i in range(0, len(data), step):
                self.write(data[i:i + step])
                time.sleep(pause)
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
        does not fit the heap. A board whose console is a UART (console_baud) gets pieces too.
        On the SiWx917 DK one paste past about 1.6 KB came back as a SyntaxError or never
        finished, while ~1 KB pieces always worked. The cause turned out to be on the board
        (growing a heap block past a few KB corrupts memory: 2000 list.append()s raised
        AttributeError 'list' has no 'LED'), and the raw REPL's input buffer is such a block;
        small pieces keep it small.
        """
        small = "small" in getattr(self.board, "caps", ())
        if not small and not getattr(self.board, "console_baud", None):
            out, err = self.exec_raw(device_source(body), timeout=timeout)
            return parse_results(out), out, err
        if small:
            pieces = [device_source("")] + ["import gc\ngc.collect()\n" + c
                                            for c in split_source(body, limit=600)]
        else:
            pieces = split_source(device_source(""), limit=600) + split_source(body, limit=600)
        outs, err = self.exec_raw_pieces(pieces, timeout=timeout)
        out = "".join(outs)
        return parse_results(out), out, err

    def soft_reboot(self, timeout=20):
        self.prompt()
        self.write(b"\x04")
        return self.read_until(b"soft reboot", timeout=timeout)

    # ---------- files over the raw REPL, for boards with no CIRCUITPY drive ----------
    # Python can write the filesystem when no USB host holds it: filesystem.c:258
    # filesystem_is_writable_by_python() (circuitpython 30d7addb4e). Needs binascii, so not
    # for the Metro M0, which keeps its drive. Nothing here resets or reloads the board.

    def put_file(self, rel, data, chunk=768):
        """Write data (bytes or str) to /rel on the board, `chunk` bytes per raw REPL piece,
        base64 encoded. Parent folders are made. Raises FarmError unless the board reports the
        full size back."""
        if isinstance(data, str):
            data = data.encode()
        head = "PATH = %r\n" % ("/" + rel.lstrip("/"),) + _PUT_OPEN
        pieces = [head]
        for i in range(0, len(data), chunk):
            b64 = base64.b64encode(data[i:i + chunk]).decode()
            pieces.append("f.write(binascii.a2b_base64(%r))\n" % b64)
        pieces.append(_PUT_CLOSE)
        outs, err = self.exec_raw_pieces(pieces, timeout=30 + len(data) // 500)
        if err.strip():
            raise FarmError("%s: put_file %s failed: %s" % (self.board.name, rel, err.strip()[-300:]))
        m = re.search(r"CPRP (\d+)", outs[-1] if len(outs) == len(pieces) else "")
        if not m or int(m.group(1)) != len(data):
            raise FarmError("%s: put_file %s: board reports %s bytes, sent %d"
                            % (self.board.name, rel, m.group(1) if m else "no size", len(data)))

    def get_file(self, rel, timeout=60):
        """Bytes of /rel on the board, or None when it does not exist."""
        code = "PATH = %r\n" % ("/" + rel.lstrip("/"),) + _GET_FILE
        out, err = self.exec_raw(code, timeout=timeout)
        if err.strip():
            raise FarmError("%s: get_file %s failed: %s" % (self.board.name, rel, err.strip()[-300:]))
        lines = [ln.strip() for ln in out.splitlines()]
        if "CPRG MISSING" in lines:
            return None
        if "CPRG BEGIN" not in lines or "CPRG END" not in lines:
            raise FarmError("%s: get_file %s: truncated reply" % (self.board.name, rel))
        body = lines[lines.index("CPRG BEGIN") + 1:lines.index("CPRG END")]
        return b"".join(base64.b64decode(ln) for ln in body)

    def del_file(self, rel):
        """Remove /rel on the board; a missing file is not an error."""
        code = "import os\ntry:\n    os.remove(%r)\nexcept OSError:\n    pass\n" % ("/" + rel.lstrip("/"),)
        _, err = self.exec_raw(code, timeout=15)
        if err.strip():
            raise FarmError("%s: del_file %s failed: %s" % (self.board.name, rel, err.strip()[-300:]))


class WebFs:
    """Board files over the web workflow's /fs/ API (supervisor/shared/web_workflow/
    web_workflow.c:1239-1407, circuitpython 30d7addb4e). Basic auth with an empty user. PUT
    answers 201 or 204, and 409 while a USB host holds the drive; every PUT starts an
    autoreload (web_workflow.c:1391)."""

    def __init__(self, board, base, password=WEB_API_PASSWORD, timeout=15):
        self.board = board
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode((":" + password).encode()).decode()
        self.timeout = timeout

    def _req(self, method, rel, data=None, headers=None):
        url = self.base + "/fs/" + urllib.parse.quote(rel.lstrip("/"))
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, OSError) as e:
            raise FarmError("%s: %s %s: %s" % (self.board.name, method, url, e)) from e

    def _fail(self, what, rel, status):
        why = " (a USB host holds the drive)" if status == 409 else ""
        raise FarmError("%s: web %s %s: HTTP %s%s" % (self.board.name, what, rel, status, why))

    def authorized(self):
        """True when the board takes this password: GET /fs/ as JSON answers 200."""
        try:
            st, _ = self._req("GET", "/", headers={"Accept": "application/json"})
        except FarmError:
            return False
        return st == 200

    def put(self, rel, data):
        if isinstance(data, str):
            data = data.encode()
        parent = os.path.dirname(rel.lstrip("/"))
        if parent:
            st, _ = self._req("PUT", parent + "/")
            if st not in (201, 204):
                self._fail("mkdir", parent, st)
        st, _ = self._req("PUT", rel, data, {"X-Timestamp": str(int(time.time() * 1000))})
        if st not in (201, 204):
            self._fail("put", rel, st)

    def get(self, rel):
        st, body = self._req("GET", rel)
        if st == 404:
            return None
        if st != 200:
            self._fail("get", rel, st)
        return body

    def delete(self, rel):
        st, _ = self._req("DELETE", rel)
        if st not in (204, 404):
            self._fail("delete", rel, st)


def _avahi_circuitpython(timeout=8):
    """[(hostname, ip, port)] of every _circuitpython._tcp service avahi resolves now."""
    try:
        out = subprocess.run(["avahi-browse", "-rtp", "_circuitpython._tcp"],
                             capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        f = line.split(";")
        if len(f) >= 9 and f[0] == "=" and f[2] == "IPv4":
            found.append((f[6].lower(), f[7], f[8]))
    return found


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
