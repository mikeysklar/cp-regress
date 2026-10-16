# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Fakes for the offline harness self-tests: no board, no tty, no broker, no network.

After Brent's tests_hil_board_utils/ (@ cf64e03), which split pty-only checks from the ones
that need his esp32 (test_serial_monitor_hw.py:1-3). Everything here is the pty-only half:

  pty_board     a farmlib.Board stand-in whose console is one end of an os.openpty() pair;
                the test writes "board output" into the other end, unplugs and replugs it
  fake_protomq  ProtoMQ's HTTP API (track/dump_deliveries, echo, clients, disconnect) on an
                http.server bound to 127.0.0.1, with the semantics of docs/api/README.md
                (bravo:~/protomq, adafruit/protomq 9e2e1bf)

conftest.py hands them out as fixtures; tests import wait_for from here.
"""
import json
import os
import threading
import time
import tty
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class PtyBoard:
    """Just enough of farmlib.Board for SerialMonitor: name, power, ttys(), wait_ready(),
    present(), devnum(). The console is the slave end of a pty; write() feeds the master end.

    unplug() closes the master, which the monitor sees as a lost port (pyserial raises
    SerialException, an OSError, as on a USB drop). replug() makes a new pty under a new name,
    as a re-enumerated board gets a new ttyACM; ttys() always answers with the current one.
    """

    def __init__(self, name="fake_pty"):
        self.name = name
        self.power = {"settle": 0}
        self.comes_back = True  # what wait_ready() says after unplug()
        self.fds = []
        self.master = self.path = None
        self.replug()

    def replug(self):
        master, slave = os.openpty()
        tty.setraw(master)
        tty.setraw(slave)  # a CDC tty is raw; a cooked pty echoes and rewrites line endings
        self.fds += [master, slave]
        self.master, self.path = master, os.ttyname(slave)
        return self.path

    def unplug(self):
        os.close(self.master)
        self.fds.remove(self.master)
        self.master = None

    def write(self, data):
        os.write(self.master, data.encode() if isinstance(data, str) else data)

    def ttys(self):
        return [self.path] if self.master is not None else []

    def present(self):
        return self.master is not None

    def devnum(self):
        return str(self.fds[0])

    def wait_ready(self, timeout=None, need_mount=True):  # pylint: disable=unused-argument
        """Seconds until the console is back, polling like Board.wait_ready; None if it isn't
        within timeout, or at once when comes_back is False."""
        t0 = time.monotonic()
        end = t0 + (timeout or 10)
        while self.comes_back and time.monotonic() < end:
            if self.master is not None:
                return time.monotonic() - t0
            time.sleep(0.01)
        return None

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []


def wait_for(cond, timeout=5.0, step=0.01):
    """Poll cond() until it is true; return its last value."""
    end = time.monotonic() + timeout
    while True:
        v = cond()
        if v or time.monotonic() >= end:
            return v
        time.sleep(step)


class FakeProtoMQ:
    """ProtoMQ's HTTP API as the catcher uses it, on 127.0.0.1 and an ephemeral port.

    Semantics from bravo:~/protomq/docs/api/README.md: track_deliveries on a tracked client
    DELETES its mailbox and answers ERROR; dump_deliveries clears unless "clear" is literally
    false; non-protobuf payloads come back as a serialized Node Buffer; /echo takes a latin1
    string. publish() is what a board's MQTT publish would add. Every request is logged in
    `calls` as (path, body).
    """

    def __init__(self):
        self.mailboxes = {}
        self.clients = {}  # client id -> set of exact subscriptions
        self.calls = []
        self.down = False  # answer every request with HTTP 500 (a broker restarting)
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # pylint: disable=arguments-differ
                pass

            def _reply(self, body):
                if fake.down:
                    self.send_error(500)
                    return
                data = json.dumps(fake.handle(self.path, body)).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # pylint: disable=invalid-name
                self._reply(None)

            def do_POST(self):  # pylint: disable=invalid-name
                n = int(self.headers.get("content-length", 0))
                self._reply(json.loads(self.rfile.read(n) or b"{}"))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handle(self, path, body):
        cid = (body or {}).get("client")
        with self.lock:
            self.calls.append((path, body))
            if path == "/api/clients":
                return {"status": "OK", "clients": [{"id": c, "subscriptions": sorted(s)}
                                                    for c, s in self.clients.items()]}
            if path == "/api/track_deliveries":
                if cid in self.mailboxes:
                    del self.mailboxes[cid]
                    return {"status": "ERROR", "message": "Mailbox already initialized for: "
                                                          "%s, clearing it now." % cid}
                self.mailboxes[cid] = {"inbox": [], "outbox": []}
                return {"status": "OK"}
            if path == "/api/dump_deliveries":
                box = self.mailboxes.get(cid)
                if box is None:
                    return {"status": "ERROR", "message": "No mailbox initialized for: %s." % cid}
                if body.get("clear", True) is not False:
                    del self.mailboxes[cid]
                return {"status": "OK", "deliveries": json.loads(json.dumps(box))}
            if path == "/api/echo":
                self._publish(None, body["topic"], body["payload"].encode("latin1"))
                return {"status": "OK"}
            if path == "/api/disconnect":
                if self.clients.pop(cid, None) is None:
                    return {"status": "ERROR", "message": "No client found with id: %s" % cid}
                return {"status": "OK"}
        return {"status": "ERROR", "message": "unknown " + path}

    def _publish(self, sender, topic, payload):
        pkt = {"topic": topic, "payload": {"type": "Buffer", "data": list(payload)}}
        if sender in self.mailboxes:
            self.mailboxes[sender]["outbox"].append(pkt)
        for c, subs in self.clients.items():
            if topic in subs and c in self.mailboxes:
                self.mailboxes[c]["inbox"].append(pkt)

    def publish(self, sender, topic, payload):
        """What the board's MQTT client `sender` publishing payload (bytes, str or a JSON-able
        object) on topic would record."""
        if not isinstance(payload, (bytes, str)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        with self.lock:
            self._publish(sender, topic, payload)

    def paths(self):
        with self.lock:
            return [p for p, _ in self.calls]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
