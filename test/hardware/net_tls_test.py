# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""HTTPS TLS handshake, certificate validation, throughput.

pytest version of tests/net_tls.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). One module-scoped fixture per board generates a throwaway
PKI with openssl, starts a plain HTTP server and two HTTPS servers (good CA, untrusted CA) on the
host, runs the device code once against them, then stops the servers and removes the certs.
Each check is then one test, in the original's order.
Targets: #8299, #8925, #8919, #7606, #4151, #10339, #3651, #7429, #9003, #8910.
"""
import logging

import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb", "wifi"),
    pytest.mark.targets(8299, 8925, 8919, 7606, 4151, 10339, 3651, 7429, 9003, 8910),
]

log = logging.getLogger(__name__)

# Device-side code (str.format template: literal braces must be doubled; there are none).
# Plain HTTP, HTTPS against a host CA (handshake time + 64 KB blob integrity/throughput),
# rejection of a cert from an untrusted CA, then optional public hosts with the built-in bundle.
# CircuitPython's SSLSocket does the handshake in connect(), so wrap first, then connect.
DEVICE = r'''
import socketpool
import ssl
import wifi
import time

ssid = {ssid!r}
password = {password!r}
host_ip = {host_ip!r}
https_port = {https_port}
http_port = {http_port}
bad_https_port = {bad_https_port}
blob_size = {blob_size}
ca_pem = {ca_pem!r}
public_hosts = {public_hosts!r}
VERIFY_FAILED = -9984  # MBEDTLS_ERR_X509_CERT_VERIFY_FAILED (-0x2700)


def _close(s):
    if s is not None:
        try:
            s.close()
        except Exception:
            pass


def _read_http(s, limit):
    # One HTTP/1.x response. Stops at Content-Length, so a peer that closes without
    # close_notify does not turn into an error. Returns (header, body, error string).
    data = bytearray()
    buf = bytearray(2048)
    hdr_end = -1
    clen = -1
    err = ""
    while True:
        if hdr_end < 0:
            hdr_end = bytes(data).find(b"\r\n\r\n")
            if hdr_end >= 0:
                for line in bytes(data[:hdr_end]).split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        clen = int(line.split(b":")[1].strip())
        if hdr_end >= 0 and clen >= 0 and len(data) - hdr_end - 4 >= clen:
            break
        if len(data) > limit:
            err = "more than %d bytes" % limit
            break
        try:
            n = s.recv_into(buf)
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            break
        if n == 0:
            break
        data.extend(buf[:n])
    if hdr_end < 0:
        return bytes(data[:120]), bytearray(), err or "no end of headers"
    return bytes(data[:hdr_end]), data[hdr_end + 4:], err


def _net_tls():
    if not wifi.radio.connected:
        try:
            wifi.radio.connect(ssid, password)
        except Exception as e:
            check("wifi_connect", False, "%s: %s" % (type(e).__name__, e))
            return
    check("wifi_connect", wifi.radio.connected, str(wifi.radio.ipv4_address))
    if not wifi.radio.connected:
        return

    try:
        pool = socketpool.SocketPool(wifi.radio)
    except Exception as e:
        check("socketpool_create", False, "%s: %s" % (type(e).__name__, e))
        return
    check("socketpool_create", True, "")

    # 1: plain HTTP still works (#4151)
    sock = None
    try:
        sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((host_ip, http_port))
        check("http_connect", True, "%s:%d" % (host_ip, http_port))
        sock.sendall(b"GET /ok HTTP/1.0\r\nHost: " + host_ip.encode() + b"\r\n\r\n")
        hdr, body, err = _read_http(sock, 4096)
        check("http_get_ok", hdr.startswith(b"HTTP/1.") and b" 200 " in hdr and bytes(body) == b"ok",
              "%r body=%r %s" % (hdr[:40], bytes(body[:20]), err))
    except Exception as e:
        check("http_get_ok", False, "%s: %s" % (type(e).__name__, e))
    _close(sock)

    # 2: HTTPS with the host CA: handshake time, blob length + content, throughput
    tls_ok = False
    ws = None
    try:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=ca_pem)
        check("ssl_context_created", True, "cadata loaded")
        sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        sock.settimeout(15)
        ws = ctx.wrap_socket(sock, server_hostname=host_ip)
        t0 = time.monotonic_ns()
        ws.connect((host_ip, https_port))
        metric("tls_handshake_ms", (time.monotonic_ns() - t0) / 1e6, "ms", better="lower")
        tls_ok = True
        check("tls_wrap_socket", True, "handshake complete")
    except Exception as e:
        check("tls_wrap_socket", False, "%s: %s" % (type(e).__name__, e))
    if tls_ok:
        try:
            t0 = time.monotonic_ns()
            ws.send(b"GET /blob HTTP/1.0\r\nHost: " + host_ip.encode() + b"\r\n\r\n")
            hdr, body, err = _read_http(ws, blob_size + 4096)
            dt = (time.monotonic_ns() - t0) / 1e9
            n = len(body)
            bad = -1
            for i in range(0, n, 97):
                if body[i] != (i & 255):
                    bad = i
                    break
            if n and body[n - 1] != ((n - 1) & 255):
                bad = n - 1
            check("https_get_blob", n == blob_size and bad < 0,
                  "got %d of %d bytes, first bad offset %d %s" % (n, blob_size, bad, err))
            if n == blob_size and dt > 0:
                metric("https_kBps", n / 1024.0 / dt, "kB/s", better="higher")
        except Exception as e:
            check("https_get_blob", False, "%s: %s" % (type(e).__name__, e))
    else:
        skip("https_get_blob", "no TLS session")
    _close(ws)

    # 3: a cert from an untrusted CA must be refused (#10339)
    if not tls_ok:
        skip("rejects_untrusted", "good-cert handshake failed, rejection would mean nothing")
    else:
        ws = None
        try:
            ctx_bad = ssl.create_default_context()
            ctx_bad.load_verify_locations(cadata=ca_pem)
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
            sock.settimeout(15)
            ws = ctx_bad.wrap_socket(sock, server_hostname=host_ip)
            try:
                ws.connect((host_ip, bad_https_port))
                check("rejects_untrusted", False, "untrusted cert was accepted")
            except OSError as e:
                code = e.args[0] if e.args else None
                msg = str(e).lower()
                check("rejects_untrusted", code == VERIFY_FAILED or "verif" in msg or "trusted" in msg,
                      "rejected with: %s" % str(e)[:160])
        except Exception as e:
            check("rejects_untrusted", False, "%s: %s" % (type(e).__name__, e))
        _close(ws)

    # 4: optional public hosts with the built-in CA bundle; failures are skips
    for url in public_hosts:
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        name = "external_" + host.replace(".", "_").replace("-", "_")
        ws = None
        try:
            addr = pool.getaddrinfo(host, 443)[0][4]
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
            sock.settimeout(10)
            ws = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            ws.connect(addr)
            ws.send(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            buf = bytearray(64)
            n = ws.recv_into(buf)
            ok = bytes(buf[:n]).startswith(b"HTTP/1.")
            check(name, ok, repr(bytes(buf[:24])))
        except Exception as e:
            skip(name, "external service: %s: %s" % (type(e).__name__, str(e)[:100]))
        _close(ws)


_net_tls()
'''


def _openssl(*args):
    import subprocess
    subprocess.run(("openssl",) + args, capture_output=True, check=True, timeout=60)


def _external_name(url):
    """The check name the device code builds for one public_tls_hosts entry."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return "external_" + host.replace(".", "_").replace("-", "_")


