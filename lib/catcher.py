# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Host side of the MQTT catcher: what a CircuitPython board publishes, caught per board.

CircuitPython results never go to Adafruit IO or WipperSnapper topics. Boards publish to the
local ProtoMQ on bravo, on plain topics under their own prefix:

  cpr/<board>/results   device -> host, one JSON result dict per message (tests/lib/cpr_catcher.py)
  cpr/<board>/log       device -> host, one JSON log line per message
  cpr/<board>/d2b, b2d  free-form traffic (mqtt_broker_test.py); b2d also carries stimulus

Not <user>/ws-d2b/<uid>: ProtoMQ decodes those as protobuf and a plain payload can come back as
{} instead of its bytes.

How it catches. ProtoMQ's delivery capture for the board's MQTT client id, cpr-<board>:
POST /track_deliveries starts a mailbox, POST /dump_deliveries with "clear": false peeks at it.
A peek returns the whole mailbox so far, so only entries past the last count are new. A mailbox
that shrank or vanished means someone else re-tracked or reset it; that is recorded as a "gap"
record, never hidden. The host time on each record is when a poll first saw it (poll interval,
0.5 s by default); the device's own monotonic t_ms is inside the payload.

Stimulus. protomq-scripts/*.json use ProtoMQ's playback-script shape (name, description,
protoVersion, steps with trigger / after / waitFor / delay / topic / send) and are played here,
through POST /echo, by ScriptPlayer. ProtoMQ's own /scripts and /autoresponse cannot carry them
(read on bravo, adafruit/protomq 9e2e1bf): both only fire on decoded +/ws-d2b/+ protobuf
traffic and encode replies as BrokerToDevice (broker/protobuf_autoresponders.js), and /scripts
only loads files from ~/protomq/scripts at first use (broker/script_runner.js loadScripts),
which we never write to.

The expect / assert_none / mark / records / dump names and their cursor rules follow Brent's
SerialMonitor (hil_board_utils.py:757-1092 @ cf64e03), so a test reads the same against the
serial console or the catcher.

No pytest in here: test/fixtures/broker.py (the catcher fixture) and tools/catcher.py both use it.
"""
import binascii
import fcntl
import json
import os
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(HERE, "results", "catcher")
SCRIPTS_DIR = os.path.join(HERE, "protomq-scripts")

# Suffixes a board publishes on. The host never sends on these, so stimulus cannot fake a result.
DEVICE_SUFFIXES = ("results", "log")

# ProtoMQ API failures a poll survives (the broker restarting, a slow answer)
API_ERRORS = (OSError, ValueError, urllib.error.URLError)


def api(port, path, body=None, timeout=5, host="127.0.0.1"):
    """One call to ProtoMQ's HTTP API. Returns the decoded JSON."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://%s:%d/api%s" % (host, port, path), data=data,
                                 method="GET" if body is None else "POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def raw_payload(payload):
    """ProtoMQ returns non-protobuf payloads as a serialized Node Buffer."""
    if isinstance(payload, dict) and payload.get("type") == "Buffer":
        return bytes(payload.get("data", []))
    return payload


def client_id(board):
    """The MQTT client id a board uses on the farm broker (same as the protomq fixture)."""
    return "cpr-" + board


def prefix(board):
    return "cpr/%s/" % board


def decode(data):
    """(payload, form) for raw bytes: parsed JSON, else text, else hex."""
    if not isinstance(data, (bytes, bytearray)):
        return data, "decoded"  # ProtoMQ decoded it (a protobuf topic); keep what it gave
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return binascii.hexlify(bytes(data)).decode(), "hex"
    try:
        return json.loads(text), "json"
    except ValueError:
        return text, "text"


def encode_send(send, subs):
    """Bytes for a script step's send: {"json": obj} | {"text": str} | {"hex": str} | str.
    "{board}", "{run_id}" and "{nodeid}" inside strings are filled from subs."""
    def fill(v):
        if isinstance(v, str):
            for k, s in subs.items():
                v = v.replace("{%s}" % k, str(s))
            return v
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    if isinstance(send, str):
        return fill(send).encode()
    if "json" in send:
        return json.dumps(fill(send["json"]), separators=(",", ":")).encode()
    if "text" in send:
        return fill(send["text"]).encode()
    if "hex" in send:
        return binascii.unhexlify(send["hex"])
    raise ValueError("send needs one of json / text / hex")


def match(pattern, value):
    """ProtoMQ's autoresponder match rules (docs/api/README.md, "match rules"): "*" or a missing
    key matches anything, objects compare key by key, arrays by index, the rest strictly."""
    if pattern == "*":
        return True
    if isinstance(pattern, dict):
        if not isinstance(value, dict):
            return False
        return all(k not in value and p == "*" or k in value and match(p, value[k])
                   for k, p in pattern.items())
    if isinstance(pattern, list):
        if not isinstance(value, list) or len(value) < len(pattern):
            return False
        return all(match(p, v) for p, v in zip(pattern, value))
    num = (int, float)
    if isinstance(pattern, num) and isinstance(value, num) \
            and not isinstance(pattern, bool) and not isinstance(value, bool):
        return pattern == value  # JavaScript === does not tell 1 from 1.0 either
    return type(pattern) is type(value) and pattern == value


def dot_path(obj, path):
    """ProtoMQ's trigger rule: every segment exists and the final value is not null."""
    for seg in path.split("."):
        if not isinstance(obj, dict) or seg not in obj:
            return False
        obj = obj[seg]
    return obj is not None


def safe_suffix(suffix):
    """A topic suffix under cpr/<board>/ the host may publish on, or ValueError."""
    if (not isinstance(suffix, str) or not suffix or suffix.startswith("/") or ".." in suffix
            or "+" in suffix or "#" in suffix or "\0" in suffix):
        raise ValueError("bad topic suffix %r" % (suffix,))
    if suffix.split("/")[0] in DEVICE_SUFFIXES:
        raise ValueError("the host never publishes on %r (device-only)" % suffix)
    return suffix


def _stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)


class CatcherError(Exception):
    """The catcher cannot do what was asked (broker gone, client already caught)."""


class CatcherTimeout(CatcherError, AssertionError):
    """expect() saw no matching record; a test failure."""


class CatcherUnexpected(CatcherError, AssertionError):
    """assert_none() saw a matching record; a test failure."""


# One live catcher per client id per process: a second track_deliveries on the same id would
# delete the first one's mailbox.
_LIVE = {}
_LIVE_LOCK = threading.Lock()


def live(client):
    """The Catcher currently tracking this client id in this process, or None."""
    with _LIVE_LOCK:
        return _LIVE.get(client)


class Catcher:
    """What one board published through ProtoMQ, as a list of records.

    mode "own"     (fixture) a fresh mailbox at start, re-tracked if it vanishes, deleted at stop
         "join"    (tools/catcher.py --live) peek at someone else's mailbox, never create or clear
         "ensure"  (tools/catcher.py --live --track) create the mailbox if nobody tracks it,
                   never clear it; it is left tracked on exit
    """

    def __init__(self, api_port, board, nodeid="", run_id=None, mode="own", poll=0.5,
                 host="127.0.0.1", skip_existing=False):
        self.port = api_port
        self.host = host
        self.board = board
        self.client = client_id(board)
        self.prefix = prefix(board)
        self.nodeid = nodeid
        self.run_id = run_id if run_id is not None else os.environ.get("CPR_RUN_ID", "")
        self.mode = mode
        self.poll = poll
        self.skip_existing = skip_existing
        self.errors = []      # API failures seen while polling (str)
        self.player = None
        self._records = []
        self._seen = {"inbox": 0, "outbox": 0}
        self._cursor = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        self._stopped = False
        self._first = True
        self.tracked = None   # whether ProtoMQ had a mailbox for the client at the last poll

    # -- lifecycle ------------------------------------------------------------

    def call(self, path, body=None, timeout=5):
        return api(self.port, path, body, timeout=timeout, host=self.host)

    def _track(self):
        """A fresh, empty mailbox. Tracking a tracked client deletes it, so ask twice then."""
        r = self.call("/track_deliveries", {"client": self.client})
        if r.get("status") != "OK":
            r = self.call("/track_deliveries", {"client": self.client})
        return r.get("status") == "OK"

    def start(self, thread=True):
        """Start catching. With thread, a background poll keeps host timestamps close to arrival
        and lets a running script react; without it, call poll_once() yourself."""
        if self.mode == "own":
            with _LIVE_LOCK:
                other = _LIVE.get(self.client)
                if other is not None and other is not self:
                    raise CatcherError("%s is already caught by %s" % (self.client, other.nodeid))
                _LIVE[self.client] = self
            try:
                self.call("/dump_deliveries", {"client": self.client, "clear": True})
                if not self._track():
                    raise CatcherError("ProtoMQ would not track %s" % self.client)
            except API_ERRORS as e:
                self._unregister()
                raise CatcherError("ProtoMQ on :%d: %s" % (self.port, e)) from e
        self._started = True
        if thread:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="Catcher(%s)" % self.board)
            self._thread.start()
        return self

    def _unregister(self):
        with _LIVE_LOCK:
            if _LIVE.get(self.client) is self:
                del _LIVE[self.client]

    def _loop(self):
        while not self._stop.wait(self.poll):
            self.poll_once()

    def stop(self):
        """Stop polling after one last poll. In "own" mode the mailbox is deleted (which stops
        ProtoMQ tracking the client). The records stay readable."""
        if self._stopped:
            return self
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.poll_once()
        if self.mode == "own":
            try:
                self.call("/dump_deliveries", {"client": self.client, "clear": True})
            except API_ERRORS as e:
                self.errors.append("stop: %s" % e)
            self._unregister()
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        return self

    def disconnect(self):
        """Close the board's MQTT connection if it is still open (per client, never /reset)."""
        try:
            for c in self.call("/clients").get("clients", []):
                if c.get("id") == self.client:
                    return self.call("/disconnect", {"client": self.client})
        except API_ERRORS as e:
            self.errors.append("disconnect: %s" % e)
        return None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- polling --------------------------------------------------------------

    def _add(self, box, topic, payload, form, t):
        rec = {"ts": _stamp(t), "t": round(t, 3), "run_id": self.run_id, "nodeid": self.nodeid,
               "board": self.board, "client": self.client, "box": box, "topic": topic,
               "suffix": topic[len(self.prefix):] if topic and topic.startswith(self.prefix)
               else None, "payload": payload, "form": form}
        rec["index"] = len(self._records)
        self._records.append(rec)
        if self.player is not None and box == "outbox":
            self.player.on_record(rec)
        return rec

    def poll_once(self):
        """One peek at the mailbox. Returns the number of new records."""
        try:
            r = self.call("/dump_deliveries", {"client": self.client, "clear": False})
        except API_ERRORS as e:
            with self._cond:
                self.errors.append("%s: %s" % (_stamp(time.time()), e))
                del self.errors[:-50]
                self._cond.notify_all()
            return 0
        now = time.time()
        new = 0
        with self._cond:
            self.tracked = r.get("status") == "OK"
            if not self.tracked:
                # no mailbox: deleted by someone else (a /reset, a protomq fixture, the CLI)
                last_gap = bool(self._records) and self._records[-1]["box"] == "gap"
                if (self.mode == "own" or any(self._seen.values())) and not last_gap:
                    self._add("gap", None, r.get("message", "mailbox gone"), "text", now)
                self._seen = {"inbox": 0, "outbox": 0}
                if self.mode in ("own", "ensure") and self._started and not self._stop.is_set():
                    try:
                        self.tracked = self._track()
                    except API_ERRORS as e:
                        self.errors.append("re-track: %s" % e)
                self._first = False
                if self.player is not None:
                    self.player.tick(now)
                self._cond.notify_all()
                return 0
            boxes = r.get("deliveries", {})
            for box in ("outbox", "inbox"):
                items = boxes.get(box, [])
                if len(items) < self._seen[box]:
                    self._add("gap", None, "%s shrank from %d to %d: re-tracked elsewhere"
                              % (box, self._seen[box], len(items)), "text", now)
                    self._seen[box] = 0
                if self._first and self.skip_existing:
                    self._seen[box] = len(items)
                    continue
                for m in items[self._seen[box]:]:
                    payload, form = decode(raw_payload(m.get("payload")))
                    self._add(box, m.get("topic"), payload, form, now)
                    new += 1
                self._seen[box] = len(items)
            self._first = False
            if self.player is not None:
                self.player.tick(now)
            self._cond.notify_all()
        return new

    # -- host to board --------------------------------------------------------

    def echo(self, suffix, payload):
        """Publish bytes (or str) on cpr/<board>/<suffix> now, through ProtoMQ's /echo. Only under
        this board's prefix, never on a device suffix. Recorded as box "host"."""
        topic = self.prefix + safe_suffix(suffix)
        if isinstance(payload, str):
            payload = payload.encode()
        r = self.call("/echo", {"topic": topic, "payload": bytes(payload).decode("latin1")})
        with self._cond:
            p, form = decode(bytes(payload))
            self._add("host", topic, p, form, time.time())
            self._cond.notify_all()
        return r

    def play(self, script, disabled_steps=()):
        """Play a protomq-scripts script (name, path or dict) against this board. Returns the
        ScriptPlayer; its completed dict says which steps ran."""
        with self._cond:
            self.player = ScriptPlayer(self, load_script(script), disabled_steps)
            self.player.tick(time.time())
        return self.player

    def device_config(self):
        """What device code needs to publish here: str.format these into the DEVICE template."""
        return {"client_id": self.client, "board": self.board, "run_id": self.run_id,
                "nodeid": self.nodeid}

    # -- matching (after Brent's SerialMonitor, hil_board_utils.py:973-1059 @ cf64e03) --------

    def mark(self):
        """The index the next record will get."""
        with self._cond:
            return len(self._records)

    def rewind(self, index=0):
        """Move the cursor back to index, so a stopped catcher can be read again from there.
        (Not in Brent's SerialMonitor, whose cursor only moves forward.)"""
        with self._cond:
            self._cursor = index

    @staticmethod
    def _hit(rec, suffix, predicate, boxes):
        if rec["box"] not in boxes or (suffix is not None and rec["suffix"] != suffix):
            return False
        if predicate is None:
            return True
        if isinstance(predicate, dict):
            return match(predicate, rec["payload"])
        try:
            return bool(predicate(rec["payload"]))
        except (KeyError, TypeError, IndexError):
            return False  # a payload of the wrong shape is not a match

    def _tail(self, start, count=15):
        recs = self._records[start:]
        shown = ["  [%d] %s %-6s %s %s" % (r["index"], r["ts"][11:], r["box"], r["topic"],
                                            json.dumps(r["payload"])[:160]) for r in recs[-count:]]
        if len(recs) > count:
            shown.insert(0, "  ... %d earlier records" % (len(recs) - count))
        return "\n".join(shown) if shown else "  (no records)"

    def expect(self, suffix, predicate=None, timeout=10, boxes=("outbox",)):
        """Wait for the first record at or after the cursor on cpr/<board>/<suffix> whose payload
        satisfies predicate (a callable on the payload, or a dict pattern with ProtoMQ's match
        rules), move the cursor past it and return the record. Consecutive expect() calls
        therefore check order. Raises CatcherTimeout."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        deadline = time.monotonic() + timeout
        with self._cond:
            start = pos = self._cursor
            while True:
                for rec in self._records[pos:]:
                    if self._hit(rec, suffix, predicate, boxes):
                        self._cursor = rec["index"] + 1
                        return rec
                pos = len(self._records)
                remaining = deadline - time.monotonic()
                if self._stopped or remaining <= 0:
                    why = "catcher stopped" if self._stopped else "within %.3gs" % timeout
                    err = ("; API errors: %s" % "; ".join(self.errors[-3:])) if self.errors else ""
                    raise CatcherTimeout("%s: no %s record on %s%s matched %r (%s)%s; records "
                                         "from %d:\n%s" % (self.client, "/".join(boxes),
                                                           self.prefix, suffix, predicate, why,
                                                           err, start, self._tail(start)))
                self._cond.wait(min(remaining, max(self.poll, 0.1)))

    def assert_none(self, suffix, predicate=None, since=0, duration=0, boxes=("outbox",)):
        """Fail if any record from since matches, watching new ones for duration more seconds.
        Doesn't move the cursor. Raises CatcherUnexpected."""
        __tracebackhide__ = True  # pylint: disable=unused-variable
        deadline = time.monotonic() + duration
        with self._cond:
            pos = since
            while True:
                for rec in self._records[pos:]:
                    if self._hit(rec, suffix, predicate, boxes):
                        raise CatcherUnexpected("%s: record %d on %s matched %r: %s"
                                                % (self.client, rec["index"], rec["topic"],
                                                   predicate, json.dumps(rec["payload"])[:300]))
                pos = max(pos, len(self._records))
                remaining = deadline - time.monotonic()
                if self._stopped or remaining <= 0:
                    return
                self._cond.wait(remaining)

    def records(self, suffix=None, since=0, boxes=("outbox", "inbox", "host", "gap")):
        """Copies of the records from index since on, optionally only one suffix."""
        with self._cond:
            return [dict(r) for r in self._records[since:]
                    if r["box"] in boxes and (suffix is None or r["suffix"] == suffix)]

    def payloads(self, suffix, since=0):
        """Just the payloads the board published on one suffix, in order."""
        return [r["payload"] for r in self.records(suffix, since, boxes=("outbox",))]

    def gaps(self):
        return self.records(boxes=("gap",))

    def tail(self, count=15):
        with self._cond:
            return self._tail(0, count)

    # -- output ---------------------------------------------------------------

    def summary(self, **extra):
        """The "end" record dump() writes last: counts, gaps, errors, script progress."""
        with self._cond:
            boxes = {}
            for r in self._records:
                boxes[r["box"]] = boxes.get(r["box"], 0) + 1
            out = {"records": boxes, "errors": self.errors[-10:]}
            if self.player is not None:
                out["script"] = self.player.state()
        out.update(extra)
        return out

    def dump(self, path=None, **extra):
        """Append every record and one "end" record to path (default
        results/catcher/<board>.jsonl), under a lock so the CLI can tail it. Returns the path."""
        path = path or os.path.join(RESULTS_DIR, self.board + ".jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        now = time.time()
        with self._cond:
            recs = list(self._records)
        end = {"ts": _stamp(now), "t": round(now, 3), "run_id": self.run_id,
               "nodeid": self.nodeid, "board": self.board, "client": self.client, "box": "end",
               "topic": None, "suffix": None, "payload": self.summary(**extra), "form": "json"}
        with open(str(path) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(path, "a") as f:
                    for r in recs + [end]:
                        f.write(json.dumps(r, sort_keys=True, default=repr) + "\n")
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        return path


# ---------------------------------------------------------------- playback scripts

STEP_KEYS = {"name", "description", "trigger", "after", "waitFor", "match", "delay", "topic",
             "enabled", "send", "response"}


def load_script(script):
    """A protomq-scripts script as a dict, validated. script is a dict, a path, or a name in
    protomq-scripts/ without .json (ProtoMQ refers to scripts the same way)."""
    if isinstance(script, dict):
        data = script
    else:
        path = script if os.path.sep in str(script) or str(script).endswith(".json") \
            else os.path.join(SCRIPTS_DIR, script + ".json")
        with open(path) as f:
            data = json.load(f)
    problems = []
    if data.get("protoVersion") != "cpr-plain":
        problems.append('protoVersion must be "cpr-plain" (plain cpr/<board>/ topics)')
    names = set()
    for i, step in enumerate(data.get("steps", [])):
        name = step.get("name") or "#%d" % i
        if name in names:
            problems.append("step %s: duplicate name" % name)
        names.add(name)
        unknown = set(step) - STEP_KEYS
        if unknown:
            problems.append("step %s: unknown keys %s" % (name, ",".join(sorted(unknown))))
        if ("send" in step) == ("response" in step):
            problems.append("step %s: needs exactly one of send / response" % name)
        try:
            safe_suffix(step.get("topic", "b2d"))
            encode_send(step.get("send", step.get("response", "")), {})
        except (ValueError, TypeError, binascii.Error) as e:
            problems.append("step %s: %s" % (name, e))
        for key in ("trigger", "waitFor"):
            v = step.get(key)
            if v is not None and (not isinstance(v, str) or v.split(".")[0] not in DEVICE_SUFFIXES
                                  + ("d2b",)):
                problems.append("step %s: %s must be a dot path starting results / log / d2b"
                                % (name, key))
        if step.get("after") is not None and step["after"] not in names - {name}:
            problems.append("step %s: after %r is not an earlier step" % (name, step["after"]))
    if not data.get("steps"):
        problems.append("no steps")
    if problems:
        raise ValueError("%s: %s" % (data.get("name", script), "; ".join(problems)))
    return data


class ScriptPlayer:
    """Plays one cpr-plain script for one board, driven by the catcher's polls.

    Semantics follow ProtoMQ's script runner (broker/script_runner.js), restricted to plain
    topics under cpr/<board>/:
      trigger   a dot path into {suffix: payload} of a record the board published, e.g.
                "results.name"; with "match" (ProtoMQ's autoresponder pattern) it must match too
      after     the step runs once that step has sent; with waitFor it then also waits for a
                matching record published after that point
      delay     milliseconds between becoming due and sending
      topic     suffix to send on, default "b2d"; never results / log
      send      {"json": ...} | {"text": "..."} | {"hex": "..."}; "{board}", "{run_id}",
                "{nodeid}" in strings are filled in. "response" is accepted as a synonym.
      enabled   false skips the step
    A step with neither trigger nor after is due at once. Each step sends at most once.
    """

    def __init__(self, catcher, script, disabled_steps=()):
        self.catcher = catcher
        self.script = script
        self.steps = [s for s in script["steps"]
                      if s.get("enabled", True) and s["name"] not in set(disabled_steps)]
        self.completed = {}   # step name -> host time sent
        self.due = {}         # step name -> host time to send at
        self.armed = {}       # step name -> record index from which waitFor may match
        self.errors = []
        for s in self.steps:
            if not s.get("trigger") and not s.get("after"):
                self.due[s["name"]] = time.time() + s.get("delay", 0) / 1000.0

    @staticmethod
    def _matches(step, key, rec):
        msg = {rec["suffix"]: rec["payload"]}
        if not dot_path(msg, step[key]):
            return False
        return match(step["match"], msg) if step.get("match") is not None else True

    def on_record(self, rec):
        """A new record the board published. Called with the catcher's lock held."""
        if rec["suffix"] is None:
            return
        now = time.time()
        for s in self.steps:
            n = s["name"]
            if n in self.completed or n in self.due:
                continue
            if s.get("trigger") and (not s.get("after") or s["after"] in self.completed) \
                    and self._matches(s, "trigger", rec):
                self.due[n] = now + s.get("delay", 0) / 1000.0
            elif s.get("waitFor") and n in self.armed and rec["index"] >= self.armed[n] \
                    and self._matches(s, "waitFor", rec):
                self.due[n] = now + s.get("delay", 0) / 1000.0

    def tick(self, now):
        """Send every step that is due. Called with the catcher's lock held."""
        for s in self.steps:
            n = s["name"]
            if n in self.completed or n not in self.due or self.due[n] > now:
                continue
            c = self.catcher
            subs = {"board": c.board, "run_id": c.run_id, "nodeid": c.nodeid}
            try:
                payload = encode_send(s.get("send", s.get("response")), subs)
                topic = c.prefix + safe_suffix(s.get("topic", "b2d"))
                c.call("/echo", {"topic": topic, "payload": payload.decode("latin1")})
            except API_ERRORS as e:
                self.errors.append("%s: %s" % (n, e))
                self.due[n] = now + 1.0  # retry the send once a second
                continue
            p, form = decode(payload)
            c._add("host", topic, p, form, now)  # pylint: disable=protected-access
            self.completed[n] = now
            del self.due[n]
            for t in self.steps:  # steps chained on this one
                if t.get("after") == n and not t.get("trigger"):
                    if t.get("waitFor"):
                        self.armed[t["name"]] = len(c._records)  # pylint: disable=protected-access
                    else:
                        self.due[t["name"]] = now + t.get("delay", 0) / 1000.0

    def state(self):
        return {"name": self.script.get("name"), "completed": sorted(self.completed),
                "pending": [s["name"] for s in self.steps if s["name"] not in self.completed],
                "errors": self.errors[-5:]}
