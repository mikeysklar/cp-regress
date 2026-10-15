# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""code.py lifecycle: boot.py, run_reason, reset_reason, exceptions, double-start regression."""

META = {
    "id": "code_py_lifecycle",
    "title": "code.py lifecycle, boot.py execution, run_reason, reset_reason, exceptions",
    "category": "Core",
    "tier": "smoke",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 180,
    "targets": [3502, 7459, 7578, 7060, 11287, 1537, 4728],
}


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


def run(board, ctx):
    """code.py lifecycle: one start per reload, tracebacks not safe mode, boot.py, reasons."""
    import time

    tag = hex(int(time.time()))[2:]
    with board.preserved():
        # (a) marker exactly once (#3502 double start), traceback, no safe mode
        marker = "CPR_MARK_" + tag
        out = _rerun(board, 'print("%s")\nraise ValueError("cpr")\n' % marker)
        n = out.count(marker.encode())
        ctx.check("code_py_runs", n > 0, "marker seen %d times" % n)
        ctx.check("no_double_start", n == 1, "marker seen %d times (#3502)" % n)
        ctx.check("exception_traceback", b"Traceback" in out and b"ValueError: cpr" in out,
                  out.decode(errors="replace")[-200:])
        ctx.check("code_done_message", b"Code done running" in out, "printed by the supervisor")
        ctx.check("no_safe_mode", b"safe mode" not in out.lower(), "")

        # (b) run_reason / reset_reason readable
        out = _rerun(board, "import supervisor, microcontroller\n"
                            "print('CPR_RUN', supervisor.runtime.run_reason)\n"
                            "print('CPR_RESET', microcontroller.cpu.reset_reason)\n", seconds=5)
        text = out.decode(errors="replace")
        ctx.check("run_reason", "CPR_RUN supervisor.RunReason." in text, text[-200:])
        ctx.check("reset_reason", "CPR_RESET microcontroller.ResetReason." in text, text[-200:])

        # (c) deep recursion -> RuntimeError, huge allocation -> MemoryError, then the
        #     supervisor still prints its own "Code done running"
        out = _rerun(board, "def f(n):\n    return f(n + 1)\n"
                            "try:\n    f(0)\n    print('CPR_REC none')\n"
                            "except RuntimeError as e:\n    print('CPR_REC ok', e)\n"
                            "try:\n    x = bytearray(10 ** 9)\n    print('CPR_MEM none')\n"
                            "except MemoryError:\n    print('CPR_MEM ok')\n", seconds=10)
        ctx.check("recursion_raises", b"CPR_REC ok" in out, out.decode(errors="replace")[-200:])
        ctx.check("memoryerror_raises", b"CPR_MEM ok" in out, out.decode(errors="replace")[-200:])
        ctx.check("survives_exceptions", b"Code done running" in out, "")

        # (d) boot.py output reaches boot_out.txt after a hard reset
        if "reset_ok" in board.caps:
            bmark = "CPR_BOOT_" + tag
            board.write_file("boot.py", 'print("%s")\n' % bmark)
            board.write_file("code.py", "pass\n")
            time.sleep(3)
            with board.repl() as r:
                r.prompt()
                r.write(b"import microcontroller; microcontroller.reset()\r")
            time.sleep(2)
            ready = board.wait_ready()
            ctx.check("hard_reset_back", ready is not None, "%s s" % ready)
            if ready is not None:
                ctx.check("boot_py_in_boot_out", bmark in board.boot_out(), "")
        else:
            ctx.skip("boot_py_in_boot_out", "board lacks reset_ok")

    # restore: files are back; reload once so the board runs its normal code.py
    time.sleep(3)
    with board.repl() as r:
        r.prompt()
        r.write(b"\x04")
