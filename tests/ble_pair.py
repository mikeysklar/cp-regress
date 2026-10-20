# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""BLE advertising and scanning pair test.

Tests BLE advertising and scanning between two boards, including role swap.
Targets regressions in BLE advertisement, scan, and concurrent operation.
"""

import farmlib

META = {
    "id": "ble_pair",
    "title": "BLE advertising and scanning between paired boards",
    "category": "BLE",
    "tier": "nightly",
    "needs": ["ble"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": True,
    "timeout": 120,
    "targets": [6012],
}

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


def _advertise(ctx, b, name, check_name):
    try:
        with b.repl() as r:
            out, err = r.exec_raw(DEVICE_ADVERTISER.replace("@@NAME@@", name), timeout=15)
    except farmlib.FarmError as e:
        return ctx.check(check_name, False, "FarmError: %s" % e)
    return ctx.check(check_name, "ADV_OK" in out and not err.strip(),
                     (out.strip() + " " + err.strip())[-200:])


def _stop(ctx, b):
    try:
        with b.repl() as r:
            out, err = r.exec_raw(DEVICE_STOP_ADV, timeout=10)
        if "STOP_OK" not in out:
            ctx.log("%s: stop advertising: %r %r" % (b.name, out, err))
    except farmlib.FarmError as e:
        ctx.log("%s: stop advertising failed: %s" % (b.name, e))


def run(board, ctx):
    # Basic presence checks
    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    # Get partner board with BLE capability
    partner = ctx.other("ble")
    if not partner:
        ctx.skip("ble_pair", "no partner board with ble capability")
        return
    ctx.log("partner: %s" % partner.name)

    # ===== Phase 1: This board scans, partner advertises =====
    ctx.log("Phase 1: %s scanning, %s advertising" % (board.name, partner.name))
    try:
        if _advertise(ctx, partner, "CPRA", "partner_advertise_start"):
            try:
                ctx.device(DEVICE_SCANNER.replace("@@NAME@@", "CPRA"), timeout=40)
            except farmlib.FarmError as e:
                ctx.check("scan_entry_found", False, "FarmError: %s" % e)
    finally:
        _stop(ctx, partner)

    # ===== Phase 2: Swap roles =====
    ctx.log("Phase 2: roles swapped")
    try:
        if _advertise(ctx, board, "CPRB", "advertise_start"):
            try:
                with partner.repl() as pr:
                    res, out, err = pr.run_device(DEVICE_SCANNER.replace("@@NAME@@", "CPRB"),
                                                  timeout=40)
                for r in res:
                    name = "partner." + str(r.get("name"))
                    if r.get("kind") == "metric":
                        ctx.metric(name, r.get("value"), r.get("unit", ""), r.get("better", "lower"))
                    elif r.get("ok") is None:
                        ctx.skip(name, r.get("detail", ""))
                    else:
                        ctx.check(name, r.get("ok"), r.get("detail", ""), r.get("value"))
                if not res:
                    ctx.check("partner.scan_entry_found", False, "no results: " + out[-200:])
                if err.strip():
                    ctx.check("partner.device_exception", False, err.strip()[-400:])
            except farmlib.FarmError as e:
                ctx.check("partner.scan_entry_found", False, "FarmError: %s" % e)
    finally:
        _stop(ctx, board)

    # ===== Phase 3: Test advertising while scanning =====
    ctx.log("Phase 3: concurrent operations")
    try:
        ctx.device(DEVICE_ADV_WHILE_SCAN, timeout=30)
        ctx.check("adv_while_scan_no_hang", True)
    except farmlib.FarmError as e:
        ctx.check("adv_while_scan_no_hang", False, "FarmError: %s" % e)
        _stop(ctx, board)
