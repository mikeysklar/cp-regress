# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Template. Files starting with _ are not loaded by run.py. Copy this to tests/<id>.py.

One test module = one fixture. It targets a family of real regressions (issue numbers in
META["targets"]) and reports many small named checks, never one big pass/fail.
"""

META = {
    "id": "example",                 # must equal the file name without .py
    "title": "One-line description of what this fixture proves",
    "category": "USB",               # USB | Display | Network | BLE | Core | Peripherals | Power | Audio
    "tier": "smoke",                 # smoke (every merge, <2 min/board) | nightly | release
    "needs": ["native_usb"],         # board caps from farm.toml; test skips if any are missing
    "wiring": [],                    # farm.toml wiring keys; test skips until they are present
    "skip_boards": [],               # board names to never run on (e.g. ["metro_m0"] if it resets)
    "automation": "auto",            # auto | fixture (needs extra hardware) | assisted (a human step)
    "destructive": False,            # True if it rewrites boot.py/settings.toml or resets the board
    "multi_board": False,            # True if it needs a partner board via ctx.other(cap)
    "timeout": 120,
    "targets": [9888, 9956],         # issue numbers this fixture would have caught
}

# Device-side code. check/skip/metric/guard come from lib/devtest.py, which the host
# prepends. Print nothing else that starts with "@@R ".
DEVICE = r'''
import sys
check("implementation", sys.implementation.name == "circuitpython", sys.implementation.name)
import time
t0 = time.monotonic_ns()
x = 0
for i in range(20000):
    x += i
metric("loop_20k_ms", (time.monotonic_ns() - t0) / 1e6, "ms")
'''


def run(board, ctx):
    # Host-side checks first: they need no REPL.
    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    # Device-side checks through the raw REPL.
    ctx.device(DEVICE, timeout=30)

    # Anything that rewrites files must run inside board.preserved(), which restores
    # code.py/boot.py/settings.toml and verifies them by md5 afterwards:
    #
    # with board.preserved():
    #     board.write_file("code.py", "print('hi')\n")
    #     with board.repl() as r:
    #         text = r.read_for(5)
