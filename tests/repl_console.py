# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""REPL console functionality: prompt, eval, interrupt, paste mode, large output, soft reboot."""

META = {
    "id": "repl_console",
    "title": "REPL console prompt, evaluation, interrupt, paste mode, large output, soft reboot",
    "category": "USB",
    "tier": "smoke",
    "needs": [],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [417, 2686, 1630, 3588, 4164, 6480, 10514, 9454],
}


PASTE_LINES = 75           # about 4 KB pasted (#417 broke pastes over 128 chars)
LARGE_LINES = 2000         # 2000 x 100 chars = about 200 KB of output (#2686, #1630)


def _paste_block():
    lines = ["def cpr_paste():", "    s = 0"]
    for i in range(PASTE_LINES):
        lines.append("    s = (s + %d) & 0xFFFFFF  # padding padding padding" % i)
    lines.append("    return s")
    lines.append("print('PASTE_RESULT', cpr_paste())")
    return ("\n".join(lines) + "\n").encode(), sum(range(PASTE_LINES)) & 0xFFFFFF


def run(board, ctx):
    """Friendly REPL behaviour a user hits first: eval, _, Ctrl-C, paste, big output, Ctrl-D."""
    import time

    ctx.check("present", board.present(), board.usb)
    with board.repl() as r:
        try:
            r.prompt(timeout=10)
            ctx.check("prompt_reachable", True)
        except Exception as e:  # noqa: BLE001
            ctx.check("prompt_reachable", False, str(e))
            return

        # eval and _ : match the result line, not the echoed input
        try:
            r.write(b"7*6\r")
            out = r.read_until(b">>> ", timeout=5)
            ctx.check("eval", b"\r\n42\r\n" in out, out.decode(errors="replace")[-120:])
            r.write(b"_\r")
            out = r.read_until(b">>> ", timeout=5)
            ctx.check("underscore_last_result", b"\r\n42\r\n" in out, out.decode(errors="replace")[-120:])
        except Exception as e:  # noqa: BLE001
            ctx.check("eval", False, str(e))

        # Ctrl-C interrupts time.sleep(30)
        try:
            r.write(b"import time; time.sleep(30)\r")
            time.sleep(1)
            t0 = time.time()
            r.write(b"\x03")
            r.read_until(b"KeyboardInterrupt", timeout=5)
            ms = (time.time() - t0) * 1000
            ctx.check("interrupt_ctrl_c", ms < 3000, "%.0f ms after Ctrl-C" % ms)
            ctx.metric("interrupt_ms", ms, "ms", better="lower")
            r.read_until(b">>> ", timeout=5)
        except Exception as e:  # noqa: BLE001
            ctx.check("interrupt_ctrl_c", False, str(e))
            r.prompt()

        # Paste mode: ~4 KB block, result must be computed, not just echoed
        try:
            block, expect = _paste_block()
            r.write(b"\x05")
            r.read_until(b"paste mode; Ctrl-C to cancel, Ctrl-D to finish", timeout=5)
            # Read the echo as we go, like a terminal does, so it never backs up.
            for i in range(0, len(block), 256):
                r.write(block[i:i + 256])
                time.sleep(0.02)
                r.pending += r._pull()
            r.write(b"\x04")
            want = ("PASTE_RESULT %d" % expect).encode()
            out = r.read_until(b">>> ", timeout=15)
            # the echoed source contains "PASTE_RESULT', cpr_paste()" but never the number
            ctx.check("paste_4k", want in out, "expected %r; tail %r" % (want, out[-120:]))
        except Exception as e:  # noqa: BLE001
            ctx.check("paste_4k", False, str(e))
            r.prompt()

        # Large output through the raw REPL: every line, in order
        try:
            body = "for i in range(%d):\n    print('L%%05d ' %% i + 'x' * 93)\n" % LARGE_LINES
            t0 = time.time()
            out, err = r.exec_raw(body, timeout=120)
            dt = max(time.time() - t0, 1e-6)
            nums = [int(l[1:6]) for l in out.splitlines() if l.startswith("L") and l[1:6].isdigit()]
            ctx.check("large_output_count", len(nums) == LARGE_LINES, "got %d/%d lines" % (len(nums), LARGE_LINES))
            ctx.check("large_output_order", nums == list(range(len(nums))), "lines in order")
            ctx.check("large_output_no_error", not err.strip(), err.strip()[-200:])
            ctx.metric("output_kBps", len(out) / 1024 / dt, "KB/s", better="higher")
        except Exception as e:  # noqa: BLE001
            ctx.check("large_output_count", False, str(e))

        # Ctrl-D soft reboot; code.py may run forever (farm idle sketch), so get back to the
        # REPL with prompt() and look for the banner there
        try:
            r.prompt()
            r.write(b"\x04")
            r.read_until(b"soft reboot", timeout=15)
            ctx.check("ctrl_d_soft_reboot", True, "")
            time.sleep(2)
            mark = len(r.transcript)
            r.prompt(timeout=15)
            tail = bytes(r.transcript[mark:])
            ctx.check("reboot_banner", b"Adafruit CircuitPython" in tail, tail.decode(errors="replace")[-160:])
        except Exception as e:  # noqa: BLE001
            ctx.check("ctrl_d_soft_reboot", False, str(e))
