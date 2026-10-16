# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/farmlib.py restore and reset checks, offline: Board.preserved() verifies on the board
(sha256 over the REPL) and falls back to a host md5 only when the board cannot answer;
Board.wait_after_reset() needs the boot marker in boot_out.txt, not a re-mount. The REPL, the
drive and the devnum are faked, so no farm board is touched."""
import hashlib

import pytest

import farmlib
from farmlib import Board, FarmError, _read_fresh

pytestmark = pytest.mark.smoke

CFG = {"name": "s2_test", "board_id": "unexpected_maker_feather_esp32s2", "usb": "3-3.1"}


class FakeRepl:
    """Stands in for farmlib.Repl: answers exec_raw_pieces with canned output or an error."""

    def __init__(self, out="", err=""):
        self.out, self.err = out, err

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def prompt(self, timeout=20):
        return None

    def exec_raw_pieces(self, pieces, timeout=60):
        return [self.out], self.err


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A Board whose CIRCUITPY is a temp dir, with no block devices and no real REPL."""
    board = Board(CFG, {})
    monkeypatch.setattr(board, "mount", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(board, "block_devs", lambda: [])
    monkeypatch.setattr(board, "repl", lambda iface=0: FakeRepl("", "no repl"))
    return board, tmp_path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def md5(data):
    return hashlib.md5(data).hexdigest()


def seed(root, **files):
    for rel, data in files.items():
        (root / rel).write_bytes(data)


# --- Board.preserved(): restore is verified on the board -------------------------------------


def test_preserved_passes_when_board_hash_matches(drive, monkeypatch):
    board, root = drive
    seed(root, **{"code.py": b"print(1)\n", "boot.py": b"", "settings.toml": b"a=1\n"})
    seen = {}

    def board_says(rels, timeout=5):
        seen["rels"] = list(rels)
        return {"code.py": sha(b"print(1)\n"), "boot.py": sha(b""), "settings.toml": sha(b"a=1\n")}

    monkeypatch.setattr(board, "board_sha256", board_says)
    with board.preserved():
        (root / "code.py").write_bytes(b"pass\n")  # the test overwrites code.py
    assert (root / "code.py").read_bytes() == b"print(1)\n"
    assert seen["rels"] == ["boot.py", "code.py", "settings.toml"]


def test_preserved_fails_loudly_when_board_differs_even_if_host_matches(drive, monkeypatch):
    """The ESP32-S2 case: the host reads back the restored bytes (stale cache), the board
    still has the test file, so the restore did not stick and must raise."""
    board, root = drive
    seed(root, **{"code.py": b"print(1)\n"})
    monkeypatch.setattr(board, "board_sha256",
                        lambda rels, timeout=5: {"code.py": sha(b"pass\n")})
    with pytest.raises(FarmError, match="did not stick on the board: code.py"):
        with board.preserved(files=("code.py",)):
            pass


def test_preserved_host_md5_only_when_board_cannot_answer(drive, monkeypatch):
    board, root = drive
    seed(root, **{"code.py": b"print(1)\n"})
    monkeypatch.setattr(board, "board_sha256", lambda rels, timeout=5: None)
    with board.preserved(files=("code.py",)):
        pass  # host bytes match the saved ones: passes via the md5 fallback

    board2, root2 = drive
    seed(root2, **{"code.py": b"print(1)\n"})
    monkeypatch.setattr(board2, "board_sha256", lambda rels, timeout=5: None)
    monkeypatch.setattr(board2, "write_file", lambda rel, data, sync=True: None)  # no restore
    with pytest.raises(FarmError, match="failed md5 check"):
        with board2.preserved(files=("code.py",)):
            (root2 / "code.py").write_bytes(b"print(2)\n")


def test_preserved_missing_file_is_removed_and_not_hashed(drive, monkeypatch):
    board, root = drive
    calls = []
    monkeypatch.setattr(board, "board_sha256", lambda rels, timeout=5: calls.append(rels))
    with board.preserved(files=("boot.py",)):
        (root / "boot.py").write_bytes(b"print('x')\n")
    assert not (root / "boot.py").exists()
    assert calls == []  # nothing to verify when the file did not exist before


# --- Board.board_sha256(): parsing and the fallback cases ------------------------------------


def test_board_sha256_parses_each_file(drive, monkeypatch):
    board, _ = drive
    digest = sha(b"abc")
    hexed = "".join("%02x" % c for c in hashlib.sha256(b"abc").digest())
    assert hexed == digest
    monkeypatch.setattr(board, "repl",
                        lambda iface=0: FakeRepl("CPRH /code.py %s\nCPRH /boot.py MISSING\n"
                                                 % hexed, ""))
    # MISSING is a value, so the verify step sees a mismatch for it
    assert board.board_sha256(["code.py", "boot.py"]) == {"code.py": digest, "boot.py": "MISSING"}
    monkeypatch.setattr(board, "repl",
                        lambda iface=0: FakeRepl("CPRH /code.py %s\n" % hexed, ""))
    assert board.board_sha256(["code.py", "boot.py"]) is None  # boot.py not reported
    monkeypatch.setattr(board, "repl",
                        lambda iface=0: FakeRepl("CPRH /code.py %s\n" % hexed, ""))
    assert board.board_sha256(["code.py"]) == {"code.py": digest}


def test_board_sha256_script_uses_hashlib_new(drive, monkeypatch):
    # CircuitPython's hashlib only exposes new(); hashlib.sha256 raises AttributeError on the board
    board, _ = drive
    sent = []

    class Capture(FakeRepl):
        def exec_raw_pieces(self, pieces, timeout=60):
            sent.extend(pieces)
            return [""], "boom"

    monkeypatch.setattr(board, "repl", lambda iface=0: Capture())
    assert board.board_sha256(["code.py"]) is None
    code = sent[0]
    assert "hashlib.new('sha256')" in code
    assert "hashlib.sha256" not in code


def test_board_sha256_none_on_stderr_or_no_repl(drive, monkeypatch):
    board, _ = drive
    monkeypatch.setattr(board, "repl",
                        lambda iface=0: FakeRepl("", "ImportError: no module named 'hashlib'"))
    assert board.board_sha256(["code.py"]) is None

    def no_tty(iface=0):
        raise FarmError("no tty at interface 0")

    monkeypatch.setattr(board, "repl", no_tty)
    assert board.board_sha256(["code.py"]) is None


# --- reading boot_out.txt -------------------------------------------------------------------


def test_read_fresh_reads_the_file_and_defaults_when_missing(tmp_path):
    (tmp_path / "boot_out.txt").write_text("Adafruit CircuitPython 10\n")
    assert _read_fresh(str(tmp_path / "boot_out.txt")) == "Adafruit CircuitPython 10"
    assert _read_fresh(str(tmp_path / "nope.txt"), "dflt") == "dflt"


def test_boot_out_is_empty_without_a_mount(drive, monkeypatch):
    board, _ = drive
    monkeypatch.setattr(board, "mount", lambda *a, **k: None)
    assert board.boot_out() == ""


# --- Board.wait_after_reset(): evidence is the marker, not the mount -------------------------


def test_wait_after_reset_needs_the_marker_on_a_same_devnum_reset(drive, monkeypatch):
    """S2 case: devnum does not change. Stale host text without the marker must not count."""
    board, _ = drive
    monkeypatch.setattr(board, "devnum", lambda: "7")
    monkeypatch.setattr(board, "fresh_boot_out", lambda: "old boot, no marker")
    secs, found = board.wait_after_reset("CPR_BOOT_x", prev_devnum="7", timeout=1)
    assert (secs, found) == (None, False)


def test_wait_after_reset_sees_marker_in_fresh_boot_out(drive, monkeypatch):
    board, _ = drive
    texts = iter(["stale", "stale", "boot.py: CPR_BOOT_x"])
    monkeypatch.setattr(board, "devnum", lambda: "7")
    monkeypatch.setattr(board, "fresh_boot_out", lambda: next(texts))
    secs, found = board.wait_after_reset("CPR_BOOT_x", prev_devnum="7", timeout=10)
    assert found is True and secs is not None and secs < 10


def test_wait_after_reset_reenumerated_but_no_marker_is_back_not_found(drive, monkeypatch):
    board, _ = drive
    monkeypatch.setattr(board, "devnum", lambda: "9")
    monkeypatch.setattr(board, "present", lambda: True)
    monkeypatch.setattr(board, "wait_ready", lambda timeout=None, need_mount=True: 0.1)
    monkeypatch.setattr(board, "fresh_boot_out", lambda: "no marker")
    secs, found = board.wait_after_reset("CPR_BOOT_x", prev_devnum="7", timeout=1)
    assert secs is not None and found is False


def test_board_boot_out_none_when_no_repl(drive, monkeypatch):
    board, _ = drive

    def no_tty(iface=0):
        raise FarmError("no tty at interface 0")

    monkeypatch.setattr(board, "repl", no_tty)
    assert board.board_boot_out() is None


def test_repl_write_turns_tcdrain_eio_into_farm_error(drive):
    """pyserial's flush() is a bare tcdrain(). On an S2 resetting mid-write it raised
    termios.error (EIO), which is not an OSError, so board_boot_out() let it escape and
    wait_after_reset() failed phase (d) of code_py_lifecycle_test instead of polling on."""
    import termios
    board, _ = drive

    class Dying:
        def write(self, b):
            return len(b)

        def flush(self):
            raise termios.error(5, "Input/output error")

    r = farmlib.Repl.__new__(farmlib.Repl)
    r.board, r.s = board, Dying()
    with pytest.raises(FarmError, match="write failed"):
        r.write(b"\r\x03")


def test_board_boot_out_none_when_the_write_hits_eio(drive, monkeypatch):
    """The same EIO inside board_boot_out() means "cannot answer yet", not a crash."""
    import termios
    board, _ = drive

    class Dying:
        def write(self, b):
            return len(b)

        def flush(self):
            raise termios.error(5, "Input/output error")

    class EioRepl(farmlib.Repl):
        def __init__(self):  # pylint: disable=super-init-not-called
            self.board, self.s = board, Dying()

    monkeypatch.setattr(board, "repl", lambda iface=0: EioRepl())
    assert board.board_boot_out() is None


def test_read_replaces_bytes_that_are_not_utf8(tmp_path):
    """Seen on the Metro ESP32-S2: the host kept boot_out.txt at its old 239 bytes while the
    board's file was 203, so the tail was stale flash (0xc7 ...). mount()'s board id check
    read it with _read() and raised UnicodeDecodeError, failing code_py_lifecycle phase (a)."""
    p = tmp_path / "boot_out.txt"
    p.write_bytes(b"Board ID:unexpected_maker_feather_esp32s2\r\n3Gs5\xc7t\xff\x8fs")
    text = farmlib._read(str(p))  # pylint: disable=protected-access
    assert "Board ID:unexpected_maker_feather_esp32s2" in text
    assert "�" in text
