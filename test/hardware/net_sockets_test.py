# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Network socket client/server, limits, timeouts.

pytest version of tests/net_sockets.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). The host runs TCP and UDP echo servers for the whole
module; phase 1 runs the device as their client, phase 2 runs the device as a TCP server that a
host thread pings 30 times. Each phase runs once per board in its own module-scoped fixture, so
a crash in phase 2 does not hide the phase 1 results.
Targets: #10606, #9219, #4057, #8988, #2097, #8443, #4127, #8907, #7086, #7431, #8248, #4085.
"""
import logging
import socket
import threading
import time

import pytest

import farmlib

log = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb", "wifi"),
    pytest.mark.targets(10606, 9219, 4057, 8988, 2097, 8443, 4127, 8907, 7086, 7431, 8248, 4085),
]

DEVICE_SERVER_PORT = 8765
CLIENTS = 30

# Device-side code, phase 1 (client). check/skip/metric come from lib/devtest.py.
# str.format template: literal braces must be doubled. Never call sys.exit() here: in the
# raw REPL a SystemExit makes CircuitPython leave the REPL and soft reboot.
DEVICE = r"""
import socketpool
import wifi
import time
import errno as errno_mod

ssid = {ssid!r}
password = {password!r}
host_ip = {host_ip!r}
tcp_echo_port = {tcp_echo_port}
udp_echo_port = {udp_echo_port}

pool = None
try:
    if not wifi.radio.connected:
        wifi.radio.connect(ssid, password)
    check("wifi_connect", wifi.radio.ipv4_address is not None, "ip=%s" % wifi.radio.ipv4_address)
    print("DEVICE_IP:%s" % wifi.radio.ipv4_address)
except Exception as e:
    check("wifi_connect", False, "%s: %s" % (type(e).__name__, e))

if wifi.radio.ipv4_address is not None:
    try:
        pool = socketpool.SocketPool(wifi.radio)
        check("socketpool_create", True, "created")
    except Exception as e:
        check("socketpool_create", False, "%s: %s" % (type(e).__name__, e))

if pool is not None:
    # === Test 1: TCP client echo, 64 x 1 KB with recv_into ===
    try:
        sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect((host_ip, tcp_echo_port))
            check("tcp_connect", True, "connected to %s:%d" % (host_ip, tcp_echo_port))
            chunk_size = 1024
            payload = bytes(range(256)) * 4
            buf = bytearray(chunk_size)
            total_sent = 0
            total_recv = 0
            t0 = time.monotonic()
            for i in range(64):
                sock.sendall(payload)
                total_sent += chunk_size
                got = 0
                while got < chunk_size:
                    n = sock.recv_into(buf, chunk_size - got)
                    if n <= 0:
                        break
                    got += n
                total_recv += got
                if got < chunk_size:
                    break
            elapsed = time.monotonic() - t0
            check("tcp_echo_roundtrip", total_sent == total_recv == 65536,
                  "sent=%d recv=%d" % (total_sent, total_recv))
            if total_recv == 65536 and elapsed > 0:
                metric("tcp_echo_kBps", total_recv / 1024.0 / elapsed, "kB/s", better="higher")
        finally:
            sock.close()
    except Exception as e:
        check("tcp_echo", False, "%s: %s" % (type(e).__name__, e))

    # === Test 2: how many TCP sockets can be open at once (bounded at 16) ===
    sockets = []
    try:
        for i in range(16):
            try:
                s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
            except Exception:
                break
            try:
                s.settimeout(5)
                s.connect((host_ip, tcp_echo_port))
            except Exception:
                s.close()
                break
            sockets.append(s)
        count = len(sockets)
        metric("max_tcp_sockets", count, "count", better="higher")
        check("tcp_sockets_floor", count >= 4, "count=%d (need >= 4)" % count, count)
    except Exception as e:
        check("socket_count", False, "%s: %s" % (type(e).__name__, e))
    finally:
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass
        sockets = []

    # After releasing them, a new socket must work again (no leak).
    try:
        s_test = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        try:
            s_test.settimeout(5)
            s_test.connect((host_ip, tcp_echo_port))
            check("tcp_socket_after_limit", True, "new socket works after limit test")
        finally:
            s_test.close()
    except Exception as e:
        check("tcp_socket_after_limit", False, "%s: %s" % (type(e).__name__, e))

    # === Test 3: settimeout(int) and recv timeout errno on a silent connection ===
    want = (getattr(errno_mod, "ETIMEDOUT", 116), 110, 116)
    try:
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        try:
            s.settimeout(10)
            s.connect((host_ip, tcp_echo_port))
            s.settimeout(1)
            buf = bytearray(16)
            t0 = time.monotonic()
            try:
                n = s.recv_into(buf)
                check("timeout_errno", False, "recv returned %d instead of timing out" % n)
            except OSError as e:
                dt = time.monotonic() - t0
                code = e.args[0] if e.args else None
                check("timeout_errno", code in want, "errno=%s (want %s)" % (code, want), code)
                check("settimeout_int", 0.5 <= dt <= 3.0, "timed out after %.2f s (set 1)" % dt, dt)
        finally:
            s.close()
    except Exception as e:
        check("settimeout", False, "%s: %s" % (type(e).__name__, e))

    # === Test 4: UDP sendto/recvfrom_into, port > 255 (#4127) ===
    try:
        s_udp = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)
        try:
            s_udp.settimeout(5)
            msg = b"hello from device"
            s_udp.sendto(msg, (host_ip, udp_echo_port))
            check("udp_sendto", True, "sent to %s:%d" % (host_ip, udp_echo_port))
            buf = bytearray(256)
            n, addr = s_udp.recvfrom_into(buf)
            check("udp_recvfrom", n == len(msg) and bytes(buf[:n]) == msg, "recv %d bytes from %s" % (n, addr), n)
        finally:
            s_udp.close()
    except Exception as e:
        check("udp", False, "%s: %s" % (type(e).__name__, e))
