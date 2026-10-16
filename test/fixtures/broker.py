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
catcher (function)        lib/catcher.Catcher for this board: what it published on cpr/<board>/,
                          expect() on it, written to results/catcher/<board>.jsonl at teardown

One difference from his: he runs one board, so he calls POST /reset between tests. Here up to
eight boards share one broker in parallel, and /reset would disconnect the other boards. protomq
and catcher instead track and clear one client id per test and disconnect only that client.
POST /reset runs once, when the first worker of a run reaches the broker, and only when this run
started ProtoMQ or GET /clients shows nobody connected (protomq a904b5c made /reset also close
every client). Never mid-run; --broker-no-reset turns it off.

A failing test that used the broker gets a "protomq /clients" section in its report.
"""
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time

import pytest

import catcher as catcherlib
import farmlib
from catcher import api  # one copy of the HTTP call; still importable as fixtures.broker.api

PIDFILE = "/tmp/cpr-protomq.pid"
LOCK = "/tmp/cpr-protomq.lock"
LOG = "/tmp/cpr-protomq.log"
HYGIENE = "/tmp/cpr-protomq.hygiene.json"  # what the session-start reset did, per run id
HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "tests", "lib", "cpr_catcher.py")


def _cfg(config):
    b = dict(config.cpr_cfg.get("broker", {}))
    b.setdefault("mqtt_port", 1884)
    b.setdefault("api_port", 5173)
    b.setdefault("dir", "~/protomq")
    b.setdefault("node", "node")
    return b


def _up(port):
    try:
        return api(port, "/clients", timeout=2).get("status") == "OK"
    except catcherlib.API_ERRORS:
        return False


def pytest_addoption(parser):
    g = parser.getgroup("cp-regress")
    g.addoption("--broker-no-reset", action="store_true",
                help="never POST /reset to ProtoMQ, not even once at session start")


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
    if request.config.getoption("cpr_dry_run"):
        # session scope sets this up before `board` can skip; a dry run neither starts nor resets
        pytest.skip("dry run: would use ProtoMQ on :%d" % port)
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
            request.config.cpr_broker_hygiene = _session_hygiene(request.config, port)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
    return port


def _started_by_this_run():
    try:
        with open(PIDFILE) as f:
            return f.read().split()[1] == os.environ.get("CPR_RUN_ID")
    except (OSError, ValueError, IndexError):
        return False


def _session_hygiene(config, port):
    """POST /reset once per run, before any test of this run has tracked a client, to clear
    autoresponders, an active script and mailboxes left behind by earlier runs (protomq#10,
    #11). Only when this run started ProtoMQ or nobody is connected: since a904b5c /reset also
    closes every client, and a connected client may be another run's board. Called with the
    broker lock held; the first worker of the run to get here does it, the rest read its record.
    """
    run_id = os.environ.get("CPR_RUN_ID", "")
    try:
        with open(HYGIENE) as f:
            done = json.load(f)
        if done.get("run_id") == run_id:
            return done
    except (OSError, ValueError):
        pass
    rec = {"run_id": run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "started_by_this_run": _started_by_this_run()}
    try:
        rec["clients"] = api(port, "/clients").get("clients", [])
    except catcherlib.API_ERRORS as e:
        rec["clients"] = "GET /clients failed: %s" % e
    if config.getoption("broker_no_reset"):
        rec["action"] = "skipped: --broker-no-reset"
    elif not isinstance(rec["clients"], list):
        rec["action"] = "skipped: could not list clients"
    elif rec["started_by_this_run"] or not rec["clients"]:
        try:
            rec["reset"] = api(port, "/reset", {})
            rec["action"] = "reset"
        except catcherlib.API_ERRORS as e:
            rec["action"] = "reset failed: %s" % e
    else:
        rec["action"] = "skipped: %d client(s) connected: %s" % (
            len(rec["clients"]), ",".join(str(c.get("id")) for c in rec["clients"]))
    with open(HYGIENE, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    return rec


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
    other = catcherlib.live(pmq.client)
    if other is not None:  # its track would delete the catcher's mailbox
        pytest.fail("%s is being caught by %s; use catcher or protomq, not both"
                    % (pmq.client, other.nodeid), pytrace=False)
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})  # stop any old mailbox
    pmq.track()
    yield pmq
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})
    if pmq.connected():
        pmq.disconnect()


# ---------------------------------------------------------------- catcher

def catcher_helper():
    """The device-side helper's source, for a module's FS = {"lib/cpr_catcher.py": ...}."""
    with open(HELPER) as f:
        return f.read()


@contextlib.contextmanager
def catching(request, broker_port, board, script=None):
    """A started Catcher for board, stopped and appended to results/catcher/<board>.jsonl on the
    way out. For module fixtures that run the device once; single tests use `catcher`.

    The board's MQTT connection is closed afterwards (that client only, never /reset). When the
    test failed, the "end" record also carries GET /clients as it was at teardown.
    """
    cat = catcherlib.Catcher(broker_port, board.name, nodeid=request.node.nodeid)
    try:
        cat.start()
    except catcherlib.CatcherError as e:
        pytest.fail(str(e), pytrace=False)
    if script:
        cat.play(script)
    try:
        yield cat
    finally:
        cat.stop()
        cat.disconnect()
        extra = {}
        reps = getattr(request.node, "cpr_reports", {})
        if any(r.failed for r in reps.values()):
            extra["failed"] = sorted(w for w, r in reps.items() if r.failed)
            extra["clients"] = _clients_or_error(broker_port)
        cat.dump(**extra)


@pytest.fixture
def catcher(request, board, broker):
    """lib/catcher.Catcher tracking cpr-<board>: expect(suffix, predicate, timeout), records(),
    play(script), echo(suffix, payload). Appended to results/catcher/<board>.jsonl at teardown.

    Device code gets what it needs from catcher.device_config() (client_id, board, run_id,
    nodeid) and publishes with tests/lib/cpr_catcher.py. Not together with protomq in one test:
    both track the same client id.
    """
    with catching(request, broker, board) as cat:
        yield cat


def _clients_or_error(port):
    try:
        return api(port, "/clients", timeout=2).get("clients", [])
    except catcherlib.API_ERRORS as e:
        return "GET /clients failed: %s" % e


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):  # pylint: disable=unused-argument
    """Keep each phase's report on the item (the catcher's "end" record reads it), and add
    ProtoMQ's GET /clients, the session-start reset record and the catcher's last records to the
    report of a failing test that used the broker."""
    outcome = yield
    rep = outcome.get_result()
    if not hasattr(item, "cpr_reports"):
        item.cpr_reports = {}
    item.cpr_reports[rep.when] = rep
    if not rep.failed or "broker" not in getattr(item, "fixturenames", ()):
        return
    port = _cfg(item.config)["api_port"]
    rep.sections.append(("protomq /clients",
                         json.dumps(_clients_or_error(port), indent=1, sort_keys=True)))
    hyg = getattr(item.config, "cpr_broker_hygiene", None)
    if hyg is not None:
        rep.sections.append(("protomq session-start reset", json.dumps(hyg, sort_keys=True)))
    cat = getattr(item, "funcargs", {}).get("catcher")
    if isinstance(cat, catcherlib.Catcher):
        rep.sections.append(("catcher %s" % cat.client, cat.tail(20)))
