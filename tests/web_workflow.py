# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Web workflow HTTP API regression fixtures: version, devices, auth, file operations, mDNS."""

import base64
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request

META = {
    "id": "web_workflow",
    "title": "Web workflow HTTP API: endpoints, auth, file ops, mDNS",
    "category": "Network",
    "tier": "nightly",
    "needs": ["native_usb", "wifi"],
    "wiring": [],
    "skip_boards": ["metro_m0"],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 240,
    "targets": [7636, 7929, 8275, 8762, 10048, 6805, 10762, 8503, 8690, 9171, 10054, 8107],
}

# Device-side: wait for the supervisor's settings.toml connection, report the address.
DEVICE_INIT = r'''
import time
try:
    import wifi
except ImportError:
    wifi = None

if wifi is None:
    skip("wifi_connected", "no wifi module")
else:
    t0 = time.monotonic()
    while not wifi.radio.connected and (time.monotonic() - t0) < 30:
        time.sleep(0.5)
    check("wifi_connected", wifi.radio.connected, "connected=%s" % wifi.radio.connected)
    if wifi.radio.connected:
        ip = wifi.radio.ipv4_address
        check("ipv4_address", ip is not None and str(ip) != "0.0.0.0", "ip=%s" % ip)
        check("ipv4_gateway", wifi.radio.ipv4_gateway is not None, "gateway=%s" % wifi.radio.ipv4_gateway)
        if ip is not None:
            print("CPR_IP %s" % ip)
'''

WEB_PASSWORD = "cpr-test"
TEST_DATA = b"CircuitPython regression test data\n"


def _toml_str(s):
    """TOML basic string. CircuitPython's settings parser only treats "..." as quoted."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _http(url, method="GET", data=None, headers=None, timeout=10):
    """Return (status, headers, body). status is None and body the error text on transport failure."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, e.headers, body
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return None, None, ("%s: %s" % (type(e).__name__, e)).encode()


def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _desc(status, body):
    return "status=%s %r" % (status, body[:120])


def _restart(board, ctx):
    """Hard reset so the supervisor re-reads settings.toml. True when the board is back."""
    import farmlib

    if "reset_ok" in board.caps:
        try:
            with board.repl() as r:
                r.prompt()
                r.write(b"import microcontroller; microcontroller.reset()\r")
                time.sleep(0.5)
        except Exception as e:  # noqa: BLE001 - the port vanishing mid-write is expected
            ctx.log("reset write: %s" % e)
        time.sleep(3)
        if board.wait_ready() is not None:
            return True
        ctx.log("no tty/mount after microcontroller.reset(), power cycling")
    try:
        return board.power_cycle() is not None
    except farmlib.FarmError as e:
        ctx.log("power_cycle: %s" % e)
        return False


