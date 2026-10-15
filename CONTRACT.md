# Test module contract

Every fixture is one file, `tests/<id>.py`, with a `META` dict and a `run(board, ctx)`
function. Copy `tests/_template.py`. Read `lib/farmlib.py` and `lib/devtest.py` before
writing anything; use only what they provide plus the Python 3.11 standard library and
pyserial. No other host packages.

## META keys

| key | meaning |
|---|---|
| id | file name without `.py` |
| title | one line |
| category | USB, Display, Network, BLE, Core, Peripherals, Power, Audio |
| tier | `smoke` (every merge, under 2 min per board), `nightly`, `release` |
| needs | board caps from `farm.toml` (`native_usb`, `uf2`, `wifi`, `ble`, `esp32spi`, `usb_host`, `reset_ok`, `small`) |
| wiring | `farm.toml` wiring keys; the test skips until they exist |
| skip_boards / skip_ports | never run there |
| automation | `auto` (runs on the farm as-is), `fixture` (needs added hardware), `assisted` (needs a person) |
| destructive | True if it rewrites boot.py / settings.toml or resets the board |
| multi_board | True if it uses a partner from `ctx.other(cap)` |
| timeout | seconds, a soft budget |
| targets | GitHub issue numbers in adafruit/circuitpython this would have caught |

## ctx API

- `ctx.check(name, ok, detail="", value=None)` one named pass/fail
- `ctx.skip(name, why)` not applicable
- `ctx.metric(name, value, unit, better="lower"|"higher", tolerance=0.25)` compared with the
  median of earlier runs on the same board; fails when worse than tolerance
- `ctx.device(body, timeout)` runs `body` on the board in the raw REPL with `check`, `skip`,
  `metric`, `guard` predefined (from `lib/devtest.py`) and folds results in. Returns
  `(results, stdout, stderr)`. An uncaught device exception becomes a failed check.
- `ctx.other(cap)` another present board with that cap (multi-board tests)
- `ctx.secret("ssid_env")`, `ctx.secret("password_env")` wifi credentials from the environment
- `ctx.golden(name, value)` exact match against the first value recorded for this board; device side: `golden(name, value)`
- `ctx.cfg` the parsed farm.toml, `ctx.outdir` results directory, `ctx.log(msg)`

## board API (lib/farmlib.py)

`present() vid_pid() devnum() interfaces() tty() data_tty() ttys() block_devs() mount()
boot_out() version() write_file(rel, data) read_file(rel, binary) remove_file(rel)
power_cycle() wait_ready(timeout, need_mount) repl() preserved(files)`, attributes
`name board_id usb port caps wiring power`.

`Repl`: `prompt() exec_raw(code, timeout) run_device(body, timeout) soft_reboot()
read_until(token, timeout) read_for(seconds) write(bytes) drain()`; use as a context manager.

## Hard rules

1. Anything that writes code.py, boot.py or settings.toml runs inside `with board.preserved():`.
   After restoring, reset the board back to its normal state (soft reboot or
   `microcontroller.reset()` only on `reset_ok` boards, otherwise `board.power_cycle()`), then
   `board.wait_ready()`.
2. Never cache tty paths or mount points across a reset: call `board.tty()` / `board.mount()` again.
3. Never call `microcontroller.reset()` on a board without the `reset_ok` cap (metro_m0).
4. Never use sudo. Never type keystrokes into the host (no keyboard HID reports).
5. Every CircuitPython API you use must exist in the CircuitPython checkout under test
   (`shared-bindings/<module>/`; `tools/check_fixtures.py --cp-tree` checks this). Grep it. If a module may be missing on a board, check with
   `try: import x` on the device and `skip` instead of failing.
6. Device code must be CircuitPython-compatible: no f-strings with `=`, no walrus, no
   `typing`, no `dataclasses`. Keep it small enough for a SAMD21 when the test runs there.
7. Report many small named checks, not one big one. A test that has only printed PASS is
   untested, so make sure failure paths produce a failed check with a useful detail.
8. Prefer timing numbers as `metric` (connect time, boot time, throughput): slowdowns are a
   regression class users only find later.
9. Host tests that serve network traffic bind to `farmlib.host_ip()` on an ephemeral port and
   shut down in a `finally`.

## Docs

For each test also write `docs/fixtures/<id>.md`:

```
# <id>: <title>
Tier / automation / boards
## Regressions it targets      (issue numbers with one-line description each)
## What it checks              (bullet per named check)
## Setup                       (wiring, env vars, host packages; "none" if none)
## What is hard to automate    (honest: what this cannot catch, what needs a person)
## Run                         ./run.py --only <id> --boards <...>
```

## Self-check before you finish

```
python3 -m py_compile tests/<id>.py
python3 -c "import ast,sys; m=ast.parse(open('tests/<id>.py').read())"     # and compile every DEVICE string:
python3 -c "import importlib.util as u; s=u.spec_from_file_location('t','tests/<id>.py'); m=u.module_from_spec(s); s.loader.exec_module(m); [compile(v,'dev','exec') for k,v in vars(m).items() if k.startswith('DEVICE') and isinstance(v,str)]"
python3 run.py --list
python3 run.py --dry-run --only <id>
```
