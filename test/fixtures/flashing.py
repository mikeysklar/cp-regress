# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Flashing fixtures, after Brent's WipperSnapper harness: flashed_firmware and flashed_fs.

flashed_firmware  once per board per run, before its first module (the board fixture calls
                  ensure_firmware inside the board lock, ahead of the health check). With
                  --firmware DIR it flashes that board's image from DIR, unless the same image
                  is already running: the sha256 in history/flashed.json matches AND
                  boot_out.txt still reports the version that flash produced. A hash alone is
                  a proxy; the board is asked too. Without --firmware nothing is flashed.
                  Every flash records its phase timings (enter_bootloader_s, erase_s, write_s,
                  ready_s, total_s, after Brent's FlashState, firmware/conftest.py:367 @
                  cf64e03) in the run record and in history/flashed.json, and the run ends
                  with a one-line-per-board flash summary.

--flash-erase     off by default; needs --firmware. Erases the application area before the
                  write on the boards where flash.erase_refusal() says it is safe (esp_uf2,
                  SAMD and STM32 over SWD, RP2040/RP2350 only with flash.nuke_uf2 set), and
                  flashes even when the image is unchanged. Never on the nRF52840. Boards it
                  refuses are flashed as without it, and the record says why.
                  The erase wipes CIRCUITPY on the esp and RP boards (the SAMD, STM32 and
                  nRF boards keep theirs on external flash), so before erasing every file on
                  CIRCUITPY is copied to history/fs-backup/<board>-pre-erase/ and, once the
                  new firmware is up, put back and md5-checked; flashed_fs then works from
                  there as usual and restores its own originals at the end of the run. No
                  backup, no erase: a board whose drive cannot be read is not erased, and a
                  backup left over from a failed restore blocks the next erase until it is
                  restored or removed by hand.

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


def pytest_addoption(parser):
    """--flash-erase lives with the fixture that uses it; the rest are in the root conftest."""
    parser.getgroup("cp-regress").addoption(
        "--flash-erase", action="store_true",
        help="with --firmware: erase the application area before writing, on boards where that "
             "is safe (never nRF52840); CIRCUITPY is backed up and put back. Off by default")


def pytest_configure(config):
    if config.getoption("flash_erase") and not config.getoption("firmware"):
        raise pytest.UsageError("--flash-erase needs --firmware DIR (an erase with no write "
                                "leaves the board blank)")


def _erase_summary(er):
    """The short form of an EraseResult dict kept in history/flashed.json."""
    return {k: er.get(k) for k in ("done", "refused", "method", "region") if er.get(k)}