def _http_checks(board, ctx, ip):
    base = "http://%s" % ip
    auth = {"Authorization": "Basic " + base64.b64encode(b":" + WEB_PASSWORD.encode()).decode("ascii")}
    bad_auth = {"Authorization": "Basic " + base64.b64encode(b":wrong-password").decode("ascii")}
    as_json = {"Accept": "application/json"}

    # GET /cp/version.json (no auth)
    t0 = time.monotonic()
    st, _, body = _http(base + "/cp/version.json")
    ctx.check("version_json_200", st == 200, _desc(st, body))
    info = _json(body) if st == 200 else None
    if st == 200:
        ctx.metric("version_json_ms", (time.monotonic() - t0) * 1000, "ms", "lower")
    ctx.check("version_json_parses", info is not None, repr(body[:120]))
    if info is not None:
        ctx.check("version_json_has_version", bool(info.get("version")), "version=%r" % info.get("version"))
        ctx.check("version_json_board_id", info.get("board_id") == board.board_id,
                  "board_id=%r expected %r" % (info.get("board_id"), board.board_id))
        ctx.check("version_json_ip", info.get("ip") == ip, "ip=%r expected %r" % (info.get("ip"), ip))
        ctx.log("Version: %s" % info.get("version"))

    # GET /cp/devices.json (no auth, runs an mDNS query on the device)
    st, _, body = _http(base + "/cp/devices.json", timeout=15)
    ctx.check("devices_json_200", st == 200, _desc(st, body))
    dev = _json(body) if st == 200 else None
    ctx.check("devices_json_shape", isinstance(dev, dict) and "total" in dev and isinstance(dev.get("devices"), list),
              "keys=%s" % (sorted(dev) if isinstance(dev, dict) else None))

    # Auth: /fs/ must refuse missing or wrong credentials (#8275)
    st, _, body = _http(base + "/fs/", headers=as_json)
    ctx.check("fs_no_auth_401", st == 401, _desc(st, body))
    h = dict(as_json)
    h.update(bad_auth)
    st, _, body = _http(base + "/fs/", headers=h)
    ctx.check("fs_bad_auth_401", st == 401, _desc(st, body))

    # OPTIONS /fs/ (CORS preflight, unauthenticated by design)
    st, hdrs, body = _http(base + "/fs/", method="OPTIONS")
    ctx.check("fs_options_204", st == 204, _desc(st, body))
    allow = hdrs.get("Access-Control-Allow-Methods") if hdrs is not None else None
    ctx.check("fs_options_has_allow", allow is not None, "header=%s" % allow)
    ctx.check("fs_options_has_move", allow is not None and "MOVE" in allow, "methods=%s" % allow)

    # GET /fs/ as JSON listing
    h = dict(as_json)
    h.update(auth)
    st, _, body = _http(base + "/fs/", headers=h)
    ctx.check("fs_list_200", st == 200, _desc(st, body))
    listing = _json(body) if st == 200 else None
    files = listing.get("files") if isinstance(listing, dict) else None
    ctx.check("fs_list_has_files", isinstance(files, list), "keys=%s" % (sorted(listing) if isinstance(listing, dict) else None))
    names = [f.get("name") for f in files] if isinstance(files, list) else []
    ctx.check("fs_list_has_boot_out", "boot_out.txt" in names, "names=%s" % names[:10])
    writable = listing.get("writable") if isinstance(listing, dict) else None
    ctx.check("fs_list_writable_bool", isinstance(writable, bool), "writable=%r" % writable)

    # GET an existing file and compare with what the host reads over MSC
    st, _, body = _http(base + "/fs/boot_out.txt", headers=auth)
    ctx.check("get_boot_out_200", st == 200, _desc(st, body))
    if st == 200:
        try:
            host = board.read_file("boot_out.txt", binary=True)
        except Exception as e:  # noqa: BLE001
            host = None
            ctx.log("host read of boot_out.txt: %s" % e)
        if host is None:
            ctx.skip("get_boot_out_matches", "could not read boot_out.txt over MSC")
        else:
            ctx.check("get_boot_out_matches", body == host, "web %d bytes, msc %d bytes" % (len(body), len(host)))

    # Writes. While the host has CIRCUITPY writable over USB MSC the device holds the
    # blockdev lock for USB and every web write must answer 409 (#8503).
    st, _, body = _http(base + "/fs/cpr_ww.txt", method="PUT", data=TEST_DATA,
                        headers=dict(auth, **{"Content-Type": "text/plain"}))
    if writable is False:
        ctx.check("put_409_when_usb_owns_fs", st == 409, _desc(st, body))
        ctx.skip("fs_roundtrip", "CIRCUITPY writable over USB, web workflow read-only (expected 409)")
        return
    try:
        ctx.check("put_201_or_204", st in (201, 204), _desc(st, body))
        time.sleep(1)  # a successful write triggers an autoreload on the device
        st, _, body = _http(base + "/fs/cpr_ww.txt", headers=auth)
        ctx.check("get_200", st == 200, _desc(st, body))
        ctx.check("get_content_matches", st == 200 and body == TEST_DATA, "got %d bytes" % len(body))

        _http(base + "/fs/cpr_ww2.txt", method="DELETE", headers=auth)  # leftover from an aborted run
        st, _, body = _http(base + "/fs/cpr_ww.txt", method="MOVE",
                            headers=dict(auth, **{"X-Destination": "/fs/cpr_ww2.txt"}))
        ctx.check("move_201", st == 201, _desc(st, body))
        time.sleep(1)
        st_old, _, _ = _http(base + "/fs/cpr_ww.txt", headers=auth)
        st_new, _, body = _http(base + "/fs/cpr_ww2.txt", headers=auth)
        ctx.check("move_source_gone", st_old == 404, "status=%s" % st_old)
        ctx.check("move_dest_matches", st_new == 200 and body == TEST_DATA, _desc(st_new, body))

        st, _, body = _http(base + "/fs/cpr_ww2.txt", method="DELETE", headers=auth)
        ctx.check("delete_204", st == 204, _desc(st, body))
        time.sleep(1)
        st, _, _ = _http(base + "/fs/cpr_ww2.txt", headers=auth)
        ctx.check("delete_then_404", st == 404, "status=%s" % st)
    finally:
        for rel in ("cpr_ww.txt", "cpr_ww2.txt"):
            _http(base + "/fs/" + rel, method="DELETE", headers=auth)


