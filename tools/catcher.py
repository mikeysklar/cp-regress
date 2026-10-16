#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Tail the MQTT catcher for one board or all of them, outside pytest.

  python3 tools/catcher.py                         # follow results/catcher/*.jsonl, all boards
  python3 tools/catcher.py metro_esp32s3 -n 50     # one board, last 50 records first
  python3 tools/catcher.py --run 1234-1760000000   # only one run id (CPR_RUN_ID)
  python3 tools/catcher.py --live                  # peek at ProtoMQ for every farm.toml board
  python3 tools/catcher.py --live --track metro_esp32s2

Default mode reads the jsonl files the catcher fixture writes at teardown; it never talks to the
broker. --live polls ProtoMQ's delivery capture (POST /dump_deliveries, "clear": false) for
cpr-<board> and prints what the board publishes as it happens. It only peeks: a mailbox exists
while a pytest catcher (or protomq fixture) tracks the board, and --live never creates, clears
or resets one, so a running test is not disturbed. --track also starts tracking boards nobody
tracks and leaves them tracked on exit (the next run's session-start reset clears them).
Run --live on bravo, or pass --api-host. --append also writes what --live caught to
results/catcher/<board>.jsonl, with nodeid "tools/catcher.py".
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))  # lib/catcher.py, not this file
import catcher as catcherlib  # pylint: disable=wrong-import-position


def fmt(r, verbose=False):
    """One record as a line: time board box topic-suffix payload."""
    if r.get("box") == "end":
        p = r.get("payload", {})
        return "%s %-18s end    %s %s" % (r.get("ts", "")[11:], r.get("board"), r.get("nodeid"),
                                         json.dumps(p.get("records", {}), sort_keys=True))
    where = r.get("suffix") or r.get("topic") or ""
    payload = r.get("payload")
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    if not verbose and isinstance(payload, dict):
        p = {k: v for k, v in payload.items() if k not in ("v", "board", "run_id", "nodeid")}
        text = json.dumps(p, sort_keys=True)
    return "%s %-18s %-6s %-8s %s" % (r.get("ts", "")[11:], r.get("board"), r.get("box"), where,
                                      text[:400])


def keep(r, a):
    if a.run and r.get("run_id") != a.run:
        return False
    if a.node and a.node not in (r.get("nodeid") or ""):
        return False
    if a.suffix and r.get("suffix") != a.suffix:
        return False
    return True


def tail_files(a, boards):
    """Follow results/catcher/<board>.jsonl like tail -F (new files and truncation included)."""
    pos = {}
    first = True
    while True:
        paths = sorted(glob.glob(os.path.join(a.dir, "*.jsonl")))
        if boards:
            paths = [p for p in paths if os.path.basename(p)[:-6] in boards]
        for path in paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if pos.get(path, 0) > size:
                pos[path] = 0  # truncated or replaced
            with open(path, "rb") as f:
                f.seek(pos.get(path, 0))
                data = f.read()
            end = data.rfind(b"\n") + 1  # a half-written last line is read next time
            pos[path] = pos.get(path, 0) + end
            recs = []
            for line in data[:end].decode("utf-8", "replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if keep(r, a):
                    recs.append(r)
            if first and a.lines is not None:
                recs = recs[-a.lines:] if a.lines else []
            for r in recs:
                print(fmt(r, a.verbose), flush=True)
        first = False
        if not a.follow:
            return 0
        time.sleep(a.interval)


def live(a, boards):
    """Peek at ProtoMQ for each board and print new records as they arrive."""
    mode = "ensure" if a.track else "join"
    cats = [catcherlib.Catcher(a.api_port, b, nodeid="tools/catcher.py", mode=mode,
                               host=a.api_host, skip_existing=not a.all) for b in boards]
    for c in cats:
        c.start(thread=False)
    shown = {c.board: 0 for c in cats}
    warned = set()
    try:
        while True:
            for c in cats:
                c.poll_once()
                if c.tracked is False and c.board not in warned and mode == "join":
                    print("%s: not tracked by anyone; pass --track to start tracking it"
                          % c.client, file=sys.stderr)
                    warned.add(c.board)
                for r in c.records(since=shown[c.board]):
                    if keep(r, a):
                        print(fmt(r, a.verbose), flush=True)
                shown[c.board] = c.mark()
                if c.errors and c.board not in warned:
                    print("%s: %s" % (c.client, c.errors[-1]), file=sys.stderr)
                    warned.add(c.board)
            if not a.follow:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if a.append:
            for c in cats:
                if c.records():
                    c.dump()
    return 0


# ---------------------------------------------------------------- --selftest (no hardware)

class FakeProtoMQ:
    """The parts of ProtoMQ's HTTP API the catcher uses, with the semantics read from
    api/deliveries.js, api/echo.js, api/reset.js and api/clients.js (protomq 9e2e1bf)."""

    def __init__(self):
        import http.server  # pylint: disable=import-outside-toplevel
        import threading  # pylint: disable=import-outside-toplevel
        self.mailboxes = {}
        self.clients = {}  # id -> set of exact subscriptions
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply(fake.handle(self.path, None))

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                self._reply(fake.handle(self.path, json.loads(self.rfile.read(n) or b"{}")))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, path, body):
        cid = (body or {}).get("client")
        with self.lock:
            if path == "/api/clients":
                return {"status": "OK", "clients": [{"id": c, "subscriptions": sorted(s)}
                                                    for c, s in self.clients.items()]}
            if path == "/api/track_deliveries":
                if cid in self.mailboxes:
                    del self.mailboxes[cid]
                    return {"status": "ERROR", "message": "Mailbox already initialized"}
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
            if path == "/api/reset":
                n = (len(self.mailboxes), len(self.clients))
                self.mailboxes.clear()
                self.clients.clear()
                return {"status": "OK", "cleared": {"autoresponders": 0, "mailboxes": n[0],
                                                    "clients": n[1]}}
        return {"status": "ERROR", "message": "unknown " + path}

    def _publish(self, sender, topic, payload):
        pkt = {"topic": topic, "payload": {"type": "Buffer", "data": list(payload)}}
        if sender in self.mailboxes:
            self.mailboxes[sender]["outbox"].append(pkt)
        for c, subs in self.clients.items():
            if topic in subs and c in self.mailboxes:
                self.mailboxes[c]["inbox"].append(pkt)

    def publish(self, sender, topic, payload):
        with self.lock:
            self._publish(sender, topic, payload)


