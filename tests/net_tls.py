# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Network TLS/SSL regression fixture: HTTPS client, certificate validation, handshake metrics."""

META = {
    "id": "net_tls",
    "title": "HTTPS TLS handshake, certificate validation, throughput",
    "category": "Network",
    "tier": "nightly",
    "needs": ["wifi"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 180,
    "targets": [8299, 8925, 8919, 7606, 4151, 10339, 3651, 7429, 9003, 8910],
}

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


def run(board, ctx):
    import os
    import shutil
    import socket
    import ssl
    import subprocess
    import tempfile
    import threading
    import farmlib  # run.py puts lib/ on sys.path; "from lib import" would give a second FarmError

    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    ssid = ctx.secret("ssid_env")
    password = ctx.secret("password_env")
    if not ssid or not password:
        ctx.skip("network", "WiFi credentials not set")
        return

    host_ip = farmlib.host_ip()
    ctx.log("Host IP: %s" % host_ip)
    if host_ip.startswith("127."):
        ctx.check("host_ip", False, "no LAN address for the board to reach (set CPR_HOST_IP)")
        return

    # Throwaway PKI: a good CA + server cert, and an unrelated "bad" CA + server cert.
    # The CN is the host IP; mbedtls falls back to CN when there is no SAN. CircuitPython
    # builds mbedtls without MBEDTLS_HAVE_TIME_DATE, so validity dates are not checked.
    tmpdir = tempfile.mkdtemp(prefix="cp_tls_")
    try:
        try:
            r = subprocess.run(["openssl", "version"], capture_output=True, text=True, timeout=10)
            ctx.log("openssl: %s" % r.stdout.strip())
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
            ctx.skip("tls_setup", "host cert setup failed: %s %s" % (e, err[-200:]))
            return
        ctx.check("tls_setup", True, "certs generated")

        blob_size = 64 * 1024 if "small" not in board.caps else 8 * 1024
        blob_data = bytes(range(256)) * (blob_size // 256)
        stop = threading.Event()
        seen = {"bad_handshake_failed": 0, "good_requests": 0}

        def respond(client, req):
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

        listeners = {}
        threads = []
        try:
            for tag, tls in (("http", None), ("https", "good"), ("bad_https", "bad")):
                ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listeners[tag] = ls
                try:
                    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    ls.bind((host_ip, 0))
                    ls.listen(5)
                    ls.settimeout(0.5)
                except OSError as e:
                    ctx.check("host_servers", False, "%s on %s: %s" % (tag, host_ip, e))
                    return
                t = threading.Thread(target=serve, args=(ls, tls), daemon=True)
                t.start()
                threads.append(t)
            ports = {k: v.getsockname()[1] for k, v in listeners.items()}
            ctx.log("servers on %s: %s" % (host_ip, ports))

            public_hosts = ctx.cfg.get("network", {}).get("public_tls_hosts", [])
            device_code = DEVICE.format(
                ssid=ssid,
                password=password,
                host_ip=host_ip,
                https_port=ports["https"],
                http_port=ports["http"],
                bad_https_port=ports["bad_https"],
                blob_size=blob_size,
                ca_pem=ca_pem,
                public_hosts=list(public_hosts),
            )
            try:
                res, out, err = ctx.device(device_code, timeout=150)
            except (farmlib.FarmError, OSError) as e:  # OSError covers serial.SerialException
                ctx.check("device_run", False, "%s: %s" % (type(e).__name__, e))
                return
            names = set(r.get("name") for r in res)
            wifi_ok = any(r.get("name") == "wifi_connect" and r.get("ok") for r in res)
            for want in ("wifi_connect",) + (("tls_wrap_socket", "https_get_blob", "rejects_untrusted")
                                             if wifi_ok else ()):
                if want not in names:
                    ctx.check(want, False, "device did not report it; stdout tail %r" % out[-200:])
            if any(r.get("name") == "rejects_untrusted" and r.get("ok") for r in res):
                ctx.check("bad_server_handshake_aborted", seen["bad_handshake_failed"] > 0,
                          "host saw %d failed handshakes" % seen["bad_handshake_failed"])
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)
            for ls in listeners.values():
                try:
                    ls.close()
                except OSError:
                    pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
