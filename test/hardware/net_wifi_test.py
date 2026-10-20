# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Network: the supervisor joins WiFi from settings.toml, after Brent's
hil/firmware/network/test_wifi.py @ 473c1200.

His firmware's runNetFSM() scans for the SSID, then connects, and halts when the SSID is not
there. CircuitPython's counterpart is the supervisor: on every VM reset it reads
CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD and calls common_hal_wifi_radio_connect once with
an 8 s timeout (supervisor/shared/web_workflow/web_workflow.c:329 @ 2d299b0); on failure it turns
the radio off and code.py still runs. CircuitPython prints nothing about WiFi on the console, so
where his tests read serial lines, these read the radio over the REPL.

  test_ssid_found       the farm SSID is in a scan, with a plausible RSSI and channel
                        (his ssid_found_pattern: "SSID (<ssid>) found! RSSI: -NN")
  test_wifi_connected   after a hard reset the supervisor has joined the farm SSID by itself,
                        within WIFI_CONNECT_TIMEOUT, with an address and gateway
  test_wifi_invalid_password  the farm SSID with a wrong password raises ConnectionError
                        "Authentication failure" in time, and the radio rejoins (his @ 90cd5af)
  test_ssid_not_found   connect() to an SSID that does not exist raises ConnectionError
                        "No network with that ssid" (shared-bindings/wifi/Radio.c:551) without
                        hanging, and the radio can join the farm network again afterwards

The boot-time half of ssid_not_found (an unknown SSID in settings.toml) is the unknown_ssid
scenario in settings_scenarios_test.py.

Credentials come from base_secrets (the prod-guarded .env values), like his $HIL_WIFI_*.
Never print ssid or password: they are only formatted into the device code.
"""
import pytest

from fixtures.flashing import hard_reset
from fixtures.secrets import FAKE_SSID_PREFIX

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("wifi"),
]

# How long the supervisor's boot join may take. Its one connect call has an 8 s budget for
# retries, but an all-channel scan plus DHCP can run past that; his WIFI_CONNECT_TIMEOUT is 150
# for five 20 s attempts.
WIFI_CONNECT_TIMEOUT = 30
# connect() with no timeout gives up after the first failure, one all-channel scan.
NOT_FOUND_TIMEOUT = 20
MISSING_SSID = FAKE_SSID_PREFIX + "no-such-network"  # his MISSING_SSID, conftest.py:319
MISSING_PASSWORD = "cpr-fake-password"
# His WRONG_WIFI_PASSWORD, conftest.py:322 @ 90cd5af. The connect for it is bounded like the
# missing SSID's: no timeout, so one failure ends it.
WRONG_WIFI_PASSWORD = "invalidpassword"
BAD_PASSWORD_TIMEOUT = 20

# str.format templates: literal braces are doubled. check/metric/guard come from lib/devtest.py.
DEVICE_SCAN = r"""
import wifi

ssid = {ssid!r}

seen = None
count = 0
try:
    for net in wifi.radio.start_scanning_networks():
        count += 1
        name = net.ssid.decode() if isinstance(net.ssid, bytes) else net.ssid
        if name == ssid and (seen is None or net.rssi > seen[0]):
            seen = (net.rssi, net.channel)
    wifi.radio.stop_scanning_networks()
    check("scan_networks", count > 0, "found " + str(count))
except Exception as e:
    check("scan_networks", False, type(e).__name__ + ": " + str(e))

# His ws_ssid_scan_t (src/helpers/ws_helper_status.h, PR #1019), so both farms log the same words.
result = "SSID_FOUND" if seen is not None else "SSID_NOT_FOUND" if count else "NO_NETWORKS"
check("ssid_found", seen is not None, "WS_SSID_SCAN_" + result + ", networks seen: " + str(count))
if seen is not None:
    rssi, channel = seen
    check("rssi_plausible", isinstance(rssi, int) and -100 <= rssi < 0, "rssi=" + str(rssi))
    check("channel_plausible", isinstance(channel, int) and 1 <= channel <= 14,
          "channel=" + str(channel))
    metric("rssi", rssi, "dBm", "higher")
"""

DEVICE_CONNECTED = r"""
import time
import wifi

ssid = {ssid!r}
timeout = {timeout}

t0 = time.monotonic()
while not wifi.radio.connected and time.monotonic() - t0 < timeout:
    time.sleep(0.25)
