# net_tls: HTTPS TLS handshake, certificate validation, throughput

Tier: nightly / Automation: auto / Boards: all with WiFi (metro_esp32s2, metro_esp32s3)

## Regressions it targets

- #8299: TLS context creation failures
- #8925: wrap_socket parameter validation
- #8919: Certificate loading issues
- #7606: TLS handshake hang/timeout
- #4151: Plain HTTP broke when TLS is implemented
- #10339: Certificate verification not working
- #3651: SSLSocket data loss
- #7429: TLS memory leak / socket not closing
- #9003: server_hostname parameter ignored
- #8910: load_verify_locations with cadata broken

## What it checks

- **tls_setup** (host): openssl generated a good CA + server cert and an unrelated bad CA + server cert (skip if openssl is missing)
- **host_ip** / **host_servers** (host): fail if the host has no LAN address or cannot bind the three servers
- **wifi_connect**: radio is connected (connects with the env credentials if it was not already)
- **socketpool_create**: socketpool.SocketPool(wifi.radio) works
- **http_connect**, **http_get_ok**: plain HTTP GET /ok returns 200 with body `ok` (#4151, non-TLS still works)
- **ssl_context_created**: ssl.create_default_context() + load_verify_locations(cadata=...)
- **tls_wrap_socket**: wrap_socket(sock, server_hostname=host_ip) then connect() completes the handshake (CircuitPython handshakes in connect, not in wrap_socket)
- **tls_handshake_ms**: metric, time of that connect() (TCP connect + TLS handshake), lower is better
- **https_get_blob**: GET /blob over TLS returns exactly 64 KB (8 KB on `small`) with the expected byte pattern (#3651 data loss)
- **https_kBps**: metric, HTTPS download rate, higher is better
- **rejects_untrusted**: handshake to a server whose cert is from another CA raises OSError -9984 (X509 verify failed) or a verify message (#10339); skipped if the good-cert handshake failed
- **bad_server_handshake_aborted** (host): the bad server saw its handshake fail, so the rejection came from TLS and not from a refused connection
- **device_run** (host): the raw REPL run itself failed (serial error or timeout)
- missing device results (wifi_connect, tls_wrap_socket, https_get_blob, rejects_untrusted) are reported as failed checks
- **external_<host>**: for each `[network].public_tls_hosts` entry, an HTTPS GET with the built-in CA bundle returns an HTTP status line; errors are skips

## Setup

Environment variables (required for nightly runs):
- `CPR_WIFI_SSID`: SSID to connect to
- `CPR_WIFI_PASSWORD`: WiFi password

Host-side:
- `openssl` command-line tool (generates CA and server certificates on-the-fly)
- Serves HTTP and two HTTPS servers on ephemeral ports at `farmlib.host_ip()`; set `CPR_HOST_IP` if that picks the wrong interface, and allow inbound TCP from the board's network
- Optional: public HTTPS hosts listed in `farm.toml` under `[network].public_tls_hosts` for external verification tests

## What is hard to automate

- **WiFi connectivity**: Requires valid SSID/password and a working WiFi network. Flaky networks will cause spurious failures.
- **TLS version negotiation**: Devices and hosts may have different TLS version support (1.2 vs 1.3); test uses default context which may vary. Version downgrade attacks are not tested.
- **Certificate chain validation**: Test loads a single CA PEM; does not test multi-CA chains or CRL/OCSP revocation.
- **Hostname verification mismatch**: The server cert's CN is the host IP (no SAN) and server_hostname is that IP; a wrong-name cert is not tested.
- **Time-dependent validation**: CircuitPython builds mbedtls without MBEDTLS_HAVE_TIME_DATE, so certificate dates are never checked and expiry cannot be tested.
- **Cipher suite selection**: Device may have limited cipher suite support (esp. small boards); cannot force specific ciphers in the test.
- **External host changes**: Public hosts change certificates outside test control; test reports external host failures as skips, not regressions.
- **Concurrent TLS connections**: Test verifies sequential connections. Concurrent TLS does not stress hardware limitations.
- **Large certificate chains**: Test uses simple self-signed certs. Complex certificate chains with intermediate CAs are not tested.

## Run

```
./run.py --only net_tls --boards metro_esp32s2,metro_esp32s3
```

(Requires `CPR_WIFI_SSID` and `CPR_WIFI_PASSWORD` env vars set)

### Troubleshooting

If the test skips with "openssl not available":
- Host system must have `openssl` command in PATH
- Linux: `apt-get install openssl`
- macOS: `brew install openssl` (or use system-provided)

If TLS handshake times out:
- WiFi signal quality may be poor, or a host firewall drops the board's connection
- The device sockets use 10-15 s timeouts; the whole device run is capped at 150 s (device_run fails past that)

If external host checks fail:
- Public HTTPS hosts change certificates and may have infrastructure issues
- These are reported as skips (ok=None), not regressions
- Remove flaky hosts from farm.toml [network].public_tls_hosts

If certificate verification fails on a good cert:
- Check the tls_wrap_socket detail: it carries the mbedtls error and verify flags
- Certificate dates are not checked by CircuitPython, so the clock is not the cause
