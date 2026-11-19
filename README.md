# cp-regress

Automatic checks for CircuitPython on real boards.

[![Watch the 2:53 video: how cp-regress works](images/how-it-works-poster.jpg)](https://mikeysklar.github.io/cp-regress/media/cp-regress-how-it-works.mp4)

*Click to play: 2:53, narrated. The farm, farm.toml, a test, the `@@R` result line, recovery, pytest, and the first run.*

**[See the first-run results page →](https://claude.ai/artifact/HB8Lj4rmAnzNZhMCkiKWX3)**

## Contents

- [Why](#why) · [What](#what) · [How](#how) · [Tested](#tested)
- [First run](#first-run) · [Getting there](#getting-there)
- [What it covers](#what-it-covers) · [Research to results](#research-to-results)
- [Run it](#run-it) · [Or with pytest](#or-with-pytest)
- [Design and reference docs](#design-and-reference-docs)

## Why

New CircuitPython releases sometimes break things that used to work. Today, users find those bugs, about a week later.

## What

Eight real boards on a pegboard. After each code change, a script tries the basics on all eight and reports.

## How

Plug in, copy files, open the console, run code. Like a person would, but automatically, in minutes, every time.

![All eight boards run side by side](images/fig-parallel.png)

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

## Or with pytest

Same tests, same boards, pytest style. Every check is its own test.

```sh
pytest -n 8 --dist loadgroup test/
```

Details: [docs/PYTEST.md](docs/PYTEST.md).

## Design and reference docs

| Doc | What it holds |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | How it works: board addressing, flashing, recovery, tests, fixtures, pytest, results and posting |
| [docs/USAGE.md](docs/USAGE.md) | Running `run.py`: tiers, options, output |
| [docs/PYTEST.md](docs/PYTEST.md) | Running under pytest: options, markers, workers per board |
| [CONTRACT.md](CONTRACT.md) | Writing a test: `META` keys, the `ctx` and `board` APIs, hard rules |
| [docs/fixtures/](docs/fixtures/) | One page per test: what it checks and the issues it targets |
| [docs/FINDINGS-TO-TESTS.md](docs/FINDINGS-TO-TESTS.md) | Every sweep finding, the test built for it, and its result (built by `tools/trace.py`) |
| [docs/FIRST-RUN.md](docs/FIRST-RUN.md) | The first hardware run, round by round, and the STM32F405 crash |
| [docs/BRENT-SYNC.md](docs/BRENT-SYNC.md) | What is ported from Brent's HIL repo, what is equivalent, what is watched |
| [docs/BE-MORE-LIKE-BRENT.md](docs/BE-MORE-LIKE-BRENT.md) | The plan for aligning with Brent's harness |
