# SPDX-FileCopyrightText: 2026 Vladimír Smitka
# SPDX-License-Identifier: MIT-0
"""The CP Desktop Lab as a farm board (ports/desktop, github.com/MakerClassCZ/cp-desktop-lab).

A [[board]] with a `lab` table is a LabBoard: CIRCUITPY is a folder, the console a pseudo-terminal,
a power cycle a new process. Everything else (Repl, preserved(), the tests) is farmlib's own.

    [[board]]
    name = "desklab"
    board_id = "virtual"
    usb = "lab-1"                  # any unique name: it only names the lock files
    port = "desktop"
    caps = ["reset_ok"]
    power = { settle = 0 }
    lab = { bin = "~/lab/circuitpython", drive = "~/lab/CIRCUITPY", args = [] }

`bin` may be left out for CPR_LAB_BIN. The drive is created on first start, as a blank board's.

One keeper process per drive (`python3 lib/desklab.py serve ...`) runs the lab with its console
on pipes, relays it to a pseudo-terminal (the board's tty) and starts the lab again when it ends,
as a board comes back after its power is cut. Its files sit beside the drive in <drive>.cpr/:
keeper.pid, lab.pid, tty, api, log. `python3 lib/desklab.py stop <drive>` ends both.
"""

import fcntl
import json
import os
import pty
import secrets
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farmlib  # noqa: E402

# A board answers to its USB identity; the lab has none. Plainly not a real VID:PID.
LAB_VID_PID = ("0000", "0000")
# Console output the keeper holds for a reader before the lab has to wait.
HOLD = 16384


def _alive(pid):
    """A running process: not gone, and not a zombie waiting for the farm process that
    started it."""
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _put(path, text):
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