"""

# Phase 2 (server). Runs in a second raw-REPL exec; wifi stays up between execs because
# exec_raw does not soft reboot. The host client thread is already retrying connects.
DEVICE_SERVER = r"""
import socketpool
import wifi

device_server_port = {device_server_port}
clients = {clients}

if wifi.radio.ipv4_address is None:
    check("server_bind", False, "wifi not connected")
else:
    try:
        pool = socketpool.SocketPool(wifi.radio)
        server = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        try:
            try:
                server.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
            except Exception:
                pass
            server.bind(("", device_server_port))
            server.listen(5)
            server.settimeout(10)
            check("server_bind", True, "listening on port %d" % device_server_port)
            served = 0
            last_err = ""
            for i in range(clients):
                try:
                    client, addr = server.accept()
                except Exception as e:
                    last_err = "accept %d: %s: %s" % (i, type(e).__name__, e)
                    break
                try:
                    client.settimeout(5)
                    buf = bytearray(64)
                    n = client.recv_into(buf)
                    if n >= 4 and bytes(buf[:4]) == b"ping":
                        client.sendall(b"pong")
                        served += 1
                except Exception as e:
                    last_err = "client %d: %s: %s" % (i, type(e).__name__, e)
                finally:
                    client.close()
            metric("tcp_server_clients", served, "clients", better="higher")
            check("tcp_server", served == clients, "served %d/%d %s" % (served, clients, last_err), served)
        finally:
            server.close()
    except Exception as e:
        check("server_mode", False, "%s: %s" % (type(e).__name__, e))
