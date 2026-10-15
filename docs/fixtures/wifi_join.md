# wifi_join: WiFi scan, connect, ping, and reconnect cycles

Tier: nightly / automation: auto / boards: metro_esp32s2, metro_esp32s3 (wifi caps required)

## Regressions it targets

- #3576, #6866, #8115: Soft reboot reconnect stability (5 cycles)
- #8032: Reconnection after radio disable/enable cycle
- #10729: Radio enabled property state preservation across reconnect

## What it checks

- Network scan returns list of nearby SSIDs
- Target SSID appears in scan results
- Connection time measured and logged as metric (regression detection for slowdowns)
- Radio.connected property true after successful connect
- ipv4_address is not None (assigned by DHCP)
- ipv4_gateway is not None (received from AP)
- ping(gateway) returns a numeric value (milliseconds)
- socketpool.getaddrinfo() resolves external hostname (DNS functional)
- hostname property is gettable and settable
- Disable radio (enabled=False), enable (enabled=True), reconnect works
- 5 soft reboot cycles with reconnect; each must complete; reconnect time measured

## Setup

Environment variables (required):
- `CPR_WIFI_SSID`: SSID of a test WiFi network
- `CPR_WIFI_PASSWORD`: WPA2/WPA3 password for that network

Test runs unprivileged. Networks must not require browser portal or accept connections in under 10 seconds.

## What is hard to automate

- Captive portal / splash-page networks (these hang the test)
- Networks requiring certificate pinning or client certs
- 5 GHz-only networks on dual-band radios (may scan only 2.4 GHz)
- Connection failure diagnostics (timeout, wrong password, out of range): test aborts rather than details the cause
- External TLS connectivity failures (the `public_tls_hosts` config in farm.toml is separate; this test uses only adafruit.com)

## Run

```bash
# Requires WiFi environment set
export CPR_WIFI_SSID="MyNetwork"
export CPR_WIFI_PASSWORD="MyPassword"

./run.py --only wifi_join --boards metro_esp32s2,metro_esp32s3
```

Timeout: 300 seconds total (wifi modules may be slow to scan and reconnect).
