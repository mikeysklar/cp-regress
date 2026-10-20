# Brent sync

Brent's WipperSnapper HIL harness is a moving target. This file records which of his commits we
have read, where each of his pieces lives in cp-regress, and what we chose not to bring over.
`tools/brent_sync.py` reads the three tables below, so keep their shape.

```
python3 tools/brent_sync.py              # what changed upstream since the shas below
python3 tools/brent_sync.py brent -v     # one repo, and list files outside the watch list
python3 tools/brent_sync.py --symbols brent --head brent=cf64e03   # audit this table
python3 tools/brent_sync.py --record     # after porting or deciding: move the shas forward
```

It only reads GitHub (gh api GETs, or `--via git` for a cached partial clone under
`~/.cache/cp-regress/brent-sync`). It never pushes or comments, and touches no board.

Exit status: 0 nothing new, 2 new commits and every change is mapped, 3 something is UNMAPPED,
1 error.

## How to sync

1. Run the tool. Read the commits it lists.
2. For each changed symbol, open our file in the Ours column and decide: port it, note it, or
   add it to the not-ported list with a reason. A symbol marked UNMAPPED has no row yet; a
   symbol marked NEW matched only a wildcard row, so check that row still fits.
3. Keep his names and shapes where they fit, and cite him in the docstring as
   `after Brent's X, hil_board_utils.py:NNN @ <sha>`. The tool finds those citations and
   reports any whose symbol changed or moved.
4. When every change has a row, run `--record`. It writes the new shas and updates the Line
   column of exact rows to the new head. Commit this file with the port.

A "HISTORY REWRITTEN" line means he force-pushed and some synced commits are gone. The diff
is still against the recorded sha, which GitHub keeps for a while; if it is gone, pass
`--since brent=<sha>` with the closest commit you know you read. `--record` works with `--since`
(not with `--head`).

## Last synced

Watch lists use fnmatch, where `*` crosses `/`. A leading `!` excludes. Files outside the
watch list are only counted (his branch also carries firmware changes under `src/`).

| Key | Repo | Branch | SHA | Synced | Watch |
|---|---|---|---|---|---|
| brent | brentru/Adafruit_Wippersnapper_Arduino | add-hil-ci-cd | 90cd5af | 2026-10-16 | `hil/* .github/* !*/__pycache__/* !*.pyc` |
| protomq | adafruit/protomq | main | 9e2e1bf | 2026-10-16 | `* !node_modules/*` |
| protobuf | adafruit/Wippersnapper_Protobuf | api-v2 | b3688d6 | 2026-10-16 | `*` |

## Mapping

Brent column: `path` matches the file and every symbol in it; `path::Name` matches that
function, class, constant, JSON key or HTTP route, and for a class its methods too. Globs are
allowed; the most specific row wins, and an exact not-ported row beats a broader mapping row.
Line is his line at the synced sha. Status: `ported` (on be-more-like-brent), `pending` (a
port branch is in flight, named in Notes), `equivalent` (we already do this our own way),
`watch` (nothing to port; read the change).

