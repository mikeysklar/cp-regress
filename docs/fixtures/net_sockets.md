# net_sockets: Network socket client/server, limits, timeouts

Tier: nightly / Automation: auto / Boards: all with WiFi (metro_esp32s2, metro_esp32s3)

## Regressions it targets

- #10606: ESP32-S2 regression: socket count dropped from 8 to 4
- #9219: ESP32-S2: only one socket available (#9219)
- #4057: Socket limit issues
- #8988: errno ETIMEDOUT changed from -116 (unfixed)
- #2097: settimeout(int) should work without raising
- #8443: Device as TCP server lost clients
- #4127: UDP sendto with port > 255 broken
- #8907: Socket connect timeout
- #7086: Socket API regression
- #7431: Socket state corruption
- #8248: Socket recv_into buffer handling
- #4085: Socket close issues

## What it checks

Phase 1 (one raw-REPL exec, device as client of host echo servers):
- **wifi_connect**: radio connected with an IPv4 address (connects only if not already up)
- **socketpool_create**: `socketpool.SocketPool(wifi.radio)`
- **tcp_connect**: device connects to the host TCP echo server
- **tcp_echo_roundtrip**: 64 x 1 KB `sendall` + `recv_into` loop, 65536 bytes back; metric **tcp_echo_kBps**
- **max_tcp_sockets** (metric) / **tcp_sockets_floor**: sockets open at once, probe capped at 16, floor 4 (#10606, #9219)
- **tcp_socket_after_limit**: after closing the probe sockets a new socket connects (no leak)
- **timeout_errno**: `recv_into` on a silent connected socket with `settimeout(1)` raises OSError ETIMEDOUT (`errno.ETIMEDOUT`, 110 or 116; #8988)
- **settimeout_int**: that int timeout actually fires in 0.5 to 3 s (#2097)
- **udp_sendto** / **udp_recvfrom**: UDP to an ephemeral port (> 255, #4127) and the echo comes back intact

Phase 2 (second raw-REPL exec, device as server; wifi stays up between execs):
- **server_bind**: bind/listen on port 8765
- **tcp_server** / metric **tcp_server_clients**: device serves 30 sequential ping/pong clients (#8443)
- **tcp_server_host_pongs**: host side saw 30 pongs
- **device_client_phase** / **device_server_phase** / **host_servers**: harness failures (REPL timeout, host bind) as failed checks

## Setup

Environment variables (named in farm.toml `[network]`):
- `CPR_WIFI_SSID`, `CPR_WIFI_PASSWORD`; the test skips without them

The host must be reachable from the board's WiFi at `farmlib.host_ip()` (override with
`CPR_HOST_IP`), and the board must be reachable from the host on TCP 8765. Host packages:
none beyond pyserial. The host runs a threaded TCP echo server and a UDP echo server on
ephemeral ports and stops them in a `finally`.

## What is hard to automate

- **WiFi**: needs a working AP; a flaky network gives spurious failures.
- **Reachability**: AP client isolation or a host with several interfaces (wrong `host_ip()`) breaks every network check; set `CPR_HOST_IP`.
- **Socket count**: varies by chip, build and what the web workflow holds open. Floor is 4; the metric tracks drift.
- **Server mode**: the device IP comes from phase 1 stdout; the host client retries the first connect for 30 s while the device enters phase 2. Concurrent clients are not tested.
- **errno values**: espressif raises the IDF newlib ETIMEDOUT (116); other ports may use 110. A negative errno (old #8988 behaviour) fails.
- **UDP floor**: port exactly 256 is not tested.

## Run

```
./run.py --tier nightly --only net_sockets --boards metro_esp32s2,metro_esp32s3
```

(Requires `CPR_WIFI_SSID` and `CPR_WIFI_PASSWORD`)