def pytest_generate_tests(metafunc):
    """One external_<host> test per farm.toml [network].public_tls_hosts entry (no hardware)."""
    if "external_name" in metafunc.fixturenames:
        hosts = metafunc.config.cpr_cfg.get("network", {}).get("public_tls_hosts", [])
        names = [_external_name(u) for u in hosts]
        metafunc.parametrize("external_name", names, ids=names)


def _start_host(board, secret, rec, cleanup):
    """The host half of run() in tests/net_tls.py, up to the device run, recorded instead of
    reported. Appends what has to be undone to cleanup (run in reverse). Sets rec["ready"]
    once the servers are up and everything the device code needs is in rec."""
    # pylint: disable=too-many-locals,too-many-statements,import-outside-toplevel
    import os
    import shutil
    import socket
    import ssl
    import subprocess
    import tempfile
    import threading
    import farmlib

    steps = rec["steps"]
    ssid = secret("ssid_env")
    password = secret("password_env")
    if not ssid or not password:
        steps["network"] = (None, "WiFi credentials not set")
        rec["stopped"] = "WiFi credentials not set"
        return

    rec["current"] = "host_ip"
    host_ip = farmlib.host_ip()
    log.info("Host IP: %s", host_ip)
    if host_ip.startswith("127."):
        steps["host_ip"] = (False, "no LAN address for the board to reach (set CPR_HOST_IP)")
        rec["stopped"] = "host has no LAN address"
        return

    # Throwaway PKI: a good CA + server cert, and an unrelated "bad" CA + server cert.
    # The CN is the host IP; mbedtls falls back to CN when there is no SAN. CircuitPython
    # builds mbedtls without MBEDTLS_HAVE_TIME_DATE, so validity dates are not checked.
    rec["current"] = "tls_setup"
    tmpdir = tempfile.mkdtemp(prefix="cp_tls_")
    cleanup.append(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    try:
        r = subprocess.run(["openssl", "version"], capture_output=True, text=True, timeout=10)
        log.info("openssl: %s", r.stdout.strip())
        cnf = os.path.join(tmpdir, "ca.cnf")
        with open(cnf, "w") as f:
            # explicit CA extensions: mbedtls refuses a v3 trust anchor without CA:TRUE
            f.write("[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\n[dn]\n"
                    "[v3_ca]\nbasicConstraints=critical,CA:TRUE\n"
                    "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
        paths = {}
        for tag in ("good", "bad"):
            ca_key = os.path.join(tmpdir, tag + "_ca_key.pem")
            ca_cert = os.path.join(tmpdir, tag + "_ca_cert.pem")
            key = os.path.join(tmpdir, tag + "_server_key.pem")
            csr = os.path.join(tmpdir, tag + "_server.csr")
            cert = os.path.join(tmpdir, tag + "_server_cert.pem")
            _openssl("genrsa", "-out", ca_key, "2048")
            _openssl("req", "-new", "-x509", "-days", "2", "-key", ca_key, "-out", ca_cert,
                     "-config", cnf, "-subj", "/CN=CPR-Test-%s-CA" % tag)
            _openssl("genrsa", "-out", key, "2048")
            _openssl("req", "-new", "-key", key, "-out", csr, "-config", cnf,
                     "-subj", "/CN=%s" % host_ip)
            _openssl("x509", "-req", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key,
                     "-CAcreateserial", "-out", cert, "-days", "2", "-sha256")
            paths[tag] = (ca_cert, cert, key)
        with open(paths["good"][0]) as f:
            ca_pem = f.read()
        srv_ctx = {}
        for tag in ("good", "bad"):
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            c.load_cert_chain(paths[tag][1], paths[tag][2])
            srv_ctx[tag] = c
    except (OSError, subprocess.SubprocessError, ssl.SSLError) as e:
        err = getattr(e, "stderr", b"") or b""
        steps["tls_setup"] = (None, "host cert setup failed: %s %s" % (e, err[-200:]))
        rec["stopped"] = "host cert setup failed"
        return
    steps["tls_setup"] = (True, "certs generated")

    blob_size = 64 * 1024 if "small" not in board.caps else 8 * 1024
    blob_data = bytes(range(256)) * (blob_size // 256)
    stop = threading.Event()
    seen = {"bad_handshake_failed": 0, "good_requests": 0}
    rec["seen"] = seen

    def respond(client, req):  # pylint: disable=unused-argument
        if b"GET /blob" in req:
            seen["good_requests"] += 1
            return (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                    b"Content-Type: application/octet-stream\r\n\r\n" % len(blob_data)) + blob_data
        if b"GET /ok" in req:
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

    def serve(listener, tls):
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                client.settimeout(15)
                if tls is not None:
                    try:
                        client = srv_ctx[tls].wrap_socket(client, server_side=True)
                    except (ssl.SSLError, OSError):
                        if tls == "bad":
                            seen["bad_handshake_failed"] += 1
                        continue
                req = client.recv(4096)
                client.sendall(respond(client, req))
            except OSError:
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    rec["current"] = "host_servers"
    listeners = {}
    threads = []

    def stop_servers():
        stop.set()
        for t in threads:
            t.join(timeout=5)
        for ls in listeners.values():
            try:
                ls.close()
            except OSError:
                pass

    cleanup.append(stop_servers)
    for tag, tls in (("http", None), ("https", "good"), ("bad_https", "bad")):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listeners[tag] = ls
        try:
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((host_ip, 0))
            ls.listen(5)
            ls.settimeout(0.5)
        except OSError as e:
            steps["host_servers"] = (False, "%s on %s: %s" % (tag, host_ip, e))
            rec["stopped"] = "host servers did not start"
            return
        t = threading.Thread(target=serve, args=(ls, tls), daemon=True)
        t.start()
        threads.append(t)
    ports = {k: v.getsockname()[1] for k, v in listeners.items()}
    log.info("servers on %s: %s", host_ip, ports)
    rec["dev"] = {"host_ip": host_ip, "ports": ports, "blob_size": blob_size, "ca_pem": ca_pem}
    rec["ready"] = True


def _run_cleanup(cleanup):
    while cleanup:
        cleanup.pop()()


@pytest.fixture(scope="module")
def host(board, secret):
    """Host PKI + the three servers for one board, kept up until the module ends.

    rec["steps"]: host-side check name -> (ok, detail), ok None for a skip.
    rec["stopped"]: why the flow ended before the device ran, if it did.
    An unexpected host exception is kept in the record against the step that was running.
    """
    rec = {"steps": {}, "stopped": None, "results": None, "seen": None, "ready": False,
           "current": "network"}
    cleanup = []
    try:
        try:
            _start_host(board, secret, rec, cleanup)
        except Exception as e:  # pylint: disable=broad-except
            # keep it in the record so it fails the step that was running, not every test
            rec["steps"][rec["current"]] = (False, "host error: %s: %s" % (type(e).__name__, e))
            rec["stopped"] = "host error during %s" % rec["current"]
            rec["ready"] = False
        if not rec["ready"]:
            _run_cleanup(cleanup)  # the original cleans up as soon as it returns early
        yield rec
    finally:
        _run_cleanup(cleanup)


@pytest.fixture(scope="module")
def tls(host, device, secret, farm_config):
    """The one device run against the host servers. First requested by test_device_run, so a
    run that loses the board is charged to that test (later ones skip as not reached)."""
    rec = host
    if not rec["ready"]:
        return rec
    rec["current"] = "device_run"
    try:
        dev = rec["dev"]
        public_hosts = farm_config.get("network", {}).get("public_tls_hosts", [])
        device_code = DEVICE.format(
            ssid=secret("ssid_env"),
            password=secret("password_env"),
            host_ip=dev["host_ip"],
            https_port=dev["ports"]["https"],
            http_port=dev["ports"]["http"],
            bad_https_port=dev["ports"]["bad_https"],
            blob_size=dev["blob_size"],
            ca_pem=dev["ca_pem"],
            public_hosts=list(public_hosts),
        )
        res = device.run(device_code, timeout=150)
        rec["results"] = res
        if res.crashed:
            rec["steps"]["device_run"] = (False, res.crashed)
        else:
            rec["steps"]["device_run"] = (True, "")
    except Exception as e:  # pylint: disable=broad-except
        rec["steps"]["device_run"] = (False, "host error: %s: %s" % (type(e).__name__, e))
        rec["stopped"] = "host error during device_run"
    return rec


def _step(tls, name):
    """Assert one host-side step; skip it if the flow ended before reaching it."""
    if name not in tls["steps"]:
        pytest.skip("not reached: %s" % (tls["stopped"] or "no record"))
    ok, detail = tls["steps"][name]
    if ok is None:
        pytest.skip(detail)
    assert ok, detail


def _results(tls):
    """The device results, or a skip if the device code never ran."""
    res = tls["results"]
    if res is None:
        pytest.skip("not reached: %s" % (tls["stopped"] or "device did not run"))
    return res


def _wifi_ok(res):
    return any(r.get("ok") for r in res.checks.get("wifi_connect", []))


def _expect(tls, name, required=False):
    """results.expect(name). A missing name fails if run() in the original would have
    reported it as missing (wifi_connect always, the TLS checks once WiFi connected), else
    skips, because the original only reports what the device sent."""
    res = _results(tls)
    if name not in res.checks and not res.crashed and not res.err:
        if required:
            pytest.fail("%s: device did not report it; stdout tail %r" % (name, res.out[-200:]),
                        pytrace=False)
        pytest.skip("not reported by the device (an earlier step stopped it)")
    return res.expect(name)


def _device_metric(tls, metric, name):
    res = _results(tls)
    if name not in res.metrics:
        pytest.skip("device sent no %s (see the matching check)" % name)
    rec = res.metric_record(name)
    metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_network(host):
    """WiFi credentials are set (CPR_WIFI_SSID / CPR_WIFI_PASSWORD); skips without them."""
    if "network" in host["steps"]:
        _step(host, "network")


def test_host_ip(host):
    """The host has a LAN address the board can reach."""
    if "host_ip" not in host["steps"] and "tls_setup" in host["steps"]:
        return  # the original reports host_ip only when it fails
    _step(host, "host_ip")


def test_tls_setup(host):
    """openssl generated a good CA + server cert and an unrelated bad CA + server cert."""
    _step(host, "tls_setup")


def test_host_servers(host):
    """The plain HTTP and two HTTPS servers bound on the host address."""
    if "host_servers" not in host["steps"] and host["ready"]:
        return  # the original reports host_servers only when it fails
    _step(host, "host_servers")


def test_device_run(tls):
    """The raw REPL run itself worked (no serial error or timeout)."""
    _step(tls, "device_run")


def test_device_exception(tls):
    """The device code did not raise (run.py's ctx.device reports this as device_exception)."""
    res = _results(tls)
    if res.crashed:
        pytest.skip("not reached: the device run failed")
    assert not res.err, res.err[-400:]


def test_wifi_connect(tls):
    """The radio is connected (with the env credentials if it was not already)."""
    _expect(tls, "wifi_connect", required=True)


def test_socketpool_create(tls):
    """socketpool.SocketPool(wifi.radio) works."""
    _expect(tls, "socketpool_create")


def test_http_connect(tls):
    """Plain TCP connect to the host HTTP server (#4151)."""
    _expect(tls, "http_connect")


def test_http_get_ok(tls):
    """Plain HTTP GET /ok returns 200 with body ok (#4151)."""
    _expect(tls, "http_get_ok")


def test_ssl_context_created(tls):
    """ssl.create_default_context() + load_verify_locations(cadata=...)."""
    _expect(tls, "ssl_context_created")


def test_tls_handshake_ms(tls, metric):
    """TCP connect + TLS handshake time, compared with this board's history."""
    _device_metric(tls, metric, "tls_handshake_ms")


def test_tls_wrap_socket(tls):
    """wrap_socket(sock, server_hostname=host_ip) then connect() completes the handshake."""
    _expect(tls, "tls_wrap_socket", required=_wifi_ok(_results(tls)))


def test_https_get_blob(tls):
    """GET /blob over TLS returns the whole blob with the expected byte pattern (#3651)."""
    _expect(tls, "https_get_blob", required=_wifi_ok(_results(tls)))


def test_https_kBps(tls, metric):  # pylint: disable=invalid-name
    """HTTPS download rate, compared with this board's history."""
    _device_metric(tls, metric, "https_kBps")


def test_rejects_untrusted(tls):
    """A cert from an untrusted CA is refused with an X509 verify error (#10339)."""
    _expect(tls, "rejects_untrusted", required=_wifi_ok(_results(tls)))


def test_external(tls, external_name):
    """HTTPS GET to a public host with the built-in CA bundle; errors are skips."""
    _expect(tls, external_name)


def test_bad_server_handshake_aborted(tls):
    """The bad server saw its handshake fail, so the rejection came from TLS."""
    res = _results(tls)
    if not any(r.get("ok") for r in res.checks.get("rejects_untrusted", [])):
        pytest.skip("rejects_untrusted did not pass")
    seen = tls["seen"]
    assert seen["bad_handshake_failed"] > 0, \
        "host saw %d failed handshakes" % seen["bad_handshake_failed"]
