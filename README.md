# cp-regress

Automatic checks for CircuitPython on real boards.

**[See the first-run results page →](https://claude.ai/artifact/HB8Lj4rmAnzNZhMCkiKWX3)**

## Why

New CircuitPython releases sometimes break things that used to work. Today, users find those bugs, about a week later.

## What

Eight real boards on a pegboard. After each code change, a script tries the basics on all eight and reports.

## How

Plug in, copy files, open the console, run code. Like a person would, but automatically, in minutes, every time.

![All eight boards run side by side](images/fig-parallel.png)

## Watch

90 seconds, music, no voice: the research, the tests, the results.

[![Watch: the sweep, the fixtures, the results](images/story-poster.jpg)](media/cp-regress-story.mp4)

A shorter 68-second animation of one run is in [media/cp-regress-first-run.mp4](media/cp-regress-first-run.mp4).

## Tested

CircuitPython main from 13 Oct 2026 (11.0.0-alpha.1). Eight boards, five chip families, 1,075 checks per run.

## First run

63 of 64 tests passed. The one failure is a real bug: one board crashes when a list grows.

![Final grid: 63 of 64 pass](images/fig-final-grid.png)

![STM32F405 crashes at item 4097](images/fig-stm32.png)

## Getting there

The first run mostly found bugs in the tests themselves. Five rounds of fixes took it from 39 to 63.

![From 39 to 63 in five rounds](images/fig-rounds.png)

![What failed on the first run, by cause](images/fig-first-run.png)

## What it covers

The startup path (USB, drive, console, code.py) is where most past breakage happened. Every run checks it.

![Where the smoke tests aim, by past regressions](images/fig-coverage.png)

## Research to results

Each test exists because of past bugs. This traces every one, from the research to its result.

![From the sweep to the tests to the results](images/fig-traceability.png)

Full table: [docs/FINDINGS-TO-TESTS.md](docs/FINDINGS-TO-TESTS.md), rebuilt by `tools/trace.py`.

## Run it

```sh
python3 run.py --tier smoke
```

Details: [docs/USAGE.md](docs/USAGE.md), [docs/FIRST-RUN.md](docs/FIRST-RUN.md), [CONTRACT.md](CONTRACT.md), [docs/fixtures/](docs/fixtures/).
