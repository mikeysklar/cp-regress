# core_vm: Core language VM

Nightly / auto / all boards with native_usb (15 tests on "small" boards, 40 on others)

## Regressions it targets

- #6214: Type coercion edge cases
- #3169: String formatting and escape sequences
- #1881: Dictionary/set operations and iteration
- #10259: Exception handling and traceback
- #8499: Generator and iterator semantics
- #5451: List/bytearray slicing and mutation
- #4101: Integer arithmetic edge cases

## What it checks

For each selected test from the CircuitPython test suite:
- Test execution completes without device crash or hang (20 s per test; a hung test is
  interrupted with Ctrl-C and the REPL re-synced, `repl_recover` fails if that does not work)
- Device output matches expected output exactly (after normalizing whitespace); on mismatch the
  detail shows the first differing line and the device stderr
- Each test runs as `exec(src, {'__name__': '__main__'})` so globals do not leak between tests,
  and `SystemExit` is caught (otherwise it drops out of the raw REPL and soft-reboots the board)
- A device output of "SKIP" (the MicroPython feature-probe convention) is recorded as a skip

Additional direct checks:
- `memory_error`: `bytearray(1 << 28)` (256 MB, larger than RAM + PSRAM on every farm board)
  raises `MemoryError`
- `recursion_error`: 100000-deep recursion raises `RuntimeError` ("maximum recursion depth
  exceeded" or "pystack_space_exhausted"), not a hard fault
- `summary`: at least one test passed and none failed
- Pass ratio metric: percentage of tests that passed (compared to baseline if available)
- Test execution time: total time to run all tests

## Setup

**Host environment variable:**
- `CPR_CP_TREE`: path to a CircuitPython checkout; defaults to `~/circuitpython`
- If the tree is missing, the whole test is skipped with a clear reason

**Board requirements:**
- native_usb capability (CDC console available)
- Adequate flash to hold test sources (~40 KB decompressed test files)
- Adequate RAM for test execution (tests are small, but memory checks intentionally try to allocate large buffers)

**No extra wiring or hardware needed.**

## What is hard to automate

**Cannot easily detect:**
- Silent integer overflow or wrap-around (detected only if output changes)
- Subtle differences in error message formatting (test relies on exact string match)
- Performance regressions in builtin operations (detected by metrics, not individual test failure)
- GC behavior variations between platforms (e.g., when objects are freed)
- Tests that depend on specific CPython version features may fail on CircuitPython without clear failure message

**Would need manual setup:**
- Tests that require external sensors or hardware (not included; filtered out)
- Tests that require specific file system state (boot.py, settings.toml, etc.)
- Tests that depend on networking or USB enumeration state
- Tests that require timing-based assertions (Xfail: could pass depending on CPU speed)

**Automation notes:**
- The test filters source code at load time (excludes files mentioning "skip" or importing micropython/machine/uctypes/_thread)
- Tests that print "SKIP" (feature probes) are recorded as skips
- Expected output comes from the `.exp` file, else from running the file with host `python3`
  (the same rule MicroPython's run-tests.py uses), so a host Python whose output differs for a
  test shows up as a mismatch on every board
- Timeout per test: 20 seconds (sufficient for even math-heavy tests on slow boards)
- Board with the "small" cap (metro_m0) runs only the first 15 tests to stay within flash/RAM limits
- Total timeout: 10 minutes per board (cumulative, includes all tests + memory/recursion checks)

## Run

    ./run.py --only core_vm --boards metro_rp2040,metro_m4_airlift,metro_esp32s3
    ./run.py --only core_vm --tier nightly --label "11.0.0-alpha.1"
    
Check results:

    cat results/*/summary.md
    ./run.py --dry-run --only core_vm  # see which tests will run
