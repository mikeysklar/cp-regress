# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Network socket regression fixtures: TCP/UDP client, socket limits, timeouts, server mode."""

META = {
    "id": "net_sockets",
    "title": "Network socket client/server, limits, timeouts",
    "category": "Network",
    "tier": "nightly",
    "needs": ["native_usb", "wifi"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 300,
    "targets": [10606, 9219, 4057, 8988, 2097, 8443, 4127, 8907, 7086, 7431, 8248, 4085],
}

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


def run(board, ctx):
    import socket
    import threading
    import time
    import farmlib

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
    device_server_port = 8765
    clients = 30
    stop = threading.Event()
    threads = []
    tcp_echo_socket = None
    udp_echo_socket = None

    def tcp_echo_client(client):
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

    def tcp_echo_server():
        # One thread per connection: the socket-count probe holds many open at once.
        while not stop.is_set():
            try:
                client, _addr = tcp_echo_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=tcp_echo_client, args=(client,), daemon=True)
            t.start()

    def udp_echo_server():
        while not stop.is_set():
            try:
                data, addr = udp_echo_socket.recvfrom(1024)
                udp_echo_socket.sendto(data, addr)
            except socket.timeout:
                continue
            except OSError:
                break

    pongs = {"ok": 0, "err": ""}

    def device_server_client(device_ip):
        # The device needs a few seconds to enter the raw REPL and listen: retry the
        # first connect, then run the clients back to back.
        deadline = time.time() + 30
        i = 0
        while i < clients and not stop.is_set():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.settimeout(5)
                s.connect((device_ip, device_server_port))
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

    try:
        tcp_echo_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_echo_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_echo_socket.bind((host_ip, 0))
        tcp_echo_socket.listen(32)
        tcp_echo_socket.settimeout(0.5)
        tcp_echo_port = tcp_echo_socket.getsockname()[1]
        udp_echo_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_echo_socket.bind((host_ip, 0))
        udp_echo_socket.settimeout(0.5)
        udp_echo_port = udp_echo_socket.getsockname()[1]
        ctx.log("TCP echo %s:%d, UDP echo %s:%d" % (host_ip, tcp_echo_port, host_ip, udp_echo_port))
        for fn in (tcp_echo_server, udp_echo_server):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            threads.append(t)

        device_code = DEVICE.format(
            ssid=ssid,
            password=password,
            host_ip=host_ip,
            tcp_echo_port=tcp_echo_port,
            udp_echo_port=udp_echo_port,
        )
        try:
            _res, stdout, _err = ctx.device(device_code, timeout=180)
        except (farmlib.FarmError, OSError) as e:
            ctx.check("device_client_phase", False, str(e)[-400:])
            return

        device_ip = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("DEVICE_IP:"):
                device_ip = line.split(":", 1)[1].strip()
                break
        if not device_ip or device_ip == "None":
            ctx.skip("tcp_server", "device has no IP address")
            return
        ctx.log("Device IP: %s" % device_ip)

        client_thread = threading.Thread(target=device_server_client, args=(device_ip,), daemon=True)
        client_thread.start()
        threads.append(client_thread)
        try:
            ctx.device(DEVICE_SERVER.format(device_server_port=device_server_port, clients=clients),
                       timeout=120)
        except (farmlib.FarmError, OSError) as e:
            ctx.check("device_server_phase", False, str(e)[-400:])
        client_thread.join(timeout=10)
        ctx.check("tcp_server_host_pongs", pongs["ok"] == clients,
                  "host got %d/%d pongs %s" % (pongs["ok"], clients, pongs["err"]), pongs["ok"])
    except OSError as e:
        ctx.check("host_servers", False, "%s: %s" % (type(e).__name__, e))
    finally:
        stop.set()
        for sk in (tcp_echo_socket, udp_echo_socket):
            if sk is not None:
                try:
                    sk.close()
                except OSError:
                    pass
        for t in threads:
            t.join(timeout=2)