def ensure_firmware(config, b):
    """Flash b from --firmware if its image is not already the one running. Records the outcome
    in config.cpr_flashed[b.name]; a failure marks the board down for the rest of the run.

    With --flash-erase, boards that may be erased are backed up, erased, flashed and restored
    whether or not the image changed; the others take the normal path and the record says why.
    """
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
    erase = bool(config.getoption("flash_erase"))
    if erase:
        why = flash.erase_refusal(b, image)
        if why:
            rec["erase"] = {"done": False, "refused": why}
            erase = False
    snap = None
    if erase:
        snap = DriveSnapshot(b)
        t0 = time.time()
        why = snap.take()
        rec["backup_s"] = round(time.time() - t0, 2)
        if why:
            rec["erase"] = {"done": False, "refused": why}
            erase, snap = False, None
    with _locked(FLASHED):
        last = _load(FLASHED).get(b.name, {})
    try:
        running = b.version() if b.present() else None
    except farmlib.FarmError:
        running = None
    if (not config.getoption("flash_force") and not erase and last.get("sha256") == sha
            and running and running == last.get("version")):
        rec.update(action="unchanged", version=running, seconds=0.0)
        return rec
    log = []
    try:
        res = flash.flash(b, image, log=log.append, erase=erase)
    except (farmlib.FarmError, OSError, Exception) as e:  # pylint: disable=broad-except
        config.cpr_down[b.name] = "flash failed: %s | %s" % (e, " / ".join(log[-6:]))
        rec.update(log=log[-20:])
        if snap:
            rec["fs_snapshot"] = snap.dir
            config.cpr_down[b.name] += " | CIRCUITPY from before the erase is in " + snap.dir
        return rec
    version = res.version
    rec.update(action="flashed", version=version, seconds=round(res.total_s, 1), method=res.method,
               log=log[-20:], **res.timings())
    if res.erase is not None:
        rec["erase"] = res.as_record()["erase"]
    if snap:
        t0 = time.time()
        written, problems = snap.restore()
        rec.update(restore_s=round(time.time() - t0, 2), fs_restored=written)
        if problems:
            config.cpr_down[b.name] = "after --flash-erase: " + "; ".join(problems)
            rec["fs_snapshot"] = snap.dir
            return rec
        if "settings.toml" in written and "wifi" in b.caps:
            hard_reset(b)  # the supervisor read settings.toml before it was back
    commit = flash.expected_commit(image)
    if commit and ("g" + commit) not in (version or ""):
        config.cpr_down[b.name] = "flashed %s but boot_out.txt reports %s" % (rec["image"], version)
        rec["action"] = "wrong-version"
        return rec
    with _locked(FLASHED):
        data = _load(FLASHED)
        entry = {"sha256": sha, "image": rec["image"], "version": version,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": res.method}
        entry.update(res.timings())
        if rec.get("erase"):
            entry["erase"] = _erase_summary(rec["erase"])
        data[b.name] = entry
        with open(FLASHED, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
    return rec


class DriveSnapshot:
    """Every file on one board's CIRCUITPY, copied to the host before --flash-erase wipes it.

    Kept in history/fs-backup/<board>-pre-erase/ until restore() has put every file back and
    md5-checked it; only then is the copy removed. The manifest is written last, so a manifest
    on disk always means a complete copy of a drive that may since have been erased.
    """
    SKIP = {"boot_out.txt", "System Volume Information", ".fseventsd", ".Trashes",
            ".Spotlight-V100", ".metadata_never_index"}
    MAX_BYTES = 32 << 20

    def __init__(self, board):
        self.board = board
        self.dir = os.path.join(BACKUPS, board.name + "-pre-erase")
        self.manifest = os.path.join(self.dir, "manifest.json")

    def _walk(self, mp):
        """(files {rel: size}, dirs [rel]) on the drive, minus SKIP at the top level."""
        files, dirs = {}, []
        for root, dnames, fnames in os.walk(mp):
            rel_root = os.path.relpath(root, mp)
            if rel_root == ".":
                dnames[:] = [d for d in dnames if d not in self.SKIP]
                fnames = [f for f in fnames if f not in self.SKIP]
            else:
                dirs.append(rel_root)
            for fn in fnames:
                rel = os.path.normpath(os.path.join(rel_root, fn))
                files[rel] = os.path.getsize(os.path.join(root, fn))
        return files, dirs

    def take(self):
        """Copy the drive. Returns None, or why the board must not be erased."""
        b = self.board
        if os.path.exists(self.manifest):
            return ("a CIRCUITPY backup from an earlier --flash-erase is still in %s; put it back "
                    "or remove it before erasing again" % self.dir)
        try:
            mp = b.mount()
        except farmlib.FarmError as e:
            return "no CIRCUITPY backup, so no erase: %s" % e
        if not mp:
            return "no CIRCUITPY backup, so no erase: drive not mounted"
        try:
            files, dirs = self._walk(mp)
            if sum(files.values()) > self.MAX_BYTES:
                return "CIRCUITPY holds over %d MB; not backing it up for an erase" % (
                    self.MAX_BYTES >> 20)
            shutil.rmtree(self.dir, ignore_errors=True)
            md5s = {}
            for rel in files:
                with open(os.path.join(mp, rel), "rb") as f:
                    data = f.read()
                dest = os.path.join(self.dir, "files", rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
                md5s[rel] = _md5(data)
            with open(self.manifest, "w") as f:
                json.dump({"files": md5s, "dirs": dirs}, f, indent=1, sort_keys=True)
        except OSError as e:
            return "CIRCUITPY backup failed, so no erase: %s" % e
        return None

    def restore(self):
        """Make the drive match the backup: write what differs, remove what the fresh
        filesystem added. Returns ([paths written], [problems])."""
        b = self.board
        man = _load(self.manifest)
        mp = b.mount()
        if not mp:
            return [], ["%s: CIRCUITPY not mounted, pre-erase files left in %s"
                        % (b.name, self.dir)]
        written, problems = [], []
        want = man.get("files", {})
        for d in man.get("dirs", []):
            os.makedirs(os.path.join(mp, d), exist_ok=True)
        now, _ = self._walk(mp)
        for rel in now:
            if rel not in want:
                try:
                    os.remove(os.path.join(mp, rel))
                except OSError as e:
                    problems.append("%s: remove %s: %s" % (b.name, rel, e))
        # code.py last: a write to it starts an auto-reload, and the rest should already be there.
        for rel in sorted(want, key=lambda r: r == "code.py"):
            path = os.path.join(mp, rel)
            try:
                with open(path, "rb") as f:
                    if _md5(f.read()) == want[rel]:
                        continue
            except OSError:
                pass
            try:
                with open(os.path.join(self.dir, "files", rel), "rb") as f:
                    data = f.read()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                with open(path, "rb") as f:
                    if _md5(f.read()) != want[rel]:
                        problems.append("%s: %s md5 mismatch after restore" % (b.name, rel))
                written.append(rel)
            except OSError as e:
                problems.append("%s: restore %s: %s" % (b.name, rel, e))
        os.sync()
        if problems:
            problems.append("pre-erase files kept in " + self.dir)
        else:
            shutil.rmtree(self.dir, ignore_errors=True)
        return written, problems


# ---------------------------------------------------------------- flash summary

def _flash_rows(flashed):
    """Board -> the parts of its record the end-of-run summary shows."""
    keep = ("action", "version", "total_s") + flash.PHASES + ("erase",)
    return {name: {k: rec.get(k) for k in keep if rec.get(k) is not None}
            for name, rec in flashed.items() if rec.get("action") not in (None, "none")}


def pytest_sessionfinish(session):
    """xdist: each worker hands its boards' flash records to the controller."""
    config = session.config
    if hasattr(config, "workeroutput"):
        config.workeroutput["cpr_flash_rows"] = json.dumps(_flash_rows(config.cpr_flashed))


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):  # pylint: disable=unused-argument
    rows = getattr(node, "workeroutput", {}).get("cpr_flash_rows")
    if rows:
        node.config.cpr_flash_rows = dict(getattr(node.config, "cpr_flash_rows", {}),
                                          **json.loads(rows))


def pytest_terminal_summary(terminalreporter, config):
    """One line per board that --firmware looked at: what happened and where the time went."""
    rows = dict(getattr(config, "cpr_flash_rows", {}), **_flash_rows(config.cpr_flashed))
    if not rows:
        return
    terminalreporter.section("flashed_firmware")
    for name in sorted(rows):
        r = rows[name]
        line = "%-18s %-13s" % (name, r.get("action"))
        if "total_s" in r:
            line += " %5.1f s  (%s)" % (r["total_s"], ", ".join(
                "%s %.1f" % (k[:-2], r[k]) for k in flash.PHASES if r.get(k)))
        er = r.get("erase") or {}
        if er.get("refused"):
            line += "  erase refused: " + er["refused"]
        elif er.get("done"):
            line += "  erased " + er.get("region", "")
        terminalreporter.write_line(line)


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
        """Back up rel from the drive at mp, or, with mp None (an fs = "repl" board), from the
        board itself. Folders made on a repl board are not tracked, so not removed again."""
        if rel in self.saved:
            return
        if mp is None:
            self.save_bytes(rel, self.board.read_file(rel, binary=True))
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

    def save_bytes(self, rel, data):
        """Back up rel as data, already read from the board (None: it did not exist)."""
        if rel in self.saved:
            return
        if data is not None:
            dest = os.path.join(self.dir, "files", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        self.saved[rel] = data
        self._write_manifest()

    def restore(self):
        """Put every original back and md5-check it. Returns a list of problems."""
        b = self.board
        problems = []
        if b.fs != "drive":
            return self._restore_on_board()
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


    def _restore_on_board(self):
        """restore() for a board with no drive: write back through the board, then compare
        sha256 as the board reads it."""
        b = self.board
        problems = []
        for rel, data in self.saved.items():
            try:
                if data is None:
                    b.remove_file(rel)
                else:
                    b.write_file(rel, data)
            except (farmlib.FarmError, OSError) as e:
                problems.append("%s: restore %s: %s" % (b.name, rel, e))
        want = {rel: hashlib.sha256(d).hexdigest() for rel, d in self.saved.items()
                if d is not None}
        got = b.board_sha256(sorted(want)) if want else {}
        if got is None:
            problems.append("%s: cannot hash on the board, restore unverified" % b.name)
        else:
            problems += ["%s: %s sha256 mismatch after restore" % (b.name, rel)
                         for rel in sorted(want) if got[rel] != want[rel]]
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
    if b.fs != "drive":
        return _apply_tree_on_board(b, tree, backup)
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


def _apply_tree_on_board(b, tree, backup):
    """apply_tree() for a board with no drive: sha256 of every path in one REPL session
    decides what changed, then each changed file goes over b.write_file. No free-space check.
    An fs = "web" board whose workflow answers does all of it over one resolved WebFs."""
    w = b.web() if b.fs == "web" else None
    if w:
        changed = []
        for rel, data in sorted(tree.items()):
            old = w.get(rel)
            if old is None or _md5(old) != _md5(data):
                changed.append(rel)
                backup.save_bytes(rel, old)
        for rel in sorted(changed, key=lambda r: r == "code.py"):
            w.put(rel, tree[rel])
        return changed
    have = b.board_sha256(sorted(tree), timeout=30)
    if have is None:
        raise farmlib.FarmError("%s: cannot hash files on the board (no REPL or no hashlib)"
                                % b.name)
    changed = [rel for rel, data in sorted(tree.items())
               if have.get(rel) != hashlib.sha256(data).hexdigest()]
    for rel in sorted(changed, key=lambda r: r == "code.py"):
        backup.save(None, rel)
        b.write_file(rel, tree[rel])
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
    tree = desired_tree(request.module, dict(getattr(board, "settings", {}), **secrets),
                        request.config.getoption("bundle"))
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
