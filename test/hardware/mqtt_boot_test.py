# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""The board reaches the local ProtoMQ broker on its own after a real power-on.

After Brent's test_mqtt_client_connected_to_protomq, hil/firmware/mqtt/test_mqtt.py:61 @
242a162. His firmware connects at boot and prints its client id; the test power-cycles the hub
port with confirm_off, checks the broker does not list the board yet, and waits for it there.

Ours puts BOOT_CODE in code.py. It connects adafruit_minimqtt to ProtoMQ as
cpr-boot-<board_id> only when supervisor.runtime.run_reason is STARTUP, so the auto-reload from
writing it and any REPL reload leave the board off the broker, and a client seen after the
power cycle is from the cold boot. The flashed_fs reset for settings.toml is also a STARTUP, so
the test drops any session left from it before cutting power. Everything is checked from the
broker side; the host never opens the console.

Boards on a hub that does not cut VBUS (3-6.3, 3-6) or on a battery fail at the power cycle,
which is what confirm_off is for.
"""
import time

import pytest

import farmlib
from fixtures.broker import ProtoMQ

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("wifi"),
    pytest.mark.destructive,
]

LIBS = ["adafruit_minimqtt", "adafruit_connection_manager", "adafruit_ticks"]

# WiFi join plus MQTT connect after wait_ready() returns; his WIFI_CONNECT_TIMEOUT and
# MQTT_CONNECT_TIMEOUT are 150 and 120 s for the whole boot.
CONNECT_TIMEOUT = 90

BOOT_CODE = r"""
import os
import board
import socketpool
import supervisor
import wifi
import adafruit_minimqtt.adafruit_minimqtt as MQTT

if supervisor.runtime.run_reason == supervisor.RunReason.STARTUP:
    if not wifi.radio.connected:
        wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    mqtt = MQTT.MQTT(broker=os.getenv("CPR_BROKER_HOST"), port=int(os.getenv("CPR_BROKER_PORT")),
                     client_id="cpr-boot-" + board.board_id,
                     socket_pool=socketpool.SocketPool(wifi.radio), is_ssl=False, keep_alive=30)
    mqtt.connect()
    print("cpr-boot: connected")
    while True:
        mqtt.loop(timeout=1)
else:
    print("cpr-boot: idle,", supervisor.runtime.run_reason)
"""

FS = {"code.py": BOOT_CODE}


@pytest.fixture
def boot_client(board, broker):
    """ProtoMQ for the client id BOOT_CODE connects as. Disconnected at teardown, which ends
    BOOT_CODE's loop, so the board is idle for the next module."""
    pmq = ProtoMQ(broker, "cpr-boot-" + board.board_id)
    yield pmq
    if pmq.connected():
        pmq.disconnect()


def test_mqtt_client_connected_after_power_on(board, flashed_fs, boot_client):  # pylint: disable=unused-argument
    # --- Arrange ------------------------------------------------------
    # 1. flashed_fs put BOOT_CODE in code.py. Drop any session from an earlier STARTUP, so a
    #    client listed later can only be from the boot below.
    if boot_client.connected():
        boot_client.disconnect()
    assert boot_client.connected() is None, "%s is already on the broker" % boot_client.client

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the hub port; the board must leave the bus while it is off.
    try:
        ready = board.power_cycle(confirm_off=True)
    except farmlib.FarmError as e:
        pytest.fail(str(e), pytrace=False)
    assert ready is not None, "%s did not come back after the power cycle" % board.name
    t0 = time.time()

    # --- Assert -------------------------------------------------------
    # 3. BOOT_CODE joins WiFi and connects; ProtoMQ lists the board as a client.
    boot_client.expect_client(timeout=CONNECT_TIMEOUT)
    print("%s on the broker %.1f s after the drive came back" % (boot_client.client,
                                                                 time.time() - t0))
