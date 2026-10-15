# Be more like Brent

Plan to align cp-regress with Brent's WipperSnapper HIL harness: the same fixture chain, a
local broker on bravo, flash-if-changed, and a prod guard on secrets. What we keep is what we
are already good at: eight real boards, per-board flash paths, and tests that catch breakage.

GitHub reporting is out of scope for now.

## Where we are vs his diagram

| His fixture | Scope | Ours today | Gap |
|---|---|---|---|
| `board` | session, finds port | `board` (module, per board, from `farm.toml` `usb`) | none, ours is wider |
| `flashed_firmware` | session, fw + base FS | flash by hand with `fastcp.sh`; `--expect-version` only refuses | **build it** |
| `flashed_fs` | module, if changed | `board.preserved()` + `write_file()` per test | **build it** |
| `serial` | function, resets | `device` (module) via `farmlib.Repl` | thin alias |
| `broker_address` | session, host:port | `farm.toml [host] ip` | **build it** |
| `broker` / `protomq` | session / function | public hosts (`adafruit.com`, `httpbin`, `api.github.com`) | **build it** |
| `base_secrets` | session, prod guard | `secret()` reads env names from `farm.toml [network]` | add guard |
| `secrets` | override per module | none, tests write `settings.toml` themselves | **build it** |

Fixture names below copy his so a test reads the same in both repos.

## The fixture chain we end up with

```
board ──> flashed_firmware ──> flashed_fs ──> serial / device
broker_address ──> base_secrets ──> secrets ──┘
broker (session) / protomq (function)
```

Same arrows as his diagram. The only change is that `board` is parametrized over eight boards
instead of finding one.

---

## Phase 1: secrets (`base_secrets`, `secrets`)

Smallest piece, no hardware risk. Do it first.

- **`base_secrets`** (session): builds the default `settings.toml` dict from `.env`:
  `CIRCUITPY_WIFI_SSID`, `CIRCUITPY_WIFI_PASSWORD`, `CIRCUITPY_WEB_API_PASSWORD`, plus
  `CPR_BROKER_HOST` / `CPR_BROKER_PORT` from `broker_address`.
- **Prod guard**: refuse the session (`pytest.exit`) if
  - the SSID is not in `farm.toml [network] allowed_ssids`, or
  - any key looks like a real Adafruit IO credential (`AIO_KEY`, `ADAFRUIT_AIO_*`, a 32-char
    `aio_` key), or
  - the broker host is not a private address (prevents pointing tests at io.adafruit.com).
- **`secrets`** (module): `base_secrets` merged with a module-level `SECRETS = {...}` override,
  the same way his per-module override works. Tests that now write `settings.toml` by hand
  (`settings_toml_test.py`, `wifi_join_test.py`, `web_workflow_test.py`) move to this.

Done when: a bogus SSID stops the run before any board is touched, and `wifi_join_test.py`
passes with its settings coming from `secrets`.

## Phase 2: `flashed_firmware` (session, per board)

Brings flashing inside the run. His version flashes once per session; ours does the same per
board.

- New option `--firmware DIR` (a build or CI artifact folder). Each board finds its image by
  `board_id` in the filename. No `--firmware` means "do not flash, assert version only", which
  is today's behaviour.
- New `farm.toml` key per board, `flash = "..."`, picking the path we already know works:

  | Board | `flash` | Path |
  |---|---|---|
  | metro_rp2040, metro_rp2350 | `uf2` | 1200-baud touch, wait for bootloader volume (guard: `INFO_UF2.TXT`), copy |
  | metro_esp32s2, metro_esp32s3 | `uf2` (tinyuf2 already on) / `esptool` fallback | one esptool call per download-mode entry |
  | metro_m4_airlift, metro_m0 | `swd` | openocd at 500 kHz, app base `0x4000` / `0x2000` |
  | feather_nrf52840 | `swd` | two runs, the image has a gap; never `mass_erase` |
  | feather_stm32f405 | `swd` | 1000 kHz, `0x08000000`, settled, no power cycle first |

- **First step, shell out to `fastcp.sh`** per board. Port the recipes into `lib/flash.py`
  only after the fixture shape is proven. (See open questions.)
- **Skip if unchanged**: sha256 of the image vs `history/flashed.json[board]`, *and*
  `boot_out.txt` still shows the matching version. Both must agree; a hash alone is a proxy.
- After flashing: `wait_ready()`, assert `boot_out.txt`, then lay down the base FS (idle
  `code.py`, empty `lib/`), like his "fw + base FS".
- Flashing runs under the existing per-board xdist group, so eight boards flash in parallel.
  `uhubctl` stays under `flock`.

Done when: `pytest --firmware ~/cp-tip-stage test/` flashes all eight, a second run with the
same folder skips every flash, and a wrong image fails at `boot_out.txt`, not in a test.

## Phase 3: `flashed_fs` (module, if changed)

His builds a LittleFS image because the V2 has no drive. Ours mounts as CIRCUITPY, so the
"image" is a staged directory and the write is a copy. Same semantics, different transport.

- Each test module may declare `FS = {"code.py": ..., "lib/": [...], ...}`; `settings.toml`
  comes from `secrets`.
