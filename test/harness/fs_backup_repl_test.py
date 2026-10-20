# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""flashed_fs on a board with no drive (fs = "repl"): apply_tree writes only what changed,
FsBackup keeps the originals on the host and puts them back through the board."""
import hashlib

import pytest

from fixtures import flashing

pytestmark = pytest.mark.smoke


class FilesBoard:
    """An fs = "repl" board whose files are a dict; never mounts."""

    fs = "repl"
    caps = set()
    name = "fake_c6"

    def __init__(self, files):
        self.files = dict(files)
        self.writes = []

    def mount(self, *a, **k):
        pytest.fail("a repl board never mounts")

    def read_file(self, rel, binary=False):
        return self.files.get(rel)

    def write_file(self, rel, data, sync=True):  # pylint: disable=unused-argument
        self.writes.append(rel)
        self.files[rel] = data

    def remove_file(self, rel):
        self.files.pop(rel, None)

    def board_sha256(self, rels, timeout=5):  # pylint: disable=unused-argument
        return {r: hashlib.sha256(self.files[r]).hexdigest() if r in self.files else "MISSING"
                for r in rels}


@pytest.fixture
def backups_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(flashing, "BACKUPS", str(tmp_path / "fs-backup"))
    return tmp_path / "fs-backup"


def test_apply_tree_writes_only_changes_code_py_last(backups_dir):  # pylint: disable=unused-argument
    b = FilesBoard({"settings.toml": b"same", "code.py": b"old"})
    bk = flashing.FsBackup(b)
    tree = {"settings.toml": b"same", "code.py": b"new", "lib/x.py": b"x"}
    assert flashing.apply_tree(b, tree, bk) == ["code.py", "lib/x.py"]
    assert b.writes == ["lib/x.py", "code.py"]
    assert bk.saved == {"code.py": b"old", "lib/x.py": None}


def test_restore_puts_originals_back_and_removes_new_files(backups_dir):
    b = FilesBoard({"code.py": b"old"})
    bk = flashing.FsBackup(b)
    flashing.apply_tree(b, {"code.py": b"new", "lib/x.py": b"x"}, bk)
    assert (backups_dir / "fake_c6" / "files" / "code.py").read_bytes() == b"old"
    assert bk.restore() == []
    assert b.files == {"code.py": b"old"}
    assert not (backups_dir / "fake_c6").exists()


def test_restore_reports_a_write_that_did_not_stick(backups_dir):  # pylint: disable=unused-argument
    b = FilesBoard({"code.py": b"old"})
    bk = flashing.FsBackup(b)
    flashing.apply_tree(b, {"code.py": b"new"}, bk)
    b.write_file = lambda rel, data, sync=True: None  # the board ignores the restore
    assert bk.restore() == ["fake_c6: code.py sha256 mismatch after restore"]