| Repo | Brent | Line | Ours | Status | Notes |
|---|---|---|---|---|---|
| brent | `hil/hil_board_utils.py::HilBoardUtilsError` | 89 | `lib/farmlib.py::FarmError` | equivalent | one error type for the farm |
| brent | `hil/hil_board_utils.py::PortError` | 113 | `lib/farmlib.py::FarmError` | equivalent | |
| brent | `hil/hil_board_utils.py::UhubctlError` | 117 | `lib/farmlib.py::FarmError` | equivalent | raised by Board.power_cycle |
| brent | `hil/hil_board_utils.py::EspFlashError` | 105 | `lib/flash.py` | equivalent | FarmError from flash_esp_uf2 |
| brent | `hil/hil_board_utils.py::EsptoolError` | 109 | `lib/flash.py` | equivalent | FarmError from flash_esp_uf2 |
| brent | `hil/hil_board_utils.py::SecretsError` | 97 | `test/fixtures/secrets.py::SecretsError` | ported | raised by validate_settings and load_settings |
| brent | `hil/hil_board_utils.py::require_string` | 272 | `test/fixtures/secrets.py::require_string` | ported | |
| brent | `hil/hil_board_utils.py::require_number` | 282 | `test/fixtures/secrets.py::require_number` | ported | |
| brent | `hil/hil_board_utils.py::validate_secrets` | 293 | `test/fixtures/secrets.py::validate_settings` | ported | settings.toml, not secrets.json: SSID and password, optional broker host and port |
| brent | `hil/hil_board_utils.py::load_secrets` | 328 | `test/fixtures/secrets.py::load_settings` | ported | tomllib; SecretsError names the file |
| brent | `hil/hil_board_utils.py::SECRETS_FILENAME` | 83 | `test/fixtures/secrets.py::settings_toml` | equivalent | settings.toml on CIRCUITPY |
| brent | `hil/hil_board_utils.py::ESP_COMBINED_OFFSET` | 69 | `farm.toml [[board]] flash` | equivalent | we write tinyuf2, then copy a .uf2 |
| brent | `hil/hil_board_utils.py::ESP_IMAGE_MAGIC` | 70 | `lib/flash.py::ESP_IMAGE_MAGIC` | ported | the combined-image check reads it |
| brent | `hil/hil_board_utils.py::ESP_BOOTLOADER_OFFSETS` | 71 | `lib/flash.py::ESP_BOOTLOADER_OFFSETS` | ported | chip -> offset; farm.toml [[board]] flash.chip picks it |
| brent | `hil/hil_board_utils.py::Esptool` | 407 | `lib/flash.py::ESPTOOL` | equivalent | fixed venv path, v5 dash commands |
| brent | `hil/hil_board_utils.py::esp_esptool_version` | 426 | `lib/flash.py::ESPTOOL` | equivalent | |
| brent | `hil/hil_board_utils.py::esp_find_esptool` | 439 | `lib/flash.py::ESPTOOL` | equivalent | |
| brent | `hil/hil_board_utils.py::esp_bootloader_offset` | 458 | `farm.toml [[board]] flash` | equivalent | |
| brent | `hil/hil_board_utils.py::esp_check_combined_image` | 466 | `lib/flash.py::esp_check_combined_image` | ported | pre-flight check before a .bin write |
| brent | `hil/hil_board_utils.py::esp_check_flash_layout` | 488 | `lib/flash.py::esp_check_tinyuf2` | equivalent | tinyuf2 fits the app partition; his filesystem-image half is not ported |
| brent | `hil/hil_board_utils.py::esp_connect_args` | 507 | `lib/flash.py::flash_esp_uf2` | equivalent | `--before no-reset`: native USB, ROM entered by 1200-baud touch |
| brent | `hil/hil_board_utils.py::esp_write_flash_args` | 516 | `lib/flash.py::flash_esp_uf2` | equivalent | one esptool call per download-mode entry |
| brent | `hil/hil_board_utils.py::esp_run_esptool` | 528 | `lib/flash.py::_run` | equivalent | |
| brent | `hil/hil_board_utils.py::esp_flash` | 1132 | `lib/flash.py::flash` | equivalent | per-board method: uf2, esp_uf2, swd |
| brent | `hil/hil_board_utils.py::EspPartition` | 137 | `lib/flash.py::EspPartition` | ported | |
| brent | `hil/hil_board_utils.py::EspPartitionError` | 93 | `lib/flash.py::esp_parse_partitions` | equivalent | FarmError from esp_parse_partitions |
| brent | `hil/hil_board_utils.py::esp_parse_partitions` | 192 | `lib/flash.py::esp_parse_partitions` | ported | |
| brent | `hil/hil_board_utils.py::esp_erase_flash` | 1120 | `lib/flash.py::flash` (erase=True) | ported | opt-in (--flash-erase), off by default; erase_refusal() refuses nRF52 and boards whose bootloader we cannot rewrite |
| brent | `hil/hil_board_utils.py::EraseResult` | 1115 | `lib/flash.py::EraseResult` | ported | |
| brent | `hil/hil_board_utils.py::FlashResult` | 1108 | `lib/flash.py::FlashResult` | ported | |
| brent | `hil/hil_board_utils.py::USB_HUB_RE` | 546 | `test/fixtures/farm.py::USB_PATH_RE` | equivalent | sysfs path like 3-3.2, checked by validate_board_entry (farm.py:36) |
| brent | `hil/hil_board_utils.py::validate_board_entry` | 552 | `test/fixtures/farm.py::validate_board_entry` | equivalent | |
| brent | `hil/hil_board_utils.py::usb_device_path` | 577 | `lib/farmlib.py::Board.sysfs` | equivalent | |
| brent | `hil/hil_board_utils.py::match_serial_ports` | 587 | `lib/farmlib.py::Board.ttys` | equivalent | /dev/serial/by-path at each use |
| brent | `hil/hil_board_utils.py::list_serial_ports` | 599 | `lib/farmlib.py::Board.ttys` | equivalent | |
| brent | `hil/hil_board_utils.py::find_port` | 607 | `lib/farmlib.py::Board.tty` | equivalent | always resolved, never cached (see not ported) |
| brent | `hil/hil_board_utils.py::UHUBCTL_FALLBACK` | 649 | `farm.toml [host] uhubctl` | equivalent | |
| brent | `hil/hil_board_utils.py::find_uhubctl` | 652 | `farm.toml [host] uhubctl` | equivalent | |
| brent | `hil/hil_board_utils.py::run_uhubctl` | 665 | `lib/farmlib.py::Board.power_cycle` | equivalent | flock-serialized, per-board -r/-w, never sudo |
| brent | `hil/hil_board_utils.py::uhubctl_args` | 688 | `lib/farmlib.py::Board.power_cycle` | equivalent | |
| brent | `hil/hil_board_utils.py::_power_on` | 695 | `lib/farmlib.py::Board.power_cycle` | equivalent | we only cycle; -a on is a no-op on the RP2040 |
| brent | `hil/hil_board_utils.py::_power_off` | 699 | `lib/farmlib.py::Board.power_cycle` | equivalent | |
| brent | `hil/hil_board_utils.py::_power_cycle` | 703 | `lib/farmlib.py::Board.power_cycle` | equivalent | |
| brent | `hil/hil_board_utils.py::power_cycle_board` | 708 | `lib/farmlib.py::Board.power_cycle` | equivalent | |
| brent | `hil/hil_board_utils.py::wait_for_port` | 716 | `lib/farmlib.py::Board.wait_ready` | equivalent | tty + CIRCUITPY + settle |
| brent | `hil/hil_board_utils.py::SERIAL_*` | | `lib/serialmon.py` | ported | |
| brent | `hil/hil_board_utils.py::ANSI_ESCAPE_RE` | 745 | `lib/serialmon.py::ANSI_ESCAPE_RE` | ported | |
| brent | `hil/hil_board_utils.py::SerialError` | 121 | `lib/serialmon.py::SerialError` | ported | |
| brent | `hil/hil_board_utils.py::SerialTimeout` | 125 | `lib/serialmon.py::SerialTimeout` | ported | |
| brent | `hil/hil_board_utils.py::SerialUnexpectedError` | 129 | `lib/serialmon.py::SerialUnexpectedError` | ported | |
| brent | `hil/hil_board_utils.py::SerialLine` | 751 | `lib/serialmon.py::SerialLine` | ported | |
| brent | `hil/hil_board_utils.py::SerialMonitor` | 757 | `lib/serialmon.py::SerialMonitor` | ported | test/fixtures/console.py imports it |
| brent | `hil/hil_board_utils.py::SerialMonitor.port_reset` | 934 | `lib/serialmon.py::SerialMonitor.port_reset` | ported | through Board.power_cycle, tty re-resolved from sysfs |
| brent | `hil/firmware/conftest.py::FIRMWARE_DIR` | 68 | `conftest.py` | equivalent | HERE |
| brent | `hil/firmware/conftest.py::HIL_DIR` | 69 | `conftest.py` | equivalent | HERE |
| brent | `hil/firmware/conftest.py::DEFAULT_BOARDS` | 73 | `farm.toml` | equivalent | `--farm` |
| brent | `hil/firmware/conftest.py::*SECRETS*` | | `test/fixtures/secrets.py` | ported | scenario names and secrets dir |
| brent | `hil/firmware/conftest.py::*secrets*` | | `test/fixtures/secrets.py` | ported | secrets_dir, secrets_file, good/default/generated_secrets, secrets_name, secrets_path |
| brent | `hil/firmware/conftest.py::_with_wifi` | 325 | `test/fixtures/secrets.py::_with_wifi` | ported | |
| brent | `hil/firmware/conftest.py::DEVICE_UID_RE` | 83 | `test/fixtures/identity.py` | equivalent | no regex: UID comes from boot_out.txt, REPL fallback |
| brent | `hil/firmware/conftest.py::device_uid` | 495 | `test/fixtures/identity.py::device_uid` | equivalent | history/device_uid.json, fails loudly on a change |
| brent | `hil/firmware/conftest.py::pytest_addoption` | 88 | `conftest.py::pytest_addoption` | equivalent | --boards file → --farm, --board → --boards, --firmware-dir → --firmware, --uhubctl → farm.toml; --serial-log-dir is test/fixtures/console.py::pytest_addoption |
| brent | `hil/firmware/conftest.py::Board` | 106| `lib/farmlib.py::Board` | equivalent | |
| brent | `hil/firmware/conftest.py::load_board_entry` | 121 | `test/fixtures/farm.py::farm_boards` | equivalent | |
| brent | `hil/firmware/conftest.py::board` | 192 | `test/fixtures/farm.py::board` | equivalent | module scope, parametrized over every board |
| brent | `hil/firmware/conftest.py::_require_baud` | 209 | `-` | watch | USB CDC ignores baud |
| brent | `hil/firmware/conftest.py::_test_selectors` | 139 | `conftest.py::_test_selectors` | ported | farm.toml [[board]] run_tests / skip_tests |
| brent | `hil/firmware/conftest.py::_matches` | 153 | `conftest.py::_matches` | ported | |
| brent | `hil/firmware/conftest.py::pytest_collection_modifyitems` | 160 | `conftest.py::pytest_collection_modifyitems` | ported | |
| brent | `hil/firmware/conftest.py::power` | 219 | `farm.toml [[board]] power` | equivalent | uhubctl path from [host] |
| brent | `hil/firmware/conftest.py::Firmware` | 116 | `lib/flash.py::find_image` | equivalent | image by board_id in the filename |
| brent | `hil/firmware/conftest.py::_find_one` | 225 | `lib/flash.py::find_image` | equivalent | |
| brent | `hil/firmware/conftest.py::firmware` | 234 | `test/fixtures/flashing.py::ensure_firmware` | equivalent | |
| brent | `hil/firmware/conftest.py::BrokerCreds` | 248 | `test/fixtures/secrets.py::BrokerCreds` | ported | ProtoMQ takes no login (see not ported) |
| brent | `hil/firmware/conftest.py::broker_creds` | 274 | `test/fixtures/secrets.py::broker_creds` | ported | |
| brent | `hil/firmware/conftest.py::NetworkCreds` | 256 | `test/fixtures/secrets.py::NetworkCreds` | ported | |
| brent | `hil/firmware/conftest.py::network_creds` | 289 | `test/fixtures/secrets.py::network_creds` | ported | |
| brent | `hil/firmware/conftest.py::FlashState` | 438 | `lib/flash.py::FlashResult` | equivalent | per-phase seconds (PHASES); no LittleFS build time |
| brent | `hil/firmware/conftest.py::flashed_board` | 450 | `test/fixtures/flashing.py::flashed_firmware` | equivalent | flash-if-changed per module, no erase |
| brent | `hil/firmware/conftest.py::serial` | 482 | `test/fixtures/console.py::serial` | ported | SerialMonitor from lib/serialmon.py, closed and dumped after the test |
| brent | `hil/firmware/provisioning/test_secrets.py::StepTimer` | 13 | `test/fixtures/history.py::metric` | equivalent | timings go to history/ |
| brent | `hil/firmware/provisioning/test_secrets.py` | | `-` | watch | WipperSnapper provisioning tests; read for test shape |
| brent | `hil/firmware/network/test_wifi.py` | | `test/hardware/net_wifi_test.py` | ported | same three test names; radio state over the REPL instead of runNetFSM console lines |
| brent | `hil/firmware/network/test_wifi.py::WIFI_CONNECT_TIMEOUT` | 19 | `test/hardware/net_wifi_test.py::WIFI_CONNECT_TIMEOUT` | ported | 30 s: the supervisor makes one 8 s connect call, not five 20 s attempts |
| brent | `hil/firmware/network/test_wifi.py::ssid_found_pattern` | 22 | `test/hardware/net_wifi_test.py::DEVICE_SCAN` | equivalent | SSID in the scan with an RSSI, read from wifi.Network |
| brent | `hil/firmware/conftest.py::valid_secrets` | 359 | `test/fixtures/secrets.py::base_secrets` | equivalent | the `good` scenario: farm WiFi from .env, no IO keys |
| brent | `hil/firmware/conftest.py::WRONG_WIFI_PASSWORD` | 322 | `test/hardware/net_wifi_test.py::WRONG_WIFI_PASSWORD` | ported | same value |
| brent | `hil/firmware/conftest.py::INVALID_PASSWORD` | 79 | `test/hardware/net_wifi_test.py::test_wifi_invalid_password` | equivalent | no settings.toml scenario: the wrong password goes to connect() over the REPL |
| brent | `hil/firmware/conftest.py::invalid_password_secrets` | 389 | `test/hardware/net_wifi_test.py::DEVICE_BAD_PASSWORD` | equivalent | as above |
| brent | `hil/firmware/conftest.py::MISSING_SSID` | 321 | `test/hardware/net_wifi_test.py::MISSING_SSID` | ported | cpr-fake-no-such-network, allowed by the prod guard |
| brent | `hil/firmware/conftest.py::_env` | 261 | `test/fixtures/secrets.py::_values` | equivalent | farm.toml [network] names the environment variables; values never logged |
| brent | `hil/firmware/conftest.py::VALID_SCENARIOS` | 400 | `data/settings-scenarios/scenarios.toml` | equivalent | needs_creds: scenarios with the farm SSID skip when .env lacks it |
| brent | `hil/firmware/mqtt/test_mqtt.py` | | `test/hardware/mqtt_broker_test.py` | ported | minimqtt connect() over the REPL instead of runNetFSM console lines |
| brent | `hil/firmware/mqtt/test_mqtt.py::test_mqtt_connected` | 27 | `test/hardware/mqtt_broker_test.py::test_mqtt_connect` | equivalent | plus the publish / subscribe round trip through ProtoMQ |
| brent | `hil/firmware/mqtt/test_mqtt.py::expect_mqtt_gives_up` | 59 | `test/hardware/mqtt_broker_test.py::DEVICE_GIVES_UP` | ported | 5 attempts, each error recorded; plus a recovery connect his firmware cannot make |
| brent | `hil/firmware/mqtt/test_mqtt.py::*_TIMEOUT` | | `test/hardware/mqtt_broker_test.py::GIVE_UP_TIMEOUT` | equivalent | 60 s: socket errors get no backoff in minimqtt; no WDT |
| brent | `hil/firmware/mqtt/test_mqtt.py::test_mqtt_unreachable_wdt_reboot` | 139 | `-` | watch | no watchdog unless code.py arms one; it would test our script, not the firmware |
| brent | `hil/firmware/conftest.py::INVALID_BROKER_*` | | `test/hardware/mqtt_broker_test.py` | equivalent | no settings.toml scenario: scenario_guard refuses a non-IP broker host, so the bad host / port go to connect() over the REPL |
| brent | `hil/firmware/conftest.py::invalid_broker_*_secrets` | | `test/hardware/mqtt_broker_test.py` | equivalent | as above |
| brent | `hil/firmware/conftest.py::MISSING_BROKER_HOST` | 337 | `test/hardware/mqtt_broker_test.py::MISSING_BROKER_HOST` | ported | same value |
| brent | `hil/firmware/conftest.py::CLOSED_BROKER_PORT` | 338 | `test/hardware/mqtt_broker_test.py::CLOSED_BROKER_PORT` | ported | same value, on the ProtoMQ host |
| brent | `.github/scripts/hil_display_test.py` | | `-` | watch | tyeth's display + camera HIL driver (usbip-hil-controller), arrived with the api-v2 rebase |
| brent | `.github/scripts/hil_display_test.py::_proof_window` | 175 | `tools/hil_report.py::proof_window` | ported | from boot to just after the last evidence line (PR #930 hil-lib.sh) |
| brent | `.github/scripts/hil_display_test.py::_time_window` | 195 | `tools/hil_report.py::broker_window` | ported | catcher records by epoch, same span as the serial window |
| brent | `.github/scripts/hil_display_test.py::_proof_section` | 202 | `tools/hil_report.py::section` | ported | |
| brent | `.github/scripts/hil_display_test.py::append_comment` | 210 | `tools/hil_report.py::report` | ported | board x test table instead of one target |
| brent | `.github/scripts/hil_display_test.py::HIL_PROOF_*` | | `tools/hil_report.py` | ported | same env vars and defaults |
| brent | `.github/scripts/specs/*` | | `-` | watch | spec-driven sequences; we have no camera or display rig |
| brent | `.github/workflows/hil-lilygo-display.yml` | | `-` | watch | GitHub Actions + self-hosted bench; we post nothing upstream |
| brent | `.github/workflows/build-migrate-api-v2.yml` | | `-` | watch | WipperSnapper firmware builds |
| brent | `hil/README.md` | | `-` | watch | how he runs it: HIL_IO_* / HIL_WIFI_* exported on the HIL host |
| brent | `hil/hil-boards.json::board` | 2 | `farm.toml [[board]] name` | equivalent | |
| brent | `hil/hil-boards.json::chip` | 3 | `farm.toml [[board]] flash` | equivalent | `flash.chip` on ESP boards |
| brent | `hil/hil-boards.json::flash_baud` | 4 | `lib/flash.py::flash_esp_uf2` | equivalent | 921600 |
| brent | `hil/hil-boards.json::read_baud` | 5 | `-` | watch | USB CDC ignores baud |
| brent | `hil/hil-boards.json::location` | 6 | `farm.toml [[board]] usb` | equivalent | |
| brent | `hil/hil-boards.json::firmware_dir` | 7 | `conftest.py::pytest_addoption` | equivalent | `--firmware DIR` |
| brent | `hil/hil-boards.json::run_tests` | 8 | `farm.toml [[board]] run_tests` | ported | |
| brent | `hil/hil-boards.json::skip_tests` | 9 | `farm.toml [[board]] skip_tests` | ported | |
| brent | `hil/requirements.txt` | | `-` | watch | |
| brent | `hil/tests_hil_board_utils/hil_checks.py` | | `lib/hil_checks.py` | ported | |
| brent | `hil/tests_hil_board_utils/hil_power_cycle_monitor.py` | | `tools/power_cycle_monitor.py` | ported | |
| brent | `hil/tests_hil_board_utils/test_end_to_end.py` | | `test/hardware/fixture_chain_test.py` | equivalent | flashed_firmware → flashed_fs → serial on every board; the power-cycled boot is power_boot_test.py::test_power_cycle_boot |
| brent | `hil/tests_hil_board_utils/test_power.py` | | `test/hardware/farm_addressing_test.py` | ported | read-only half (tty from the USB path, uhubctl status) in smoke; the power switching is power_boot_test.py (opt-in) |
| brent | `hil/tests_hil_board_utils/test_serial_monitor_hw.py` | | `test/hardware/serialmon_hw_test.py` | ported | no-power-cut subset in smoke, with soft_reset / microcontroller.reset() for his DTR/RTS reset; the power-cycle variants are power_boot_test.py (opt-in), the pty ones test/harness/serialmon_test.py |
| brent | `.github/ISSUE_TEMPLATE/*` | | `-` | watch | upstream, not HIL |
| brent | `.github/membrowse-targets.json` | | `-` | watch | upstream, not HIL |
| brent | `.github/workflows/build-clang-doxy.yml` | | `-` | watch | upstream CI; a new HIL workflow shows as UNMAPPED |
| brent | `.github/workflows/membrowse-*.yml` | | `-` | watch | upstream CI |
| brent | `.github/workflows/nightly-migrate-api-v2.yml` | | `-` | watch | upstream CI |
| brent | `.github/workflows/release-*.yml` | | `-` | watch | upstream CI |
| protomq | `api/clients.js` | | `test/fixtures/broker.py::ProtoMQ.clients` | ported | |
| protomq | `api/disconnect.js` | | `test/fixtures/broker.py::ProtoMQ.disconnect` | ported | |
| protomq | `api/echo.js` | | `test/fixtures/broker.py::ProtoMQ.echo` | ported | |
| protomq | `api/deliveries.js` | | `test/fixtures/broker.py::ProtoMQ.deliveries` | ported | track_deliveries toggles; see ProtoMQ.track |
| protomq | `api/reset.js` | | `test/fixtures/broker.py::_session_hygiene` | ported | POST /reset once per run, only with --broker-reset (see not ported) |
| protomq | `api/routes.js` | | `test/fixtures/broker.py::api` | ported | middleware reads `client` from the body |
| protomq | `api/index.js` | | `test/fixtures/broker.py::api` | ported | |
| protomq | `api/autoresponse.js` | | `-` | watch | protobuf autoresponders, unused on cpr/ topics |
| protomq | `api/scripts.js` | | `-` | watch | WipperSnapper scripts, unused |
| protomq | `broker/authorization.js` | | `test/fixtures/broker.py::protomq` | watch | client ids containing "invalid" are refused |
| protomq | `broker/index.js` | | `test/fixtures/broker.py::broker` | watch | |
| protomq | `broker/listeners.js` | | `test/fixtures/broker.py::ProtoMQ.connected` | ported | getClientsState shape {id, subscriptions}; decodes ws-* topics |
| protomq | `broker/protobuf_autoresponders.js` | | `-` | watch | |
| protomq | `broker/script_runner.js` | | `-` | watch | |
| protomq | `protobufs.js` | | `test/fixtures/broker.py` | watch | decodeByTopic: why we use plain cpr/ topics |
| protomq | `main.js` | | `test/fixtures/broker.py::broker` | ported | started as `node main.js` |
| protomq | `docs/api/README.md` | | `test/fixtures/broker.py` | watch | the API contract |
| protomq | `package.json` | | `farm.toml [broker] node` | watch | Node 22.11.0 in ~/node22 |
| protomq | `package-lock.json` | | `farm.toml [broker] node` | watch | |
| protomq | `.tool-versions` | | `farm.toml [broker] node` | watch | |
| protomq | `.env.example.json` | | `farm.toml [broker] dir` | watch | protobufSource |
| protomq | `frontend/*` | | `-` | watch | |
| protomq | `protobufs-v1/*` | | `-` | watch | |
| protomq | `scripts/*` | | `-` | watch | |
| protomq | `script/*` | | `-` | watch | |
| protomq | `.vscode/*` | | `-` | watch | |
| protomq | `.git*` | | `-` | watch | |
| protomq | `index.html` | | `-` | watch | |
| protomq | `vite.config.js` | | `-` | watch | |
| protomq | `README.md` | | `-` | watch | |
| protomq | `TODOs.txt` | | `-` | watch | |
| protomq | `LICENSE` | | `-` | watch | |
| protobuf | `*` | | `-` | watch | ProtoMQ decodes ws-* topics with these; cpr/ topics are plain |

## Deliberately not ported

A row with a Brent key silences the tool for that key; a `-` row is a decision with no single
upstream symbol, kept here so it is not reopened by accident.

| Repo | Brent | What | Why |
|---|---|---|---|
| brent | `-` | sudo -n uhubctl (`run_uhubctl(sudo=True)`, hil_board_utils.py:665; `--sudo`, conftest.py:88) | uhubctl is unprivileged on bravo through udev. Never sudo in an unattended run. |
| brent | `hil/hil_board_utils.py::get_port` | `_port_cache`, hil_board_utils.py:549 | A tty number changes on every reset and power cycle. We resolve it from the sysfs USB path at the moment of use. |
| brent | `hil/hil_board_utils.py::clear_port` | `_port_cache`, hil_board_utils.py:635 | as above |
| brent | `hil/hil_board_utils.py::SerialMonitor.board_reset` | DTR/RTS board_reset, hil_board_utils.py:900 | Needs a USB-UART auto-reset circuit (his docstring, :910). All eight farm boards are native USB. We reset with microcontroller.reset, uhubctl, or SWD. |
| brent | `hil/hil_board_utils.py::esp_erase_flash_args` | per-test full erase, hil_board_utils.py:524 | Leaves the chip with no second-stage bootloader for the whole erase (flash.py:352). The opt-in erase is flash(erase=True), see the mapping table. |
| brent | `hil/hil_board_utils.py::Esptool.erase_flash_command` | per-test full erase, hil_board_utils.py:421 | As above: no per-test erase. The opt-in erase is flash(erase=True), see the mapping table. |
| brent | `hil/hil_board_utils.py::ESP_*` | LittleFS image provisioning: partition table geometry | CircuitPython boards present CIRCUITPY over USB MSC. flashed_fs copies a staged tree, with backup and restore. |
| brent | `hil/hil_board_utils.py::EspPartition*` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_*partition*` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_check_table_length` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_check_md5_entry` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_get_fs_*` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::*littlefs*` | LittleFS image provisioning: mklittlefs | as above |
| brent | `hil/hil_board_utils.py::esp_geometry_args` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_create_image` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::BuildResult` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::esp_flash_fs` | LittleFS image provisioning | as above |
| brent | `hil/hil_board_utils.py::parse_args` | LittleFS image CLI | as above |
| brent | `hil/hil_board_utils.py::main` | LittleFS image CLI | as above |
| brent | `hil/firmware/conftest.py::NO_FS_IMAGE` | LittleFS scenario: fs partition left erased | as above |
| brent | `hil/tests_hil_board_utils/test_filesystem_flash.py` | LittleFS image and erase tests | as above |
| brent | `hil/firmware/conftest.py::VALID_IO_ONLY` | scenario with valid Adafruit IO credentials | CircuitPython results never go to Adafruit IO, and the prod guard refuses AIO credentials on farm boards. |
| brent | `hil/firmware/conftest.py::VALID_WIFI_ONLY` | WiFi valid, IO placeholders | Our `good` scenario already has the farm WiFi and no IO keys. |
| brent | `hil/firmware/conftest.py::SSID_VALID` | WiFi valid, FAKE_IO_CREDS (was SSID_FOUND) | as above: there are no IO credentials to fake |
| brent | `hil/firmware/conftest.py::FAKE_IO_CREDS` | placeholder-passing IO login | as above |
| brent | `hil/firmware/conftest.py::valid_io_secrets` | VALID_IO_ONLY's secrets | as above |
| brent | `hil/firmware/conftest.py::valid_wifi_secrets` | VALID_WIFI_ONLY's secrets | as above |
| brent | `hil/firmware/conftest.py::ssid_valid_secrets` | SSID_VALID's secrets | as above |
| brent | `hil/firmware/conftest.py::_with_io` | merges IO credentials into secrets | as above |
| brent | `-` | broker_creds default `io.adafruit.com:8883`, conftest.py:329 | CircuitPython results never go to Adafruit IO. The prod guard refuses a broker that is not a private address. |
| brent | `-` | single-board session scope: `board` (session), `--board`, conftest.py:182 | We parametrize every board at module scope and run them in parallel under xdist. |
| protomq | `api/reset.js::POST /reset` | POST /reset per test | Resets every client and autoresponder. With eight boards on one broker a per-test reset would disconnect the others, so protomq clears one client id per test. Once per run it is ported, behind --broker-reset (see the mapping table). |
