# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""WiFi scan, connect, ping, and reconnect cycles.

pytest version of tests/wifi_join.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). DEVICE_INITIAL runs once per board; then the board is
soft-rebooted 5 times and DEVICE_REBOOT reconnects after each reboot. Each phase runs once in a
module-scoped fixture, and every check is its own test.
Targets: #3576, #6866, #8115 (soft reboot reconnect cycles), #8032 (reconnect after
disable/enable), #10729 (enabled property on reconnect).

Never print ssid or password: they are only formatted into the device code.
"""
import pytest

from fixtures.device import DeviceResults

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("wifi", "native_usb"),
    pytest.mark.targets(3576, 6866, 8115, 8032, 10729),
]

CYCLES = 5
NOT_CONFIGURED = "ssid_not_configured"

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
    try:
        wifi.radio.hostname = old_hn
    except ValueError:
        pass  # a default with "_" is refused by the setter; hostname_default_valid reports it
    return new_hn == "cpr-test", old_hn

def hostname_problem(hn):
    # The setter's rules (shared-bindings/wifi/Radio.c hostname_valid): dot-separated parts of
    # 1-63 letters, digits or dashes, not starting with a dash.
    for part in hn.split("."):
        if not 0 < len(part) < 64:
            return "part %r is %d long" % (part, len(part))
        if part[0] == "-":
            return "part %r starts with a dash" % part
        for c in part:
            if not (c.isalpha() or c.isdigit() or c == "-"):
                return "%r is not allowed" % c
    return None

hostname = guard("hostname_test", test_hostname)
check("hostname_settable", hostname is not None and hostname[0] is True, str(hostname))
if hostname is not None and isinstance(hostname[1], str):
    problem = hostname_problem(hostname[1])
    check("hostname_default_valid", problem is None,
          "default %s: %s" % (hostname[1], problem) if problem else hostname[1])

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


# Every check in each soft-reboot cycle, in the original's per-cycle order. reconnect_ms is a
# metric; cycle_<n>_error is the host-side check on what the cycle's device code raised.
CYCLE_CHECKS = [(c, name) for c in range(CYCLES)
                for name in ("cycle_connect", "reconnect_ms", "cycle_connected",
                             "cycle_%d_error" % c)]


@pytest.fixture(scope="module")
def creds(base_secrets):
    """(ssid, password) from base_secrets (the prod-guarded .env values), or None."""
    ssid = base_secrets.get("CIRCUITPY_WIFI_SSID")
    password = base_secrets.get("CIRCUITPY_WIFI_PASSWORD")
    if not ssid or not password:
        return None
    return ssid, password


def _need_creds(creds):
    if creds is None:
        pytest.skip(NOT_CONFIGURED + ": CPR_WIFI_SSID and/or CPR_WIFI_PASSWORD not set")
    return creds


@pytest.fixture(scope="module")
def initial(creds, device):
    """DEVICE_INITIAL once per board: scan, connect, ping, DNS, hostname, radio toggle."""
    if creds is None:
        return None
    ssid, password = creds
    device_code = DEVICE_INITIAL.format(SSID=repr(ssid), PASSWORD=repr(password))
    return device.run(device_code, timeout=90)


@pytest.fixture(scope="module")
def cycles(board, creds, initial):
    """Five soft reboots, each followed by DEVICE_REBOOT. One DeviceResults per cycle.

    Runs after `initial`, as in the original. If the host loses the board partway, that cycle
    keeps the error (as `crashed`) and the cycles after it are None (not reached).
    """
    out = [None] * CYCLES
    if creds is None or initial is None or initial.crashed:
        return out
    ssid, password = creds
    cycle_num = 0
    try:
        with board.repl() as repl:
            for cycle_num in range(CYCLES):
                repl.soft_reboot()
                repl.prompt()

                cycle_code = DEVICE_REBOOT.format(SSID=repr(ssid), PASSWORD=repr(password))
                res, dout, err = repl.run_device(cycle_code, timeout=30)
                out[cycle_num] = DeviceResults(board, res, dout, err)
    except Exception as e:  # pylint: disable=broad-except
        out[cycle_num] = DeviceResults(board, [], "", "", crashed="%s: %s" % (type(e).__name__, e))
    return out


def _initial(creds, initial):
    _need_creds(creds)
    return initial


def _not_reached(results):
    if results.crashed:
        pytest.skip("not reached: the device run failed (%s)" % results.crashed[:200])
    if results.err:
        pytest.skip("not reached: the device code raised (%s)" % results.err[-200:])


def _expect_if_reported(results, name, after):
    """guard() checks: reported only when the guarded call raised, so absent means passed.

    `after` is the check or metric the device reports right after the guard. If it is there,
    the guard was reached and passed, even if the device code raised later.
    """
    if name in results.checks:
        results.expect(name)
    elif after not in results.checks and after not in results.metrics:
        _not_reached(results)


def _expect_conditional(results, name, why):
    """Checks the device only runs on some paths; absent (and no crash) skips with why."""
    if name in results.checks:
        results.expect(name)
    else:
        _not_reached(results)
        pytest.skip(why)


def _device_metric(results, metric, name):
    if name not in results.metrics:
        _not_reached(results)
        pytest.fail("device sent no %s" % name, pytrace=False)
    rec = results.metric_record(name)
    metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


# ----- tests, in the original's order -----

def test_board_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_ssid_not_configured(board, creds):  # pylint: disable=unused-argument
    """WiFi credentials are set; without them the original skips everything after this."""
    _need_creds(creds)


def test_scan_networks(creds, initial):
    """A scan finds at least one network (if the scan raised, "connect" is skipped too)."""
    _initial(creds, initial).expect("scan_networks")


def test_target_ssid_found(creds, initial):
    """CPR_WIFI_SSID is among the scanned networks."""
    _initial(creds, initial).expect("target_ssid_found")


def test_connect(creds, initial):
    """wifi.radio.connect() did not raise."""
    _expect_if_reported(_initial(creds, initial), "connect", "connect_ms")


def test_connect_ms(creds, initial, metric):
    """Connect time, compared with the median of this board's last five runs."""
    _device_metric(_initial(creds, initial), metric, "connect_ms")


