# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Flashing fixtures, after Brent's WipperSnapper harness: flashed_firmware and flashed_fs.

flashed_firmware  once per board per run, before its first module (the board fixture calls
                  ensure_firmware inside the board lock, ahead of the health check). With
                  --firmware DIR it flashes that board's image from DIR, unless the same image
                  is already running: the sha256 in history/flashed.json matches AND
                  boot_out.txt still reports the version that flash produced. A hash alone is
                  a proxy; the board is asked too. Without --firmware nothing is flashed.

flashed_fs        module scope. The files a module needs on CIRCUITPY: the idle code.py,
                  settings.toml from `secrets`, the module's FS = {...}, and the module's
                  LIBS = [...] from the pinned --bundle. His version builds a LittleFS image
                  because the Feather ESP32 V2 has no drive; ours mounts as CIRCUITPY, so the
                  "image" is a file list and only files whose bytes differ are copied.
                  Everything it replaces is backed up on first touch and restored, md5
                  checked, at the end of the run (unless --keep-fs).
"""
import fcntl
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager

import pytest

import farmlib
import flash

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLASHED = os.path.join(HERE, "history", "flashed.json")
BACKUPS = os.path.join(HERE, "history", "fs-backup")
IDLE_CODE = os.path.join(HERE, "data", "idle_code.py")


@contextmanager
def _locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ensure_firmware(config, b):
    """Flash b from --firmware if its image is not already the one running. Records the outcome
    in config.cpr_flashed[b.name]; a failure marks the board down for the rest of the run."""
    if b.name in config.cpr_flashed:
        return config.cpr_flashed[b.name]
    fw_dir = config.getoption("firmware")
    if not fw_dir:
        config.cpr_flashed[b.name] = {"action": "none"}
        return config.cpr_flashed[b.name]
    rec = {"action": "failed"}
    config.cpr_flashed[b.name] = rec
    image = flash.find_image(os.path.expanduser(fw_dir), b)
    if not image:
        config.cpr_down[b.name] = "no image for %s in %s" % (b.board_id, fw_dir)
        return rec
    sha = flash.sha256(image)
    rec.update(image=os.path.basename(image), sha256=sha)
    with _locked(FLASHED):
        last = _load(FLASHED).get(b.name, {})
    try:
        running = b.version() if b.present() else None
    except farmlib.FarmError:
        running = None
    if (not config.getoption("flash_force") and last.get("sha256") == sha
            and running and running == last.get("version")):
        rec.update(action="unchanged", version=running, seconds=0.0)
        return rec
    log = []
    t0 = time.time()
    try:
        version = flash.flash(b, image, log=log.append)
    except (farmlib.FarmError, OSError, Exception) as e:  # pylint: disable=broad-except
        config.cpr_down[b.name] = "flash failed: %s | %s" % (e, " / ".join(log[-6:]))
        rec.update(log=log[-20:])
        return rec
    rec.update(action="flashed", version=version, seconds=round(time.time() - t0, 1),
               log=log[-20:])
    commit = flash.expected_commit(image)
    if commit and ("g" + commit) not in (version or ""):
        config.cpr_down[b.name] = "flashed %s but boot_out.txt reports %s" % (rec["image"], version)
        rec["action"] = "wrong-version"
        return rec
    with _locked(FLASHED):
        data = _load(FLASHED)
        data[b.name] = {"sha256": sha, "image": rec["image"], "version": version,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(FLASHED, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
    return rec


@pytest.fixture(scope="module")
def flashed_firmware(request, board):
    """What happened to this board's firmware this run: action flashed / unchanged / none."""
    return request.config.cpr_flashed.get(board.name, {"action": "none"})


# ---------------------------------------------------------------- flashed_fs

def _bundle_files(bundle, names):
    """{relative path on CIRCUITPY: host path} for each library in the pinned bundle."""
    out = {}
    if not names:
        return out
    if not bundle:
        pytest.skip("module needs libraries %s; pass --bundle DIR" % ",".join(names))
    libdir = os.path.join(os.path.expanduser(bundle), "lib")
    for name in names:
        base = os.path.join(libdir, name)
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for fn in files:
                    p = os.path.join(root, fn)
                    out[os.path.relpath(p, os.path.dirname(libdir))] = p
            continue
        for ext in (".mpy", ".py"):
            if os.path.isfile(base + ext):
                out["lib/" + name + ext] = base + ext
                break
        else:
            pytest.fail("library %s not in bundle %s" % (name, bundle), pytrace=False)
    return out


def desired_tree(module, secrets, bundle):
    """{relative path: bytes} the module wants on CIRCUITPY."""
    with open(IDLE_CODE, "rb") as f:
        tree = {"code.py": f.read()}
    from fixtures.secrets import settings_toml  # pylint: disable=import-outside-toplevel
    tree["settings.toml"] = settings_toml(secrets).encode()
    for rel, data in getattr(module, "FS", {}).items():
        tree[rel] = data.encode() if isinstance(data, str) else data
    for rel, path in _bundle_files(bundle, getattr(module, "LIBS", [])).items():
        with open(path, "rb") as f:
            tree[rel] = f.read()
    return tree


def _md5(data):
    return hashlib.md5(data).hexdigest()