class FakeMQTT:
    """adafruit_minimqtt's publish(), as tests/lib/cpr_catcher.py calls it."""

    def __init__(self, broker, client):
        self.broker = broker
        self.client = client

    def publish(self, topic, msg, retain=False, qos=0):  # pylint: disable=unused-argument
        assert isinstance(msg, bytes), "minimqtt publishes bytes"
        self.broker.publish(self.client, topic, msg)


def selftest():
    """Catcher, ScriptPlayer, the device helper and every protomq-scripts file against a fake
    ProtoMQ. Returns the number of failures."""
    import importlib.util  # pylint: disable=import-outside-toplevel
    import tempfile  # pylint: disable=import-outside-toplevel
    fails = []

    def ok(name, cond, detail=""):
        print("%-4s %-28s %s" % ("ok" if cond else "FAIL", name, detail if not cond else ""))
        if not cond:
            fails.append(name)

    spec = importlib.util.spec_from_file_location(
        "cpr_catcher", os.path.join(HERE, "tests", "lib", "cpr_catcher.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    for path in sorted(glob.glob(os.path.join(catcherlib.SCRIPTS_DIR, "*.json"))):
        try:
            catcherlib.load_script(path)
            ok("script " + os.path.basename(path), True)
        except (ValueError, OSError) as e:
            ok("script " + os.path.basename(path), False, str(e))
    for bad, why in (({"protoVersion": "v2", "steps": [{"name": "a", "send": "x"}]}, "v2"),
                     ({"protoVersion": "cpr-plain", "steps": [{"name": "a", "topic": "results",
                                                               "send": "x"}]}, "results topic"),
                     ({"protoVersion": "cpr-plain", "steps": [{"name": "a", "topic": "b2d/#",
                                                               "send": "x"}]}, "wildcard")):
        try:
            catcherlib.load_script(bad)
            ok("script refuses " + why, False, "loaded")
        except ValueError:
            ok("script refuses " + why, True)

    fake = FakeProtoMQ()
    board = "fake_board"
    cid = catcherlib.client_id(board)
    fake.clients[cid] = {"cpr/%s/b2d" % board}
    with tempfile.TemporaryDirectory() as tmp:
        cat = catcherlib.Catcher(fake.port, board, nodeid="sel::t", run_id="run-1", poll=0.05)
        cat.start()
        try:
            catcherlib.Catcher(fake.port, board).start()
            ok("one catcher per client", False, "second start allowed")
        except catcherlib.CatcherError:
            ok("one catcher per client", True)
        rep = helper.Reporter(FakeMQTT(fake, cid), board, run_id="run-1", nodeid="sel::t")
        player = cat.play("cpr-ack")
        rep.result("first", True, value={"a": [1, 2.5, None]}, detail='q"\\\t')
        rep.log("hello", level="warning")
        r = cat.expect("results", {"name": "first"}, timeout=3)
        ok("expect result", r["payload"]["value"] == {"a": [1, 2.5, None]}
           and r["payload"]["detail"] == 'q"\\\t', r["payload"])
        ok("expect log", cat.expect("log", lambda p: p["msg"] == "hello", timeout=3) is not None)
        try:
            cat.expect("results", {"name": "first"}, timeout=0.2)
            ok("cursor moves forward", False)
        except catcherlib.CatcherTimeout:
            ok("cursor moves forward", True)
        rep.result("catcher_ready", True)
        cat.expect("b2d", {"cmd": "ack", "run_id": "run-1"}, timeout=3, boxes=("inbox",))
        rep.result("ack_received", True, value="run-1")
        cat.expect("b2d", {"cmd": "bye"}, timeout=3, boxes=("inbox",))
        ok("script played", sorted(player.completed) == ["ack", "bye"], player.state())
        try:
            cat.echo("results", "fake")
            ok("echo refuses results", False)
        except ValueError:
            ok("echo refuses results", True)
        # someone else re-tracks the mailbox: recorded as a gap, catching continues
        fake.handle("/api/dump_deliveries", {"client": cid, "clear": True})
        time.sleep(0.2)
        rep.result("after_gap", True)
        cat.expect("results", {"name": "after_gap"}, timeout=3)
        ok("gap recorded", len(cat.gaps()) == 1, cat.gaps())
        try:
            cat.assert_none("results", {"ok": False}, duration=0.1)
            ok("assert_none", True)
        except catcherlib.CatcherUnexpected as e:
            ok("assert_none", False, str(e))
        cat.stop()
        ok("stop untracks", cid not in fake.mailboxes)
        cat.disconnect()
        ok("disconnect only own", cid not in fake.clients)
        path = cat.dump(os.path.join(tmp, "x.jsonl"))
        with open(path) as f:
            lines = [json.loads(x) for x in f]
        ok("jsonl", lines[-1]["box"] == "end" and all(x["run_id"] == "run-1" for x in lines)
           and all(x["nodeid"] == "sel::t" for x in lines), lines[-1])
        seqs = [p["seq"] for p in cat.payloads("results") + cat.payloads("log")]
        ok("seq contiguous", sorted(seqs) == list(range(rep.seq)), seqs)
        ok("released", catcherlib.live(cid) is None)
    return len(fails)


def farm_boards(farm):
    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore  # pylint: disable=import-outside-toplevel
    with open(farm, "rb") as f:
        cfg = tomllib.load(f)
    return cfg, [b["name"] for b in cfg.get("board", [])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("boards", nargs="*", help="board names from farm.toml (default: all)")
    ap.add_argument("--farm", default=os.path.join(HERE, "farm.toml"))
    ap.add_argument("--dir", default=catcherlib.RESULTS_DIR, help="where the jsonl files are")
    ap.add_argument("-n", "--lines", type=int, default=20,
                    help="records to show from each file before following (default 20)")
    ap.add_argument("--no-follow", dest="follow", action="store_false",
                    help="print and exit instead of following")
    ap.add_argument("--run", default="", help="only this run id")
    ap.add_argument("--node", default="", help="only nodeids containing this")
    ap.add_argument("--suffix", default="", help="only this topic suffix (results, log, ...)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the full envelope")
    ap.add_argument("--interval", type=float, default=0.5, help="poll seconds (default 0.5)")
    ap.add_argument("--live", action="store_true", help="read ProtoMQ instead of the files")
    ap.add_argument("--track", action="store_true",
                    help="with --live: start tracking boards nobody tracks (left tracked)")
    ap.add_argument("--all", action="store_true",
                    help="with --live: also print what the mailbox held before we started")
    ap.add_argument("--append", action="store_true",
                    help="with --live: append what was caught to results/catcher on exit")
    ap.add_argument("--api-host", default="127.0.0.1", help="ProtoMQ host (default 127.0.0.1)")
    ap.add_argument("--api-port", type=int, default=0,
                    help="ProtoMQ HTTP API port (default farm.toml [broker] api_port, or 5173)")
    ap.add_argument("--selftest", action="store_true",
                    help="check the catcher, the device helper and protomq-scripts/ against a "
                         "fake ProtoMQ; no hardware, no broker")
    a = ap.parse_args(argv)
    if a.selftest:
        n = selftest()
        print("\n%d failures" % n)
        return 1 if n else 0
    cfg, names = farm_boards(a.farm)
    unknown = [b for b in a.boards if b not in names]
    if unknown:
        ap.error("unknown board(s) %s; farm.toml has %s" % (",".join(unknown), ",".join(names)))
    boards = a.boards or names
    if a.live:
        a.api_port = a.api_port or cfg.get("broker", {}).get("api_port", 5173)
        return live(a, boards)
    if a.track or a.append or a.all:
        ap.error("--track, --append and --all only apply with --live")
    return tail_files(a, set(a.boards))


if __name__ == "__main__":
    sys.exit(main())
