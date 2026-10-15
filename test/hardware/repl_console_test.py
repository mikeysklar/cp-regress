# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""REPL console: prompt, eval, interrupt, paste mode, large output, soft reboot.

pytest version of tests/repl_console.py. Each check there is one test here, in the same order,
sharing one open REPL per board. Targets: #417 (pastes over 128 chars), #2686 and #1630
(console stops after lots of output), #3588, #4164, #6480, #10514, #9454.
"""
import time

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(417, 2686, 1630, 3588, 4164, 6480, 10514, 9454),
]

PASTE_LINES = 75           # about 4 KB pasted (#417 broke pastes over 128 chars)
LARGE_LINES = 2000         # 2000 x 100 chars = about 200 KB of output (#2686, #1630)


def _paste_block():
    lines = ["def cpr_paste():", "    s = 0"]
    for i in range(PASTE_LINES):
        lines.append("    s = (s + %d) & 0xFFFFFF  # padding padding padding" % i)
    lines.append("    return s")
    lines.append("print('PASTE_RESULT', cpr_paste())")
    return ("\n".join(lines) + "\n").encode(), sum(range(PASTE_LINES)) & 0xFFFFFF


@pytest.fixture(scope="module")
def repl(board):
    """One friendly-REPL session per board for the whole module."""
    with board.repl() as r:
        yield r


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_prompt_reachable(repl):
    """Ctrl-C reaches a >>> prompt."""
    repl.prompt(timeout=10)


def test_eval(repl):
    """7*6 prints 42 (matched on the result line, not the echoed input)."""
    repl.prompt()
    repl.write(b"7*6\r")
    out = repl.read_until(b">>> ", timeout=5)
    assert b"\r\n42\r\n" in out, out.decode(errors="replace")[-120:]


def test_underscore_last_result(repl):
    """_ holds the last result."""
    repl.write(b"_\r")
    out = repl.read_until(b">>> ", timeout=5)
    assert b"\r\n42\r\n" in out, out.decode(errors="replace")[-120:]


def test_interrupt_ctrl_c(repl, metric):
    """Ctrl-C stops time.sleep(30) within 3 s."""
    repl.prompt()
    repl.write(b"import time; time.sleep(30)\r")
    time.sleep(1)
    t0 = time.time()
    repl.write(b"\x03")
    try:
        repl.read_until(b"KeyboardInterrupt", timeout=5)
    finally:
        repl.prompt()
    ms = (time.time() - t0) * 1000
    assert ms < 3000, "%.0f ms after Ctrl-C" % ms
    metric("interrupt_ms", ms, "ms", better="lower")


def test_paste_4k(repl):
    """A ~4 KB paste-mode block computes and prints its result."""
    block, expect = _paste_block()
    repl.prompt()
    repl.write(b"\x05")
    repl.read_until(b"paste mode; Ctrl-C to cancel, Ctrl-D to finish", timeout=5)
    # Read the echo as we go, like a terminal does, so it never backs up.
    for i in range(0, len(block), 256):
        repl.write(block[i:i + 256])
        time.sleep(0.02)
        repl.pending += repl._pull()  # pylint: disable=protected-access
    repl.write(b"\x04")
    want = ("PASTE_RESULT %d" % expect).encode()
    out = repl.read_until(b">>> ", timeout=15)
    # the echoed source contains "PASTE_RESULT', cpr_paste()" but never the number
    assert want in out, "expected %r; tail %r" % (want, out[-120:])


@pytest.fixture(scope="module")
def large_output(repl):
    """2000 lines of 100 chars from the raw REPL: (line numbers, stdout, stderr, seconds)."""
    repl.prompt()
    body = "for i in range(%d):\n    print('L%%05d ' %% i + 'x' * 93)\n" % LARGE_LINES
    t0 = time.time()
    out, err = repl.exec_raw(body, timeout=120)
    dt = max(time.time() - t0, 1e-6)
    nums = [int(l[1:6]) for l in out.splitlines() if l.startswith("L") and l[1:6].isdigit()]
    return nums, out, err, dt


def test_large_output_count(large_output):
    """Every one of the 2000 lines arrives."""
    nums = large_output[0]
    assert len(nums) == LARGE_LINES, "got %d/%d lines" % (len(nums), LARGE_LINES)


def test_large_output_order(large_output):
    """The lines arrive in order."""
    nums = large_output[0]
    assert nums == list(range(len(nums))), "lines out of order"


def test_large_output_no_error(large_output):
    """Printing that much raises nothing."""
    err = large_output[2]
    assert not err.strip(), err.strip()[-200:]


def test_output_kBps(large_output, metric):  # pylint: disable=invalid-name
    """Console throughput, compared with this board's history."""
    _, out, _, dt = large_output
    metric("output_kBps", len(out) / 1024 / dt, "KB/s", better="higher")


@pytest.fixture(scope="module")
def soft_reboot(repl):
    """Ctrl-D, then back to the REPL. Returns the text seen on the way back."""
    repl.prompt()
    repl.write(b"\x04")
    repl.read_until(b"soft reboot", timeout=15)
    time.sleep(2)
    mark = len(repl.transcript)
    # code.py may run forever (farm idle sketch), so get back to the REPL with prompt()
    repl.prompt(timeout=15)
    return bytes(repl.transcript[mark:])


def test_ctrl_d_soft_reboot(soft_reboot):
    """Ctrl-D soft-reboots the board."""
    assert soft_reboot is not None


def test_reboot_banner(soft_reboot):
    """The CircuitPython banner appears on the way back to the REPL."""
    assert b"Adafruit CircuitPython" in soft_reboot, soft_reboot.decode(errors="replace")[-160:]
