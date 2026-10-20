# perf_core: Core performance micro-benchmarks

Tier: smoke / automation: auto / boards: all

## Regressions it targets

- #10733 - Performance regression in integer loop throughput
- #10681 - Float math operations slowdown
- #9359 - String concatenation performance
- #8919 - Dictionary operation efficiency
- #9080 - List sorting regression
- #10176 - REPL roundtrip latency

## What it checks

- `int_loop_100k`: Sum loop over 100,000 iterations (scaled 0.2x on small boards); should be < 500ms
- `float_math_20k`: Compute sin() and sqrt() 20,000 times; should be < 1000ms (skipped if math module unavailable)
- `str_concat_5k`: Build a string by concatenation 5,000 times; should be < 200ms
- `dict_ops_10k`: Insert and retrieve from dictionary 10,000 times; should be < 200ms
- `list_sort_5k`: Sort a 5,000-item list of pseudo-random integers; should be < 500ms
- `json_roundtrip_200`: Encode and decode 200 items to/from JSON (skipped if json unavailable); should be < 300ms
- `gc_collect`: Time of garbage collection after allocating 200 objects; should be < 50ms
- `bitmap_fill`: Fill a 64x64 region of a displayio.Bitmap(128,128,256) with per-pixel writes (skipped if displayio unavailable); should be < 200ms
- `bitmaptools_fill_region`: Fill a full 128x128 bitmap 20 times using bitmaptools (skipped if unavailable); should be < 500ms
- `sleep_accuracy`: Mean error in time.sleep(0.1) over 5 samples; should be < 20ms
- `repl_roundtrip_ms`: Time for the host to send a simple `pass` command and receive acknowledgment; should be < 50ms

## Setup

No extra hardware or environment variables required. Uses only built-in CircuitPython modules and the standard library (time, math, gc). Scales iteration counts by 0.2x on boards with the "small" capability (typically SAMD21 with limited RAM).

## What is hard to automate

- **Performance comparison over time**: Benchmarks have natural variance; the test compares against a running baseline median. Boards with different CPU speeds (e.g., RP2040 vs SAMD21) will have very different absolute numbers, but slowdowns within a board are reliably detected.
- **Floating-point precision**: math.sin() results may vary slightly between platforms; the test only checks that computation happened, not exact values.
- **Module availability**: Some tests (json, displayio, bitmaptools) skip gracefully if not compiled into the CircuitPython build.
- **GC behavior**: gc.collect() timing varies based on heap state and implementation details across ports.

## Run

```bash
./run.py --only perf_core --boards all
./run.py --only perf_core --boards metro_rp2040,metro_m0
./run.py --tier smoke  # includes perf_core
```

To see how a metric has drifted over time:

```bash
grep '"metric":"perf_core\.' history/metrics.jsonl | jq '.value' | tail -10
```
