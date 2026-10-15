# cp-regress on bravo: the first hardware run

2026-10-14. Smoke tier, 8 fixtures × 8 boards, CircuitPython 11.0.0-alpha.1 (main).

## The short version

- **63 of 64 pass, twice in a row.** That took five rounds, starting from 39 of 64.
- **One failure is a real firmware bug.** The Feather STM32F405 hard-faults when a list grows
  past 4096 items in a fragmented heap. It reproduces every time, and the other seven boards
  raise a clean `MemoryError` at the same point.
- **One spec deviation shows up on 7 boards:** settings.toml reads `017` as octal 15. toml.io
  says leading zeros are invalid.
- **19 of the 25 first-run failures were in the harness or the fixtures**, as predicted. Five
  more came after the Metro M0 dropped off USB, and one SWD reset brought it back. Each bug
  taught us something about the boards, and the fixes are now in the code.
- **A smoke pass takes about 2 minutes** on each board, and the 8 boards run in parallel. 1,075
  named checks and 114 timing metrics come back on every run.

## 1. Why this exists

The ten-year sweep of adafruit/circuitpython (11,479 issues and PRs) found:

| Finding | Number |
|---|---|
| Bug reports that are regressions ("worked in X, broke in Y") | 339 |
| Share of regressions on the boot path (USB, REPL, filesystem, safe mode) | 131 (39%) |
| Median time from merge to the first user report | 7.5 days |
| Reported within 30 days of the merge | 76% |
| Regressions that broke in an `x.0` release | 140 of 238 |
| Judged catchable by a scripted hardware test | 85% |
| Hardware tests that run after a merge today | 0 |

CI builds every board and runs the unix port, but nothing runs on real hardware after a merge,
so users are the test suite. The goal of cp-regress is to put eight real boards between a
merge and the users and run them on every merge.

## 2. What ran

The smoke tier is the "plug it in and it works" path, which is where most regressions land.

| Fixture | What it does on each board | Research area it covers | Regressions it targets |
|---|---|---|---|
| usb_enumeration | VID:PID, CDC and MSC interfaces present, drive read-write, right board id | USB device (49) | #9888 #9956 #10390 #8134 |
| usb_msc_rw | host writes a 256 KB pseudo-random file and 20 small ones; the board reads them back and checksums them | USB device, filesystem (24) | #9956 #10390 #4670 #9971 #10901 #11097 |
| repl_console | eval, `_`, Ctrl-C into `sleep(30)`, 4 KB paste mode, 2,000 lines of output in order, Ctrl-D | REPL / console (34) | #417 #2686 #1630 #3588 #4164 #6480 #10514 #9454 |
| code_py_lifecycle | code.py runs once, a traceback instead of safe mode, recursion and MemoryError raise cleanly | boot, crash / safe mode (24) | #3502 #7459 #7578 #7060 #11287 #1537 #4728 |
| settings_toml | `os.getenv` types, escapes, unicode, values toml.io says to reject | filesystem, core | #11118 #7677 |
| perf_core | int, float, string, dict, sort, json, gc, Bitmap fill and sleep accuracy, timed against a rolling baseline | core VM (19), slowdowns | #10733 #10681 #9359 #8919 #9080 #10176 |
| displayio_headless | Bitmap, Palette, TileGrid, Group, vectorio and bitmaptools logic, plus a render checksum per board | display (36) | 19 issues, including #8873 #4193 #10363 #10883 |
| peripherals_selfcheck | pin claimed twice, use after deinit, I2C without pull-ups, PWM frequency and duty round trip, ADC, temperature | peripherals | #1603 #5476 |

Smoke fixtures target 52 distinct past regressions by issue number, and they cover the boot path
where 131 of the 339 regressions occurred.

**Boards:** Metro RP2040, Metro M4 AirLift, Metro M0, Feather nRF52840, Metro ESP32-S2,
Metro RP2350, Metro ESP32-S3, Feather STM32F405. That is one or more boards per port family:
espressif, raspberrypi, atmel-samd (SAMD21 and SAMD51), nordic and stm.

## 3. How it works

```
merge to main
  → CI builds firmware (already happens)
  → flash the farm (fastcp.sh)
  → run.py --tier smoke                    8 boards in parallel, one thread per board
      per board, per fixture:
        host side: USB sysfs, mount, write files, power control (uhubctl)
        device side: code sent over the raw REPL, results come back as "@@R {json}" lines
        after each fixture: health check (tty, REPL eval, supervisor.runtime.safe_mode_reason)
          → if the board died, the fixture is charged with it, the board is power-cycled,
            and the run continues
  → results/<stamp>/summary.md   board × fixture grid, every failing check with its detail
  → history/metrics.jsonl        every timing; a run fails a metric that is 25% worse than
                                 the median of the last five runs on that board
  → history/golden.json          exact values per board, such as the render checksum
```

