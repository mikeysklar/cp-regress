# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Repl.put_file / get_file / del_file against a fake raw REPL that runs the device code in
CPython, with the board's / mapped onto a temp folder."""
import binascii
import contextlib
import io
import os
import types

import pytest

import farmlib

pytestmark = pytest.mark.smoke


class FakeRawSerial:
    """Just enough of CircuitPython's REPL (pyexec.c raw mode) for exec_raw_pieces: Ctrl-C to a
    '>>> ' prompt, Ctrl-A into raw mode, code up to Ctrl-D answered with OK, stdout, \\x04,
    stderr, \\x04, '>'. Globals live for one raw session, as on the board."""

    def __init__(self, root):
        self.root = root
        self.out = bytearray()
        self.raw = False
        self.code = bytearray()
        self.env = None

    def _path(self, p):
        return os.path.join(self.root, p.lstrip("/"))

    def _fake_os(self):
        return types.SimpleNamespace(
            mkdir=lambda p: os.mkdir(self._path(p)),
            remove=lambda p: os.remove(self._path(p)),
            rename=lambda a, b: os.rename(self._path(a), self._path(b)),
            stat=lambda p: tuple(os.stat(self._path(p))),
        )

    def _globals(self):
        mods = {"os": self._fake_os(), "binascii": binascii}
        real_import = __import__

        def imp(name, *a, **k):
            return mods[name] if name in mods else real_import(name, *a, **k)

        def fopen(p, mode="r"):
            if os.path.isdir(self._path(p)):
                raise OSError(21, "is a directory")
            return open(self._path(p), mode)

        b = dict(__builtins__ if isinstance(__builtins__, dict) else vars(__builtins__))
        b["__import__"] = imp
        b["open"] = fopen
        return {"__builtins__": b}

    def write(self, data):
        for c in data:
            ch = bytes([c])
            if self.raw:
                if ch == b"\x04":
                    self._run()
                elif ch == b"\x02":
                    self.raw = False
                    self.out += b"\r\n>>> "
                else:
                    self.code += ch
            elif ch == b"\x03":
                self.out += b"\r\n>>> "
            elif ch == b"\x01":
                self.raw, self.env = True, self._globals()
                self.out += b"raw REPL; CTRL-B to exit\r\n>"

    def _run(self):
        src, self.code = bytes(self.code).decode(), bytearray()
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so):
            try:
                exec(src, self.env)  # pylint: disable=exec-used
            except Exception as e:  # pylint: disable=broad-except
                se.write("Traceback\r\n%s: %s\r\n" % (type(e).__name__, e))
        self.out += b"OK" + so.getvalue().encode() + b"\x04" + se.getvalue().encode() + b"\x04>"

    def flush(self):
        pass

    def read(self, n):
        got, self.out = bytes(self.out[:n]), self.out[n:]
        return got

    def close(self):
        pass


@pytest.fixture
def repl(tmp_path):
    r = farmlib.Repl.__new__(farmlib.Repl)  # no port opened
    r.board = types.SimpleNamespace(name="fake", caps=set())
    r.s = FakeRawSerial(str(tmp_path))
    r.transcript = bytearray()
    r.pending = bytearray()
    return r


def test_put_then_get_round_trips_binary(repl, tmp_path):
    data = bytes(range(256)) * 13 + b"\x00tail"  # several chunks, a short last one
    repl.put_file("lib/sub/blob.bin", data, chunk=500)
    assert (tmp_path / "lib" / "sub" / "blob.bin").read_bytes() == data
    assert not (tmp_path / "lib" / "sub" / "blob.bin.cprtmp").exists()
    assert repl.get_file("lib/sub/blob.bin") == data


def test_put_replaces_an_existing_file(repl, tmp_path):
    (tmp_path / "code.py").write_text("old contents, longer than the new ones\n")
    repl.put_file("code.py", "print(1)\n")
    assert (tmp_path / "code.py").read_text() == "print(1)\n"


def test_empty_file(repl, tmp_path):
    repl.put_file("empty.txt", b"")
    assert (tmp_path / "empty.txt").read_bytes() == b""
    assert repl.get_file("empty.txt") == b""


def test_get_missing_is_none(repl):
    assert repl.get_file("nope.py") is None


def test_del_file_and_missing_is_fine(repl, tmp_path):
    (tmp_path / "x.py").write_text("x")
    repl.del_file("x.py")
    assert not (tmp_path / "x.py").exists()
    repl.del_file("x.py")


def test_device_error_raises_and_keeps_the_old_file(repl, tmp_path):
    (tmp_path / "code.py").write_text("keep me")
    (tmp_path / "code.py.cprtmp").mkdir()  # open(... 'wb') on the board fails
    with pytest.raises(farmlib.FarmError, match="put_file code.py failed"):
        repl.put_file("code.py", "new")
    assert (tmp_path / "code.py").read_text() == "keep me"
