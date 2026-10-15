# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Memory soak: leaks, fragmentation, monotonic time drift.

pytest version of tests/memory_soak.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). Per board, one module-scoped fixture reads the device
clock, runs the soak (60 s, 20 s on "small" boards), and reads the clock again; each check is
then one test, in the original's order. Targets: #10733 (leaks in long-running programs),
#10681 (heap fragmentation), #9359 (monotonic drift), #8919 (longer GC pauses), #9080 (reset
on memory exhaustion), #10176 (timekeeper inaccuracy).
"""
import re
import time

import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(10733, 10681, 9359, 8919, 9080, 10176),
]

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


@pytest.fixture(scope="module")
def soak(board, device):
    """Clock read, device soak, clock read, once per board. Errors are kept in the record."""
    rec = {"dev0": None, "host0": None, "clock_start_err": None,
           "results": None, "dev1": None, "host1": None, "end_err": None}
    run_time = 20 if "small" in board.caps else 60
    device_code = DEVICE.replace("@@RUN_TIME@@", str(run_time))
    try:
        rec["dev0"], rec["host0"] = _device_clock(board)
    except Exception as e:  # pylint: disable=broad-except
        rec["clock_start_err"] = str(e)
    # timeout covers the soak plus the fragmentation binary search
    rec["results"] = device.run(device_code, timeout=run_time + 60)
    if rec["results"].crashed:
        # run.py charges a failed soak run to soak_reported and still takes the end clock
        # reading. The test that set this fixture up is test_clock_start, which would not charge
        # it, so clear the module-wide "not reached" skip: no later test touches the board, and
        # test_soak_reported fails with the reason. The board teardown still health-checks.
        board.cpr_crashed = None
    if rec["dev0"] is not None:
        try:
            rec["dev1"], rec["host1"] = _device_clock(board)
        except Exception as e:  # pylint: disable=broad-except
            rec["end_err"] = str(e)
    return rec


@pytest.fixture(scope="module")
def results(soak):
    """The DeviceResults of the soak run."""
    return soak["results"]


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_clock_start(soak):
    """The first device clock read worked (run.py reports this only when it fails)."""
    assert soak["clock_start_err"] is None, soak["clock_start_err"]


def test_mem_free_start(results, metric):
    """Free heap after gc.collect, compared with this board's history."""
    rec = results.metric_record("mem_free_start")
    metric("mem_free_start", rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def test_alloc_memory(results):
    """No MemoryError during the alloc/free loop."""
    results.expect("alloc_memory")


def test_leak_detection(results):
    """Free heap after the soak is within 2% (or 2 KB) of the start."""
    results.expect("leak_detection")


def test_max_block(results, metric):
    """Largest allocatable block after the burst, compared with this board's history."""
    rec = results.metric_record("max_block")
    metric("max_block", rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def test_fragmentation(results):
    """Largest block after 50 small allocations is within 10% of its pre-burst size."""
    results.expect("fragmentation")


def test_soak_complete(results):
    """The loop took at least run_time / 5 samples."""
    results.expect("soak_complete")


def test_device_exception(results):
    """The device code ran without raising (run.py's ctx.device adds this on stderr)."""
    if results.crashed:
        # run.py adds no device_exception when the run itself fails; soak_reported charges it
        pytest.skip("not reached: the device run failed (%s)" % results.crashed[:200])
    assert not results.err, results.err[-400:]


def test_soak_reported(results):
    """The device body ran to the end and reported soak_complete."""
    assert not results.crashed, results.crashed
    names = [r.get("name") for r in results.records]
    assert "soak_complete" in names, "device results: %s" % names


def test_monotonic_drift(soak):
    """Device monotonic delta across the soak matches the host's within 0.5% or 0.3 s."""
    if soak["dev0"] is None:
        pytest.fail("no start reading from device", pytrace=False)
    if soak["end_err"] is not None:
        pytest.fail("end reading failed: %s" % soak["end_err"], pytrace=False)
    if soak["dev1"] is None:
        pytest.fail("no end reading from device", pytrace=False)
    d_dev = soak["dev1"] - soak["dev0"]
    d_host = soak["host1"] - soak["host0"]
    tol = max(0.005 * d_host, 0.3)
    assert abs(d_dev - d_host) <= tol, (
        "device=%.3fs host=%.3fs diff=%.3fs tol=%.3fs" % (d_dev, d_host, d_dev - d_host, tol))