class FsBackup:
    """Originals of every path flashed_fs touched on one board, kept on the host."""

    def __init__(self, board):
        self.board = board
        self.dir = os.path.join(BACKUPS, board.name)
        self.saved = {}      # rel -> bytes, or None when the file did not exist
        self.new_dirs = []   # directories flashed_fs created, removed again on restore
        # A --keep-fs run leaves its backups on disk. Load them, so this run never backs up
        # an already-changed file over the true original.
        man = _load(os.path.join(self.dir, "manifest.json"))
        for rel, existed in man.get("saved", {}).items():
            if existed:
                try:
                    with open(os.path.join(self.dir, "files", rel), "rb") as f:
                        self.saved[rel] = f.read()
                except OSError:
                    continue  # backup file lost: back up again from the drive
            else:
                self.saved[rel] = None
        self.new_dirs = list(man.get("new_dirs", []))

    def _write_manifest(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "manifest.json"), "w") as f:
            json.dump({"saved": {r: d is not None for r, d in self.saved.items()},
                       "new_dirs": self.new_dirs}, f, indent=1, sort_keys=True)

    def save(self, mp, rel):
        if rel in self.saved:
            return
        path = os.path.join(mp, rel)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read()
            dest = os.path.join(self.dir, "files", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            self.saved[rel] = data
        else:
            self.saved[rel] = None
            d = os.path.dirname(rel)
            while d and not os.path.isdir(os.path.join(mp, d)):
                if d not in self.new_dirs:
                    self.new_dirs.append(d)
                d = os.path.dirname(d)
        self._write_manifest()

    def restore(self):
        """Put every original back and md5-check it. Returns a list of problems."""
        b = self.board
        problems = []
        mp = b.mount()
        if not mp:
            return ["%s: CIRCUITPY not mounted, originals left in %s" % (b.name, self.dir)]
        for rel, data in self.saved.items():
            path = os.path.join(mp, rel)
            try:
                if data is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    with open(path, "rb") as f:
                        if _md5(f.read()) != _md5(data):
                            problems.append("%s: %s md5 mismatch after restore" % (b.name, rel))
            except OSError as e:
                problems.append("%s: restore %s: %s" % (b.name, rel, e))
        for d in sorted(self.new_dirs, key=len, reverse=True):
            p = os.path.join(mp, d)
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        os.sync()
        if not problems:
            shutil.rmtree(self.dir, ignore_errors=True)
        return problems


def hard_reset(b):
    """Reset so the supervisor re-reads settings.toml. microcontroller.reset() where it is safe,
    a port power cycle where it is not (Metro M0)."""
    if "reset_ok" in b.caps:
        try:
            with b.repl() as r:
                r.prompt()
                r.write(b"import microcontroller\r\nmicrocontroller.reset()\r\n")
                time.sleep(1)
        except Exception:  # pylint: disable=broad-except
            pass  # the port vanishing is the reset working
        time.sleep(2)
        if b.wait_ready() is not None:
            time.sleep(b.power.get("settle", 12) / 2)
            return True
    b.power_cycle()
    time.sleep(b.power.get("settle", 12))
    return b.present()


def apply_tree(b, tree, backup):
    """Write the files of tree whose bytes differ from the drive. Returns the paths written."""
    mp = b.mount()
    if not mp:
        raise farmlib.FarmError("%s: CIRCUITPY not mounted" % b.name)
    changed = []
    for rel, data in sorted(tree.items()):
        path = os.path.join(mp, rel)
        try:
            with open(path, "rb") as f:
                if _md5(f.read()) == _md5(data):
                    continue
        except OSError:
            pass
        changed.append(rel)
    if not changed:
        return []
    need = sum(len(tree[r]) for r in changed)
    free = shutil.disk_usage(mp).free
    if need > free:
        raise farmlib.FarmError("%s: flashed_fs needs %d bytes, CIRCUITPY has %d free"
                                % (b.name, need, free))
    # code.py last: a write to it starts an auto-reload, and the rest should already be there.
    for rel in sorted(changed, key=lambda r: r == "code.py"):
        backup.save(mp, rel)
        b.write_file(rel, tree[rel], sync=False)
    os.sync()
    return changed


@pytest.fixture(scope="session")
def _fs_backups(request):
    """Board name -> FsBackup. Restores everything at the end of the run unless --keep-fs."""
    backups = {}
    yield backups
    if request.config.getoption("keep_fs"):
        return
    problems = []
    for name, bk in backups.items():
        if not bk.saved:
            continue
        b = bk.board
        try:
            settings_touched = "settings.toml" in bk.saved
            problems += bk.restore()
            if settings_touched and "wifi" in b.caps:
                hard_reset(b)
        except Exception as e:  # pylint: disable=broad-except
            problems.append("%s: restore failed: %s" % (name, e))
    if problems:
        # A teardown failure is reported as an error on the last test of the run.
        pytest.fail("flashed_fs restore: " + "; ".join(problems), pytrace=False)


@pytest.fixture(scope="module")
def flashed_fs(request, board, secrets, flashed_firmware, _fs_backups):  # pylint: disable=unused-argument
    """Put the module's files on CIRCUITPY, copying only what changed. Returns the paths it
    wrote this time (empty when the board already had them)."""
    tree = desired_tree(request.module, secrets, request.config.getoption("bundle"))
    bk = _fs_backups.setdefault(board.name, FsBackup(board))
    try:
        changed = apply_tree(board, tree, bk)
    except farmlib.FarmError as e:
        pytest.fail(str(e), pytrace=False)
    if "settings.toml" in changed and "wifi" in board.caps:
        if not hard_reset(board):
            pytest.fail("%s did not come back after the reset for settings.toml" % board.name,
                        pytrace=False)
    elif changed:
        time.sleep(1)  # let the auto-reload from the writes start before anyone opens the REPL
    return changed