Three kinds of result come back:

- **Checks** are named pass/fail results with a detail string. There are about 130 per board.
  One failure doesn't hide the others.
- **Metrics** are timings. They catch slowdowns, the kind of regression users only notice
  months later (#10733: CircuitPython 10 was 3× slower).
- **Golden values** are exact outputs recorded once per board, such as a displayio pixel
  checksum. A build that renders different pixels fails.

Boards are found by USB path at the moment of use, never by `ttyACM` number or drive label,
since both shuffle on every reflash. Fixtures that rewrite code.py, boot.py or settings.toml
back the files up and restore them with an md5 check.

## 4. The first run, round by round

| Run | Boards | Pass | Fail | What changed before it |
|---|---|---|---|---|
| 1 | 8 | 39 / 64 | 25 | nothing: the suite as written |
| 2 | 8 (M0 off USB) | 50 / 64 | 6 | prompt fix, PWM and NeoPixel fixes, MSC free-space check, small-board chunking |
| 3 | 6 | 47 / 48 | 1 | perf_core memory fixes |
| 4 | 8 (M0 off USB) | 54 / 56 | 2 | health check and recovery, safe-mode detection |
| 5 | 8 | 63 / 64 | 1 | M0 recovered over SWD, SAMD21 memory sizing, M0 NeoPixel skip |
| 6 | 8 | 63 / 64 | 1 | none: a repeat to check it holds |

The one failure left is the STM32F405 bug.

## 5. What we found that is real

### STM32F405: a growing list hard-faults the chip

Found by `perf_core`, then narrowed down with one-change-at-a-time probes (2 to 4 trials each,
every variant against a crashing control):

| Variant | Result |
|---|---|
| 4096 items built with `append` from bigint arithmetic, then `nums[0]` | survives, 2 of 2 |
| 4097 items, the same way | **hard fault, 2 of 2** |
| 5000 items, the same way | **hard fault, 13 of 13 across all probes** |
| 5000 small ints, no bigint garbage | survives, 2 of 2 |
| 5000 items, only `len()` read | survives, 3 of 3 |
| bigint arithmetic, no list | survives, 3 of 3 |
| one 64 KB `bytearray` | survives |
| the same 5000-item code on RP2040, M4, nRF52840 | clean `MemoryError` |
| the same code on ESP32-S3 | runs (it has PSRAM) |

The 4097th append doubles the list's storage from 16 KB to 32 KB. With a heap fragmented by
bigint garbage, that realloc has to move the block. The other ports either move it or raise
`MemoryError`. The STM32F405 hands back storage that hard-faults on the first read and reboots
into safe mode (`SafeModeReason.HARD_FAULT`, once reported as a stack overflow).

**Not yet known:** whether it is new in 11.0. The control is the same probe on a 10.x build,
which means reflashing the board. The candidate merges between 10.x and this build include the
MicroPython v1.27 merge. Nothing has been bisected yet, so treat that as a lead, not a cause.

### settings.toml accepts leading zeros

`017` parses as 15 (octal) on every board that reads settings.toml. toml.io says leading zeros
are invalid. The fixture records this as informational rather than a failure, because it is a
deliberate parser choice (`strtol` base 0) that should be decided on purpose, not fixed silently.

### Smaller things the run surfaced

- The Metro M0 runs a different build from the other seven (`-48` instead of `-86`), and the
  RP2350's build is marked dirty. The version table in every summary makes this visible.
- The ESP32-S2's drive has 18 KB free, so the MSC test skips there until it is cleared.

## 6. What we fixed in the harness and fixtures

Each of these failed on the first run, which is what first runs are for.

| # | Bug | Seen as | Fix |
|---|---|---|---|
| 1 | The prompt helper sent its keys once. When a host write triggered an auto-reload afterwards, the board sat at "Press any key" for good. | 6 timeouts on the M4 and S2 | keep poking until a stable `>>>` prompt appears |
| 2 | Leaving the raw REPL drops globals, so code sent in pieces lost its variables | found by a probe, before it broke a run | all the pieces run in one raw REPL session |
| 3 | A 7 KB script doesn't compile in a SAMD21's 15 KB heap | M0 MemoryErrors | small boards get whole top-level statements in pieces of about 600 bytes |
| 4 | No check after a test, so one crash failed every test after it | M0 and STM32 cascades | health check plus power-cycle recovery; the test that killed the board is charged once |
| 5 | Safe mode was invisible, because the banner prints before the port opens | an STM32 in safe mode looked healthy | ask `supervisor.runtime.safe_mode_reason` |
| 6 | The first serial open after a boot sometimes gets no reply (M0) | a false "did not recover" | retry once on a fresh open |
| 7 | `PWMOut.frequency` is only writable with `variable_frequency=True` | 7 boards | construct with it |
| 8 | `neopixel_write` takes a `DigitalInOut`, not a `Pin` | 8 boards | wrap the pin |
| 9 | Benchmarks asked for 32 to 38 KB contiguous blocks | 5 boards | bounded dict, in-place sort, freeing between sections, sizes scaled for small boards |
| 10 | The MSC test didn't check free space | S2 "no space left" | size the test file to the drive, or skip with a reason |
| 11 | The paste test wrote 4 KB without reading the echo | S2, 1 in 9 runs | read while writing; 0 failures in 5 runs since |
| 12 | The M0 NeoPixel pin is SWCLK, and an attached SWD probe locks it | would fail on the M0 | skip only on the M0 and only for that error (from `board-weirdisms.md`) |

Fixes 4 to 6 change the harness for good: a crash in any future fixture now shows up as one
clear `board_survived: HARD_FAULT` line instead of eight confusing failures.

## 7. What running this regularly buys

Mapped to the research:

| Research finding | What the smoke tier does about it |
|---|---|
| 39% of regressions are on the boot path | 5 of the 8 fixtures run that path on every board, on every run |
| Median 7.5 days from merge to report | about 2 minutes from flash to result, so a bad merge is caught the same day, before users see it |
| x.0 releases break the most | run the full tier set before each alpha, and on SDK, IDF, TinyUSB or MicroPython bumps |
| Shared-code refactors break ports nobody tested | every run covers five port families; the STM32 bug is exactly that kind of port-only break |
| Slowdowns go unreported for months | 114 timing metrics against a rolling baseline |
| Crashes and safe mode (24 regressions) | every fixture is followed by a safe-mode check, so a crash anywhere is caught |

**What it cannot catch yet:**

- **Peripherals with real signals**, such as GPIO, UART, SPI and I2C traffic or PWM measured by
  another pin. These need jumpers and a sensor (`peripherals_loopback`, wiring declared in
  farm.toml). That is about 30% of regressions, for an afternoon of parts.
- **Real displays.** Init, refresh and backlight need a TFT (`display_fixture`).
- **Networking, BLE, USB HID/MIDI, sleep and audio.** The nightly fixtures are written and
  statically checked, but haven't run on the farm yet.
- **macOS- or Windows-only USB bugs, true deep-sleep current, and what a glitch looks like on
  screen.** These need other rigs or a person.

## 8. How to use it

```sh
# once
rsync -a cp-regress/ bravo.local:~/cp-regress/
ssh bravo.local 'cd ~/cp-regress && python3 tools/check_fixtures.py --cp-tree ~/circuitpython \
  && python3 tools/selftest.py'

# seed baselines on a known-good release
python3 run.py --tier smoke --label baseline-10.3

# after each batch of merges: flash with fastcp.sh, then
python3 run.py --tier smoke --expect-version 11.0.0-alpha --label "main@<sha>"

# nightly
CPR_WIFI_SSID=... CPR_WIFI_PASSWORD=... python3 run.py --tier nightly

# one fixture or one board while debugging
python3 run.py --only perf_core --boards feather_stm32f405 --no-history
```

When something fails, open `results/<stamp>/summary.md`. A failure on one board points at a
port, and a failure on all of them points at shared code. Flash the builds between the last
pass and the first fail to find the merge.

## 9. Takeaways

1. **The first run tests the tests.** Of the 25 first-run failures, 19 were the suite's own
   bugs, 5 came after the M0 dropped off USB (cause still unknown), and 1 was the STM32 firmware
   bug. We expected a shakeout like this, and it's why a first run belongs on a known-good build.
2. **Real boards are not the unix port.** All eight fixtures passed or skipped cleanly on the
   unix port, yet most of the hardware failures (prompt timing, heap size, pin quirks, safe
   mode) cannot exist there.
3. **Recovery is part of the harness.** One crash used to fail everything after it. Now it
   shows up as a single line and the run carries on.
4. **Controls beat single observations.** The STM32 bug looked like a sort bug, then a print
   bug. Changing one thing at a time against a crashing control pinned it to one append.
5. **Farm notes save hours.** `board-weirdisms.md` already knew about the M0's NeoPixel pin and
   the SWD rung that brought the M0 back.
6. **It found a real bug on day one,** on a port with 57 bug reports in ten years. Small
   ports get the least testing, and that's where shared-code changes break things.

## 10. Next

1. Run the STM32 probe on 10.x to decide whether it is a regression, then bisect.
2. Seed the baselines on 10.3.x, so metrics and golden values mean something.
3. Run the nightly tier, with WiFi credentials from the environment.
4. Wire the loopback jumpers and an I2C sensor (the next 30%).
5. Hook the smoke tier to merges: pull the CI build, flash, run, post the grid on the PR.
