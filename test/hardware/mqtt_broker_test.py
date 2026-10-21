# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""MQTT round trip through the local ProtoMQ broker, the way Brent's WipperSnapper tests work.

New with the be-more-like-brent fixtures; there is no tests/mqtt_broker.py. The board joins
WiFi from the settings.toml that flashed_fs wrote from `secrets`, connects adafruit_minimqtt to
ProtoMQ on bravo, subscribes to its broker-to-device topic and publishes three payloads on its
device-to-broker topic. The host asserts on what ProtoMQ recorded the board sending, byte for
byte, and pushes one message back with /echo that the board must receive.

Topics are cpr/<board>/d2b and cpr/<board>/b2d. Not his <user>/ws-d2b/<uid> layout on purpose:
ProtoMQ decodes everything on a ws-d2b topic as a DeviceToBroker protobuf, and a plain payload
that happens to parse (2000 x "x" does) comes back as {} instead of its bytes.

The failure half is after Brent's hil/firmware/mqtt/test_mqtt.py @ eec4238. His runNetFSM()
makes 5 MQTT connect attempts, prints "MQTT Connection Error" for each, then halts; ours is
minimqtt's connect(connect_retries=5), which retries socket errors without backoff and then
raises MMQTTException "Repeated connect failures" (adafruit_minimqtt.py:436-483, MiniMQTT
8.1.0 in bundle 20261015). MicroPython keeps no __cause__, so DEVICE_GIVES_UP wraps _connect
to record each attempt's error in place of his serial lines.

  test_mqtt_invalid_broker_url   MISSING_BROKER_HOST never resolves: 5 socketpool.gaierror
  test_mqtt_invalid_broker_port  CLOSED_BROKER_PORT on the ProtoMQ host: 5 OSErrors
                                 (refused, or a 1 s timeout if bravo drops the SYN)

Both then connect to the real broker with a fresh client, which his firmware cannot (it
halts), to catch a socket the failed attempts leaked. The bad host and port go straight into
the device code, not a settings.toml scenario: scenario_guard stops the run on any broker
host that is not a private IP. His test_mqtt_unreachable_wdt_reboot is not ported:
CircuitPython has no watchdog unless code.py arms one, so it would test our script, not the
firmware.
"""
import threading
import time

import pytest

from fixtures.broker import ProtoMQ

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("wifi"),
    pytest.mark.destructive,
]

LIBS = ["adafruit_minimqtt", "adafruit_connection_manager", "adafruit_ticks"]

PAYLOADS = [b"hello from cp-regress", bytes(range(256)), b"x" * 2000]
B2D_PAYLOAD = b"\x00\x01b2d-from-protomq\xff"

# His MISSING_BROKER_HOST and CLOSED_BROKER_PORT, conftest.py:419-420 @ 242a162. RFC 2606
# reserves .invalid, so the name can reach no one's broker.
MISSING_BROKER_HOST = "hil-no-such-broker.invalid"
CLOSED_BROKER_PORT = 1
# His runNetFSM() makes 5 attempts; minimqtt's default connect_retries is also 5.
CONNECT_RETRIES = 5
# His MQTT_CONNECT_TIMEOUT is 120 for 5 TLS attempts with 3 s waits. Socket errors get no
# backoff here, so 5 DNS failures or 1 s connect timeouts fit well inside this.
GIVE_UP_TIMEOUT = 60

# Device-side. str.format template: literal braces are doubled. Never call sys.exit() here.
DEVICE = r"""
import os
import time
import wifi
import socketpool
import adafruit_minimqtt.adafruit_minimqtt as MQTT

client_id = {client_id!r}
d2b = {d2b!r}
b2d = {b2d!r}
payloads = {payloads!r}
want_b2d = {b2d_payload!r}

host = os.getenv("CPR_BROKER_HOST")
port = os.getenv("CPR_BROKER_PORT")
check("settings_broker", bool(host) and bool(port), "%s:%s" % (host, port))

try:
    if not wifi.radio.connected:
        wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    check("wifi", wifi.radio.connected, "ip=%s" % wifi.radio.ipv4_address)
except Exception as e:
    check("wifi", False, "%s: %s" % (type(e).__name__, e))

got = []
def on_message(client, topic, message):
    got.append((topic, bytes(message)))

