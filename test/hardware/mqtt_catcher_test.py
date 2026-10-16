# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""The MQTT catcher end to end: a board publishes result dicts and log lines, the host catches
them per board through the local ProtoMQ.

New with the catcher; there is no tests/catcher.py. flashed_fs puts tests/lib/cpr_catcher.py on
CIRCUITPY. The board joins WiFi from settings.toml, connects adafruit_minimqtt to ProtoMQ on
bravo and publishes on cpr/<board>/results and cpr/<board>/log. The host asserts on what the
catcher caught: order, payload bytes, the run id / nodeid envelope, no loss, nothing outside the
board's own prefix, and the jsonl it writes to results/catcher/<board>.jsonl.

test_stimulus_script then plays protomq-scripts/cpr-ack.json: the host answers the board's
"catcher_ready" result on cpr/<board>/b2d, waits for its "ack_received", and sends "bye".

Nothing here goes to Adafruit IO: the topics are cpr/..., the broker is the farm's ProtoMQ, and
the prod guard has already refused any public broker address.
"""
import json
import os

import pytest

import catcher as catcherlib
from fixtures.broker import catcher_helper, catching

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb", "wifi"),
    pytest.mark.destructive,
]

LIBS = ["adafruit_minimqtt", "adafruit_connection_manager", "adafruit_ticks"]
FS = {"lib/cpr_catcher.py": catcher_helper()}

VALUE = {"nested": [1, 2.5, None, True], "text": "café ✓"}
DETAIL = 'quotes " and \\ and a\ttab'
BURST = 20
LOGS = [("catcher log line one", "info"), ("catcher log line two", "warning")]

# Device-side. str.format template: literal braces are doubled. Never call sys.exit() here.
DEVICE_PUBLISH = r"""
import time
from cpr_catcher import connect, Reporter

cfg = {cfg}
mqtt = None
try:
    t0 = time.monotonic()
    mqtt = connect(cfg["client_id"])
    metric("connect_ms", (time.monotonic() - t0) * 1000, "ms")
    check("mqtt_connect", True, cfg["client_id"])
except Exception as e:
    check("mqtt_connect", False, "%s: %s" % (type(e).__name__, e))

if mqtt is not None:
    rep = Reporter(mqtt, cfg["board"], run_id=cfg["run_id"], nodeid=cfg["nodeid"])
    rep.result("catcher_hello", True, "first result")
    rep.log({log0!r}, level={lvl0!r})
    rep.result("catcher_value", True, detail={detail}, value={value})
    rep.log({log1!r}, level={lvl1!r})
    for i in range({burst}):
        rep.result("catcher_burst", True, value=i)
    rep.result("catcher_done", True, value=rep.seq + 1)
    check("published", not rep.errors, "; ".join(rep.errors)[:200] or "%d messages" % rep.seq)
    try:
        mqtt.disconnect()
    except Exception:
        pass
"""

DEVICE_STIMULUS = r"""
import json
import time
from cpr_catcher import connect, Reporter

cfg = {cfg}
got = []
def on_message(client, topic, message):
    got.append((topic, bytes(message)))

mqtt = None
try:
    mqtt = connect(cfg["client_id"])
    mqtt.on_message = on_message
    mqtt.subscribe("cpr/%s/b2d" % cfg["board"], qos=1)
    check("subscribe", True, cfg["client_id"])
except Exception as e:
    check("subscribe", False, "%s: %s" % (type(e).__name__, e))

if mqtt is not None:
    rep = Reporter(mqtt, cfg["board"], run_id=cfg["run_id"], nodeid=cfg["nodeid"])
    rep.result("catcher_ready", True)
    done = False
    end = time.monotonic() + 30
    while not done and time.monotonic() < end:
        try:
            mqtt.loop(timeout=1)
        except Exception as e:
            check("loop", False, "%s: %s" % (type(e).__name__, e))
            break
        while got:
            topic, raw = got.pop(0)
            try:
                cmd = json.loads(raw.decode())
            except ValueError:
                rep.log("unparsed b2d %r" % raw, level="error")
                continue
            if cmd.get("cmd") == "ack":
                rep.result("ack_received", True, value=cmd.get("run_id"))
            elif cmd.get("cmd") == "bye":
                rep.result("bye_received", True)
                done = True
    check("stimulus_done", done, "ack and bye" if done else "no bye within 30 s")
    try:
        mqtt.disconnect()
    except Exception:
        pass
