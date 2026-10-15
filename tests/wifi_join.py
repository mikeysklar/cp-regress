# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""WiFi connection stability and attribute verification.

Targets:
  #3576, #6866, #8115 - soft reboot reconnect cycles
  #8032 - reconnection after disable/enable
  #10729 - enabled property behavior on reconnect
"""

META = {
    "id": "wifi_join",
    "title": "WiFi scan, connect, ping, and reconnect cycles",
    "category": "Network",
    "tier": "nightly",
    "needs": ["wifi", "native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 300,
    "targets": [3576, 6866, 8115, 8032, 10729],
}

DEVICE_INITIAL = r'''
import time
import wifi
import socketpool

ssid = {SSID}
password = {PASSWORD}

networks = []
try:
    for net in wifi.radio.start_scanning_networks():
        networks.append(net.ssid.decode() if isinstance(net.ssid, bytes) else net.ssid)
        if len(networks) >= 20:
            break
    wifi.radio.stop_scanning_networks()
    check("scan_networks", len(networks) > 0, "found " + str(len(networks)))
except Exception as e:
    check("scan_networks", False, str(type(e).__name__) + ": " + str(e))
    skip("connect", "scan failed")

check("target_ssid_found", ssid in networks, "networks: " + ", ".join(networks[:5]))

t0 = time.monotonic_ns()
guard("connect", lambda: wifi.radio.connect(ssid, password))
connect_ms = (time.monotonic_ns() - t0) / 1e6
metric("connect_ms", connect_ms, "ms", "lower")

check("connected", wifi.radio.connected, str(wifi.radio.connected))
check("ipv4_address_set", wifi.radio.ipv4_address is not None, str(wifi.radio.ipv4_address))
check("ipv4_gateway_set", wifi.radio.ipv4_gateway is not None, str(wifi.radio.ipv4_gateway))

if wifi.radio.ipv4_gateway:
    ping_ms = guard("ping", lambda: wifi.radio.ping(wifi.radio.ipv4_gateway))
    check("ping_result_numeric", ping_ms is not None and isinstance(ping_ms, (int, float)),
          "type=" + str(type(ping_ms).__name__) + " value=" + str(ping_ms))

def test_getaddrinfo():
    pool = socketpool.SocketPool(wifi.radio)
    return pool.getaddrinfo("adafruit.com", 80)

addr_info = guard("getaddrinfo", test_getaddrinfo)
check("dns_resolves", addr_info is not None and len(addr_info) > 0,
      "addresses=" + str(len(addr_info) if addr_info else 0))

def test_hostname():
    old_hn = wifi.radio.hostname
    wifi.radio.hostname = "cpr-test"
    new_hn = wifi.radio.hostname
    wifi.radio.hostname = old_hn
    return new_hn == "cpr-test"

hostname_ok = guard("hostname_test", test_hostname)
check("hostname_settable", hostname_ok is True, str(hostname_ok))

def toggle_and_reconnect():
    # #8032 / #10729: after the radio is turned off and on, a fresh connect must work
    wifi.radio.enabled = False
    time.sleep(1)
    wifi.radio.enabled = True
    wifi.radio.connect(ssid, password)
    return wifi.radio.connected

reconnected = guard("toggle", toggle_and_reconnect)
check("reconnected_after_toggle", reconnected is True, str(reconnected))
'''

DEVICE_REBOOT = r'''
import time
import wifi

ssid = {SSID}
password = {PASSWORD}

t0 = time.monotonic_ns()
guard("cycle_connect", lambda: wifi.radio.connect(ssid, password))
reconnect_ms = (time.monotonic_ns() - t0) / 1e6
metric("reconnect_ms", reconnect_ms, "ms", "lower")
check("cycle_connected", wifi.radio.connected, str(wifi.radio.connected))
'''


def run(board, ctx):
    ctx.check("board_present", board.present(), board.usb)

    ssid = ctx.secret("ssid_env")
    password = ctx.secret("password_env")

    if not ssid or not password:
        ctx.skip("ssid_not_configured", "CPR_WIFI_SSID and/or CPR_WIFI_PASSWORD not set")
        return

    device_code = DEVICE_INITIAL.format(SSID=repr(ssid), PASSWORD=repr(password))

    ctx.device(device_code, timeout=90)

    with board.repl() as repl:
        for cycle_num in range(5):
            repl.soft_reboot()
            repl.prompt()

            cycle_code = DEVICE_REBOOT.format(SSID=repr(ssid), PASSWORD=repr(password))
            res, out, err = repl.run_device(cycle_code, timeout=30)
            ctx.absorb(res)

            if err.strip():
                ctx.check(f"cycle_{cycle_num}_error", False, err.strip()[-200:])
