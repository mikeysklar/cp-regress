# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""lib/catcher.py against a fake ProtoMQ HTTP API on 127.0.0.1 (harness/fakes.py): delivery
capture, record matching with Brent's expect / assert_none cursor rules (SerialMonitor,
hil_board_utils.py:1055-1141 @ 242a162), gaps, host stimulus, playback scripts and the jsonl
dump. Only cpr/<board>/ topics; nothing goes near Adafruit IO."""
import json
import threading

import pytest

import catcher as cat
from catcher import Catcher, CatcherError, CatcherTimeout, CatcherUnexpected

pytestmark = pytest.mark.smoke

BOARD = "selftest_board"
CLIENT = "cpr-" + BOARD
TOPIC = "cpr/%s/" % BOARD


@pytest.fixture
def catcher(fake_protomq):
    """An "own" catcher polled by hand (thread=False), stopped at teardown."""
    c = Catcher(fake_protomq.port, BOARD, nodeid="harness::x", run_id="run-1", poll=0.05)
    c.start(thread=False)
    yield c
    c.stop()


def publish(fake, suffix, payload):
    fake.publish(CLIENT, TOPIC + suffix, payload)


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,value,hit", [
    ("*", None, True),
    ({"a": 1}, {"a": 1, "b": 2}, True),
    ({"a": 1}, {"a": 2}, False),
    ({"a": "*"}, {}, True),             # "*" matches a missing key too
    ({"a": 1}, {}, False),
    ({"a": {"b": [1, "*"]}}, {"a": {"b": [1, 9, 3]}}, True),
    ([1, 2], [1], False),
    (1, 1.0, True),                     # JavaScript === does not tell them apart
    (True, 1, False),
    ("1", 1, False),
    ({"a": 1}, [1], False),
])
def test_match_rules(pattern, value, hit):
    assert cat.match(pattern, value) is hit


def test_decode_forms():
    assert cat.decode(b'{"ok": true}') == ({"ok": True}, "json")
    assert cat.decode(b"plain words") == ("plain words", "text")
    assert cat.decode(b"\xff\x00") == ("ff00", "hex")
    assert cat.decode({"decoded": 1}) == ({"decoded": 1}, "decoded")
    assert cat.raw_payload({"type": "Buffer", "data": [104, 105]}) == b"hi"


@pytest.mark.parametrize("suffix", ["results", "log", "results/x", "", "/b2d", "../b2d", "b2d/#",
                                    "+", None])
def test_host_never_publishes_on_device_or_bad_topics(suffix):
    with pytest.raises(ValueError):
        cat.safe_suffix(suffix)


def test_dot_path_and_encode_send():
    assert cat.dot_path({"results": {"name": "x"}}, "results.name")
    assert not cat.dot_path({"results": {"name": None}}, "results.name")
    assert not cat.dot_path({"results": "flat"}, "results.name")
    subs = {"board": BOARD, "run_id": "r"}
    assert cat.encode_send({"json": {"b": "{board}", "n": [1, "{run_id}"]}}, subs) == \
        b'{"b":"selftest_board","n":[1,"r"]}'
    assert cat.encode_send({"hex": "00ff"}, subs) == b"\x00\xff"
    assert cat.encode_send("go {board}", subs) == b"go selftest_board"
    with pytest.raises(ValueError):
        cat.encode_send({"neither": 1}, subs)


# --- catching -----------------------------------------------------------------------------------


def test_start_tracks_a_fresh_mailbox(fake_protomq, catcher):
    assert fake_protomq.paths()[:2] == ["/api/dump_deliveries", "/api/track_deliveries"]
    assert CLIENT in fake_protomq.mailboxes
    assert cat.live(CLIENT) is catcher


def test_records_and_matching(fake_protomq, catcher):
    publish(fake_protomq, "results", {"name": "a", "ok": True})
    publish(fake_protomq, "log", "booted")
    publish(fake_protomq, "results", {"name": "b", "ok": False})
    publish(fake_protomq, "results", b"\xff\xfe")
    assert catcher.poll_once() == 4
    assert catcher.poll_once() == 0  # a peek returns the whole mailbox; only new ones count

    rec = catcher.expect("results", {"name": "a"}, timeout=0)
    assert (rec["box"], rec["suffix"], rec["form"], rec["index"]) == ("outbox", "results",
                                                                      "json", 0)
    assert (rec["run_id"], rec["nodeid"], rec["board"]) == ("run-1", "harness::x", BOARD)
    assert catcher.expect("log", timeout=0)["payload"] == "booted"
    assert catcher.expect("results", lambda p: p["ok"] is False, timeout=0)["index"] == 2
    # The cursor is past "a": Brent's ordering rule.
    with pytest.raises(CatcherTimeout, match="no outbox record") as exc:
        catcher.expect("results", {"name": "a"}, timeout=0.2)
    assert isinstance(exc.value, AssertionError)
    # A payload of the wrong shape is not a match, not a crash.
    assert catcher.expect("results", lambda p: p == "fffe", timeout=0)["form"] == "hex"

    catcher.assert_none("results", {"name": "c"})
    with pytest.raises(CatcherUnexpected, match="record 2"):
        catcher.assert_none("results", {"ok": False})
    catcher.assert_none("results", {"ok": False}, since=3)
    assert catcher.payloads("log") == ["booted"]
    catcher.rewind()
    assert catcher.expect("results", timeout=0)["index"] == 0


def test_expect_waits_for_a_late_record(fake_protomq):
    with Catcher(fake_protomq.port, BOARD, run_id="r", poll=0.05) as c:  # polling thread
        threading.Timer(0.3, publish, (fake_protomq, "results", {"name": "late"})).start()
        assert c.expect("results", {"name": "late"}, timeout=5)["payload"]["name"] == "late"
        with pytest.raises(CatcherUnexpected):
            c.assert_none("results", duration=1)


def test_one_live_catcher_per_client(fake_protomq, catcher):
    with pytest.raises(CatcherError, match="already caught"):
        Catcher(fake_protomq.port, BOARD).start(thread=False)
    catcher.stop()
    assert cat.live(CLIENT) is None
    assert CLIENT not in fake_protomq.mailboxes  # "own" deletes its mailbox
    Catcher(fake_protomq.port, BOARD).start(thread=False).stop()


def test_mailbox_reset_elsewhere_is_a_gap(fake_protomq, catcher):
    publish(fake_protomq, "results", {"name": "a"})
    catcher.poll_once()
    del fake_protomq.mailboxes[CLIENT]   # someone ran POST /reset or re-tracked it
    catcher.poll_once()
    assert [g["payload"] for g in catcher.gaps()] == ["No mailbox initialized for: %s." % CLIENT]
    assert catcher.tracked and CLIENT in fake_protomq.mailboxes  # re-tracked
    catcher.poll_once()
    assert len(catcher.gaps()) == 1      # one gap per outage, not one per poll
    publish(fake_protomq, "results", {"name": "b"})
    assert catcher.poll_once() == 1

    # A mailbox that shrank under us was re-tracked by someone else.
    publish(fake_protomq, "results", {"name": "c"})
    catcher.poll_once()
    fake_protomq.mailboxes[CLIENT]["outbox"][:] = []
    publish(fake_protomq, "results", {"name": "d"})
    catcher.poll_once()
    assert "outbox shrank from 2 to 1" in catcher.gaps()[-1]["payload"]
    assert catcher.payloads("results")[-1] == {"name": "d"}


def test_broker_down(fake_protomq, catcher):
    fake_protomq.down = True
    assert catcher.poll_once() == 0
    assert catcher.errors and "500" in catcher.errors[-1]
    with pytest.raises(CatcherError, match="ProtoMQ on"):
        Catcher(fake_protomq.port, "other_board").start(thread=False)
    assert cat.live("cpr-other_board") is None  # a failed start leaves nothing registered
    fake_protomq.down = False


def test_join_never_creates_or_clears(fake_protomq):
    c = Catcher(fake_protomq.port, BOARD, mode="join").start(thread=False)
    c.poll_once()
    assert CLIENT not in fake_protomq.mailboxes and not c.gaps() and c.tracked is False
    c.stop()
    fake_protomq.mailboxes[CLIENT] = {"inbox": [], "outbox": []}  # someone else tracks it
    publish(fake_protomq, "log", "x")
    c = Catcher(fake_protomq.port, BOARD, mode="join", skip_existing=True).start(thread=False)
    c.poll_once()
    publish(fake_protomq, "log", "y")
    c.poll_once()
    c.stop()
    assert c.payloads("log") == ["y"]
    assert len(fake_protomq.mailboxes[CLIENT]["outbox"]) == 2  # still there, not cleared
    assert "/api/track_deliveries" not in fake_protomq.paths()


def test_echo_is_host_stimulus(fake_protomq, catcher):
    fake_protomq.clients[CLIENT] = {TOPIC + "b2d"}
    catcher.echo("b2d", b"\x00\xffbytes")
    catcher.poll_once()
    host = catcher.records(boxes=("host",))
    assert [(r["topic"], r["form"]) for r in host] == [(TOPIC + "b2d", "hex")]
    inbox = catcher.records(boxes=("inbox",))
    assert inbox[0]["payload"] == "00ff6279746573"  # latin1 round trip, byte for byte
    with pytest.raises(ValueError):
        catcher.echo("results", b"fake result")


def test_disconnect_is_per_client(fake_protomq, catcher):
    fake_protomq.clients = {CLIENT: set(), "cpr-other": set()}
    assert catcher.disconnect() == {"status": "OK"}
    assert list(fake_protomq.clients) == ["cpr-other"]
    assert catcher.disconnect() is None
    assert "/api/reset" not in fake_protomq.paths()


def test_dump_jsonl(fake_protomq, catcher, tmp_path):
    publish(fake_protomq, "results", {"name": "a"})
    publish(fake_protomq, "log", "hello")
    catcher.poll_once()
    path = tmp_path / "out" / (BOARD + ".jsonl")
    assert catcher.dump(path, outcome="passed") == path
    catcher.dump(path)  # appends: one file per board across tests
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert [r["box"] for r in rows] == ["outbox", "outbox", "end"] * 2
    for r in rows:
        assert set(r) >= {"ts", "t", "run_id", "nodeid", "board", "client", "box", "topic",
                          "suffix", "payload", "form"}
        assert r["board"] == BOARD and r["client"] == CLIENT
    end = rows[2]["payload"]
    assert end["records"] == {"outbox": 2} and end["outcome"] == "passed"
    assert rows[0]["payload"] == {"name": "a"} and rows[1]["payload"] == "hello"


# --- playback scripts ---------------------------------------------------------------------------

SCRIPT = {
    "name": "selftest", "protoVersion": "cpr-plain",
    "steps": [
        {"name": "hello", "send": {"text": "hello {board}"}},
        {"name": "on_ping", "trigger": "results.name", "match": {"results": {"name": "ping"}},
         "send": {"json": {"pong": "{run_id}"}}},
        {"name": "after_done", "after": "on_ping", "waitFor": "results.done",
         "topic": "b2d/next", "send": {"hex": "01"}},
        {"name": "off", "enabled": False, "send": "never"},
    ],
}


def test_script_player(fake_protomq, catcher):
    player = catcher.play(SCRIPT)
    assert player.completed.keys() == {"hello"}  # due at once
    publish(fake_protomq, "results", {"name": "other"})
    catcher.poll_once()
    assert "on_ping" not in player.completed
    publish(fake_protomq, "results", {"name": "ping"})
    catcher.poll_once()
    assert "on_ping" in player.completed and "after_done" not in player.completed
    publish(fake_protomq, "results", {"done": True})
    catcher.poll_once()
    assert player.state()["completed"] == ["after_done", "hello", "on_ping"]
    assert player.state()["pending"] == []
    sent = [(r["topic"], r["payload"]) for r in catcher.records(boxes=("host",))]
    assert sent == [(TOPIC + "b2d", "hello " + BOARD), (TOPIC + "b2d", {"pong": "run-1"}),
                    (TOPIC + "b2d/next", "\x01")]  # valid UTF-8, so text
    assert catcher.summary()["script"]["name"] == "selftest"


@pytest.mark.parametrize("change,problem", [
    ({"protoVersion": "v2"}, "protoVersion"),
    ({"steps": []}, "no steps"),
    ({"steps": [{"name": "a", "send": "x", "bogus": 1}]}, "unknown keys bogus"),
    ({"steps": [{"name": "a"}]}, "exactly one of send / response"),
    ({"steps": [{"name": "a", "send": "x", "topic": "results"}]}, "device-only"),
    ({"steps": [{"name": "a", "send": "x", "trigger": "ws-d2b.x"}]}, "dot path"),
    ({"steps": [{"name": "a", "send": "x", "after": "b"}]}, "not an earlier step"),
    ({"steps": [{"name": "a", "send": "x"}, {"name": "a", "send": "y"}]}, "duplicate"),
])
def test_load_script_refuses(change, problem):
    with pytest.raises(ValueError, match=problem):
        cat.load_script(dict(SCRIPT, **change))


def test_shipped_scripts_load():
    import glob  # pylint: disable=import-outside-toplevel
    import os  # pylint: disable=import-outside-toplevel

    paths = glob.glob(os.path.join(cat.SCRIPTS_DIR, "*.json"))
    for p in paths:
        cat.load_script(p)
