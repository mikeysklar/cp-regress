# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""BLE advertising and scanning between two boards, including role swap.

pytest version of tests/ble_pair.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). One module-scoped fixture runs the three phases once per
board, holding the partner board's lock for all of them, and stops advertising and scanning
on both boards after each phase as the original does. Each check is then one test.
Targets: #6012 (hang when advertising while scanning on the same board).
"""
import logging

import pytest

import farmlib
from fixtures.device import DeviceResults
from fixtures.farm import board_lock

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb", "ble"),
    pytest.mark.targets(6012),
    pytest.mark.multi_board,
]

log = logging.getLogger(__name__).info

NO_PARTNER = "no partner board with ble capability"


# Advertiser role. Starts a non-connectable advertisement and returns at once: advertising
# keeps running in the background of the same VM until stop_advertising() or soft reboot,
# so the host can scan from the other board while this one keeps advertising.
# Plain exec_raw (no devtest), so it reports with ADV_OK / ADV_ERR markers.
DEVICE_ADVERTISER = r'''
import _bleio
a = _bleio.adapter
NAME = b"@@NAME@@"
try:
    if not a.enabled:
        a.enabled = True
    a.stop_advertising()
    a.start_advertising(b"\x02\x01\x06" + bytes((len(NAME) + 1, 0x09)) + NAME,
                        connectable=False, timeout=60)
    print("ADV_OK advertising=%s" % a.advertising)
except Exception as e:
    print("ADV_ERR %s: %s" % (type(e).__name__, e))
'''

DEVICE_STOP_ADV = r'''
import _bleio
_bleio.adapter.stop_advertising()
_bleio.adapter.stop_scan()
print("STOP_OK")
'''

# Scanner role (devtest helpers available). Looks for a Complete/Shortened Local Name equal
# to NAME in raw advertisement bytes.
DEVICE_SCANNER = r'''
import _bleio
import time

a = _bleio.adapter
NAME = b"@@NAME@@"

def _has_name(adv):
    i = 0
    n = len(adv)
    while i + 1 < n:
        ln = adv[i]
        if ln == 0 or i + 1 + ln > n:
            break
        if adv[i + 1] in (0x08, 0x09) and adv[i + 2:i + 1 + ln] == NAME:
            return True
        i += 1 + ln
    return False

try:
    if not a.enabled:
        a.enabled = True
    found = None
    seen = 0
    t0 = time.monotonic()
    try:
        for entry in a.start_scan(timeout=10, minimum_rssi=-100):
            seen += 1
            if _has_name(entry.advertisement_bytes):
                found = ((time.monotonic() - t0) * 1000, entry.rssi)
                break
    finally:
        a.stop_scan()
    check("scan_entry_found", found is not None,
          "name=%s seen=%d entries" % (NAME, seen))
    if found:
        metric("scan_find_ms", found[0], "ms")
        check("rssi_sane", -110 < found[1] < 0, "rssi=%d dBm" % found[1], value=found[1])
except Exception as e:
    check("scan_error", False, "%s: %s" % (type(e).__name__, e))
'''

# Advertising while scanning on one board (#6012 was a hang). Either outcome is fine as
# long as it is clean and the scan still finishes; a hang shows up as a host timeout.
DEVICE_ADV_WHILE_SCAN = r'''
import _bleio

a = _bleio.adapter
data = b"\x02\x01\x06\x05\x09CPRS"

try:
    a.stop_advertising()
    scan_results = a.start_scan(timeout=5, minimum_rssi=-100)
    try:
        a.start_advertising(data, connectable=False, timeout=3)
        check("advertising_while_scanning", True, "allowed")
    except (_bleio.BluetoothError, RuntimeError) as e:
        check("advertising_while_scanning", True, "refused cleanly: %s: %s" % (type(e).__name__, e))
    n = 0
    for entry in scan_results:
        n += 1
    check("scan_finished_after_adv", True, "%d entries" % n)
except Exception as e:
    check("advertising_while_scanning", False, "%s: %s" % (type(e).__name__, e))
finally:
    a.stop_scan()
    a.stop_advertising()
'''


def _why(e):
    """The detail text the original gives a host-side exception."""
    if isinstance(e, farmlib.FarmError):
        return "FarmError: %s" % e
    return "%s: %s" % (type(e).__name__, e)


def _advertise(b, name):
    """Start the advertiser on b. Returns (ok, detail), like the original's _advertise check."""
    try:
        with b.repl() as r:
            out, err = r.exec_raw(DEVICE_ADVERTISER.replace("@@NAME@@", name), timeout=15)
    except Exception as e:  # pylint: disable=broad-except
        return False, _why(e)
    return "ADV_OK" in out and not err.strip(), (out.strip() + " " + err.strip())[-200:]


def _stop(b):
    try:
        with b.repl() as r:
            out, err = r.exec_raw(DEVICE_STOP_ADV, timeout=10)
        if "STOP_OK" not in out:
            log("%s: stop advertising: %r %r" % (b.name, out, err))
    except Exception as e:  # pylint: disable=broad-except
        log("%s: stop advertising failed: %s" % (b.name, e))