check("connected", wifi.radio.connected,
      "after %.1f s of %d, radio enabled=%s" % (time.monotonic() - t0, timeout,
                                               wifi.radio.enabled))
if wifi.radio.connected:
    ap = wifi.radio.ap_info
    name = None if ap is None else (ap.ssid.decode() if isinstance(ap.ssid, bytes) else ap.ssid)
    check("joined_farm_ssid", name == ssid, "ap_info " + ("present" if ap else "None"))
    check("ipv4_address_set", wifi.radio.ipv4_address is not None, str(wifi.radio.ipv4_address))
    check("ipv4_gateway_set", wifi.radio.ipv4_gateway is not None, str(wifi.radio.ipv4_gateway))
"""

DEVICE_NOT_FOUND = r"""
import time
import wifi

ssid = {ssid!r}
password = {password!r}
missing = {missing!r}
missing_password = {missing_password!r}
timeout = {timeout}

t0 = time.monotonic()
raised = None
try:
    wifi.radio.connect(missing, missing_password)
except Exception as e:
    raised = e
took = time.monotonic() - t0
metric("not_found_s", took, "s", "lower")
check("not_found_raises", raised is not None, "connect() returned after %.1f s" % took)
if raised is not None:
    check("not_found_connection_error", isinstance(raised, ConnectionError),
          type(raised).__name__ + ": " + str(raised))
    check("not_found_message", str(raised) == "No network with that ssid", str(raised))
check("not_found_in_time", took < timeout, "%.1f s, limit %d" % (took, timeout))
check("not_connected_after", not wifi.radio.connected, str(wifi.radio.connected))

# His firmware halts here; CircuitPython keeps going, so the radio must still work.
def rejoin():
    if not wifi.radio.enabled:
        wifi.radio.enabled = True
    wifi.radio.connect(ssid, password)
    return wifi.radio.connected

guard("rejoin", rejoin)
check("rejoined", wifi.radio.connected, str(wifi.radio.connected))
"""

DEVICE_BAD_PASSWORD = r"""
import time
import wifi

ssid = {ssid!r}
password = {password!r}
wrong = {wrong!r}
timeout = {timeout}

if wifi.radio.connected:
    wifi.radio.stop_station()
    # connected goes False at once (sta_mode), but connect() reads the event bits, which only
    # change when the STA_DISCONNECTED event lands; called first it sees "already joined to
    # this SSID" and returns without trying the password (espressif Radio.c:364-371).
    time.sleep(2)
t0 = time.monotonic()
raised = None
try:
    wifi.radio.connect(ssid, wrong)
except Exception as e:
    raised = e
took = time.monotonic() - t0
metric("bad_password_s", took, "s", "lower")
check("bad_password_raises", raised is not None, "connect() returned after %.1f s%s" % (
    took, " (still marked joined: Radio.c:364-371)" if took < 0.5 else ""))
if raised is not None:
    check("bad_password_connection_error", isinstance(raised, ConnectionError),
          type(raised).__name__ + ": " + str(raised))
    check("bad_password_message", str(raised) == "Authentication failure", str(raised))
check("bad_password_in_time", took < timeout, "%.1f s, limit %d" % (took, timeout))
check("not_connected_after", not wifi.radio.connected, str(wifi.radio.connected))

def rejoin():
    if not wifi.radio.enabled:
        wifi.radio.enabled = True
    wifi.radio.connect(ssid, password)
    return wifi.radio.connected

