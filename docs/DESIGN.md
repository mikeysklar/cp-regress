# cp-regress design

**Status: outline.** Facts are filled in from the code on `brent-ports`. Each section still
needs a diagram (marked *Diagram*) and a short paragraph on why it is built this way.

How CircuitPython boards are brought into hardware-in-the-loop testing, what runs on them,
the fixtures that manage them, the pytest layer, and how results are stored and posted.
For running it, see [USAGE.md](USAGE.md) and [PYTEST.md](PYTEST.md). For writing a test, see
[../CONTRACT.md](../CONTRACT.md).

## Contents

1. [The farm at a glance](#1-the-farm-at-a-glance)
2. [Bringing a board into the farm](#2-bringing-a-board-into-the-farm)
3. [Power, flashing and recovery](#3-power-flashing-and-recovery)
4. [What the tests check](#4-what-the-tests-check)
5. [Fixtures](#5-fixtures)
6. [The pytest layer](#6-the-pytest-layer)
7. [Results: format, storage, reports, posting](#7-results-format-storage-reports-posting)
8. [Relationship to Brent's HIL repo](#8-relationship-to-brents-hil-repo)
9. [Known gaps](#9-known-gaps)

---

## 1. The farm at a glance

*Diagram: bravo → uhubctl hubs → boards; SWD probes; ProtoMQ on bravo; results → reports → Adafruit IO.*

- One Linux host (bravo) runs everything. Boards connect through USB hubs whose port power
  `uhubctl` can switch, without sudo (a udev rule allows it).
- 14 boards in `farm.toml`: 8 Metro/Feather boards on hub 3-3 (they ran the first run) and 6
  newer or non-native-USB boards on hub 3-6.
- Two runners share one library: `run.py` (the original, one `@@R` result stream per test) and
  pytest (`test/`, one pytest test per check, one xdist worker per board).
- Nothing custom runs on the boards: stock CircuitPython plus code sent over the raw REPL.

## 2. Bringing a board into the farm

### 2.1 Addressing: the hub port is the identity

- `usb = "3-3.2"` is the only fixed address. Everything else is looked up from it each time it
  is needed, because boards re-enumerate on every reset (`lib/farmlib.py` `Board`):
  - `present()`: `/sys/bus/usb/devices/<usb>` exists
  - `ttys()` / `tty()` / `data_tty()`: `/dev/serial/by-path/*usb-0:<path>:1.*`, by interface number
  - `block_devs()`, `mount()`: the mount is accepted only if its `boot_out.txt` names this `board_id`
  - `boot_volume()`: the UF2 bootloader drive (`INFO_UF2.TXT`, no `boot_out.txt`)
  - `web()`: boards without native USB, found by MAC over mDNS, `board_id` checked via `/cp/version.json`
- Rule: never cache a tty path or mount point across a reset.

### 2.2 farm.toml keys

| key | meaning |
|---|---|
| `name`, `board_id`, `port` | farm name, CircuitPython board id (checked against `boot_out.txt`), port family |
| `usb` | sysfs hub path, the board's identity |
| `caps` | `native_usb`, `uf2`, `wifi`, `ble`, `esp32spi`, `usb_host`, `reset_ok`, `small` |
| `wiring` | jumpers or fixtures attached; tests that need them skip until listed |
| `power` | `r`/`w` uhubctl repeat and wait, `settle`, `tries`, `also` (extra hub ports cut with it) |
| `fs` | `drive` (CIRCUITPY, default), `repl` (raw REPL base64), `web` (web workflow `/fs/`) |
| `console_baud` | console is a real UART; device code is sent in ~600-byte pieces |
| `reset = "rts"` | EN wired to the UART bridge's RTS; `line_reset()` |
| `settings` | `settings.toml` keys this board always needs |
| `flash` | `uf2`, `esp_uf2`, or `swd`, with method keys |
| `run_tests` / `skip_tests` / `dns_tests` | pytest selectors (folder, file or `::test`) |
| `lab` | `{ bin, drive, args }`: a CP Desktop Lab on this host (`lib/desklab.py`, [USAGE.md](USAGE.md)); `usb` is then any unique name |

Every entry is validated at session start (`test/fixtures/farm.py` `validate_board_entry`,
`validate_farm`): no duplicate name, usb or probe, no board's hub path below another's, and
`uhubctl` must not be sudo. `pytest --list-boards` validates and prints the farm without
touching any board.

### 2.3 The boards

| board | port | caps | fs | flash |
|---|---|---|---|---|
| metro_rp2040 | raspberrypi | native_usb uf2 reset_ok | drive | uf2 |
| metro_m4_airlift | atmel-samd | native_usb uf2 esp32spi reset_ok | drive | swd |
| metro_m0 | atmel-samd | native_usb uf2 small | drive | swd |
| feather_nrf52840 | nordic | native_usb ble reset_ok | drive | swd |
| metro_esp32s2 | espressif | native_usb uf2 wifi reset_ok | drive | esp_uf2 |
| metro_rp2350 | raspberrypi | native_usb uf2 reset_ok | drive | uf2 |
| metro_esp32s3 | espressif | native_usb uf2 wifi ble reset_ok | drive | esp_uf2 |
| feather_stm32f405 | stm | native_usb reset_ok | drive | swd |
| feather_esp32_v2 | espressif | wifi reset_ok | web | none |
| esp32c5_devkitc | espressif | wifi ble reset_ok | web | none |
| korvo_esp32s31 | espressif | reset_ok | repl | none |
| p4gpio | espressif | native_usb reset_ok | drive | none |
| nrf54lm20dk | zephyr-cp | native_usb ble reset_ok | drive | none |
| siwx917_dk2605a | zephyr-cp | wifi ble reset_ok | repl | none |

Boards with `flash = none` run whatever build is on them, so `--firmware` runs leave them out.

### 2.4 Adding a board (to write)

Checklist: hub port, `board_id`, caps, power values that actually cut the port, flash method,
`--list-boards`, `fixture_chain_test.py`.

## 3. Power, flashing and recovery

*Diagram: the recover() escalation ladder.*

### 3.1 Power (`farmlib.Board.power_cycle`, `wait_ready`)

- Groups `usb` plus `also` ports by hub. One `uhubctl -a cycle` call, or off/sleep/on.
- Every uhubctl call takes a blocking `flock` on `/tmp/uhubctl.lock`, shared with
  `farm-helpers.sh`, so two runs never switch hubs at the same time.
- `wait_ready()` polls present + tty + mount (drive boards) for up to 60 s + settle.

### 3.2 Flashing (`lib/flash.py`, `test/fixtures/flashing.py`)

| method | boards | steps |
|---|---|---|
| `uf2` | RP2040, RP2350 | 1200-baud touch → wait for boot volume → copy UF2 |
| `esp_uf2` | ESP32-S2, ESP32-S3 | UF2 family check → touch → one esptool call writes bootloader, partitions, ota_data, tinyuf2 → copy UF2 |
| `swd` | M0, M4, nRF52840, STM32F405 | split the UF2 into runs → `openocd` `flash write_image erase` per run → `reset run`; on failure, SWD rescue and a second pass |

Common to all: `refuse_open_port` (no process may hold the tty: 3 checks, 0.5 s apart) →
method → `wait_ready` → read the version. The fixture records the image's sha256, skips a
reflash if it is unchanged, and checks the build's commit (`g<hash>` in `boot_out.txt`).
`--flash-erase` is opt-in and refused where erasing is unsafe (nRF52, RP2040/RP2350 unless
`nuke_uf2`). `FlashResult` records each phase's time: enter bootloader, erase, write,
ready, total.

### 3.3 Health check and recovery (`run.py` `board_health`, `recover`)

- Health: on the bus → tty → raw REPL prints `cpr 42` and
  `safe_mode_reason == NONE` → drive not mounted read-only.
- Recovery ladder: REPL reset (`reset_ok`) → RTS line reset (`reset = "rts"`) →
  power-cycle up to `power.tries` → SWD reset (swd boards). Health is re-checked after each step.
- Run before the first test and after every test. The test that killed the board gets
  `board_survived: FAIL`, once. If recovery fails, the board's later tests report `board_down`.
- Per-board lock: `flock` on `/tmp/cp-regress-<usb>.lock`.

### 3.4 Restoring the board's files (`Board.preserved()`)

Snapshots `code.py`, `boot.py`, `settings.toml` (a missing file is recorded as absent),
restores them in `finally`, then verifies by sha256 over the REPL. On drive boards with no
REPL answer, it falls back to a host-side md5 read.

## 4. What the tests check

*Diagram: sweep → tests → results (from FINDINGS-TO-TESTS.md).*

### 4.1 Why these tests

From the 10-year sweep of adafruit/circuitpython (11,479 issues and PRs): 339 regressions,
131 (39%) on the boot path, median 7.5 days from merge to first report. Every test lists
the issue numbers it would have caught (`targets`): 158 entries, 143 distinct issues.
`tools/trace.py` builds [FINDINGS-TO-TESTS.md](FINDINGS-TO-TESTS.md).

### 4.2 The 23 test modules (`tests/*.py`)

Tiers: 8 smoke (every merge, under 2 min per board), 14 nightly, 1 release.

| id | category | tier | needs | destructive | targets |
|---|---|---|---|---|---|
| code_py_lifecycle | Core | smoke | | yes | 7 |
| displayio_headless | Display | smoke | | | 19 |
| perf_core | Core | smoke | | | 6 |
| peripherals_selfcheck | Peripherals | smoke | | | 2 |
| repl_console | USB | smoke | | | 8 |
| settings_toml | Core | smoke | | yes | 2 |
| usb_enumeration | USB | smoke | native_usb | | 4 |
| usb_msc_rw | USB | smoke | native_usb | | 6 |
| audio_headless | Audio | nightly | | | 3 |
| ble_pair | BLE | nightly | ble, multi-board | | 1 |
| core_vm | Core | nightly | | | 7 |
| display_fixture | Display | nightly | wiring: display | | 3 |
| memory_soak | Core | nightly | | | 6 |
| net_sockets | Network | nightly | wifi | | 12 |
| net_tls | Network | nightly | wifi | | 10 |
| peripherals_loopback | Peripherals | nightly | wiring | | 5 |
| reset_cycles | Core | nightly | | yes | 2 |
| sleep_alarm | Power | nightly | | yes | 8 |
| usb_cdc_data | USB | nightly | native_usb, reset_ok | yes | 10 |
| usb_hid_midi | USB | nightly | native_usb | | 13 |
| web_workflow | Network | nightly | native_usb, wifi | yes | 12 |
| wifi_join | Network | nightly | wifi | | 5 |
| bootloader_roundtrip | USB | release | uf2, reset_ok | yes | 7 |

### 4.3 pytest-only hardware tests (no `tests/` counterpart)

| file | covers |
|---|---|
| farm_addressing_test | each board found by hub path; port power state read |
| fixture_chain_test | board → flashed_firmware → flashed_fs → serial, on every board |
| identity_crash_test | chip UID, soft-reset banner, a negative test that the Traceback watch fails |
| mqtt_boot_test | board reaches ProtoMQ after a real cold power-on |
| mqtt_broker_test | byte-exact MQTT round trip on `cpr/<board>/d2b` and `b2d` |
| mqtt_catcher_test | board publishes results and logs; host catches them, plays `cpr-ack.json` |
| net_wifi_test | supervisor joins WiFi from `settings.toml`, survives a hard reset |
| power_boot_test | console across uhubctl cycles and soft reboots (opt-in: `-m power_boot`) |
| serialmon_hw_test | SerialMonitor on real boards: Ctrl-D, hard reset, timestamps |
| settings_scenarios_test | named `settings.toml` scenarios: good, malformed, wrong types, unicode |

### 4.4 Harness self-tests (`test/harness/`)

22 offline test files against fakes (pty board, fake ProtoMQ, mocked openocd and subprocess):
`python3 -m pytest test/harness -q`. No hardware needed.

## 5. Fixtures

*Diagram: the fixture dependency chain board → flashed_firmware → flashed_fs → device / serial / catcher.*

| fixture | scope | file | manages |
|---|---|---|---|
| `board` | module | farm.py | lock, applicability skips, health check, power-cycle if unhealthy |
| `_not_after_crash` | function, autouse | farm.py | fails a module once if the board crashed, then power-cycles |
| `farm_config`, `farm_boards` | session | farm.py | parsed farm.toml; all boards |
| `partner` | function | farm.py | locks a second board with a cap, or skips |
| `secret` | session | farm.py | env var named in `[network]` |
| `flashed_firmware` | module | flashing.py | flash this board's image if changed (`--firmware`) |
| `flashed_fs` | module | flashing.py | write module files + `settings.toml`; `_fs_backups` restores CIRCUITPY at session end |
| `base_secrets`, `secrets` | session, module | secrets.py | settings from `.env` with the prod SSID guard; per-module overrides |
| `secrets_*` scenarios | session/function | secrets.py | named `settings.toml` scenarios rendered to tmp |
| `broker`, `broker_address` | session | broker.py | start ProtoMQ if not up; stop only if this run started it |
| `protomq`, `catcher` | function | broker.py | ProtoMQ API and a per-test mailbox for `cpr-<board>` |
| `serial` | function | console.py | SerialMonitor; per-test log on teardown |
| `device` | module | device.py | run device code over the raw REPL → `DeviceResults` |
| `metric`, `golden` | function | history.py | compare with the median of the last 5 runs; first-value match |
| `device_uid` | function | identity.py | chip UID checked against `history/device_uid.json` |

Fixture use per test file: (to fill from the agent matrix).

## 6. The pytest layer

### 6.1 Selection

- `--tier smoke|nightly|release` (cumulative; unmarked tests count as nightly), `--boards a,b|all`.
- `farm.toml` `run_tests` drops items, `skip_tests` and `dns_tests` mark them skipped. These
  narrow the tier and markers, never widen them.
- `needs`, `wiring`, `skip_boards`, `skip_ports` are enforced at runtime by `not_applicable`.

### 6.2 One worker per board

`pytest_generate_tests` parametrizes every test with a `board` argument at module scope.
Each item gets `xdist_group(<board>)`, so `pytest -n 8 --dist loadgroup` keeps each board on
one worker. `multi_board` tests take a farm-wide lock first.

### 6.3 One device run, many tests

The `device` fixture runs the module's device code once per board. Each named check becomes
its own `test_<check>`; a device skip becomes a pytest skip, a crash before a check is
"not reached" (DNF).

### 6.4 Options

`--farm --boards --tier --label --expect-version --no-history --cpr-dry-run --firmware
--flash-force --flash-erase --bundle --keep-fs --broker-keep --broker-reset
--serial-log-dir --list-boards`. (Table with one line each: to write.)

### 6.5 Parity with run.py (`tools/pytest_parity.py`)

Checks each pytest module against its `tests/<id>.py`: markers match META, device code
strings are byte-identical, every check name appears. Exits 1 on any gap.

## 7. Results: format, storage, reports, posting

*Diagram: @@R line → row → results.jsonl / junit → summary.md / hil_report → Adafruit IO.*

### 7.1 On the board

`lib/devtest.py` is prepended to device code. `check()`, `skip()`, `metric()`, `golden()`
each print one line: `@@R {"name": ..., "ok": ..., "detail": ..., "value": ...}`.
`skip` sets `ok` to null; metrics always send `ok: true` and the host decides.

### 7.2 On the host

| kind | rule |
|---|---|
| check | pass/fail as sent |
| metric | median of the last 5 runs, same board and key; fail beyond tolerance (default 25%); pytest adds an absolute `slack` |
| golden | first value per board, in `history/golden.json`; later mismatches fail |
| host rows | `_duration`, `board_survived`, `_recovered`, `board_down`, `harness`, `applicable` |

### 7.3 On disk

| path | contents |
|---|---|
| `results/<stamp>/results.jsonl` | one row per check (run.py) |
| `results/<stamp>/summary.md` | version table, test × board grid, failures |
| `results/pytest-<stamp>/serial/<board>/*.serial.log` | per-test console, timestamped |
| `results/catcher/<board>.jsonl` | every MQTT record caught |
| `history/metrics.jsonl` | append-only metric history |
| `history/golden.json` | golden values |

### 7.4 Reports and posting

- `tools/hil_report.py`: junit XML + serial logs + catcher logs → Markdown. Test × board grid
  and, for each failure, the pytest message, a serial window around the evidence line and the
  matching ProtoMQ records. Exit 1 on any failure.
- `tools/aio_upload.py`: posts passed / failed / errors / `fail-<board>` and chosen metrics to
  the Adafruit IO group `cp-regress`. Only with `--send`, only to the one allowed account,
  credentials from a file only the owner can read, at most 10 feeds.
- `tools/overnight.sh`: Brent-module nightly phase, then the smoke phase, then `hil_report`
  for each and `aio_upload` (only when credentials exist and `DRY` is unset). Never
  flashes, erases or resets the broker.

### 7.5 Results over MQTT

Network tests publish to `cpr/<board>/results` and `cpr/<board>/log` on the local ProtoMQ.
Tests read them through the catcher, with the same `expect()` / `assert_none()` / `mark()`
as SerialMonitor. They are not merged into `run.py` rows; `hil_report` joins them to the
serial window by nodeid and time.

## 8. Relationship to Brent's HIL repo

From [BRENT-SYNC.md](BRENT-SYNC.md): 62 rows ported, 83 equivalent, 40 watched, 0 pending.
Deliberately different: no sudo uhubctl, no tty port cache, no DTR/RTS board reset except
`reset = "rts"`, no single-board `--board` fixture, no per-test ProtoMQ reset, no Adafruit IO
credentials on the boards. (Table to write.)

## 9. Known gaps

Unverified notes from the code read. Each needs a check before it is fixed or documented as
behaviour.

- `wait_ready()` docstring says it waits the settle time; the code does not, and callers sleep
  it themselves.
- `preserved()` restores files in one loop with no per-file `try`: one failed write would skip
  the rest and the verification.
- `tools/trace.py` hard-codes the section 1 numbers in FINDINGS-TO-TESTS.md (for example
  "63 of 64") instead of computing them.
- `dns_tests` is used in `farm.toml` but missing from its header comment.
- `pytest_parity.py` does not check check order, though PYTEST.md asks for it.
- The `destructive` marker has no runtime effect in pytest.
