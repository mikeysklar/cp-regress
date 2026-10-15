#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Hardware-free self-test of the harness's REPL protocol.

Runs a fake CircuitPython REPL on a pseudo-terminal that follows shared/runtime/pyexec.c:
"Press any key" eats the first byte, Ctrl-C gives KeyboardInterrupt and '>>> ', Ctrl-A enters
the raw REPL ('raw REPL; CTRL-B to exit\r\n>'), Ctrl-D executes and answers
'OK' + stdout + '\x04' + stderr + '\x04' + '>', Ctrl-B returns to the friendly REPL.
Device code runs in CPython, which is enough to test framing, result parsing and
error handling in lib/farmlib.py and lib/devtest.py.

  python3 tools/selftest.py        (needs pyserial)
"""

import contextlib
import io
import os
import pty
import sys
import threading
import time
import traceback
import tty

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))
import farmlib  # noqa: E402


def fake_repl(fd, stop):
    def out(b):
        os.write(fd, b if isinstance(b, bytes) else b.encode())

    out("code.py output:\r\nhello\r\n\r\nCode done running.\r\n\r\n"
        "Press any key to enter the REPL. Use CTRL-D to reload.\r\n")
    mode = "waiting"
    buf = bytearray()
    while not stop.is_set():
        try:
            data = os.read(fd, 1024)
        except OSError:
            return
        for c in data:
            c = bytes([c])
            if mode == "waiting":           # first keystroke is eaten
                mode = "friendly"
                out("\r\nAdafruit CircuitPython 11.0.0-alpha.1 on 2026-10-13; Fake Board\r\n>>> ")
                continue
            if mode == "friendly":
                if c == b"\x03":
                    out("\r\nKeyboardInterrupt\r\n>>> ")
                    buf.clear()
                elif c == b"\x01":
                    mode = "raw"
                    buf.clear()
                    out("\r\nraw REPL; CTRL-B to exit\r\n>")
                elif c == b"\x04":
                    out("\r\nsoft reboot\r\n\r\ncode.py output:\r\nhello\r\n\r\nCode done running.\r\n")
                    mode = "waiting"
                    out("Press any key to enter the REPL. Use CTRL-D to reload.\r\n")
                elif c == b"\r":
                    line = buf.decode()
                    buf.clear()
                    out("\r\n")
                    try:
                        r = eval(line, {}) if line.strip() else None
                        if r is not None:
                            out(repr(r) + "\r\n")
                    except Exception as e:  # noqa: BLE001
                        out("%s: %s\r\n" % (type(e).__name__, e))
                    out(">>> ")
                else:
                    buf += c
                    out(c)
            elif mode == "raw":
                if c == b"\x02":
                    mode = "friendly"
                    out("\r\n>>> ")
                elif c == b"\x03":
                    buf.clear()
                elif c == b"\x04":
                    code = buf.decode()
                    buf.clear()
                    so = io.StringIO()
                    se = ""
                    with contextlib.redirect_stdout(so):
                        try:
                            exec(code, {})
                        except Exception:  # noqa: BLE001
                            se = traceback.format_exc()
                    out("OK" + so.getvalue().replace("\n", "\r\n") + "\x04" + se.replace("\n", "\r\n") + "\x04>")
                else:
                    buf += c


class FakeBoard:
    """Just enough of farmlib.Board for Repl."""

    def __init__(self, path):
        self.name = "fake"
        self._path = path

    def ttys(self):
        return [self._path]


def main():
    if farmlib.serial is None:
        print("pyserial missing")
        return 2
    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)  # a real CDC tty is raw; a cooked pty echoes and blocks writes
    stop = threading.Event()
    th = threading.Thread(target=fake_repl, args=(master, stop), daemon=True)
    th.start()
    board = FakeBoard(os.ttyname(slave))
    fails = 0

    def expect(name, cond, detail=""):
        nonlocal fails
        print("%s %s %s" % ("ok  " if cond else "FAIL", name, detail))
        fails += 0 if cond else 1

    time.sleep(0.2)
    with farmlib.Repl(board) as r:
        r.prompt()
        expect("prompt_after_eaten_key", True)
        out, err = r.exec_raw("print('a')\nprint('b' * 3000)\n")
        expect("exec_raw_stdout", out == "a\r\n" + "b" * 3000 + "\r\n", repr(out[:20]))
        expect("exec_raw_no_stderr", err == "", repr(err))
        out, err = r.exec_raw("1/0\n")
        expect("exec_raw_stderr", "ZeroDivisionError" in err, repr(err[-60:]))
        res, out, err = r.run_device('check("a", 1 + 1 == 2, "q\\"x")\nmetric("m", 1.5, "ms")\n'
                                     'golden("g", 42)\nskip("s", "why")\ncheck("bad", False, "boom")\n')
        names = {x["name"]: x for x in res}
        expect("devtest_check", names.get("a", {}).get("ok") is True and names["a"]["detail"] == 'q"x')
        expect("devtest_metric", names.get("m", {}).get("kind") == "metric" and names["m"]["value"] == 1.5)
        expect("devtest_golden", names.get("g", {}).get("kind") == "golden")
        expect("devtest_skip", names.get("s", {}).get("ok") is None)
        expect("devtest_fail", names.get("bad", {}).get("ok") is False)
        r.write(b"6*7\r")
        tail = r.read_until(b">>> ", timeout=3)
        expect("friendly_eval", b"\r\n42\r\n" in tail, repr(tail[-30:]))
        r.write(b"\x04")
        r.read_until(b"soft reboot", timeout=3)
        r.prompt()
        expect("prompt_after_soft_reboot", True)
        try:
            r.read_until(b"never-printed", timeout=0.5)
            expect("read_until_timeout_raises", False)
        except farmlib.FarmError:
            expect("read_until_timeout_raises", True)
    stop.set()
    print("\n%d failures" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