guard("rejoin", rejoin)
check("rejoined", wifi.radio.connected, str(wifi.radio.connected))
"""


@pytest.fixture(scope="module")
def creds(base_secrets):
    """(ssid, password) from base_secrets, like his network_creds from $HIL_WIFI_*; skips the
    module when .env does not set them."""
    ssid = base_secrets.get("CIRCUITPY_WIFI_SSID")
    password = base_secrets.get("CIRCUITPY_WIFI_PASSWORD")
    if not ssid or not password:
        pytest.skip("ssid_not_configured: CPR_WIFI_SSID and/or CPR_WIFI_PASSWORD not set")
    return ssid, password


def _run(device, code, timeout):
    results = device.run(code, timeout=timeout)
    if results.crashed:
        pytest.fail("device run failed: %s" % results.crashed[:300], pytrace=False)
    return results


def test_ssid_found(creds, flashed_fs, device):  # pylint: disable=unused-argument
    # --- Arrange ------------------------------------------------------
    # 1. CIRCUITPY carries settings.toml with the farm WiFi from base_secrets (flashed_fs).
    ssid, _ = creds

    # --- Act ----------------------------------------------------------
    # 2. Scan from the REPL.
    results = _run(device, DEVICE_SCAN.format(ssid=ssid), timeout=60)

    # --- Assert -------------------------------------------------------
    # 3. The farm SSID is in the scan, with an RSSI and channel a real AP would report.
    for name in ("scan_networks", "ssid_found", "rssi_plausible", "channel_plausible"):
        results.expect(name)


def test_wifi_connected(board, creds, flashed_fs, device):  # pylint: disable=unused-argument
    # --- Arrange ------------------------------------------------------
    # 1. settings.toml names the farm WiFi (flashed_fs). Nothing below calls connect().
    ssid, _ = creds

    # --- Act ----------------------------------------------------------
    # 2. Hard reset, so the supervisor reads settings.toml and joins on its own (a soft
    #    reload keeps the old connection: wifi_user_reset in ports/espressif).
    if not hard_reset(board):
        pytest.fail("%s did not come back after the hard reset" % board.name, pytrace=False)

    # --- Assert -------------------------------------------------------
    # 3. Within WIFI_CONNECT_TIMEOUT the radio is connected to the farm SSID with an address.
    code = DEVICE_CONNECTED.format(ssid=ssid, timeout=WIFI_CONNECT_TIMEOUT)
    results = _run(device, code, timeout=WIFI_CONNECT_TIMEOUT + 30)
    for name in ("connected", "joined_farm_ssid", "ipv4_address_set", "ipv4_gateway_set"):
        results.expect(name)


def test_wifi_invalid_password(creds, flashed_fs, device):  # pylint: disable=unused-argument
    # --- Arrange ------------------------------------------------------
    # 1. The farm SSID with a wrong password: the SSID is there, so only the join can fail.
    ssid, password = creds

    # --- Act ----------------------------------------------------------
    # 2. Leave the farm network, join with the wrong password, then rejoin properly.
    code = DEVICE_BAD_PASSWORD.format(ssid=ssid, password=password, wrong=WRONG_WIFI_PASSWORD,
                                      timeout=BAD_PASSWORD_TIMEOUT)
    results = _run(device, code, timeout=BAD_PASSWORD_TIMEOUT + 60)

    # --- Assert -------------------------------------------------------
    # 3. ConnectionError "Authentication failure" (shared-bindings/wifi/Radio.c:550), in time,
    #    not connected; then the radio joins again (his firmware halts for the WDT here).
    #    Radio.c:435 maps reasons 202, 15 and the two NO_AP_FOUND_* variants to AUTH_FAIL;
    #    a 204 handshake timeout would read "Unknown failure 204" and fail the message check.
    for name in ("bad_password_raises", "bad_password_connection_error", "bad_password_message",
                 "bad_password_in_time", "not_connected_after", "rejoined"):
        results.expect(name)
    if "rejoin" in results.checks:
        results.expect("rejoin")


def test_ssid_not_found(creds, flashed_fs, device):  # pylint: disable=unused-argument
    # --- Arrange ------------------------------------------------------
    # 1. A WiFi network that does not exist (cpr-fake-*, which the prod guard allows).
    ssid, password = creds

    # --- Act ----------------------------------------------------------
    # 2. Ask the radio to join it, then rejoin the farm network.
    code = DEVICE_NOT_FOUND.format(ssid=ssid, password=password, missing=MISSING_SSID,
                                   missing_password=MISSING_PASSWORD, timeout=NOT_FOUND_TIMEOUT)
    results = _run(device, code, timeout=NOT_FOUND_TIMEOUT + 60)

    # --- Assert -------------------------------------------------------
    # 3. ConnectionError "No network with that ssid", in time, not connected; then the radio
    #    joins the farm network again (where his firmware would halt and wait for the WDT).
    for name in ("not_found_raises", "not_found_connection_error", "not_found_message",
                 "not_found_in_time", "not_connected_after", "rejoined"):
        results.expect(name)
    if "rejoin" in results.checks:
        results.expect("rejoin")
