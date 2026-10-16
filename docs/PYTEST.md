# Running cp-regress with pytest

`run.py` still works. This is a second way to run the same farm, laid out like
[Adafruit_Wippersnapper_Python](https://github.com/adafruit/Adafruit_Wippersnapper_Python):
a root `conftest.py`, fixture modules in `test/fixtures/`, tests named `*_test.py`.

```sh
pip install pytest pytest-xdist pyserial      # Python 3.11+, or add tomli
pytest -v test/                                # smoke tier, every board in farm.toml
pytest -n 8 --dist loadgroup test/             # all boards at once, one worker per board
pytest --tier nightly --boards metro_rp2040,metro_esp32s3 test/
pytest test/hardware/repl_console_test.py -k eval
pytest --cpr-dry-run --tier release -q test/   # what would run where; touches no hardware
pytest --junitxml=results/junit.xml test/      # for CI; metrics land in <properties>
```

WiFi tests read `CPR_WIFI_SSID` and `CPR_WIFI_PASSWORD` from the environment or from `.env`
(copy `.env.example`; needs `python-dotenv`). They skip without them.

## Brent's fixture chain (flashing, files, secrets, broker)

Same names and arrows as Brent's WipperSnapper harness, on eight boards instead of one. Plan and
status: [BE-MORE-LIKE-BRENT.md](BE-MORE-LIKE-BRENT.md).

```sh
pytest -n 8 --dist loadgroup --tier nightly test/ \
  --firmware ~/cpr-fw/main-5548815 \
  --bundle ~/cpr-bundles/adafruit-circuitpython-bundle-10.x-mpy-20261015
```

| Fixture | Scope | What it does |
|---|---|---|
| `flashed_firmware` | once per board | `--firmware DIR`: flash the image whose name carries the board_id, by `farm.toml` `flash.method` (`uf2`, `esp_uf2`, `swd`). Skipped when history/flashed.json's sha256 matches AND boot_out.txt reports the version that flash produced. `--flash-force` flashes anyway |
| `flashed_fs` | module | idle `code.py`, `settings.toml` from `secrets`, the module's `FS = {...}` and `LIBS = [...]` from `--bundle`. Copies only files whose bytes differ. Originals are backed up to history/fs-backup/ and restored, md5 checked, at the end of the run; `--keep-fs` leaves them (later runs keep the first original) |
| `base_secrets` | session | settings.toml values from `.env`, plus the broker address. Prod guard: stops the run if the SSID is not in `[network] allowed_ssids`, an Adafruit IO credential is in the environment, or the broker is not private |
| `secrets` | module | `base_secrets` plus the module's `SECRETS = {...}` (None deletes a key) |
| `broker_address` | session | `(host_ip, 1884)` |
| `broker` | session | starts adafruit/protomq (`[broker]` in farm.toml) if nothing answers on :5173; stops it at the end only if this run started it (`--broker-keep` leaves it). Never POSTs `/reset` unless `--broker-reset` is given, and then only if this run started ProtoMQ or no client is connected |
| `protomq` | test | ProtoMQ's HTTP API for client id `cpr-<board>`: `track()`, `deliveries()`, `outbox()`, `echo()`, `wait_subscribed()`. No global `/reset`: boards share the broker in parallel |
| `serial` | test | `reset()` (soft reboot), `expect(regex)`, `send(line)` on the console |

Use plain topics with ProtoMQ (`cpr/<board>/d2b`): it decodes anything on a `ws-d2b` topic as a
protobuf, and a plain payload that happens to parse comes back as `{}`.

## How it maps

| run.py | pytest |
|---|---|
| one fixture, `tests/<id>.py` | one module, `test/hardware/<id>_test.py` |
| one named check | one test function, `test_<check>` |
| a board | the `board` parameter: `test_eval[metro_rp2040]` |
| `--tier`, cumulative | `--tier`, or `-m smoke` / `-m nightly` for one tier only |
| `--jobs 8` | `-n 8 --dist loadgroup` |
| `META["needs"]`, `wiring`, `skip_boards` | `pytestmark` markers; the module skips on that board |
| health check after each fixture, power cycle on a crash | same, in the `board` fixture: a crash is one teardown error |
| `ctx.metric()`, baselines in `history/` | the `metric` fixture, same `history/metrics.jsonl` |

Each test reads one thing the board reported, so a failure names it:
`FAILED test/hardware/repl_console_test.py::test_paste_4k[metro_m0]`.

## Fixtures (test/fixtures/)

| Fixture | Scope | What it gives you |
|---|---|---|
| `board` | module, per board | `farmlib.Board`: `present()`, `repl()`, `write_file()`, `read_file()`, `preserved()`, `power_cycle()`, `mount()`, `caps`, `wiring` |
| `device` | module | `device.run(code, timeout)` runs CircuitPython code with `check()/skip()/metric()/golden()` and returns `DeviceResults` |
| `metric` | test | `metric(name, value, unit, better="lower", tolerance=0.25)`: records, and fails if worse than the median of the last 5 runs |
| `golden` | test | `golden(name, value)`: first value per board is stored; later runs must match |
| `partner` | test | `partner("ble")`: another board with that cap, locked for the test |
| `secret` | session | `secret("ssid_env")`: the env var farm.toml `[network]` names |
| `farm_config`, `farm_boards` | session | farm.toml as a dict; every board in it |

`DeviceResults`: `expect(name)` asserts that check passed (a device `skip()` becomes a pytest
skip), `value(name)`, `detail(name)`, `metric_record(name)`, `golden_value(name)`,
`expect_no_exception()`. If the device code raised or the board stopped answering, the checks it
never reached skip as "not reached"; the crash is charged once.

## Converting a fixture (the rules every `*_test.py` follows)

1. **File**: `test/hardware/<id>_test.py`, SPDX `MIT-0` header, a module docstring that says
   "pytest version of tests/<id>.py" and lists the targeted issues.
2. **Markers mirror META** in `pytestmark`: the tier (`pytest.mark.smoke`), then `needs(...)`,
   `wiring(...)`, `skip_boards(...)`, `skip_ports(...)` when non-empty, `targets(...)`, and
   `destructive` / `multi_board` when true. Leave out empty ones.
3. **Device code is copied byte for byte**, under the same constant names. Do not edit it. Fix
   device-side bugs in `tests/<id>.py` first, then copy again.
4. **One test per check**, named `test_<check name>`, in the original's order. Families of
   checks generated in a loop become one `@pytest.mark.parametrize` test over a module-level
   list of names.
5. **Run device code once per board** in a module-scoped fixture (`device.run(...)`), then
   assert in tests with `results.expect("name")`. Device metrics:
   `rec = results.metric_record(name); metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"))`.
   Device goldens: `golden(name, results.golden_value(name))`.
6. **Host-side checks**: `ctx.check(name, ok, detail)` becomes `assert ok, detail`;
   `ctx.skip(name, why)` becomes `pytest.skip(why)`; `ctx.metric` the `metric` fixture;
   `ctx.secret` the `secret` fixture; `ctx.other(cap)` the `partner` fixture; `ctx.cfg`
   `farm_config`; `ctx.log` `logging.getLogger(__name__).info`.
7. **Steps that change the board** (write `code.py`, reboot, sleep) run once in a module-scoped
   fixture that records what it saw and restores the board in `finally` (`board.preserved()`).
   Tests then assert on the record. Catch exceptions inside module-scoped fixtures and keep
   them in the record, so one error fails one test instead of erroring every test that uses
   the fixture.
8. **Nothing touches hardware at import or collection time.**
9. **Check your work**: `python3 tools/pytest_parity.py <id>` must print `ok`, and
   `pytest --cpr-dry-run --tier release -q test/hardware/<id>_test.py` must collect and skip
   cleanly.
