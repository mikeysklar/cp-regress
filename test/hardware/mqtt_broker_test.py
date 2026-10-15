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
"""
import threading
import time

import pytest

from fixtures.broker import ProtoMQ

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb", "wifi"),
    pytest.mark.destructive,
]

LIBS = ["adafruit_minimqtt", "adafruit_connection_manager", "adafruit_ticks"]

PAYLOADS = [b"hello from cp-regress", bytes(range(256)), b"x" * 2000]
B2D_PAYLOAD = b"\x00\x01b2d-from-protomq\xff"

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