- Stage the tree on the host, hash it, compare with `/.cpr_fs` on the drive. Copy only when
  different, then write the new hash last (so a half copy never looks current).
- Keep `board.preserved()` as the restore path and its md5 check.
- Libraries come from a pinned bundle folder (`--bundle DIR`), like his pinned protobuf repo:
  the test and the libs it runs against are named together.

Done when: two modules with the same `FS` cause one copy, and the M0 (`small`) still fits.

## Phase 4: `broker_address`, `broker`, `protomq`

The local server on bravo. This is his biggest idea and our biggest flakiness source.

- **`broker_address`** (session): `farm.toml [host] ip` (or `CPR_HOST_IP`) plus fixed ports.
  Use his numbers: MQTT `1884`, HTTP API `5173`. Add HTTPS `8443` for TLS tests.
- **`broker`** (session): starts the server, waits for the ports, stops it at session end.
- **`protomq`** (function): a client for the HTTP API. Clear state before the test, then
  `protomq.messages(topic)` and `protomq.publish(topic, payload)` so a test asserts on what
  the board *sent*, not just that it connected.
- Server choice, in order of preference:
  1. **ProtoMQ itself** under node, so we run the exact same thing he does.
  2. If it cannot carry plain (non-protobuf) payloads from `adafruit_minimqtt`, a small
     Python stand-in that exposes the same HTTP API shape on 5173.
- Self-signed CA for the HTTPS listener, installed on boards through `flashed_fs`.
- Move `net_sockets_test.py`, `net_tls_test.py`, `wifi_join_test.py` to the local server.
  Keep the public hosts as a separate `external` marker, non-blocking, like `farm.toml`
  already labels them.

Done when: network tests pass with bravo's WAN unplugged, and a test fails if the board
publishes the wrong payload.

## Phase 5: `serial` (function, resets)

Thin wrapper so tests that only need console output read like his.

- `serial.reset()` = soft reboot; `serial.expect(pattern, timeout)` = `Repl.read_until`.
- Syncs on `>>> ` before sending anything (the REPL eats the first keystroke).
- `device` stays as is for raw-REPL code runs.

---

## What we keep that he does not have

- Eight boards across five ports, flashed in parallel, each by its fastest path.
- Addressing by USB path at the moment of use; never cached tty or volume names.
- Health check and power cycle after each module, so a crash costs one teardown.
- Metric baselines and goldens in `history/`.
- Tiers, `needs` / `wiring` / `skip_boards` markers, partner locking for BLE.

## Open questions

1. **Get his `conftest.py`.** This plan is built from his diagram only. Reading his actual
   fixtures would let us match signatures and skip-if-changed logic exactly.
2. **`fastcp.sh` vs `lib/flash.py`.** Shelling out is faster to land; porting is easier to
   test on the unix fake. Recommend shell out first.
3. **ProtoMQ with plain payloads.** Verify before Phase 4 whether it accepts non-protobuf
   publishes; that decides option 1 vs 2.
4. **Allowed SSIDs.** Which networks count as the farm's test network for the guard.

## Order

Phase 1 → 2 → 3 → 4 → 5. Phases 1 and 4 do not depend on flashing and can swap if the broker
is more urgent. Each phase lands as its own change.

---

## Status, 2026-10-15

All five phases are implemented on branch `be-more-like-brent` and were run on bravo against six
boards (M4, M0, nRF52840, ESP32-S2, ESP32-S3, STM32F405; RP2040 and RP2350 were held out).

| Run | What | Result | Wall |
|---|---|---|---|
| 1 | flash all six from `~/cpr-fw/main-5548815`, chain + MQTT | 5 flashed; M4 erase landed, write did not (fixed: SWD retry); MQTT payload 3 decoded as protobuf (fixed: plain topics) | 163 s |
| 2 | same image, `--keep-fs` | 5 `unchanged`, M4 flashed in 16.7 s; MQTT byte-exact on S2 and S3 both ways | 54 s |
| 3 | same again, plus wifi_join | 6/6 firmware `unchanged`, 6/6 FS copied `nothing` | 44 s |
| 4 | no `--keep-fs` | every original restored, md5 matched independently | 19 s |

Open questions answered: ProtoMQ carries plain payloads (as Node Buffers), off `ws-d2b` topics;
allowed SSID is `foreverrun`; `lib/flash.py` ports the fastcp.sh recipes instead of shelling out,
because fastcp.sh flashes all boards from a fixed folder in one script.

Pinned: adafruit/protomq `9e2e1bf`, Wippersnapper_Protobuf `api-v2` `b3688d6`, Node 22.11.0 in
`~/node22`, library bundle `20261015` (10.x mpy loads on 11.0.0-alpha.1).

Found along the way (not harness bugs):
- `wifi.radio.hostname` default `cpy-metro_esp32s3-14c19f2fad04` cannot be assigned back:
  `ValueError: invalid hostname` (underscore from the board name). S2 and S3. wifi_join never
  ran before because bravo had no `.env`.

Still open: Brent's own conftest.py was not available, so signatures follow his diagram;
net_tls still mixes local and public hosts (the `external` marker exists but is not applied yet).
