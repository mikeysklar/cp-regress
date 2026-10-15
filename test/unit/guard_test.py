# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Host-only checks for the prod guard, settings.toml rendering and the UF2 splitter."""
import struct

import pytest

import flash
from fixtures.secrets import prod_guard, settings_toml

pytestmark = pytest.mark.smoke

CFG = {"network": {"allowed_ssids": ["farmnet"]}}


def test_guard_passes_farm():
    assert prod_guard(CFG, {"CPR_WIFI_SSID": "farmnet"}, {"CIRCUITPY_WIFI_SSID": "farmnet"},
                      "192.168.0.76") == []


def test_guard_blocks_ssid():
    assert prod_guard(CFG, {}, {"CIRCUITPY_WIFI_SSID": "office"}, "192.168.0.76")


def test_guard_blocks_aio_name():
    assert prod_guard(CFG, {"ADAFRUIT_AIO_KEY": "x"}, {}, "10.0.0.2")


def test_guard_blocks_aio_value():
    assert prod_guard(CFG, {"SOMETHING": "aio_" + "a" * 28}, {}, "10.0.0.2")


def test_guard_blocks_public_broker():
    assert prod_guard(CFG, {}, {}, "52.1.2.3")


def test_settings_toml_stable_and_quoted():
    text = settings_toml({"B": 'x"y', "A": 1884})
    assert text == 'A = 1884\nB = "x\\"y"\n'


def _block(addr, data, flags=0):
    hdr = struct.pack("<8I", 0x0A324655, 0x9E5D5157, flags, addr, len(data), 0, 1, 0)
    return hdr + data.ljust(476, b"\0") + struct.pack("<I", 0x0AB16F30)


def test_uf2_runs_split_on_gap(tmp_path):
    p = tmp_path / "x.uf2"
    p.write_bytes(_block(0x27000, b"b" * 256) + _block(0x26000, b"a" * 256)
                  + _block(0x27100, b"c" * 256) + _block(0x0, b"z" * 256, flags=0x1000))
    runs = flash.uf2_runs(str(p))
    assert [(a, len(d)) for a, d in runs] == [(0x26000, 256), (0x27000, 512)]