"""


def _device_cfg(cat):
    # ascii() so the device source stays 7-bit; CircuitPython reads the \\u escapes back
    return ascii(cat.device_config())


@pytest.fixture(scope="module")
def published(request, board, flashed_fs, device, broker):  # pylint: disable=unused-argument
    """Run DEVICE_PUBLISH once per board. Returns (DeviceResults, the stopped Catcher)."""
    with catching(request, broker, board) as cat:
        res = device.run(DEVICE_PUBLISH.format(cfg=_device_cfg(cat), value=ascii(VALUE),
                                               detail=ascii(DETAIL), burst=BURST,
                                               log0=LOGS[0][0], lvl0=LOGS[0][1],
                                               log1=LOGS[1][0], lvl1=LOGS[1][1]), timeout=120)
        try:  # QoS 1 publishes are acked before the device run ends; give the poll a moment
            cat.expect("results", {"name": "catcher_done"}, timeout=5)
        except catcherlib.CatcherTimeout:
            pass  # the tests below say what is missing
    cat.rewind()  # the tests below read from the first record
    return res, cat


def _results(cat, name=None):
    return [p for p in cat.payloads("results") if name is None or p.get("name") == name]


def test_device_ran(published):
    published[0].expect_no_exception()


def test_mqtt_connect(published):
    published[0].expect("mqtt_connect")


def test_connect_ms(published, metric):
    # MQTT connect over WiFi swings widely between runs; only flag a blowup against the median
    metric("connect_ms", published[0].metric_record("connect_ms")["value"], "ms", tolerance=4.0)


def test_published(published):
    """The device saw no publish error, and no caught result reports a failure."""
    published[0].expect("published")
    published[1].assert_none("results", {"ok": False})


def test_expect_order(published):
    """expect() walks forward like Brent's SerialMonitor.expect: hello, value, done, in order."""
    cat = published[1]
    cat.expect("results", {"name": "catcher_hello"}, timeout=0)
    cat.expect("results", lambda p: p["name"] == "catcher_value", timeout=0)
    cat.expect("results", {"name": "catcher_done", "ok": True}, timeout=0)
    with pytest.raises(catcherlib.CatcherTimeout):
        cat.expect("results", {"name": "catcher_hello"}, timeout=0)  # already passed


def test_payload_roundtrip(published):
    """Nested values, unicode and escapes arrive exactly as the board sent them."""
    rs = _results(published[1], "catcher_value")
    assert rs, "no catcher_value result caught"
    assert rs[0]["value"] == VALUE
    assert rs[0]["detail"] == DETAIL


def test_logs_caught(published):
    got = [(p.get("msg"), p.get("level")) for p in published[1].payloads("log")]
    assert got == LOGS


def test_envelope(published, board):
    """Every message carries this run's id, this module's nodeid and the board name."""
    cat = published[1]
    msgs = cat.payloads("results") + cat.payloads("log")
    assert msgs, "nothing caught"
    bad = [m for m in msgs if (m.get("v"), m.get("run_id"), m.get("nodeid"), m.get("board"))
           != (1, cat.run_id, cat.nodeid, board.name)]
    assert not bad, "wrong envelope: %r" % bad[:2]


def test_no_loss(published):
    """seq is contiguous from 0 to the count catcher_done reported. QoS 1 may repeat one."""
    cat = published[1]
    done = _results(cat, "catcher_done")
    assert done, "no catcher_done result caught"
    n = done[-1]["value"]
    seqs = [m["seq"] for m in cat.payloads("results") + cat.payloads("log")]
    missing = sorted(set(range(n)) - set(seqs))
    assert not missing, "lost seq %s of %d" % (missing[:20], n)
    assert len(_results(cat, "catcher_burst")) >= BURST


def test_only_own_topics(published, board):
    """The board published nowhere but cpr/<board>/, and nothing on a WipperSnapper topic."""
    cat = published[1]
    outside = [r["topic"] for r in cat.records(boxes=("outbox",)) if r["suffix"] is None]
    assert not outside, "published outside cpr/%s/: %s" % (board.name, outside)
    assert not [r for r in cat.records() if r["topic"] and "/ws-" in r["topic"]]


def test_no_gaps(published):
    """Nobody else re-tracked or reset the board's mailbox while it was caught."""
    cat = published[1]
    assert not cat.gaps(), "mailbox gaps: %r" % cat.gaps()
    assert not cat.errors, "ProtoMQ API errors: %s" % cat.errors[-3:]


def test_jsonl(published, tmp_path):
    """dump() writes one JSON line per record plus an "end" summary, all tagged with run and
    node; the fixture already appended the same to results/catcher/<board>.jsonl."""
    cat = published[1]
    path = cat.dump(str(tmp_path / "caught.jsonl"))
    with open(path) as f:
        lines = [json.loads(line) for line in f]
    assert lines[-1]["box"] == "end"
    assert lines[-1]["payload"]["records"].get("outbox", 0) >= BURST + 4
    for r in lines:
        assert r["ts"] and r["run_id"] == cat.run_id and r["nodeid"] == cat.nodeid
        assert r["board"] == cat.board
    real = os.path.join(catcherlib.RESULTS_DIR, cat.board + ".jsonl")
    with open(real) as f:
        mine = [json.loads(line) for line in f if cat.run_id in line]
    assert any(r["box"] == "end" and r["nodeid"] == cat.nodeid for r in mine)


def test_stimulus_script(board, flashed_fs, device, catcher):  # pylint: disable=unused-argument
    """protomq-scripts/cpr-ack.json drives the board over cpr/<board>/b2d: the host's ack carries
    the run id back, then bye ends the device loop."""
    player = catcher.play("cpr-ack")
    res = device.run(DEVICE_STIMULUS.format(cfg=_device_cfg(catcher)), timeout=90)
    res.expect_no_exception()
    res.expect("subscribe")
    ack = catcher.expect("results", {"name": "ack_received"}, timeout=10)
    assert ack["payload"]["value"] == catcher.run_id
    catcher.expect("results", {"name": "bye_received"}, timeout=10)
    assert sorted(player.completed) == ["ack", "bye"], player.state()
    sent = catcher.records("b2d", boxes=("host",))
    assert [r["payload"].get("cmd") for r in sent] == ["ack", "bye"]
    res.expect("stimulus_done")
