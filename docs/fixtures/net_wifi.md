# net_wifi: Brent's network tests, on CircuitPython

Tier: nightly / automation: auto / boards: metro_esp32s2, metro_esp32s3 (wifi caps required)

Port of `hil/firmware/network/test_wifi.py` from brentru/Adafruit_Wippersnapper_Arduino
add-hil-ci-cd @ 90cd5af. Same four test names. His tests read runNetFSM()'s console lines;
CircuitPython prints nothing about WiFi on the console, so these read the radio over the REPL.

## What it checks

| Test | His version | Ours |
|---|---|---|
| `test_ssid_found` | scan line `SSID (<ssid>) found! RSSI: -NN` | farm SSID in `start_scanning_networks()`, RSSI -100..-1, channel 1..14; result logged as his `WS_SSID_SCAN_*` (PR #1019) |
| `test_wifi_connected` | `Connected to WiFi!` within 150 s | after a hard reset the supervisor joins from settings.toml by itself within 30 s: connected, `ap_info.ssid` is the farm SSID, address and gateway set |
| `test_wifi_invalid_password` | 5 failed attempts, then WDT halt | `connect()` with his `invalidpassword` raises `ConnectionError("Authentication failure")` within 20 s, not connected, then rejoins |
| `test_ssid_not_found` | `Unable to find WiFi network`, halt | `connect()` to `cpr-fake-no-such-network` raises `ConnectionError("No network with that ssid")` within 20 s, not connected, then rejoins |

Where his firmware halts and waits for the watchdog, CircuitPython keeps running, so each
failure test also checks that the radio can join the farm network again.

The boot-time missing SSID (an unknown SSID in settings.toml) is the `unknown_ssid` scenario in
`settings_scenarios_test.py`.

## Known risk

`shared-bindings/wifi/Radio.c:549` raises "Authentication failure" only for the reasons that
`ports/espressif/common-hal/wifi/Radio.c:435` maps to `WIFI_RADIO_ERROR_AUTH_FAIL` (202, 15,
and the two `NO_AP_FOUND_*` variants). If the ESP reports a 204 handshake timeout for a wrong
password, the message reads "Unknown failure 204" and `bad_password_message` fails while
`bad_password_connection_error` passes. That would be a firmware finding, not a harness bug.

## Setup

`CPR_WIFI_SSID` / `CPR_WIFI_PASSWORD` in `.env` (through base_secrets and the prod guard).
Without them the module skips.

## Run

```bash
pytest --tier nightly --boards metro_esp32s2,metro_esp32s3 test/hardware/net_wifi_test.py
```

Offline self-test of the device code against a fake radio:
`pytest test/harness/net_wifi_device_test.py`.
