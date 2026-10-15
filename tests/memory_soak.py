# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Memory soak test: detect leaks, fragmentation, and monotonic time drift."""

import re
import time

import farmlib

META = {
    "id": "memory_soak",
    "title": "Memory soak: leaks, fragmentation, monotonic time drift",
    "category": "Core",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 180,
    "targets": [10733, 10681, 9359, 8919, 9080, 10176],
}

# Device-side code: runs for ~60s (20s on small), recording mem state.
# Clock drift is measured host-side (see run()), not by comparing two device clocks.
DEVICE = r'''
import gc
import time

run_time = @@RUN_TIME@@
interval = 5  # sample every 5 seconds

# Ensure clean slate
gc.collect()
mem_free_start = gc.mem_free()
metric("mem_free_start", mem_free_start, "bytes", better="higher")

samples = []
alloc_errors = 0
t_start = time.monotonic()

# Allocate/deallocate in a loop for the specified duration
while time.monotonic() - t_start < run_time:
    alloc_set = []
    try:
        for size in [10, 50, 100, 500]:
            alloc_set.append([0] * size)
            alloc_set.append("x" * size)
            alloc_set.append(bytearray(size))
    except MemoryError:
        alloc_errors += 1
    alloc_set = None
    gc.collect()
    elapsed = time.monotonic() - t_start
    if len(samples) * interval <= elapsed:
        samples.append(gc.mem_free())

check("alloc_memory", alloc_errors == 0, "MemoryError in %d loop passes" % alloc_errors, alloc_errors)

# Leak detection: final within 2% or 2 KB of start
gc.collect()
mem_free_final = gc.mem_free()
leak_bytes = mem_free_start - mem_free_final
max_leak = max(2000, int(mem_free_start * 0.02))
check("leak_detection", leak_bytes <= max_leak,
      "leak=%d bytes (max %d), samples=%s" % (leak_bytes, max_leak, samples[:12]), leak_bytes)

# Fragmentation: largest allocatable bytearray, before and after a burst of small allocations
def find_max_alloc():
    gc.collect()
    low, high = 100, gc.mem_free()
    result = 0
    while low <= high:
        mid = (low + high) // 2
        try:
            test_alloc = bytearray(mid)
            test_alloc = None
            gc.collect()
            result = mid
            low = mid + 1
        except MemoryError:
            high = mid - 1
    return result

max_block_before = find_max_alloc()
temp_allocs = []
try:
    for _ in range(50):
        temp_allocs.append(bytearray(100))
except MemoryError:
    pass
temp_allocs = None
gc.collect()
max_block_after = find_max_alloc()

frag_ratio = max_block_after / max(max_block_before, 1)
metric("max_block", max_block_after, "bytes", better="higher")
check("fragmentation", max_block_before > 0 and frag_ratio >= 0.9,
      "ratio=%.2f (before=%d, after=%d)" % (frag_ratio, max_block_before, max_block_after), max_block_after)

expected = int(run_time // interval)
check("soak_complete", len(samples) >= expected, "samples=%d expected>=%d" % (len(samples), expected), len(samples))
'''

CLOCK = "import time\nprint('@@T', time.monotonic_ns())\n"


def _device_clock(board):
    """Device monotonic seconds and host monotonic seconds taken right after the read returns.
    The REPL round-trip latency is the same for every call, so it cancels in a difference."""
    with board.repl() as r:
        out, _err = r.exec_raw(CLOCK, timeout=10)
    host = time.monotonic()
    m = re.search(r"@@T (\d+)", out)
    return (int(m.group(1)) / 1e9 if m else None), host


def run(board, ctx):
    # Host-side: check board presence
    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    # Determine run time: 60s for normal boards, 20s for small
    run_time = 20 if "small" in board.caps else 60
    device_code = DEVICE.replace("@@RUN_TIME@@", str(run_time))

    try:
        dev0, host0 = _device_clock(board)
    except farmlib.FarmError as e:
        dev0, host0 = None, None
        ctx.check("clock_start", False, str(e))

    # Device-side memory soak through the raw REPL; timeout covers the soak plus
    # the fragmentation binary search.
    try:
        res, out, err = ctx.device(device_code, timeout=run_time + 60)
        names = [r.get("name") for r in res]
        ctx.check("soak_reported", "soak_complete" in names, "device results: %s" % names)
    except farmlib.FarmError as e:
        ctx.check("soak_reported", False, str(e))

    # Monotonic drift: device clock delta vs host clock delta across the soak.
    if dev0 is None:
        ctx.check("monotonic_drift", False, "no start reading from device")
        return
    try:
        dev1, host1 = _device_clock(board)
    except farmlib.FarmError as e:
        ctx.check("monotonic_drift", False, "end reading failed: %s" % e)
        return
    if dev1 is None:
        ctx.check("monotonic_drift", False, "no end reading from device")
        return
    d_dev = dev1 - dev0
    d_host = host1 - host0
    tol = max(0.005 * d_host, 0.3)
    ctx.check("monotonic_drift", abs(d_dev - d_host) <= tol,
              "device=%.3fs host=%.3fs diff=%.3fs tol=%.3fs" % (d_dev, d_host, d_dev - d_host, tol),
              round(d_dev - d_host, 3))