mqtt = None
if wifi.radio.connected:
    try:
        pool = socketpool.SocketPool(wifi.radio)
        mqtt = MQTT.MQTT(broker=host, port=int(port), client_id=client_id, socket_pool=pool,
                         is_ssl=False, use_binary_mode=True, keep_alive=30)
        mqtt.on_message = on_message
        t0 = time.monotonic()
        mqtt.connect()
        metric("connect_ms", (time.monotonic() - t0) * 1000, "ms")
        check("mqtt_connect", True, "%s:%s" % (host, port))
    except Exception as e:
        mqtt = None
        check("mqtt_connect", False, "%s: %s" % (type(e).__name__, e))

if mqtt is not None:
    try:
        mqtt.subscribe(b2d, qos=1)
        check("subscribe", True, b2d)
    except Exception as e:
        check("subscribe", False, "%s: %s" % (type(e).__name__, e))
    sent = 0
    for p in payloads:
        try:
            mqtt.publish(d2b, p, qos=1)
            sent += 1
        except Exception as e:
            check("publish", False, "%s: %s" % (type(e).__name__, e))
            break
    else:
        check("publish", True, "%d payloads" % sent)
    # the host pushes one message through ProtoMQ's /echo once it sees the subscription
    end = time.monotonic() + 20
    while not got and time.monotonic() < end:
        try:
            mqtt.loop(timeout=1)
        except Exception as e:
            check("b2d_received", False, "loop: %s: %s" % (type(e).__name__, e))
            break
    if got:
        check("b2d_received", got[0] == (b2d, want_b2d), repr(got[0])[:120])
    else:
        check("b2d_received", False, "nothing on %s within 20 s" % b2d)
    try:
        mqtt.disconnect()
    except Exception:
        pass
"""


@pytest.fixture(scope="module")
def exchange(board, device, flashed_fs, broker):  # pylint: disable=unused-argument
    """Run the round trip once per board. Returns (DeviceResults, ProtoMQ outbox list)."""
    pmq = ProtoMQ(broker, "cpr-" + board.name)
    d2b = "cpr/%s/d2b" % board.name
    b2d = "cpr/%s/b2d" % board.name
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})
    pmq.track()
    pushed = {}

    def push():
        if pmq.wait_subscribed(b2d, timeout=90):
            time.sleep(0.5)  # the SUBACK reaches the board after the broker records it
            pushed["echo"] = pmq.echo(b2d, B2D_PAYLOAD)

    t = threading.Thread(target=push, daemon=True)
    t.start()
    res = device.run(DEVICE.format(client_id=pmq.client, d2b=d2b, b2d=b2d, payloads=PAYLOADS,
                                   b2d_payload=B2D_PAYLOAD), timeout=120)
    t.join(timeout=5)
    outbox = pmq.outbox(d2b)
    pmq.call("/dump_deliveries", {"client": pmq.client, "clear": True})
    return res, outbox, pushed


def test_device_ran(exchange):
    exchange[0].expect_no_exception()


def test_settings_broker(exchange):
    """flashed_fs wrote CPR_BROKER_HOST / CPR_BROKER_PORT from base_secrets."""
    exchange[0].expect("settings_broker")


def test_wifi(exchange):
    exchange[0].expect("wifi")


def test_mqtt_connect(exchange):
    exchange[0].expect("mqtt_connect")


def test_connect_ms(exchange, metric):
    # MQTT connect over WiFi swings widely between runs (73 to 1041 ms seen on the first four);
    # only flag a blowup against the median.
    metric("connect_ms", exchange[0].metric_record("connect_ms")["value"], "ms", tolerance=4.0)


def test_subscribe(exchange):
    exchange[0].expect("subscribe")


def test_publish(exchange):
    exchange[0].expect("publish")


def test_broker_got_payloads(exchange):
    """ProtoMQ recorded exactly the bytes the board published, in order."""
    exchange[0].expect("publish")
    got = [m["payload"] for m in exchange[1]]
    assert got == PAYLOADS, "broker outbox %r" % [g[:24] if isinstance(g, bytes) else g for g in got]


def test_b2d_received(exchange):
    """A message pushed with ProtoMQ's /echo reached the board's on_message intact."""
    assert exchange[2].get("echo", {}).get("status") == "OK", "host never saw the subscription"
    exchange[0].expect("b2d_received")


# Device-side, his expect_mqtt_gives_up (test_mqtt.py:91 @ 242a162) plus the recovery connect.
# str.format template: literal braces are doubled. Never call sys.exit() here.
DEVICE_GIVES_UP = r"""
import os
import time
import wifi
import socketpool
import adafruit_minimqtt.adafruit_minimqtt as MQTT

