# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""code.py lifecycle: boot.py, run_reason, reset_reason, exceptions, double-start regression.

pytest version of tests/code_py_lifecycle.py. One module-scoped fixture per board does what the
original run() does, in the same order, inside board.preserved(): (a) a marker plus a
ValueError, (b) run_reason / reset_reason, (c) deep recursion and a huge allocation, (d) on
reset_ok boards, boot.py output after microcontroller.reset(). It records what each phase saw;
the tests below assert on that record. code.py and boot.py are restored
(sha256-checked on the board) and the board is reloaded once in the fixture's finally block.
Targets: #3502 (code.py started twice), #7459, #7578, #7060, #11287, #1537, #4728.
"""
import contextlib
import time

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(3502, 7459, 7578, 7060, 11287, 1537, 4728),
    pytest.mark.destructive,
]


def _rerun(board, code, seconds=8):
    """Write code.py, soft reboot from the REPL, capture what code.py prints, stop it again."""
    import time
    board.write_file("code.py", code)
    time.sleep(3)  # let any autoreload triggered by the write finish
    with board.repl() as r:
        r.prompt()
        r.write(b"\x04")
        out = r.read_for(seconds)
        r.prompt()  # interrupt whatever is still running, leave a clean REPL
    return out


def _phases(board, rec):
    """The body of the original run() inside board.preserved(); fills rec as it goes."""
    tag = hex(int(time.time()))[2:]

    # (a) marker exactly once (#3502 double start), traceback, no safe mode
    rec["at"] = "a"
    marker = "CPR_MARK_" + tag
    rec["marker"] = marker
    rec["a"] = _rerun(board, 'print("%s")\nraise ValueError("cpr")\n' % marker)

    # (b) run_reason / reset_reason readable
    rec["at"] = "b"
    rec["b"] = _rerun(board, "import supervisor, microcontroller\n"
                             "print('CPR_RUN', supervisor.runtime.run_reason)\n"
                             "print('CPR_RESET', microcontroller.cpu.reset_reason)\n", seconds=5)

    # (c) deep recursion -> RuntimeError, huge allocation -> MemoryError, then the
    #     supervisor still prints its own "Code done running"
    rec["at"] = "c"
    rec["c"] = _rerun(board, "def f(n):\n    return f(n + 1)\n"
                             "try:\n    f(0)\n    print('CPR_REC none')\n"
                             "except RuntimeError as e:\n    print('CPR_REC ok', e)\n"
                             "try:\n    x = bytearray(10 ** 9)\n    print('CPR_MEM none')\n"
                             "except MemoryError:\n    print('CPR_MEM ok')\n", seconds=10)

    # (d) boot.py output reaches boot_out.txt after a hard reset
    rec["at"] = "d"
    rec["reset_ok"] = "reset_ok" in board.caps
    if rec["reset_ok"]:
        bmark = "CPR_BOOT_" + tag
        board.write_file("boot.py", 'print("%s")\n' % bmark)
        board.write_file("code.py", "pass\n")
        time.sleep(3)
        devnum = board.devnum()
        with board.repl() as r:
            r.prompt()
            r.write(b"import microcontroller; microcontroller.reset()\r")
        time.sleep(2)
        # S2 can reset with only a USB bus reset (same devnum): wait for bmark in boot_out.txt
        # as the board reads it, not for the drive to re-mount.
        ready, found = board.wait_after_reset(bmark, prev_devnum=devnum)
        rec["ready"] = ready
        if ready is not None:
            rec["d_boot"] = found
    rec["d"] = True
    rec["at"] = None


@pytest.fixture(scope="module")
def lifecycle(board):
    """Run phases (a)-(d) once per board; restore files and reload in finally.

    Errors inside the phases, or while backing up the files, are kept in the record
    (rec["error"], rec["at"]) so the first test of the phase that broke fails with them and the
    later ones skip as not reached. A failed restore (md5 mismatch) surfaces as one teardown
    error.
    """
    rec = {"error": None, "at": None}
    stack = contextlib.ExitStack()
    entered = False
    try:
        stack.enter_context(board.preserved())
        entered = True
        _phases(board, rec)
    except Exception as e:  # pylint: disable=broad-except
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        if rec["at"] is None:
            rec["at"] = "a"  # backing up the files failed: charge it to the first test
    try:
        yield rec
    finally:
        if entered:
            try:
                stack.close()  # restore code.py / boot.py / settings.toml (sha256 on the board)
            finally:
                # restore: files are back; reload once so the board runs its normal code.py
                try:
                    time.sleep(3)
                    with board.repl() as r:
                        r.prompt()
                        r.write(b"\x04")
                except Exception:  # pylint: disable=broad-except
                    # after a phase error this is already charged once (and the board
                    # fixture's health check power-cycles a board that stopped answering)
                    if not rec["error"]:
                        raise


def _out(rec, phase, first=False):
    """What phase printed; fail (first check of the phase) or skip if it never finished."""
    if phase in rec:
        return rec[phase]
    if rec["error"] and rec["at"] == phase and first:
        pytest.fail("phase (%s) raised %s" % (phase, rec["error"]), pytrace=False)
    pytest.skip("not reached: %s" % (rec["error"] or "phase (%s) did not run" % phase)[:160])
    return None


# (a)

def test_code_py_runs(lifecycle):
    """The code.py marker is printed after Ctrl-D."""
    out = _out(lifecycle, "a", first=True)
    n = out.count(lifecycle["marker"].encode())
    assert n > 0, "marker seen %d times" % n


def test_no_double_start(lifecycle):
    """The marker is printed exactly once (#3502)."""
    out = _out(lifecycle, "a")
    n = out.count(lifecycle["marker"].encode())
    assert n == 1, "marker seen %d times (#3502)" % n


def test_exception_traceback(lifecycle):
    """The ValueError shows up as a normal traceback."""
    out = _out(lifecycle, "a")
    assert b"Traceback" in out and b"ValueError: cpr" in out, out.decode(errors="replace")[-200:]


def test_code_done_message(lifecycle):
    """The supervisor prints "Code done running" after the exception."""
    out = _out(lifecycle, "a")
    assert b"Code done running" in out, "printed by the supervisor"


def test_no_safe_mode(lifecycle):
    """An uncaught exception does not put the board in safe mode."""
    out = _out(lifecycle, "a")
    assert b"safe mode" not in out.lower(), ""


# (b)

def test_run_reason(lifecycle):
    """supervisor.runtime.run_reason prints as supervisor.RunReason.*."""
    text = _out(lifecycle, "b", first=True).decode(errors="replace")
    assert "CPR_RUN supervisor.RunReason." in text, text[-200:]


def test_reset_reason(lifecycle):
    """microcontroller.cpu.reset_reason prints as microcontroller.ResetReason.*."""
    text = _out(lifecycle, "b").decode(errors="replace")
    assert "CPR_RESET microcontroller.ResetReason." in text, text[-200:]


# (c)

def test_recursion_raises(lifecycle):
    """Unbounded recursion raises RuntimeError instead of crashing."""
    out = _out(lifecycle, "c", first=True)
    assert b"CPR_REC ok" in out, out.decode(errors="replace")[-200:]


def test_memoryerror_raises(lifecycle):
    """bytearray(10 ** 9) raises MemoryError."""
    out = _out(lifecycle, "c")
    assert b"CPR_MEM ok" in out, out.decode(errors="replace")[-200:]


def test_survives_exceptions(lifecycle):
    """The supervisor still prints "Code done running" afterwards."""
    out = _out(lifecycle, "c")
    assert b"Code done running" in out, ""


# (d)

def test_hard_reset_back(lifecycle):
    """After microcontroller.reset() from the REPL the board comes back (reset_ok boards)."""
    if lifecycle.get("reset_ok") is False:
        pytest.skip("board lacks reset_ok")
    if "ready" not in lifecycle:
        _out(lifecycle, "d", first=True)
    ready = lifecycle["ready"]
    assert ready is not None, "%s s" % ready


def test_boot_py_in_boot_out(lifecycle):
    """boot.py's print reaches boot_out.txt after the hard reset."""
    if lifecycle.get("reset_ok") is False:
        pytest.skip("board lacks reset_ok")
    if "d_boot" not in lifecycle:
        if lifecycle.get("ready", 0) is None:
            pytest.skip("not reached: board did not come back after the hard reset")
        # charge an error here only if it happened after test_hard_reset_back's part passed
        _out(lifecycle, "d", first="ready" in lifecycle)
    assert lifecycle["d_boot"], ""