def _scan(b, name, timeout):
    """Run DEVICE_SCANNER on b. Returns (DeviceResults or None, error detail or None).

    Not device.run(): a FarmError here fails scan_entry_found and the next phase still runs,
    as in the original, instead of marking the board crashed for the rest of the module.
    """
    try:
        with b.repl() as r:
            res, out, err = r.run_device(DEVICE_SCANNER.replace("@@NAME@@", name), timeout=timeout)
    except Exception as e:  # pylint: disable=broad-except
        return None, _why(e)
    return DeviceResults(b, res, out, err), None


def _find_partner(board, farm_boards):
    """ctx.other("ble"): the first other present board with the ble cap."""
    for b in farm_boards:
        if "ble" in b.caps and b is not board and b.present():
            return b
    return None


def _phases(rec, board, partner):
    # ===== Phase 1: This board scans, partner advertises =====
    log("Phase 1: %s scanning, %s advertising" % (board.name, partner.name))
    try:
        rec["partner_advertise_start"] = _advertise(partner, "CPRA")
        if rec["partner_advertise_start"][0]:
            rec["scan"], rec["scan_error_host"] = _scan(board, "CPRA", 40)
    finally:
        _stop(partner)

    # ===== Phase 2: Swap roles =====
    log("Phase 2: roles swapped")
    try:
        rec["advertise_start"] = _advertise(board, "CPRB")
        if rec["advertise_start"][0]:
            rec["partner_scan"], rec["partner_scan_error_host"] = _scan(partner, "CPRB", 40)
    finally:
        _stop(board)

    # ===== Phase 3: Test advertising while scanning =====
    log("Phase 3: concurrent operations")
    try:
        with board.repl() as r:
            res, out, err = r.run_device(DEVICE_ADV_WHILE_SCAN, timeout=30)
        rec["adv_while_scan"] = DeviceResults(board, res, out, err)
        rec["adv_while_scan_no_hang"] = (True, "")
    except Exception as e:  # pylint: disable=broad-except
        rec["adv_while_scan_no_hang"] = (False, _why(e))
        _stop(board)


@pytest.fixture(scope="module")
def pair(board, farm_boards):
    """Run all three phases once per board and return what each step saw.

    Keys: partner, partner_error, partner_advertise_start, scan, scan_error_host,
    advertise_start, partner_scan, partner_scan_error_host, adv_while_scan,
    adv_while_scan_no_hang, error. A key that is missing means that step never ran.
    """
    rec = {"partner": None}
    try:
        partner = _find_partner(board, farm_boards)
        if partner is None:
            return rec
        log("partner: %s" % partner.name)
        rec["partner"] = partner.name
        with board_lock(partner):
            _phases(rec, board, partner)
    except (Exception, pytest.fail.Exception) as e:  # pylint: disable=broad-except
        rec["error"] = _why(e)
    return rec


def _need_partner(pair):
    if pair["partner"] is None:
        pytest.skip(NO_PARTNER)


def _scanner(pair, key, adv_key):
    """The DeviceResults of a scan phase, or a skip/fail that says why there are none."""
    _need_partner(pair)
    if adv_key not in pair:
        pytest.skip("not reached: %s" % pair.get("error", "phase did not run"))
    if not pair[adv_key][0]:
        pytest.skip("not reached: %s failed" % adv_key)
    host_err = pair.get(key + "_error_host")
    if host_err:
        return None, host_err
    return pair[key], None


def _expect_found(res):
    """scan_entry_found. When the scanner code raised, it never reports it: scan_error carries
    that failure, so this skips instead of failing a second time."""
    if "scan_entry_found" not in res.checks and "scan_error" in res.checks:
        pytest.skip("not reached: scan_error: %s" % res.detail("scan_error"))
    res.expect("scan_entry_found")


def _check_or_absent(results, name):
    """A check the device only reports on failure: absent means pass."""
    if name in results.checks:
        results.expect(name)


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_ble_pair(pair):
    """Another present board with the ble cap was found and locked (skips "ble_pair" otherwise)."""
    _need_partner(pair)
    assert "error" not in pair, pair.get("error")


# ----- Phase 1: this board scans, partner advertises CPRA -----

def test_partner_advertise_start(pair):
    """The partner starts a non-connectable advertisement named CPRA."""
    _need_partner(pair)
    if "partner_advertise_start" not in pair:
        pytest.skip("not reached: %s" % pair.get("error", "phase did not run"))
    ok, detail = pair["partner_advertise_start"]
    assert ok, detail


def test_scan_entry_found(pair):
    """This board's 10 s scan sees a Local Name of exactly CPRA."""
    res, host_err = _scanner(pair, "scan", "partner_advertise_start")
    assert host_err is None, host_err
    _expect_found(res)


