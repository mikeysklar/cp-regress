# web_workflow: Web workflow HTTP API endpoints and file operations

Tier: nightly / Automation: auto / Boards: all with WiFi (metro_esp32s2, metro_esp32s3)

## Regressions it targets

- #7636: Web API endpoint inconsistency
- #7929: Web workflow file MOVE operation regression
- #8275: Web API authentication bypass
- #8762: mDNS resolution not working
- #10048: mDNS service discovery issues
- #6805: OPTIONS method support and Access-Control-Allow-Methods header
- #10762: Web API directory listing JSON format
- #8503: PUT file conflict handling (409 when USB mounted)
- #8690: GET file content comparison
- #9171: Web workflow startup race condition
- #10054: IPv4 address assignment timing
- #8107: Web workflow initialization order

## What it checks

- **ready_after_reset**: board comes back after the hard reset that makes the supervisor read the test settings.toml (`microcontroller.reset()` on reset_ok boards, else power cycle)
- **wifi_connected**: supervisor joined WiFi from settings.toml (waits up to 30 s)
- **ipv4_address**: a non-zero IPv4 address; the device prints it as `CPR_IP <ip>` for the host
- **ipv4_gateway**: a gateway address
- **version_json_200** / **version_json_parses**: GET /cp/version.json, no auth (metric: version_json_ms)
- **version_json_has_version**: non-empty "version"
- **version_json_board_id**: "board_id" equals farm.toml board_id
- **version_json_ip**: "ip" equals the address the device reported
- **devices_json_200** / **devices_json_shape**: GET /cp/devices.json returns `{"total": n, "devices": [...]}`
- **fs_no_auth_401** / **fs_bad_auth_401**: GET /fs/ without or with a wrong password is refused with 401 (#8275)
- **fs_options_204**: OPTIONS /fs/ (unauthenticated CORS preflight) returns 204
- **fs_options_has_allow** / **fs_options_has_move**: Access-Control-Allow-Methods present and lists MOVE (#6805)
- **fs_list_200** / **fs_list_has_files** / **fs_list_has_boot_out**: authenticated JSON listing of / includes boot_out.txt
- **fs_list_writable_bool**: listing has a boolean "writable"
- **get_boot_out_200** / **get_boot_out_matches**: GET /fs/boot_out.txt equals the bytes read over USB MSC
- When "writable" is false (host owns CIRCUITPY over USB MSC, the normal farm state):
  - **put_409_when_usb_owns_fs**: PUT /fs/cpr_ww.txt answers 409 (#8503); **fs_roundtrip** is skipped
- When "writable" is true:
  - **put_201_or_204**, **get_200**, **get_content_matches**: write and read back
  - **move_201**, **move_source_gone**, **move_dest_matches**: MOVE with X-Destination (#7929)
  - **delete_204**, **delete_then_404**: DELETE removes the file
- **mdns_resolve**: the "hostname" from version.json resolves as `<hostname>.local` to the device IP; skipped when the host cannot resolve .local names (#8762, #10048)
- **ready_after_restore**: board back after restoring settings.toml/code.py and resetting

## Setup

Environment variables (test skips without them):
- `CPR_WIFI_SSID`: SSID to connect to
- `CPR_WIFI_PASSWORD`: WiFi password

Host-side: Python standard library only. For mdns_resolve, nss-mdns (avahi) on the host.

Device-side: settings.toml and code.py are replaced inside `board.preserved()` (WiFi credentials,
`CIRCUITPY_WEB_API_PASSWORD = "cpr-test"`, idle code.py), then restored, then the board is reset again.

## What is hard to automate

- **Write round trip on the farm**: CircuitPython gives the filesystem lock to USB MSC as soon as the host
  sees CIRCUITPY as writable, and Linux probes that on enumeration. So on the farm PUT/MOVE/DELETE
  normally get 409 and only the 409 contract is checked. Exercising the real write path needs
  CIRCUITPY ejected (or `storage.disable_usb_drive()`) while keeping a way to restore files.
- **WiFi**: needs a working network and valid credentials; a flaky AP shows up as wifi_connected failures.
- **mDNS**: depends on host nss-mdns and multicast on the network; skipped rather than failed when the name does not resolve.
- **devices.json content**: "total" depends on other CircuitPython devices on the network, so only the shape is checked.
- **Web UI pages** (/code/, /edit/, serial websocket) are not exercised.

## Run

```
./run.py --only web_workflow --boards metro_esp32s2,metro_esp32s3
```

(Requires `CPR_WIFI_SSID` and `CPR_WIFI_PASSWORD` env vars set)

With specific board:
```
./run.py --only web_workflow --boards metro_esp32s3
```

Notes:
- Test is **destructive**: rewrites settings.toml and code.py, restores them, and resets the board twice.
- Test is **nightly tier**: full HTTP API validation with file operations.
- Test has a **240 second budget**: two hard resets, up to 30 s WiFi wait, and the HTTP requests.