client_id = {client_id!r}
bad_host = {bad_host!r}
bad_port = {bad_port!r}
retries = {retries}
give_up_timeout = {give_up_timeout}

host = os.getenv("CPR_BROKER_HOST")
port = int(os.getenv("CPR_BROKER_PORT") or 0)
try:
    if not wifi.radio.connected:
        wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    check("wifi", wifi.radio.connected, "ip=%s" % wifi.radio.ipv4_address)
except Exception as e:
    check("wifi", False, "%s: %s" % (type(e).__name__, e))

if wifi.radio.connected:
    pool = socketpool.SocketPool(wifi.radio)
    mqtt = MQTT.MQTT(broker=bad_host or host, port=bad_port or port, client_id=client_id,
                     socket_pool=pool, is_ssl=False, keep_alive=30, connect_retries=retries)
    # His "MQTT Connection Error: -N" per attempt: connect() drops each cause.
    errors = []
    _connect = mqtt._connect
    def counted(*args, **kwargs):
        try:
            return _connect(*args, **kwargs)
        except Exception as e:
            errors.append((type(e).__name__, getattr(e, "errno", None) or (e.args or [None])[0],
                           isinstance(e, OSError)))
            raise
    mqtt._connect = counted

    t0 = time.monotonic()
    try:
        mqtt.connect()
        check("gives_up", False, "connected to %s:%s" % (bad_host or host, bad_port or port))
    except MQTT.MMQTTException as e:
        check("gives_up", "Repeated connect failures" in str(e), str(e))
    except Exception as e:
        check("gives_up", False, "%s: %s" % (type(e).__name__, e))
    took = time.monotonic() - t0
    metric("give_up_s", took, "s")
    check("gives_up_in_time", took < give_up_timeout, "%.1f s of %d" % (took, give_up_timeout))
    check("attempts", len(errors) == retries, "%d of %d: %s" % (len(errors), retries, errors))
    check("socket_errors", bool(errors) and all(os_err for _, _, os_err in errors), repr(errors))
    check("dns_failed", bool(errors) and all(n == "gaierror" for n, _, _ in errors), repr(errors))
    try:
        mqtt.deinit()
    except Exception:
        pass

    # Not in his test: his firmware halts here. A fresh client must still reach the broker.
    try:
        ok = MQTT.MQTT(broker=host, port=port, client_id=client_id, socket_pool=pool,
                       is_ssl=False, keep_alive=30)
        ok.connect()
        check("recovers", True, "%s:%s" % (host, port))
        ok.disconnect()
    except Exception as e:
        check("recovers", False, "%s: %s" % (type(e).__name__, e))
"""


def _gives_up(board, device, bad_host=None, bad_port=None):
    return device.run(DEVICE_GIVES_UP.format(
        client_id="cpr-%s-fail" % board.name, bad_host=bad_host, bad_port=bad_port,
        retries=CONNECT_RETRIES, give_up_timeout=GIVE_UP_TIMEOUT), timeout=GIVE_UP_TIMEOUT + 60)


def test_mqtt_invalid_broker_url(board, device, flashed_fs, broker):  # pylint: disable=unused-argument
    """His test_mqtt_invalid_broker_url, test_mqtt.py:105 @ 242a162: the host never resolves,
    every attempt fails at DNS, connect() gives up, and the board can still reach the broker."""
    res = _gives_up(board, device, bad_host=MISSING_BROKER_HOST)
    res.expect_no_exception()
    for name in ("wifi", "gives_up", "gives_up_in_time", "attempts", "socket_errors",
                 "dns_failed", "recovers"):
        res.expect(name)


def test_mqtt_invalid_broker_port(board, device, flashed_fs, broker):  # pylint: disable=unused-argument
    """His test_mqtt_invalid_broker_port, test_mqtt.py:137 @ 242a162: nothing listens on the
    ProtoMQ host's CLOSED_BROKER_PORT, so every attempt fails at the socket, connect() gives
    up, and the board can still reach the broker."""
    res = _gives_up(board, device, bad_port=CLOSED_BROKER_PORT)
    res.expect_no_exception()
    for name in ("wifi", "gives_up", "gives_up_in_time", "attempts", "socket_errors",
                 "recovers"):
        res.expect(name)