def test_scan_find_ms(pair, metric):
    """Time to the first CPRA hit, compared with this board's history."""
    res, host_err = _scanner(pair, "scan", "partner_advertise_start")
    if host_err or "scan_find_ms" not in res.metrics:
        pytest.skip("not reached: no scan hit")
    rec = res.metric_record("scan_find_ms")
    metric("scan_find_ms", rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def test_rssi_sane(pair):
    """-110 < rssi < 0 for the CPRA hit."""
    res, host_err = _scanner(pair, "scan", "partner_advertise_start")
    if host_err or "rssi_sane" not in res.checks:
        pytest.skip("not reached: no scan hit")
    res.expect("rssi_sane")


def test_scan_error(pair):
    """The scanner code raised nothing unexpected."""
    res, host_err = _scanner(pair, "scan", "partner_advertise_start")
    if host_err:
        pytest.skip("not reached: " + host_err)
    _check_or_absent(res, "scan_error")


# ctx.device() adds "device_exception" after phase 1 and again after phase 3.
DEVICE_EXCEPTION_PHASES = ["scan", "adv_while_scan"]


@pytest.mark.parametrize("phase", DEVICE_EXCEPTION_PHASES)
def test_device_exception(pair, phase):
    """The device code on this board raised nothing (phase 1 scan, phase 3 adv-while-scan)."""
    if phase == "scan":
        res, host_err = _scanner(pair, "scan", "partner_advertise_start")
        if host_err:
            pytest.skip("not reached: " + host_err)
    else:
        _need_partner(pair)
        res = pair.get("adv_while_scan")
        if res is None:
            pytest.skip("not reached: %s" % (pair.get("adv_while_scan_no_hang", (0, ""))[1]
                                             or pair.get("error", "phase did not run")))
    assert not res.err, res.err[-400:]


# ----- Phase 2: roles swapped, this board advertises CPRB, partner scans -----

def test_advertise_start(pair):
    """This board starts a non-connectable advertisement named CPRB."""
    _need_partner(pair)
    if "advertise_start" not in pair:
        pytest.skip("not reached: %s" % pair.get("error", "phase did not run"))
    ok, detail = pair["advertise_start"]
    assert ok, detail


def test_partner_scan_entry_found(pair):
    """The partner's scan sees CPRB ("partner.scan_entry_found")."""
    res, host_err = _scanner(pair, "partner_scan", "advertise_start")
    assert host_err is None, host_err
    assert res.records, "no results: " + res.out[-200:]
    _expect_found(res)


def test_partner_scan_find_ms(pair, metric):
    """Partner time to the first CPRB hit ("partner.scan_find_ms")."""
    res, host_err = _scanner(pair, "partner_scan", "advertise_start")
    if host_err or "scan_find_ms" not in res.metrics:
        pytest.skip("not reached: no scan hit")
    rec = res.metric_record("scan_find_ms")
    metric("partner.scan_find_ms", rec.get("value"), rec.get("unit", ""), rec.get("better", "lower"))


def test_partner_rssi_sane(pair):
    """-110 < rssi < 0 for the partner's CPRB hit ("partner.rssi_sane")."""
    res, host_err = _scanner(pair, "partner_scan", "advertise_start")
    if host_err or "rssi_sane" not in res.checks:
        pytest.skip("not reached: no scan hit")
    res.expect("rssi_sane")


def test_partner_scan_error(pair):
    """The partner's scanner code raised nothing unexpected ("partner.scan_error")."""
    res, host_err = _scanner(pair, "partner_scan", "advertise_start")
    if host_err:
        pytest.skip("not reached: " + host_err)
    _check_or_absent(res, "scan_error")


def test_partner_device_exception(pair):
    """The partner's device code raised nothing ("partner.device_exception")."""
    res, host_err = _scanner(pair, "partner_scan", "advertise_start")
    if host_err:
        pytest.skip("not reached: " + host_err)
    assert not res.err, res.err[-400:]


# ----- Phase 3: advertising while scanning on this board (#6012) -----

def _adv_while_scan(pair):
    _need_partner(pair)
    res = pair.get("adv_while_scan")
    if res is None:
        pytest.skip("not reached: %s" % (pair.get("adv_while_scan_no_hang", (0, ""))[1]
                                         or pair.get("error", "phase did not run")))
    return res


def test_advertising_while_scanning(pair):
    """Starting an advertisement during a scan works or raises BluetoothError/RuntimeError."""
    _adv_while_scan(pair).expect("advertising_while_scanning")


def test_scan_finished_after_adv(pair):
    """The scan still finishes after the advertising attempt."""
    _adv_while_scan(pair).expect("scan_finished_after_adv")


def test_adv_while_scan_no_hang(pair):
    """Phase 3 returns before the 30 s host timeout (#6012 was a hang)."""
    _need_partner(pair)
    if "adv_while_scan_no_hang" not in pair:
        pytest.skip("not reached: %s" % pair.get("error", "phase did not run"))
    ok, detail = pair["adv_while_scan_no_hang"]
    assert ok, detail