"""


# ----- host side: echo servers and the phase 2 client, copied from tests/net_sockets.py -----

def _tcp_echo_client(client):
    try:
        client.settimeout(30)
        data = client.recv(4096)
        while data:
            client.sendall(data)
            data = client.recv(4096)
    except OSError:
        pass
    finally:
        client.close()


def _tcp_echo_server(tcp_echo_socket, stop):
    # One thread per connection: the socket-count probe holds many open at once.
    while not stop.is_set():
        try:
            client, _addr = tcp_echo_socket.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(target=_tcp_echo_client, args=(client,), daemon=True)
        t.start()


def _udp_echo_server(udp_echo_socket, stop):
    while not stop.is_set():
        try:
            data, addr = udp_echo_socket.recvfrom(1024)
            udp_echo_socket.sendto(data, addr)
        except socket.timeout:
            continue
        except OSError:
            break


def _device_server_client(device_ip, stop, pongs):
    # The device needs a few seconds to enter the raw REPL and listen: retry the
    # first connect, then run the clients back to back.
    deadline = time.time() + 30
    i = 0
    while i < CLIENTS and not stop.is_set():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(5)
            s.connect((device_ip, DEVICE_SERVER_PORT))
            s.sendall(b"ping")
            resp = s.recv(4)
            if resp == b"pong":
                pongs["ok"] += 1
            else:
                pongs["err"] = "client %d got %r" % (i, resp)
            i += 1
        except OSError as e:
            if i == 0 and time.time() < deadline:
                time.sleep(0.3)
                continue
            pongs["err"] = "client %d: %s" % (i, e)
            i += 1
        finally:
            s.close()
        time.sleep(0.05)


@pytest.fixture(scope="module")
def host(board, secret):
    """Host TCP and UDP echo servers for the whole module, stopped in finally.

    Yields a record: skip (why the network tests cannot run), error (host bind failure), and
    the ssid/password/ports phase 1 formats into DEVICE. Never print ssid or password.
    """
    rec = {"skip": None, "error": None, "stop": threading.Event(), "threads": []}
    ssid = secret("ssid_env")
    password = secret("password_env")
    if not ssid or not password:
        rec["skip"] = "WiFi credentials not set"
        yield rec
        return
    rec["ssid"], rec["password"] = ssid, password
    rec["host_ip"] = farmlib.host_ip()
    log.info("Host IP: %s", rec["host_ip"])
    tcp_echo_socket = None
    udp_echo_socket = None
    try:
        try:
            tcp_echo_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_echo_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp_echo_socket.bind((rec["host_ip"], 0))
            tcp_echo_socket.listen(32)
            tcp_echo_socket.settimeout(0.5)
            rec["tcp_echo_port"] = tcp_echo_socket.getsockname()[1]
            udp_echo_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_echo_socket.bind((rec["host_ip"], 0))
            udp_echo_socket.settimeout(0.5)
            rec["udp_echo_port"] = udp_echo_socket.getsockname()[1]
            log.info("TCP echo %s:%d, UDP echo %s:%d", rec["host_ip"], rec["tcp_echo_port"],
                     rec["host_ip"], rec["udp_echo_port"])
            for fn, sk in ((_tcp_echo_server, tcp_echo_socket), (_udp_echo_server, udp_echo_socket)):
                t = threading.Thread(target=fn, args=(sk, rec["stop"]), daemon=True)
                t.start()
                rec["threads"].append(t)
        except OSError as e:
            rec["error"] = "%s: %s" % (type(e).__name__, e)
        yield rec
    finally:
        rec["stop"].set()
        for sk in (tcp_echo_socket, udp_echo_socket):
            if sk is not None:
                try:
                    sk.close()
                except OSError:
                    pass
        for t in rec["threads"]:
            t.join(timeout=2)


@pytest.fixture(scope="module")
def client(host, device):
    """Phase 1: the device as client of the host echo servers. None if it could not start."""
    if host["skip"] or host["error"]:
        return None
    device_code = DEVICE.format(
        ssid=host["ssid"],
        password=host["password"],
        host_ip=host["host_ip"],
        tcp_echo_port=host["tcp_echo_port"],
        udp_echo_port=host["udp_echo_port"],
    )
    return device.run(device_code, timeout=180)


@pytest.fixture(scope="module")
def server(host, client, device):
    """Phase 2: the device serves CLIENTS ping/pong clients from a host thread.

    Returns {"results", "pongs", "no_ip"}; results is None when phase 2 never started.
    """
    rec = {"results": None, "pongs": {"ok": 0, "err": ""}, "no_ip": False}
    if client is None or client.crashed:
        return rec
    device_ip = None
    for line in client.out.splitlines():
        line = line.strip()
        if line.startswith("DEVICE_IP:"):
            device_ip = line.split(":", 1)[1].strip()
            break
    if not device_ip or device_ip == "None":
        rec["no_ip"] = True
        return rec
    log.info("Device IP: %s", device_ip)
    client_thread = threading.Thread(target=_device_server_client,
                                     args=(device_ip, host["stop"], rec["pongs"]), daemon=True)
    client_thread.start()
    host["threads"].append(client_thread)
    rec["results"] = device.run(DEVICE_SERVER.format(device_server_port=DEVICE_SERVER_PORT,
                                                     clients=CLIENTS), timeout=120)
    client_thread.join(timeout=10)
    return rec


def _need_host(host):
    if host["skip"]:
        pytest.skip(host["skip"])
    if host["error"]:
        pytest.skip("not reached: host servers failed (%s)" % host["error"])


def _p1(host, client):
    _need_host(host)
    return client


def _p2(host, server):
    _need_host(host)
    if server["no_ip"]:
        pytest.skip("device has no IP address")
    if server["results"] is None:
        pytest.skip("not reached: phase 1 did not finish")
    return server["results"]


# The device sections that gate later checks. A section that fails (wifi down, no pool, or an
# exception caught by its except clause) leaves the checks after it unreported; those skip as
# not reached, so the failure is charged once, by the gate's own test.
POOL = ("wifi_connect", "socketpool_create")
GATES = {
    "socketpool_create": ("wifi_connect",),
    "tcp_connect": POOL + ("tcp_echo",),
    "tcp_echo_roundtrip": POOL + ("tcp_echo",),
    "tcp_echo": POOL,
    "tcp_sockets_floor": POOL + ("socket_count",),
    "socket_count": POOL,
    "tcp_socket_after_limit": POOL,
    "timeout_errno": POOL + ("settimeout",),
    "settimeout_int": POOL + ("settimeout", "timeout_errno"),
    "settimeout": POOL,
    "udp_sendto": POOL + ("udp",),
    "udp_recvfrom": POOL + ("udp",),
    "udp": POOL,
    "tcp_server": ("server_bind", "server_mode"),
    "server_bind": ("server_mode",),
}


def _skip_if_gated(results, name):
    """Skip `name` as not reached when it is absent because a gating section failed."""
    if name in results.checks:
        return
    for gate in GATES.get(name, ()):
        rs = results.checks.get(gate, [])
        if any(r.get("ok") is False for r in rs):
            pytest.skip("not reached: %s failed (%s)" % (gate, str(rs[-1].get("detail", ""))[:160]))


def _expect(results, name):
    _skip_if_gated(results, name)
    results.expect(name)


def _expect_if_reported(results, name):
    """Checks the device only reports when an exception escapes: absent means passed."""
    _skip_if_gated(results, name)
    if name in results.checks:
        results.expect(name)
    elif results.crashed:
        pytest.skip("not reached: the device run failed (%s)" % results.crashed[:200])
    elif results.err:
        pytest.skip("not reached: the device code raised (%s)" % results.err[-200:])


def _device_metric(results, metric, name):
    if name not in results.metrics:
        if results.crashed or results.err:
            results.metric_record(name)  # skips as not reached
        pytest.skip("device sent no %s" % name)
    rec = results.metric_record(name)
    metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


# ----- tests, in the original's order -----

def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_network(host):
    """WiFi credentials are set (CPR_WIFI_SSID / CPR_WIFI_PASSWORD)."""
    if host["skip"]:
        pytest.skip(host["skip"])


def test_host_servers(host):
    """The host TCP and UDP echo servers bound and started."""
    if host["skip"]:
        pytest.skip(host["skip"])
    assert not host["error"], host["error"]


def test_device_client_phase(host, client):
    """Phase 1 ran on the device without the board stopping answering."""
    r = _p1(host, client)
    assert not r.crashed, r.crashed[-400:]


def test_device_client_exception(host, client):
    """Phase 1 device code raised nothing (run.py's device_exception)."""
    r = _p1(host, client)
    assert not r.err, r.err[-400:]


def test_wifi_connect(host, client):
    """The radio is connected with an IPv4 address."""
    _expect(_p1(host, client), "wifi_connect")


def test_socketpool_create(host, client):
    """socketpool.SocketPool(wifi.radio) works."""
    _expect(_p1(host, client), "socketpool_create")


def test_tcp_connect(host, client):
    """The device connects to the host TCP echo server."""
    _expect(_p1(host, client), "tcp_connect")


def test_tcp_echo_roundtrip(host, client):
    """64 x 1 KB sendall + recv_into comes back as 65536 bytes."""
    _expect(_p1(host, client), "tcp_echo_roundtrip")


def test_tcp_echo_kBps(host, client, metric):  # pylint: disable=invalid-name
    """TCP echo throughput, compared with this board's history."""
    _device_metric(_p1(host, client), metric, "tcp_echo_kBps")


def test_tcp_echo(host, client):
    """No exception escaped the TCP echo test."""
    _expect_if_reported(_p1(host, client), "tcp_echo")


def test_max_tcp_sockets(host, client, metric):
    """TCP sockets open at once (probe capped at 16), compared with history (#10606, #9219)."""
    _device_metric(_p1(host, client), metric, "max_tcp_sockets")


def test_tcp_sockets_floor(host, client):
    """At least 4 TCP sockets can be open at once."""
    _expect(_p1(host, client), "tcp_sockets_floor")


def test_socket_count(host, client):
    """No exception escaped the socket count probe."""
    _expect_if_reported(_p1(host, client), "socket_count")


def test_tcp_socket_after_limit(host, client):
    """A new socket connects after the probe sockets are closed (no leak)."""
    _expect(_p1(host, client), "tcp_socket_after_limit")


def test_timeout_errno(host, client):
    """recv_into on a silent socket raises OSError ETIMEDOUT (#8988)."""
    _expect(_p1(host, client), "timeout_errno")


def test_settimeout_int(host, client):
    """settimeout(1) fires in 0.5 to 3 s (#2097)."""
    _expect(_p1(host, client), "settimeout_int")


def test_settimeout(host, client):
    """No exception escaped the timeout test."""
    _expect_if_reported(_p1(host, client), "settimeout")


def test_udp_sendto(host, client):
    """UDP sendto an ephemeral port > 255 (#4127)."""
    _expect(_p1(host, client), "udp_sendto")


def test_udp_recvfrom(host, client):
    """The UDP echo comes back intact."""
    _expect(_p1(host, client), "udp_recvfrom")


def test_udp(host, client):
    """No exception escaped the UDP test."""
    _expect_if_reported(_p1(host, client), "udp")


def test_device_server_phase(host, server):
    """Phase 2 ran on the device without the board stopping answering."""
    r = _p2(host, server)
    assert not r.crashed, r.crashed[-400:]


def test_device_server_exception(host, server):
    """Phase 2 device code raised nothing (run.py's device_exception)."""
    r = _p2(host, server)
    assert not r.err, r.err[-400:]


def test_server_bind(host, server):
    """The device binds and listens on port 8765."""
    _expect(_p2(host, server), "server_bind")


def test_tcp_server_clients(host, server, metric):
    """Clients the device served, compared with history."""
    _device_metric(_p2(host, server), metric, "tcp_server_clients")


def test_tcp_server(host, server):
    """The device served all 30 ping/pong clients (#8443)."""
    _expect(_p2(host, server), "tcp_server")


def test_server_mode(host, server):
    """No exception escaped the server test."""
    _expect_if_reported(_p2(host, server), "server_mode")


def test_tcp_server_host_pongs(host, server):
    """The host saw 30 pongs."""
    _p2(host, server)
    pongs = server["pongs"]
    assert pongs["ok"] == CLIENTS, "host got %d/%d pongs %s" % (pongs["ok"], CLIENTS, pongs["err"])
