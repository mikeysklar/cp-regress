# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""The device code of test/hardware/net_wifi_test.py, run in CPython against a fake `wifi`
module, so each check is seen to fail when it should before it goes near a board.

The fake follows shared-bindings/wifi/Radio.c @ 2d299b0 only as far as these tests use it:
connect() to an SSID no scan returns raises ConnectionError("No network with that ssid").
"""
import importlib.util
import json
import pathlib
import sys
import types

import pytest

pytestmark = pytest.mark.smoke

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEVTEST = (ROOT / "lib" / "devtest.py").read_text()
FARM_SSID = "farmnet"
FARM_PASSWORD = "cpr-selftest-pass-0123"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "net_wifi_hw", ROOT / "test" / "hardware" / "net_wifi_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NW = _load_module()


class Net:
    def __init__(self, ssid, rssi=-50, channel=6):
        self.ssid, self.rssi, self.channel = ssid, rssi, channel


class Radio:
    def __init__(self, nets, connected=False, not_found_exc=None, auth_exc=None):
        self.nets = nets
        self.connected = connected
        self.enabled = True
        self.not_found_exc = not_found_exc
        self.auth_exc = auth_exc
        self.ssid = FARM_SSID if connected else None
        self.stale = False  # espressif: event bits still say joined until STA_DISCONNECTED lands
        self.joined_bits = None

    def stop_station(self):
        if self.stale:
            self.joined_bits = self.ssid
        self.connected, self.ssid = False, None

    def settle(self):
        self.joined_bits = None

    def start_scanning_networks(self):
        return iter(self.nets)

    def stop_scanning_networks(self):
        pass

    def connect(self, ssid, password):
        if self.joined_bits == ssid:
            return  # Radio.c:364-371: "already connected", password never tried
        if not any(n.ssid == ssid for n in self.nets):
            self.connected = False
            if self.not_found_exc is None:
                return  # a firmware that forgets to raise
            raise self.not_found_exc
        if password != FARM_PASSWORD:
            self.connected = False
            if self.auth_exc is None:
                return
            raise self.auth_exc
        self.connected, self.ssid = True, ssid

    @property
    def ap_info(self):
        return Net(self.ssid) if self.connected else None

    @property
    def ipv4_address(self):
        return "192.168.0.50" if self.connected else None

    @property
    def ipv4_gateway(self):
        return "192.168.0.1" if self.connected else None


def run(code, radio, monkeypatch, capsys):
    """Exec devtest + code with `wifi` faked and time.sleep a no-op. Returns name -> ok."""
    monkeypatch.setitem(sys.modules, "wifi", types.SimpleNamespace(radio=radio))
    monkeypatch.setattr("time.sleep", lambda s: radio.settle())
    exec(compile(DEVTEST + "\n" + code, "<device>", "exec"), {"__name__": "__device__"})
    out = {}
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("@@R "):
            r = json.loads(line[4:])
            if r.get("kind") != "metric":
                out[r["name"]] = r["ok"]
    return out


def scan(ssid=FARM_SSID):
    return NW.DEVICE_SCAN.format(ssid=ssid)


def test_scan_finds_farm_ssid(monkeypatch, capsys):
    radio = Radio([Net("other", -80, 1), Net(FARM_SSID, -55, 11)])
    r = run(scan(), radio, monkeypatch, capsys)
    assert r == {"scan_networks": True, "ssid_found": True, "rssi_plausible": True,
                 "channel_plausible": True}


def test_scan_without_farm_ssid_fails(monkeypatch, capsys):
    r = run(scan(), Radio([Net("other")]), monkeypatch, capsys)
    assert r["ssid_found"] is False and "rssi_plausible" not in r


def test_scan_rejects_zero_rssi(monkeypatch, capsys):
    r = run(scan(), Radio([Net(FARM_SSID, 0, 6)]), monkeypatch, capsys)
    assert r["rssi_plausible"] is False


def test_connected_passes_when_supervisor_joined(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], connected=True)
    r = run(NW.DEVICE_CONNECTED.format(ssid=FARM_SSID, timeout=1), radio, monkeypatch, capsys)
    assert all(r.values()) and set(r) == {"connected", "joined_farm_ssid", "ipv4_address_set",
                                          "ipv4_gateway_set"}


def test_connected_fails_when_never_joined(monkeypatch, capsys):
    clock = iter(range(1000))
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    r = run(NW.DEVICE_CONNECTED.format(ssid=FARM_SSID, timeout=3), Radio([Net(FARM_SSID)]),
            monkeypatch, capsys)
    assert r == {"connected": False}


def not_found(timeout=20):
    return NW.DEVICE_NOT_FOUND.format(ssid=FARM_SSID, password=FARM_PASSWORD,
                                      missing=NW.MISSING_SSID,
                                      missing_password=NW.MISSING_PASSWORD, timeout=timeout)


def test_not_found_passes_on_the_documented_error(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], connected=True,
                  not_found_exc=ConnectionError("No network with that ssid"))
    r = run(not_found(), radio, monkeypatch, capsys)
    assert all(r.values()), r
    assert r["rejoined"] is True and "rejoin" not in r


def test_not_found_fails_when_connect_returns(monkeypatch, capsys):
    r = run(not_found(), Radio([Net(FARM_SSID)], connected=True), monkeypatch, capsys)
    assert r["not_found_raises"] is False


def test_not_found_fails_on_another_message(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], not_found_exc=ConnectionError("Unknown failure 205"))
    r = run(not_found(), radio, monkeypatch, capsys)
    assert r["not_found_connection_error"] is True and r["not_found_message"] is False


def test_not_found_fails_when_slow(monkeypatch, capsys):
    clock = iter([0.0, 25.0] + [26.0] * 10)
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    radio = Radio([Net(FARM_SSID)], not_found_exc=ConnectionError("No network with that ssid"))
    r = run(not_found(timeout=20), radio, monkeypatch, capsys)
    assert r["not_found_in_time"] is False


def bad_password(timeout=20):
    return NW.DEVICE_BAD_PASSWORD.format(ssid=FARM_SSID, password=FARM_PASSWORD,
                                         wrong=NW.WRONG_WIFI_PASSWORD, timeout=timeout)


def test_bad_password_passes_on_the_documented_error(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], connected=True,
                  auth_exc=ConnectionError("Authentication failure"))
    r = run(bad_password(), radio, monkeypatch, capsys)
    assert all(r.values()), r
    assert r["rejoined"] is True


def test_bad_password_fails_on_unknown_failure(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], connected=True,
                  auth_exc=ConnectionError("Unknown failure 204"))
    r = run(bad_password(), radio, monkeypatch, capsys)
    assert r["bad_password_connection_error"] is True and r["bad_password_message"] is False


def test_bad_password_fails_when_connect_returns(monkeypatch, capsys):
    r = run(bad_password(), Radio([Net(FARM_SSID)], connected=True), monkeypatch, capsys)
    assert r["bad_password_raises"] is False


def test_bad_password_waits_for_the_disconnect_event(monkeypatch, capsys):
    """The S3 on bravo (2026-10-17): connect() straight after stop_station() returned at once."""
    radio = Radio([Net(FARM_SSID)], connected=True,
                  auth_exc=ConnectionError("Authentication failure"))
    radio.stale = True
    assert all(run(bad_password(), radio, monkeypatch, capsys).values())


def test_bad_password_without_the_wait_is_caught(monkeypatch, capsys):
    """Test the test: the old code (no sleep after stop_station) fails, as on the S3."""
    radio = Radio([Net(FARM_SSID)], connected=True,
                  auth_exc=ConnectionError("Authentication failure"))
    radio.stale = True
    code = bad_password().replace("    time.sleep(2)\n", "", 1)
    assert "time.sleep(2)" not in code
    assert run(code, radio, monkeypatch, capsys)["bad_password_raises"] is False


def test_device_code_never_carries_the_password_into_detail(monkeypatch, capsys):
    radio = Radio([Net(FARM_SSID)], not_found_exc=ConnectionError("No network with that ssid"))
    monkeypatch.setitem(sys.modules, "wifi", types.SimpleNamespace(radio=radio))
    exec(compile(DEVTEST + "\n" + not_found(), "<device>", "exec"), {"__name__": "__device__"})
    out = capsys.readouterr().out
    assert FARM_PASSWORD not in out and NW.MISSING_PASSWORD not in out