def _mdns_check(ctx, ip):
    st, _, body = _http("http://%s/cp/version.json" % ip)
    info = _json(body) if st == 200 else None
    hostname = info.get("hostname") if isinstance(info, dict) else None
    if not hostname:
        ctx.skip("mdns_resolve", "no hostname in version.json")
        return
    name = hostname + ".local"
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)})
    except OSError as e:
        ctx.skip("mdns_resolve", "%s did not resolve (nss-mdns missing?): %s" % (name, e))
        return
    ctx.check("mdns_resolve", ip in addrs, "%s -> %s, expected %s" % (name, addrs, ip))


def run(board, ctx):
    import farmlib

    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    ssid = ctx.secret("ssid_env")
    password = ctx.secret("password_env")
    if not ssid or not password:
        ctx.skip("network", "WiFi credentials not set")
        return

    settings_toml = (
        "# cp-regress web_workflow\n"
        "CIRCUITPY_WIFI_SSID = %s\n"
        "CIRCUITPY_WIFI_PASSWORD = %s\n"
        "CIRCUITPY_WEB_API_PASSWORD = %s\n"
    ) % (_toml_str(ssid), _toml_str(password), _toml_str(WEB_PASSWORD))
    code_py = "# cp-regress web_workflow idle\nimport time\nwhile True:\n    time.sleep(1)\n"

    try:
        with board.preserved(files=("settings.toml", "code.py")):
            board.write_file("settings.toml", settings_toml)
            board.write_file("code.py", code_py)

            # The web workflow reads settings.toml once per hard boot.
            back = _restart(board, ctx)
            ctx.check("ready_after_reset", back, "board back after reset with test settings.toml")
            if not back:
                return

            try:
                _, out, _ = ctx.device(DEVICE_INIT, timeout=60)
            except Exception as e:  # noqa: BLE001 - FarmError timeouts, serial errors
                ctx.check("device_init", False, "%s: %s" % (type(e).__name__, e))
                return
            m = re.search(r"CPR_IP (\d+\.\d+\.\d+\.\d+)", out)
            if not m:
                ctx.skip("http_api", "device reported no IPv4 address (see wifi_connected)")
                return
            ip = m.group(1)
            ctx.log("device IP %s" % ip)

            _http_checks(board, ctx, ip)
            _mdns_check(ctx, ip)
    except farmlib.FarmError as e:
        ctx.check("farm_error", False, str(e))
    finally:
        # Files are restored by now; reset so the original settings.toml takes effect.
        ctx.check("ready_after_restore", _restart(board, ctx), "board back after restoring settings.toml")
