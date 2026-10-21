# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""power.tries: recover() and Device.run() give a board that does not always come up (the
Feather STM32F405's crystal load caps) more than one power cycle, and only that board."""
from contextlib import contextmanager

import pytest

import run as cpr_run
from fixtures.device import Device
from fixtures.farm import FarmConfigError, validate_board_entry

pytestmark = pytest.mark.smoke


class FlakyBoard:
    """Comes up healthy only on the power cycles listed in `good` (1-based). Its device runs
    fail with OSError until a power cycle in `good` has happened."""

    def __init__(self, tries=None, good=(), runs_fail=0):
        self.name = "flaky"
        self.power = {"settle": 0}
        if tries is not None:
            self.power["tries"] = tries
        self.good, self.cycles, self.runs_fail, self.runs = set(good), 0, runs_fail, 0

    def power_cycle(self):
        self.cycles += 1

    @contextmanager
    def repl(self):
        yield self

    def run_device(self, body, timeout):  # pylint: disable=unused-argument
        self.runs += 1
        if self.runs <= self.runs_fail:
            raise OSError("device reports readiness to read but returned no data")
        return [{"kind": "check", "name": "ok", "ok": True}], "", ""


@pytest.fixture
def health(monkeypatch):
    """board_health answers from the board's power-cycle count."""
    monkeypatch.setattr(cpr_run, "board_health",
                        lambda b: (b.cycles in b.good, "" if b.cycles in b.good else "no tty"))


def test_recover_default_is_one_power_cycle(health):  # pylint: disable=unused-argument
    b = FlakyBoard(good={2})
    assert cpr_run.recover(b) == (False, "no tty")
    assert b.cycles == 1


def test_recover_spends_its_tries(health):  # pylint: disable=unused-argument
    b = FlakyBoard(tries=3, good={3})
    ok, why = cpr_run.recover(b)
    assert ok and b.cycles == 3
    assert why.startswith("healthy after 3 power cycles")


def test_recover_gives_up_after_its_tries(health):  # pylint: disable=unused-argument
    b = FlakyBoard(tries=3, good={4})
    ok, why = cpr_run.recover(b)
    assert not ok and b.cycles == 3 and why.count("no tty") == 3


def test_device_run_without_tries_fails_once(health):  # pylint: disable=unused-argument
    b = FlakyBoard(good={1}, runs_fail=1)
    res = Device(b).run("x")
    assert res.crashed and b.runs == 1 and b.cycles == 0


def test_device_run_retries_after_a_power_cycle(health, capsys):  # pylint: disable=unused-argument
    b = FlakyBoard(tries=3, good={1}, runs_fail=1)
    res = Device(b).run("x")
    assert not res.crashed and b.runs == 2 and b.cycles == 1
    assert len(res.retried) == 1 and "OSError" in res.retried[0]
    assert "running again" in capsys.readouterr().out
    res.expect("ok")


def test_device_run_stops_at_tries(health):  # pylint: disable=unused-argument
    b = FlakyBoard(tries=3, good={1, 2, 3}, runs_fail=5)
    res = Device(b).run("x")
    assert res.crashed and b.runs == 3 and b.cycles == 2
    assert "after 2 earlier attempts" in res.crashed
    with pytest.raises(AssertionError, match=r"^DNF \(did not finish\): device run failed"):
        res.expect_no_exception()
    with pytest.raises(pytest.skip.Exception, match=r"^DNF \(did not finish\): not reached"):
        res.expect("ok")


def test_device_run_stops_when_recovery_fails(health):  # pylint: disable=unused-argument
    b = FlakyBoard(tries=3, good=(), runs_fail=1)  # power.tries counts cycles inside recover too
    res = Device(b).run("x")
    assert res.crashed and b.runs == 1 and "power cycle: no tty" in res.crashed


class SwdBoard(FlakyBoard):
    """A FlakyBoard with a probe; `swd_good` says whether the SWD reset brings it back."""

    def __init__(self, swd_good, **kw):
        super().__init__(**kw)
        self.flash = {"method": "swd"}
        self.swd_good, self.swd_resets = swd_good, 0