def test_connected(creds, initial):
    """wifi.radio.connected is true after connect."""
    _initial(creds, initial).expect("connected")


def test_ipv4_address_set(creds, initial):
    """DHCP assigned an IPv4 address."""
    _initial(creds, initial).expect("ipv4_address_set")


def test_ipv4_gateway_set(creds, initial):
    """The AP gave a gateway."""
    _initial(creds, initial).expect("ipv4_gateway_set")


def test_ping(creds, initial):
    """wifi.radio.ping(gateway) did not raise (only runs when there is a gateway)."""
    r = _initial(creds, initial)
    if "ping" not in r.checks and "ping_result_numeric" not in r.checks:
        _not_reached(r)
        pytest.skip("no gateway, ping not run")
    _expect_if_reported(r, "ping", "ping_result_numeric")


def test_ping_result_numeric(creds, initial):
    """ping() returns a number of milliseconds."""
    _expect_conditional(_initial(creds, initial), "ping_result_numeric", "no gateway, ping not run")


def test_getaddrinfo(creds, initial):
    """socketpool getaddrinfo("adafruit.com", 80) did not raise."""
    _expect_if_reported(_initial(creds, initial), "getaddrinfo", "dns_resolves")


def test_dns_resolves(creds, initial):
    """getaddrinfo returned at least one address."""
    _initial(creds, initial).expect("dns_resolves")


def test_hostname_test(creds, initial):
    """Getting and setting wifi.radio.hostname did not raise."""
    _expect_if_reported(_initial(creds, initial), "hostname_test", "hostname_settable")


def test_hostname_settable(creds, initial):
    """wifi.radio.hostname reads back what was set."""
    _initial(creds, initial).expect("hostname_settable")


def test_hostname_default_valid(board, creds, initial):
    """The hostname the firmware picks at boot is one its own setter accepts.

    Fails on espressif @ 2d299b0: the default is "cpy-" + the end of the board ID + "-" + the
    MAC (ports/espressif/common-hal/wifi/__init__.c:538), and board IDs have underscores, which
    hostname_valid() refuses (shared-bindings/wifi/Radio.c:21-54) and DNS does not allow. Seen
    on bravo 2026-10-17: wifi.radio.hostname = wifi.radio.hostname raises ValueError on the
    S2 and S3. Expected to fail there until the firmware changes; an espressif pass is news.
    """
    results = _initial(creds, initial)
    rs = results.checks.get("hostname_default_valid") or []
    detail = str(rs[-1].get("detail", "")) if rs else ""
    if rs and rs[-1].get("ok") is False and board.port == "espressif" \
            and "'_' is not allowed" in detail:
        pytest.xfail("known: espressif default hostname has '_' (%s)" % detail)
    results.expect("hostname_default_valid")


def test_toggle(creds, initial):
    """enabled=False, enabled=True, connect() did not raise (#8032, #10729)."""
    _expect_if_reported(_initial(creds, initial), "toggle", "reconnected_after_toggle")


def test_reconnected_after_toggle(creds, initial):
    """connected is true after the radio toggle and reconnect."""
    _initial(creds, initial).expect("reconnected_after_toggle")


def test_device_exception(creds, initial):
    """DEVICE_INITIAL ran to the end without raising, and the board kept talking."""
    _initial(creds, initial).expect_no_exception()


@pytest.mark.parametrize("cycle,name", CYCLE_CHECKS,
                         ids=["%d-%s" % (c, n) for c, n in CYCLE_CHECKS])
def test_cycle(creds, initial, cycles, metric, cycle, name):
    """One check of one soft-reboot reconnect cycle (#3576, #6866, #8115)."""
    _initial(creds, initial)
    if initial is None or initial.crashed:
        pytest.skip("not reached: the initial device run failed")
    r = cycles[cycle]
    if r is None:
        pytest.skip("not reached: an earlier cycle lost the board")
    if name == "cycle_%d_error" % cycle:
        assert not r.crashed, "cycle %d failed: %s" % (cycle, r.crashed[-200:])
        assert not r.err, r.err[-200:]
    elif name == "reconnect_ms":
        _device_metric(r, metric, "reconnect_ms")
    elif name == "cycle_connect":
        _expect_if_reported(r, "cycle_connect", "reconnect_ms")
    else:
        r.expect(name)
