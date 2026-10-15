# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Broker fixtures, after Brent's WipperSnapper harness: broker_address, broker, protomq.

The broker is adafruit/protomq, the same local MQTT broker his tests run against, on his ports
(MQTT 1884, HTTP API 5173). It runs on bravo, so network tests no longer depend on the internet
or on io.adafruit.com, and a test can assert on what a board actually sent.

broker_address (session)  (host_ip, mqtt_port): where boards connect
broker (session)          starts ProtoMQ if nothing answers on the API port; stops it at the
                          end of the run only if this run started it
protomq (function)        ProtoMQ's HTTP API for one board's MQTT client id

One difference from his: he runs one board, so he calls POST /reset between tests. Here up to
eight boards share one broker in parallel, and /reset would disconnect the other boards. protomq
instead tracks and clears one client id per test and disconnects only that client afterwards.
"""
import fcntl
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

import pytest

import farmlib

PIDFILE = "/tmp/cpr-protomq.pid"
LOCK = "/tmp/cpr-protomq.lock"
LOG = "/tmp/cpr-protomq.log"


def _cfg(config):
    b = dict(config.cpr_cfg.get("broker", {}))
    b.setdefault("mqtt_port", 1884)
    b.setdefault("api_port", 5173)
    b.setdefault("dir", "~/protomq")
    b.setdefault("node", "node")
    return b


def api(port, path, body=None, timeout=5):
    """One call to ProtoMQ's HTTP API. Returns the decoded JSON."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/api%s" % (port, path), data=data,
                                 method="GET" if body is None else "POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _up(port):
    try:
        return api(port, "/clients", timeout=2).get("status") == "OK"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def pytest_configure(config):
    """Tag the run so the xdist workers (which inherit the environment) agree on who owns the
    broker. Only the controller, or a run without xdist, sets it."""
    if not hasattr(config, "workerinput"):
        os.environ.setdefault("CPR_RUN_ID", "%d-%d" % (os.getpid(), time.time()))


def pytest_unconfigure(config):
    """Stop ProtoMQ if this run started it (controller or non-xdist only)."""
    if hasattr(config, "workerinput") or config.getoption("broker_keep", False):
        return
    try:
        with open(PIDFILE) as f:
            pid, run_id = f.read().split()
    except (OSError, ValueError):
        return
    if run_id != os.environ.get("CPR_RUN_ID"):
        return
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except OSError:
        pass
    os.remove(PIDFILE)


@pytest.fixture(scope="session")
def broker_address(request):
    """(host_ip, mqtt_port) boards use to reach the broker. CPR_HOST_IP overrides the guess."""
    ip = request.config.cpr_cfg.get("host", {}).get("ip") or farmlib.host_ip()
    return ip, _cfg(request.config)["mqtt_port"]


@pytest.fixture(scope="session")
def broker(request, broker_address):
    """A running ProtoMQ. Returns its API port. Skips if it cannot be started."""
    cfg = _cfg(request.config)
    port = cfg["api_port"]
    with open(LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)  # xdist workers race to start it
        try:
            if not _up(port):
                pdir = os.path.expanduser(cfg["dir"])
                if not os.path.isfile(os.path.join(pdir, "main.js")):
                    pytest.skip("ProtoMQ not installed at %s (farm.toml [broker] dir)" % pdir)
                with open(LOG, "ab") as logf:
                    p = subprocess.Popen([os.path.expanduser(cfg["node"]), "main.js"], cwd=pdir,
                                         stdout=logf, stderr=subprocess.STDOUT,
                                         start_new_session=True)
                with open(PIDFILE, "w") as f:
                    f.write("%d %s\n" % (p.pid, os.environ.get("CPR_RUN_ID", "")))
                end = time.time() + 30
                while time.time() < end and not _up(port):
                    if p.poll() is not None:
                        pytest.fail("ProtoMQ exited at startup (rc %s); see %s" % (p.returncode, LOG),
                                    pytrace=False)
                    time.sleep(0.3)
                if not _up(port):
                    pytest.fail("ProtoMQ did not answer on :%d within 30 s; see %s" % (port, LOG),
                                pytrace=False)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
    return port


class ProtoMQ:
    """ProtoMQ's HTTP API, scoped to one MQTT client id."""

    def __init__(self, port, client):
        self.port = port
        self.client = client

    def call(self, path, body=None):
        return api(self.port, path, body)

    def clients(self):
        return self.call("/clients").get("clients", [])

    def connected(self):
        """This client's entry from /clients, or None."""
        return next((c for c in self.clients() if c.get("id") == self.client), None)

    def wait_subscribed(self, topic, timeout=30):
        """Block until this client is subscribed to topic. True if it happened in time."""
        end = time.time() + timeout
        while time.time() < end:
            c = self.connected()
            if c and topic in c.get("subscriptions", []):
                return True
            time.sleep(0.25)
        return False

    def track(self):
        """Start a fresh mailbox. Calling track on a tracked client deletes it, so call twice
        when the first answer says it already existed."""
        r = self.call("/track_deliveries", {"client": self.client})
        if r.get("status") != "OK":
            self.call("/track_deliveries", {"client": self.client})
        return r

    def deliveries(self, clear=False):
        """{'inbox': [...], 'outbox': [...]}; each payload decoded to bytes when it is raw."""
        r = self.call("/dump_deliveries", {"client": self.client, "clear": clear})
        if r.get("status") != "OK":
            return {"inbox": [], "outbox": []}
        out = {}
        for box in ("inbox", "outbox"):
            out[box] = [{"topic": m["topic"], "payload": _raw(m["payload"])}
                        for m in r["deliveries"].get(box, [])]
        return out

    def outbox(self, topic=None):
        return [m for m in self.deliveries()["outbox"] if topic is None or m["topic"] == topic]

    def echo(self, topic, payload):
        """Publish bytes (or str) to topic now, the way his tests push a B2D message."""
        if isinstance(payload, str):
            payload = payload.encode()
        return self.call("/echo", {"topic": topic, "payload": payload.decode("latin1")})

    def disconnect(self):
        return self.call("/disconnect", {"client": self.client})


def _raw(payload):
    """ProtoMQ returns non-protobuf payloads as a serialized Node Buffer."""
    if isinstance(payload, dict) and payload.get("type") == "Buffer":
        return bytes(payload.get("data", []))
    return payload


@pytest.fixture
def protomq(broker, board):
    """ProtoMQ for this board's MQTT client id, cpr-<board name>. Fresh mailbox per test."""
    pmq = ProtoMQ(broker, "cpr-" + board.name)
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})  # stop any old mailbox
    pmq.track()
    yield pmq
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})
    if pmq.connected():
        pmq.disconnect()
