# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Core performance micro-benchmarks: CPU and memory throughput on every board.

pytest version of tests/perf_core.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). It runs once per board; each benchmark then gets a
correctness test and a timing test, and the host measures the REPL round trip.
Targets: #10733, #10681, #9359, #8919, #9080, #10176.
"""
import time

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.targets(10733, 10681, 9359, 8919, 9080, 10176),
]

BENCHMARKS = ["int_loop_100k", "float_math_20k", "str_concat_5k", "dict_ops_10k", "list_sort_5k",
              "json_roundtrip_200", "gc_collect", "bitmap_fill", "bitmaptools_fill_region",
              "sleep_accuracy"]

# Device-side code. Benchmarks execute on-device and report via metric().
# Scale factor is applied via string formatting from the host for small boards.
DEVICE = r'''
import time
import math
import gc

# Read scale factor from environment; tests on small boards use 0.2x
scale = @@SCALE@@

# int_loop_100k: Sum loop 100k iterations (or scaled down)
t0 = time.monotonic_ns()
x = 0
for i in range(int(100000 * scale)):
    x += i
ms = (time.monotonic_ns() - t0) / 1e6
metric("int_loop_100k", ms, "ms", better="lower")
check("int_loop_100k", x > 0, "sum=%d" % x, x)

# float_math_20k: math.sin/sqrt 20k iterations
try:
    t0 = time.monotonic_ns()
    f = 0.0
    for i in range(int(20000 * scale)):
        f += math.sin(i * 0.01) + math.sqrt(i + 1)
    ms = (time.monotonic_ns() - t0) / 1e6
    metric("float_math_20k", ms, "ms", better="lower")
    check("float_math_20k", f > 0, "result=%.2f" % f, value=f)
except Exception as e:
    skip("float_math_20k", "math not available: %s" % str(e))

# str_concat_5k: String concatenation 5k iterations
t0 = time.monotonic_ns()
s = ""
for i in range(int(5000 * scale)):
    s = s + "x"
ms = (time.monotonic_ns() - t0) / 1e6
metric("str_concat_5k", ms, "ms", better="lower")
check("str_concat_5k", len(s) > 0, "len=%d" % len(s), len(s))
del s
gc.collect()

# dict_ops_10k: 10k dictionary operations on at most 1000 keys (scaled), so the table stays small
# enough to grow in a fragmented heap (a 10k-key dict needs one ~38 KB block)
t0 = time.monotonic_ns()
d = {}
keys = int(1000 * scale)  # 200 on small boards: a 1000-key table is 4 KB, too big for a SAMD21
for i in range(int(10000 * scale)):
    k = i % keys
    d[k] = i * 2
    if i % 2 == 0 and k in d:
        _ = d[k]
ms = (time.monotonic_ns() - t0) / 1e6
metric("dict_ops_10k", ms, "ms", better="lower")
check("dict_ops_10k", len(d) > 0, "len=%d" % len(d), len(d))
del d
gc.collect()

# list_sort_5k: Sort a pseudo-random list 5k items (LCG)
t0 = time.monotonic_ns()
seed = 12345
nums = []
for i in range(int(5000 * scale)):
    seed = (seed * 1103515245 + 12345) & 0x7fffffff
    nums.append(seed % 1000)
nums.sort()  # in place: sorted() would copy a list whose capacity has grown to 32 KB
ms = (time.monotonic_ns() - t0) / 1e6
metric("list_sort_5k", ms, "ms", better="lower")
check("list_sort_5k", nums[0] <= nums[-1], "sorted", value=len(nums))
del nums
gc.collect()

# json_roundtrip_200: JSON encode/decode 200 items
try:
    import json
    t0 = time.monotonic_ns()
    items = []
    for i in range(int(200 * scale)):
        items.append({"id": i, "val": i * 2, "name": "item_%d" % i})
    data = json.dumps(items)
    decoded = json.loads(data)
    ms = (time.monotonic_ns() - t0) / 1e6
    metric("json_roundtrip_200", ms, "ms", better="lower")
    check("json_roundtrip_200", len(decoded) > 0, "items=%d" % len(decoded), len(decoded))
    del items, data, decoded
    gc.collect()
except ImportError:
    skip("json_roundtrip_200", "json module not available")
except Exception as e:
    check("json_roundtrip_200", False, "%s: %s" % (type(e).__name__, e))

# gc_collect: Time of gc.collect after allocating 200 small objects
gc.collect()
t0 = time.monotonic_ns()
temp_objs = []
for i in range(int(200 * scale)):
    temp_objs.append({"data": [i, i+1, i+2]})
del temp_objs
t1 = time.monotonic_ns()
gc.collect()
ms = (time.monotonic_ns() - t1) / 1e6
metric("gc_collect", ms, "ms", better="lower")
check("gc_collect", ms >= 0, "time=%.2f ms" % ms, value=ms)

# bitmap_fill: displayio.Bitmap fill test (128x128, 256 colors, fill 64x64 area; 32x32 on
# small boards, where a SAMD21 cannot find even 4 KB late in the run)
side = 128 if scale >= 1 else 32
fill = min(64, side)
try:
    import displayio
    t0 = time.monotonic_ns()
    bm = displayio.Bitmap(side, side, 256)
    for y in range(fill):
        for x in range(fill):
            bm[x, y] = (x + y) % 256
    ms = (time.monotonic_ns() - t0) / 1e6
    metric("bitmap_fill", ms, "ms", better="lower")
    check("bitmap_fill", bm[fill - 1, fill - 1] >= 0, "pixels=%d" % (fill * fill), value=fill * fill)
except ImportError:
    skip("bitmap_fill", "displayio not available")
except Exception as e:
    check("bitmap_fill", False, "%s: %s" % (type(e).__name__, e))

# bitmaptools_fill_region: if available, fill full 128x128 bitmap 20 times
try:
    import bitmaptools
    bm = None
    gc.collect()
    bm = displayio.Bitmap(side, side, 256)
    t0 = time.monotonic_ns()
    for _ in range(int(20 * scale)):
        bitmaptools.fill_region(bm, x1=0, y1=0, x2=side, y2=side, value=42)
    ms = (time.monotonic_ns() - t0) / 1e6
    metric("bitmaptools_fill_region", ms, "ms", better="lower")
    check("bitmaptools_fill_region", bm[0, 0] == 42, "fills=20", value=20)
except ImportError:
    skip("bitmaptools_fill_region", "bitmaptools not available")
except NameError:
    skip("bitmaptools_fill_region", "displayio not available")
except Exception as e:
    check("bitmaptools_fill_region", False, "%s: %s" % (type(e).__name__, e))

# sleep_accuracy: time.sleep(0.1) x5, report mean error ms
try:
    times = []
    for _ in range(5):
        t0 = time.monotonic_ns()
        time.sleep(0.1)
        times.append((time.monotonic_ns() - t0) / 1e6)
    errors = [abs(t - 100) for t in times]
    mean_error = sum(errors) / len(errors)
    metric("sleep_accuracy", mean_error, "ms", better="lower")
    check("sleep_accuracy", mean_error < 20, "error=%.1f ms" % mean_error, value=mean_error)
except Exception as e:
    check("sleep_accuracy", False, "%s: %s" % (type(e).__name__, e))
'''


@pytest.fixture(scope="module")
def results(board, device):
    """Run every benchmark once per board (0.2x sizes on "small" boards such as the SAMD21)."""
    scale = 0.2 if "small" in board.caps else 1.0
    return device.run(DEVICE.replace("@@SCALE@@", str(scale)), timeout=60)


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_device_ran(results):
    """The benchmark code ran to the end without raising."""
    results.expect_no_exception()


@pytest.mark.parametrize("name", BENCHMARKS)
def test_benchmark(results, name):
    """The benchmark produced a correct result (a skip means the module is not built in)."""
    results.expect(name)


# Absolute margins (in the metric's unit) below which a change never fails. sleep_accuracy
# comes in steps of ~0.012 ms (0.0244, 0.0391, 0.0513 on bravo, 2026-10-17), so one step reads
# as x1.6; the device's own 20 ms check is the real limit.
SLACK = {"sleep_accuracy": 1}


@pytest.mark.parametrize("name", BENCHMARKS)
def test_benchmark_time(results, metric, name):
    """The benchmark's time, compared with the median of this board's last five runs."""
    if name not in results.metrics:
        results.expect(name)  # skips if the device skipped it, fails if it failed
        pytest.fail("%s passed but sent no timing" % name, pytrace=False)
    rec = results.metric_record(name)
    metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"),
           slack=SLACK.get(name, 0))


def test_repl_roundtrip_ms(board, metric):
    """Host-measured time for the raw REPL to run `pass`."""
    with board.repl() as repl:
        t0 = time.perf_counter()
        repl.exec_raw("pass", timeout=5)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    metric("repl_roundtrip_ms", elapsed_ms, "ms", better="lower", tolerance=0.5)