@pytest.fixture
def swd(monkeypatch):
    import flash as cpr_flash  # pylint: disable=import-outside-toplevel

    def fake_reset(b, log=None):  # pylint: disable=unused-argument
        b.swd_resets += 1
        if b.swd_good:
            b.good.add(b.cycles)
        return True
    monkeypatch.setattr(cpr_flash, "swd_reset", fake_reset)


def test_recover_falls_back_to_an_swd_reset(health, swd):  # pylint: disable=unused-argument
    b = SwdBoard(swd_good=True)
    ok, why = cpr_run.recover(b)
    assert ok and b.cycles == 1 and b.swd_resets == 1
    assert why.startswith("healthy after an SWD reset")


def test_recover_reports_a_failed_swd_reset(health, swd):  # pylint: disable=unused-argument
    b = SwdBoard(swd_good=False)
    ok, why = cpr_run.recover(b)
    assert not ok and b.swd_resets == 1 and "after an SWD reset: no tty" in why


def test_recover_needs_no_swd_reset_when_power_works(health, swd):  # pylint: disable=unused-argument
    b = SwdBoard(swd_good=True, good={1})
    assert cpr_run.recover(b) == (True, "")
    assert b.swd_resets == 0


@pytest.mark.parametrize("value", [0, -1, True, "3"])
def test_tries_must_be_a_positive_integer(value):
    entry = {"name": "b", "board_id": "x", "usb": "3-3.2", "port": "stm", "caps": [],
             "power": {"tries": value}}
    with pytest.raises(FarmConfigError, match="power.tries"):
        validate_board_entry(entry)


def test_recover_adds_the_board_settings_first(health):  # pylint: disable=unused-argument
    b = FlakyBoard(good={1})
    b.caps = set()
    b.settings = {"CIRCUITPY_SAFE_MODE_DELAY": 0}
    b.add_missing_settings = lambda: ["CIRCUITPY_SAFE_MODE_DELAY"]
    ok, why = cpr_run.recover(b)
    assert ok and b.cycles == 1
    assert why == "healthy after 1 power cycles (added CIRCUITPY_SAFE_MODE_DELAY to settings.toml)"


def test_add_missing_settings_only_adds(monkeypatch):
    import farmlib  # pylint: disable=import-outside-toplevel
    b = farmlib.Board({"name": "s31", "board_id": "x", "usb": "3-6.4.2", "fs": "repl",
                       "settings": {"CIRCUITPY_SAFE_MODE_DELAY": 0, "A": "x", "B": True}}, {})
    files = {"settings.toml": b'A = "keep"'}
    monkeypatch.setattr(b, "read_file", lambda rel, binary=False: files.get(rel))
    monkeypatch.setattr(b, "write_file", lambda rel, data, sync=True: files.__setitem__(rel, data))
    assert b.add_missing_settings() == ["B", "CIRCUITPY_SAFE_MODE_DELAY"]
    assert files["settings.toml"] == b'A = "keep"\nB = true\nCIRCUITPY_SAFE_MODE_DELAY = 0\n'
    assert b.add_missing_settings() == []


def test_recover_resets_over_the_repl_after_adding_settings(health):  # pylint: disable=unused-argument
    b = FlakyBoard(good={0})  # healthy once reset, with no power cycle
    b.caps = {"reset_ok"}
    b.settings = {"CIRCUITPY_SAFE_MODE_DELAY": 0}
    b.add_missing_settings = lambda: ["CIRCUITPY_SAFE_MODE_DELAY"]
    b.repl_reset = lambda: None
    ok, why = cpr_run.recover(b)
    assert ok and b.cycles == 0 and why.startswith("healthy after a REPL reset")


def test_recover_tries_an_rts_reset_first(health):  # pylint: disable=unused-argument
    b = FlakyBoard(good={0})
    b.reset = "rts"
    b.line_reset = lambda: None
    ok, why = cpr_run.recover(b)
    assert ok and b.cycles == 0 and why == "healthy after an RTS reset"
