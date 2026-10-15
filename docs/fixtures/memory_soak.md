# memory_soak: Memory soak: leaks, fragmentation, monotonic time drift

Tier: nightly / automation: auto / boards: all with native_usb

## Regressions it targets

- #10733 - Memory leak in long-running programs
- #10681 - Heap fragmentation over time
- #9359 - Monotonic time drift
- #8919 - GC pauses becoming longer
- #9080 - Device reset due to memory exhaustion
- #10176 - Timekeeper inaccuracy on various ports

## What it checks

- `present`, `vid_pid`: board is on the bus at its hub path
- `mem_free_start` (metric): free heap after gc.collect, bytes, higher is better
- `alloc_memory`: no MemoryError during the alloc/free loop
- `leak_detection`: free heap after the soak is within 2% (or 2 KB, whichever is larger) of the start; detail lists the 5 s samples
- `fragmentation`: largest allocatable bytearray (binary search up to gc.mem_free()) is within 10% of its pre-burst value after 50 small allocations are freed
- `max_block` (metric): largest allocatable block after the burst, bytes, higher is better
- `soak_complete`: the loop took at least run_time / 5 samples (loop was not starved or cut short)
- `soak_reported`: the device body ran to the end and reported `soak_complete`
- `monotonic_drift`: device `time.monotonic_ns()` delta across the soak vs the host's `time.monotonic()` delta, read through two identical raw-REPL round-trips so latency cancels; tolerance 0.5% or 0.3 s, whichever is larger
- `clock_start`: only reported (as a failure) when the first device clock read fails

## Setup

No extra hardware or environment variables. Runs for 60 seconds on normal boards, 20 seconds on boards with "small" capability (e.g., SAMD21). Allocates and frees:
- Lists of 10, 50, 100, 500 integers
- Strings of 10, 50, 100, 500 characters
- Bytearrays of 10, 50, 100, 500 bytes

Samples free memory every 5 seconds and performs a fragmentation check via binary search for the largest allocatable block.

## What is hard to automate

- **Real-world memory pressure**: The test uses synthetic allocation patterns. A real memory leak in user code (e.g., dangling references in a data structure) might not be caught if memory is mostly freed in the test loop.
- **Flash wear**: The test does not write to flash, so wear leveling / FLASH page exhaustion bugs are not caught.
- **Long-term drift (days)**: The test runs for 1 minute; multi-day leaks or drift will not be detected. The drift check resolves roughly 0.5% over 60 s, so it catches a wrong tick rate, not ppm-level crystal error.
- **Interrupt handler leaks**: Leaks in interrupt or callback handlers might not trigger during this test if handlers don't fire frequently.
- **GC latency spikes**: gc.collect() pause times are not measured.
- **Port-specific memory managers**: Some ports (e.g., ESP32 with PSRAM) have complex memory hierarchies that may behave differently under soak.

## Run

```bash
./run.py --only memory_soak --tier nightly
./run.py --only memory_soak --tier nightly --boards metro_rp2040,metro_esp32s3
```

To see the largest-block trend:

```bash
grep '"metric": "memory_soak.max_block"' history/metrics.jsonl | tail -20
```
