# SPDX-FileCopyrightText: 2026 Vladimír Smitka
# SPDX-License-Identifier: MIT-0
"""A CP Desktop Lab as a farm board (lib/desklab.py): its farm.toml entry, and with CPR_LAB_BIN
set (the lab program, github.com/MakerClassCZ/cp-desktop-lab releases) one real start, health
check, reset and power cycle on a drive in tmp_path."""
import copy
import os
import shutil
import time

import pytest

import desklab
import farmlib
import run
from fixtures.farm import FarmConfigError, validate_board_entry

pytestmark = pytest.mark.smoke

LAB = {"name": "desklab", "board_id": "virtual", "usb": "lab-1", "port": "desktop",
       "caps": ["reset_ok"], "power": {"settle": 0},
       "lab": {"bin": "~/lab/circuitpython", "drive": "~/lab/CIRCUITPY"}}


def test_lab_entry_is_valid():
    assert validate_board_entry(copy.deepcopy(LAB)) == []


@pytest.mark.parametrize("change", [
    {"usb": "/dev/ttyACM0"},
    {"lab": {"bin": "x"}},
    {"lab": {"drive": "d", "args": "--fast-time"}},
    {"flash": {"method": "uf2"}},
    {"fs": "repl"},
])
def test_bad_lab_entries(change):
    entry = dict(copy.deepcopy(LAB), **change)
    with pytest.raises(FarmConfigError):
        validate_board_entry(entry)


def test_make_board():
    assert isinstance(farmlib.make_board(copy.deepcopy(LAB), {}), desklab.LabBoard)
    real = {"name": "m4", "board_id": "x", "usb": "3-3.3.1"}
    b = farmlib.make_board(real, {})
    assert type(b) is farmlib.Board and b.on_hub


def test_lab_that_does_not_start(tmp_path):
    cfg = copy.deepcopy(LAB)
    cfg["lab"] = {"bin": shutil.which("false"), "drive": str(tmp_path / "CIRCUITPY")}
    b = desklab.LabBoard(cfg, {})
    try:
        with pytest.raises(farmlib.FarmError, match="does not start"):
            b.present()
    finally:
        b.stop()


@pytest.mark.skipif(not os.environ.get("CPR_LAB_BIN"), reason="CPR_LAB_BIN not set")
def test_lab_board_lifecycle(tmp_path):
    program = tmp_path / "circuitpython"
    shutil.copy(os.environ["CPR_LAB_BIN"], program)
    cfg = copy.deepcopy(LAB)
    cfg["lab"] = {"bin": str(program), "drive": str(tmp_path / "CIRCUITPY")}
    b = desklab.LabBoard(cfg, {})
    try:
        assert b.present() and b.tty()
        keeper = b._file("keeper.pid")
        first_keeper = open(keeper).read()
        os.utime(program, ns=(time.time_ns(), time.time_ns() + 10**9))
        assert b.present() and open(keeper).read() != first_keeper, "a new program, a new lab"
        assert "Board ID:virtual" in b.boot_out()
        assert run.board_health(b) == (True, "")
        first = b.devnum()
        b.repl_reset()
        end = time.time() + 10
        while b.devnum() in (None, first) and time.time() < end:
            time.sleep(0.1)
        assert b.devnum() not in (None, first), "microcontroller.reset() gives a new boot"
        assert run.board_health(b) == (True, "")
        before = b.devnum()
        assert b.power_cycle() is not None
        assert b.devnum() not in (None, before)
        with b.preserved():
            b.write_file("code.py", "print('cpr')\n")
        assert run.board_health(b) == (True, "")
    finally:
        b.stop()
    assert not b._keeper_alive()
