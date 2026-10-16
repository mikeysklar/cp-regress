# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Device side of the cp-regress MQTT catcher (CircuitPython, runs on the board).

A test module puts this file on CIRCUITPY through flashed_fs and lists the MQTT libraries:

    FS = {"lib/cpr_catcher.py": catcher_helper()}   # from fixtures.broker
    LIBS = ["adafruit_minimqtt", "adafruit_connection_manager", "adafruit_ticks"]

and its device code publishes result dicts that the host's `catcher` fixture catches:

    from cpr_catcher import connect, Reporter
    mqtt = connect({client_id!r})
    rep = Reporter(mqtt, {board!r}, run_id={run_id!r}, nodeid={nodeid!r})
    rep.result("adc_ok", True, value=812)
    rep.log("sampled 64 points")

Topics are cpr/<board>/results and cpr/<board>/log on the farm's ProtoMQ (CPR_BROKER_HOST and
CPR_BROKER_PORT in settings.toml, written by flashed_fs from base_secrets). Never Adafruit IO:
connect() refuses an adafruit.com / adafruit.io host, and nothing here knows an IO topic.
"""
import os
import time

try:
    import json
except ImportError:  # no json on the smallest builds
    json = None

RESULTS = "results"
LOG = "log"


def _enc(v):
    """A tiny JSON encoder for builds without json (same rules as lib/devtest.py)."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return "{" + ", ".join(_enc(str(k)) + ": " + _enc(x) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_enc(x) for x in v) + "]"
    out = '"'
    for c in str(v):
        o = ord(c)
        if c == '"' or c == "\\":
            out += "\\" + c
        elif o < 32:
            out += "\\u%04x" % o
        else:
            out += c
    return out + '"'


def dumps(obj):
    if json is not None:
        try:
            return json.dumps(obj)
        except (TypeError, ValueError):
            pass
    return _enc(obj)


def connect(client_id, host=None, port=None, keep_alive=30, socket_pool=None):
    """Join WiFi from settings.toml if needed and connect adafruit_minimqtt to the farm broker.
    Returns the connected MQTT client (binary mode: on_message gets bytes)."""
    import adafruit_minimqtt.adafruit_minimqtt as MQTT  # pylint: disable=import-outside-toplevel
    import socketpool  # pylint: disable=import-outside-toplevel
    import wifi  # pylint: disable=import-outside-toplevel

    host = host or os.getenv("CPR_BROKER_HOST")
    port = int(port or os.getenv("CPR_BROKER_PORT") or 1884)
    if not host:
        raise RuntimeError("no CPR_BROKER_HOST in settings.toml")
    h = host.lower().rstrip(".")
    if h.endswith("adafruit.com") or h.endswith("adafruit.io"):
        raise RuntimeError("refusing %s: CircuitPython results never go to Adafruit IO" % host)
    if not wifi.radio.connected:
        wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    pool = socket_pool or socketpool.SocketPool(wifi.radio)
    mqtt = MQTT.MQTT(broker=host, port=port, client_id=client_id, socket_pool=pool,
                     is_ssl=False, use_binary_mode=True, keep_alive=keep_alive)
    mqtt.connect()
    return mqtt


class Reporter:
    """Publishes JSON dicts on cpr/<board>/<suffix>. Every message carries v, kind, seq, t_ms,
    board, run_id and nodeid, so the host can spot loss (seq) and stale runs (run_id).

    Publishing never raises: a failure returns False and is kept in .errors, so one dropped
    connection cannot kill the device code. The host's expect() then reports what is missing.
    """

    def __init__(self, mqtt, board, run_id="", nodeid="", qos=1):
        if "/" in board or "+" in board or "#" in board:
            raise ValueError("bad board name %r" % board)
        self.mqtt = mqtt
        self.board = board
        self.prefix = "cpr/%s/" % board
        self.run_id = run_id
        self.nodeid = nodeid
        self.qos = qos
        self.seq = 0
        self.errors = []

    def publish(self, suffix, obj):
        """One message on cpr/<board>/<suffix>. obj is a dict; the envelope keys are added."""
        if "+" in suffix or "#" in suffix or suffix.startswith("/"):
            raise ValueError("bad topic suffix %r" % suffix)
        msg = {"v": 1, "seq": self.seq, "t_ms": int(time.monotonic() * 1000),
               "board": self.board, "run_id": self.run_id, "nodeid": self.nodeid}
        msg.update(obj)
        self.seq += 1
        try:
            self.mqtt.publish(self.prefix + suffix, dumps(msg).encode(), qos=self.qos)
            return True
        except Exception as e:
            self.errors.append("%s: %s: %s" % (suffix, type(e).__name__, e))
            return False

    def result(self, name, ok, detail="", value=None, **extra):
        """A test result, the same fields devtest.check() prints: name, ok, detail, value."""
        obj = {"kind": "result", "name": name, "ok": ok, "detail": detail, "value": value}
        obj.update(extra)
        self.publish(RESULTS, obj)
        return ok

    def log(self, msg, level="info"):
        return self.publish(LOG, {"kind": "log", "level": level, "msg": msg})