class LabBoard(farmlib.Board):
    on_hub = False

    def __init__(self, cfg, host_cfg):
        super().__init__(cfg, host_cfg)
        lab = cfg["lab"]
        self.lab_bin = os.path.expanduser(lab.get("bin") or os.environ.get("CPR_LAB_BIN", ""))
        self.lab_drive = os.path.abspath(os.path.expanduser(lab["drive"]))
        self.lab_args = [str(a) for a in lab.get("args", [])]
        self.state = self.lab_drive + ".cpr"

    def __repr__(self):
        return "<LabBoard %s @%s>" % (self.name, self.lab_drive)

    def _file(self, name):
        return os.path.join(self.state, name)

    def _keeper_alive(self):
        return _alive(_pid(self._file("keeper.pid")))

    def _made_of(self):
        """What a keeper runs: a changed program, args or keeper means a new one."""
        def stamp(path):
            try:
                st = os.stat(path)
                return [os.path.realpath(path), st.st_mtime_ns, st.st_size]
            except OSError:
                return [path]
        return json.dumps([stamp(self.lab_bin), self.lab_args, stamp(os.path.abspath(__file__))])

    def _current(self):
        try:
            with open(self._file("made_of")) as f:
                return self._keeper_alive() and f.read() == self._made_of()
        except OSError:
            return False

    def start(self, timeout=20):
        """Start the keeper unless one runs for this drive with this program; True once the
        console is there. FarmError when the lab will not start."""
        os.makedirs(self.state, exist_ok=True)
        with open(self._file("lock"), "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            if not self._current():
                if self._keeper_alive():
                    stop(self.state, wait=True)
                if not os.access(self.lab_bin, os.X_OK):
                    raise farmlib.FarmError("%s: no lab program at %r (lab.bin or CPR_LAB_BIN)"
                                            % (self.name, self.lab_bin))
                for name in ("tty", "api", "lab.pid", "failed"):
                    try:
                        os.remove(self._file(name))
                    except OSError:
                        pass
                with open(self._file("log"), "ab") as log:
                    keeper = subprocess.Popen(
                        [sys.executable, os.path.abspath(__file__), "serve",
                         self.state, self.lab_bin, self.lab_drive] + self.lab_args,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                # Before the lock goes: another process (an xdist worker) sees it running.
                _put(self._file("keeper.pid"), str(keeper.pid))
                _put(self._file("made_of"), self._made_of())
        end = time.time() + timeout
        while time.time() < end:
            if self._up() and os.path.isfile(os.path.join(self.lab_drive, "boot_out.txt")):
                return True
            if not self._keeper_alive():
                try:
                    with open(self._file("failed")) as f:
                        why = f.read().strip()
                except OSError:
                    why = "the keeper ended (%s)" % self._file("log")
                raise farmlib.FarmError("%s: the lab does not start: %s" % (self.name, why))
            time.sleep(0.1)
        return False

    def stop(self):
        stop(self.state, wait=True)

    # ---------- what the USB bus says for a real board ----------
    def _up(self):
        return (self._keeper_alive() and _alive(_pid(self._file("lab.pid")))
                and os.path.exists(self._file("tty")))

    def present(self):
        """The lab is plugged in from the first time the farm looks for it, which starts it
        (run.py and the pytest board fixture ask this before anything else). A lab that will
        not start raises FarmError with the reason, rather than looking unplugged."""
        if not self._current():
            self.start()
        return self._up()

    def vid_pid(self):
        return LAB_VID_PID if self.present() else None

    def devnum(self):
        """The lab's boot id: new after every reset, as a board's devnum is after it
        enumerates again."""
        info = self._api("/api/info")
        return str(info.get("boot")) if info else None

    def interfaces(self):
        return {}

    def ttys(self):
        if not self.present():
            return []
        try:
            with open(self._file("tty")) as f:
                return [f.read().strip()]
        except OSError:
            return []

    def block_devs(self):
        return []

    def mount(self, timeout=20, check_id=True):
        self.present()
        path = os.path.join(self.lab_drive, "boot_out.txt")
        if not os.path.isfile(path):
            return None
        if check_id and self.board_id not in (farmlib._read(path, "") or ""):
            raise farmlib.FarmError("%s: %s is a different board" % (self.name, self.lab_drive))
        return self.lab_drive

    def mount_readonly(self):
        return False

    def boot_volume(self, timeout=30):
        return None

    def touch1200(self):
        return False

    def mac(self):
        return None

    def line_reset(self):
        raise farmlib.FarmError("%s: the lab has no RTS reset" % self.name)

    # ---------- power ----------
    def power_cycle(self, off_s=3, wait=True, confirm_off=False, off_timeout=10):
        """End the lab: the keeper starts it again, from boot.py."""
        if not self._current():
            self.start()
        else:
            pid = _pid(self._file("lab.pid"))
            if pid:
                try:
                    os.remove(self._file("lab.pid"))
                except OSError:
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return self.wait_ready() if wait else 0

    def wait_ready(self, timeout=None, need_mount=True):
        timeout = timeout or 30
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.present() and self.tty() and (not need_mount or self.mount(timeout=1)):
                return time.time() - t0
            time.sleep(0.1)
        return None

    def _api(self, path):
        try:
            with open(self._file("api")) as f:
                url, token = f.read().split()
            req = urllib.request.Request(url.rstrip("/") + path,
                                         headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())
        except (OSError, ValueError):
            return None


def stop(state, wait=False):
    """End the keeper and its lab; with wait, until both are gone."""
    keeper = _pid(os.path.join(state, "keeper.pid"))
    lab = _pid(os.path.join(state, "lab.pid"))
    for pid, sig in ((keeper, signal.SIGTERM), (lab, signal.SIGKILL)):
        if pid and _alive(pid):
            try:
                os.kill(pid, sig)
            except OSError:
                pass
    end = time.time() + (5 if wait else 0)
    while time.time() < end and (_alive(keeper) or _alive(lab)):
        time.sleep(0.05)


def serve(state, binary, drive, args):
    """The keeper: the lab's console relayed to a pseudo-terminal, the lab started again
    whenever it ends."""
    _put(os.path.join(state, "keeper.pid"), str(os.getpid()))
    master, slave = pty.openpty()
    tty.setraw(slave)
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    fcntl.fcntl(master, fcntl.F_SETFL, fcntl.fcntl(master, fcntl.F_GETFL) | os.O_NONBLOCK)
    token = secrets.token_hex(16)
    env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy",
               CIRCUITPY_API_TOKEN=token)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *a: stopping.append(1))
    lab = None
    quick = 0
    try:
        while not stopping:
            for name in ("lab.pid", "api"):
                try:
                    os.remove(os.path.join(state, name))
                except OSError:
                    pass
            log_path = os.path.join(state, "lab.log")
            log = open(log_path, "ab")
            start = log.tell()
            began = time.time()
            lab = subprocess.Popen([binary, "--drive", drive, "--api", "0"] + args,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   env=env, start_new_session=True)
            log.close()
            api = _await_api(log_path, start, lab)
            if api:
                _put(os.path.join(state, "api"), "%s %s" % (api, token))
            _put(os.path.join(state, "tty"), os.ttyname(slave))
            # Last: a board is up once lab.pid is there, with its api and tty already.
            _put(os.path.join(state, "lab.pid"), str(lab.pid))
            out = lab.stdout.fileno()
            pending, moved = bytearray(), time.time()
            while not stopping:
                # A reader keeps up: the lab waits for it, as on a USB console. Nobody has read
                # for a second: what waits is dropped, and so is what the terminal holds.
                if len(pending) >= HOLD and time.time() - moved > 1:
                    pending.clear()
                    termios.tcflush(slave, termios.TCIFLUSH)
                try:
                    ready, writable, _ = select.select(
                        [master] + ([out] if len(pending) < HOLD else []),
                        [master] if pending else [], [], 0.5)
                except InterruptedError:
                    continue
                if out in ready:
                    data = os.read(out, 4096)
                    if not data:
                        break
                    pending += data
                if writable:
                    try:
                        n = os.write(master, pending)
                        del pending[:n]
                        moved = time.time()
                    except BlockingIOError:
                        pass
                if master in ready:
                    try:
                        data = os.read(master, 4096)
                    except (BlockingIOError, OSError):
                        data = b""
                    if data:
                        try:
                            lab.stdin.write(data)
                            lab.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
            if lab.poll() is None and stopping:
                break
            lab.wait()
            quick = quick + 1 if time.time() - began < 2 else 0
            if quick >= 5:
                with open(log_path, "rb") as f:
                    f.seek(start)
                    tail = f.read().decode(errors="replace").strip()[-400:]
                _put(os.path.join(state, "failed"), "exit status %s, five times in a row: %s"
                     % (lab.returncode, tail or "(nothing on stderr)"))
                break
            time.sleep(0.2)
    finally:
        if lab and lab.poll() is None:
            lab.kill()
        for name in ("tty", "api", "lab.pid", "keeper.pid"):
            try:
                os.remove(os.path.join(state, name))
            except OSError:
                pass


def _await_api(log_path, start, lab, timeout=10):
    """The API's address, from the line the lab prints on stderr at start."""
    import re
    end = time.time() + timeout
    while time.time() < end and lab.poll() is None:
        try:
            with open(log_path, "rb") as f:
                f.seek(start)
                hits = re.findall(rb"api (http://\S+) token", f.read())
            if hits:
                return hits[-1].decode()
        except OSError:
            pass
        time.sleep(0.05)
    return None


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
    elif len(sys.argv) == 3 and sys.argv[1] == "stop":
        stop(os.path.abspath(os.path.expanduser(sys.argv[2])) + ".cpr")
    else:
        print(__doc__)
        sys.exit(2)
