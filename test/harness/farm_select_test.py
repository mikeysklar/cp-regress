# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""farm.toml validation (fixtures/farm.py, after Brent's validate_board_entry,
hil_board_utils.py:552 @ cf64e03) and the per-board run_tests / skip_tests selection in the
root conftest.py (his _test_selectors / _matches, conftest.py:130-180 @ cf64e03). Direct
function calls plus one `pytest --list-boards` subprocess, which reads sysfs and nothing else."""
import copy
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from fixtures.farm import (FarmConfigError, normalize_selector, validate_board_entry,
                           validate_farm)

pytestmark = pytest.mark.smoke

HERE = Path(__file__).resolve().parents[2]
TEST_DIR = HERE / "test"

GOOD = {"name": "metro_m4_airlift", "board_id": "metro_m4_airlift_lite", "usb": "3-3.3.1",
        "port": "atmel-samd", "caps": ["native_usb", "uf2", "reset_ok"],
        "power": {"r": 30, "w": 500, "settle": 12}, "wiring": {},
        "flash": {"method": "swd", "probe": "E6647C740349682C", "target": "target/atsame5x.cfg",
                  "speed": 500}}


@pytest.fixture(scope="module")
def root_conftest(pytestconfig):
    """The root conftest.py module pytest already loaded (not a second copy)."""
    for plugin in pytestconfig.pluginmanager.get_plugins():
        if Path(getattr(plugin, "__file__", "") or "/").resolve() == HERE / "conftest.py":
            return plugin
    pytest.fail("root conftest.py is not loaded")
    return None


def entry(**changes):
    e = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


# --- farm.toml ----------------------------------------------------------------------------------


def test_the_real_farm_toml_is_clean(root_conftest):
    cfg, boards, warnings = root_conftest.load_farm(str(HERE / "farm.toml"))
    assert len(boards) == len(cfg["board"]) >= 8
    assert warnings == []
    assert validate_farm(cfg, str(TEST_DIR)) == ([], [])


@pytest.mark.parametrize("changes,problem", [
    ({"name": "metro m4"}, "'name' must be"),
    ({"name": None}, "'name' must be"),
    ({"board_id": ""}, "'board_id' must be set"),
    ({"usb": "/dev/ttyACM0"}, "never a tty or drive"),
    ({"usb": "3-3"}, "'usb' must be the sysfs path"),
    ({"location": {"hub": "1-1.3", "port": 2}}, "Brent's location"),
    ({"tty": "/dev/ttyACM0"}, "tty not allowed"),
    ({"label": "CIRCUITPY"}, "label not allowed"),
    ({"drive": "/dev/sdb1", "mount": "/media/x"}, "drive, mount not allowed"),
    ({"caps": "wifi"}, "'caps' must be a list"),
    ({"wiring": []}, "'wiring' must be a table"),
    ({"power": {"r": 0}}, "power.r must be an integer >= 1"),
    ({"power": {"settle": True}}, "power.settle must be an integer"),
    ({"power": {"sudo": True}}, "power.sudo not allowed"),
    ({"flash": {"method": "jlink"}}, "flash.method must be one of"),
    ({"flash": {"method": "swd", "target": "target/atsame5x.cfg"}}, "needs probe"),
    ({"flash": {"method": "swd", "probe": "P", "target": "t", "bin_base": "134217728"}},
     "flash.bin_base must be a hex string"),
    ({"flash": {"method": "swd", "probe": "P", "target": "t", "speed": 0}},
     "flash.speed must be a positive integer"),
    ({"flash": {"method": "esp_uf2", "chip": "esp32s3"}}, "needs flash_size"),
    ({"run_tests": "hardware/"}, "must be a list of test paths"),
    ({"skip_tests": ["/"]}, "must be a list of test paths"),
    ({"run_tests": ["hardware/no_such_test.py"]}, "names no file or folder"),
])
def test_bad_board_entries(changes, problem):
    with pytest.raises(FarmConfigError, match=problem):
        validate_board_entry(entry(**changes), str(TEST_DIR))


@pytest.mark.parametrize("changes,warning", [
    ({"caps": ["native_usb", "lasers"]}, "unknown caps lasers"),
    ({"power": {"r": 30, "x": 1}}, "unknown power key 'x'"),
    ({"colour": "blue"}, "unknown key 'colour'"),
    ({"flash": dict(GOOD["flash"], chip="esp32s3")}, "unknown key flash.chip for method swd"),
])
def test_board_entry_warnings(changes, warning):
    assert any(warning in w for w in validate_board_entry(entry(**changes)))


@pytest.mark.parametrize("flash_cfg", [
    # The opt-in --flash-erase keys lib/flash.py reads (erase_refusal): never a warning.
    {"method": "uf2", "nuke_uf2": "~/rp/flash_nuke.uf2"},
    dict(GOOD["flash"], app_base="0x4000"),
    dict(GOOD["flash"], fast_speed=2000, rescue_speed=400, connect="reset halt"),
])
def test_flash_keys_lib_flash_reads_are_known(flash_cfg):
    assert validate_board_entry(entry(flash=flash_cfg)) == []


def test_selectors_are_checked_against_test_dir():
    ok = entry(run_tests=["hardware/", "test/hardware/core_vm_test.py"],
               skip_tests=["hardware/core_vm_test.py::test_vid_pid"])
    assert validate_board_entry(ok, str(TEST_DIR)) == []
    assert validate_board_entry(entry(run_tests=["nope/"])) == []  # no test_dir: no path check


def test_farm_level_checks():
    a, b = entry(), entry(name="other", usb="3-3.3.2")
    b["flash"] = dict(b["flash"], probe="OTHER")
    assert validate_farm({"board": [a, b]}) == ([], [])

    errors, _ = validate_farm({"board": [a, entry(usb="3-3.3.2")]})
    assert any("share name 'metro_m4_airlift'" in e for e in errors)
    assert any("share probe 'E6647C740349682C'" in e for e in errors)
    errors, _ = validate_farm({"board": [a, dict(b, usb="3-3.3.1.4")]})
    assert any("sits below board 'metro_m4_airlift'" in e for e in errors)
    errors, _ = validate_farm({"host": {"uhubctl": "sudo uhubctl"}, "board": [a]})
    assert any("never sudo" in e for e in errors)
    assert validate_farm({})[0] == ["farm.toml has no [[board]] entries"]
    # Every bad entry is reported in one go, not just the first.
    errors, _ = validate_farm({"board": [entry(usb="x"), entry(name="n2", board_id="")]})
    assert len(errors) == 2


@pytest.mark.parametrize("raw,norm", [
    ("hardware/", "hardware"),
    ("test/hardware/core_vm_test.py", "hardware/core_vm_test.py"),
    ("./hardware/core_vm_test.py::test_vid_pid", "hardware/core_vm_test.py::test_vid_pid"),
    (" hardware/x_test.py:: ", "hardware/x_test.py"),
])
def test_normalize_selector(raw, norm):
    assert normalize_selector(raw) == norm


# --- run_tests / skip_tests ---------------------------------------------------------------------


@pytest.mark.parametrize("test_id,selectors,hit", [
    ("hardware/core_vm_test.py::test_a[metro_m0]", ["hardware"], True),
    ("hardware/core_vm_test.py::test_a[metro_m0]", ["hardware/core_vm_test.py"], True),
    ("hardware/core_vm_test.py::test_a[metro_m0]", ["hardware/core_vm_test.py::test_a"], True),
    ("hardware/core_vm_test.py::test_ab[metro_m0]", ["hardware/core_vm_test.py::test_a"], False),
    ("hardware/core_vm_test_2.py::test_a", ["hardware/core_vm_test.py"], False),
    ("hardwarex/a_test.py::t", ["hardware"], False),
    ("hardware/a_test.py::t", [], False),
])
def test_matches(root_conftest, test_id, selectors, hit):
    assert root_conftest._matches(test_id, selectors) is hit  # pylint: disable=protected-access


class Item:
    """The parts of a pytest Item that pytest_collection_modifyitems reads and writes."""

    def __init__(self, rel, name, board=None, tier="smoke"):
        self.path = TEST_DIR / rel
        self.nodeid = "test/%s::%s%s" % (rel, name, "[%s]" % board if board else "")
        if board:
            self.callspec = types.SimpleNamespace(params={"board": board})
        self.marks = [tier] if tier else []
        self.skip_reason = None

    def get_closest_marker(self, name):
        return name if name in self.marks else None

    def add_marker(self, marker):
        mark = marker.mark
        self.marks.append(mark.name)
        if mark.name == "skip":
            self.skip_reason = mark.kwargs["reason"]
        if mark.name == "xdist_group":
            self.group = mark.args[0]


def collect(root_conftest, cfg, items, tier="smoke"):
    deselected = []
    config = types.SimpleNamespace(
        getoption=lambda name: tier,
        cpr_selectors=root_conftest.board_selectors(cfg),
        hook=types.SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)))
    root_conftest.pytest_collection_modifyitems(config, items)
    return deselected


def test_run_and_skip_tests_per_board(root_conftest):
    cfg = {"board": [
        {"name": "m0", "run_tests": ["hardware/core_vm_test.py"],
         "skip_tests": ["test/hardware/core_vm_test.py::test_b"]},
        {"name": "rp", "skip_tests": ["hardware/"]},
        {"name": "s3"},
        {"name": "none", "run_tests": []},
    ]}
    items = [Item("hardware/core_vm_test.py", "test_a", b) for b in ("m0", "rp", "s3", "none")]
    items += [Item("hardware/core_vm_test.py", "test_b", "m0"),
              Item("hardware/repl_console_test.py", "test_c", "m0"),
              Item("hardware/repl_console_test.py", "test_c", "s3"),
              Item("unit/guard_test.py", "test_x"),                  # no board: never touched
              Item("hardware/core_vm_test.py", "test_n", "s3", tier="nightly")]
    every = list(items)
    dropped = collect(root_conftest, cfg, items)
    kept = {i.nodeid for i in items}
    assert {i.nodeid for i in dropped} == {
        "test/hardware/core_vm_test.py::test_a[none]",               # run_tests = []
        "test/hardware/repl_console_test.py::test_c[m0]",            # not in m0's run_tests
        "test/hardware/core_vm_test.py::test_n[s3]"}                 # above --tier smoke
    assert kept == {i.nodeid for i in every} - {i.nodeid for i in dropped}
    by_id = {i.nodeid: i for i in items}
    assert by_id["test/hardware/core_vm_test.py::test_b[m0]"].skip_reason == \
        "in skip_tests for board 'm0'"
    assert by_id["test/hardware/core_vm_test.py::test_a[rp]"].skip_reason == \
        "in skip_tests for board 'rp'"
    assert by_id["test/hardware/core_vm_test.py::test_a[m0]"].skip_reason is None
    assert by_id["test/unit/guard_test.py::test_x"].marks == ["smoke"]
    for i in items:
        if hasattr(i, "callspec"):
            assert "hardware" in i.marks and i.group == i.callspec.params["board"]


def test_nightly_tier_keeps_smoke(root_conftest):
    items = [Item("hardware/core_vm_test.py", "t", "s3", tier=t)
             for t in ("smoke", "nightly", "release", None)]
    dropped = collect(root_conftest, {"board": [{"name": "s3"}]}, items, tier="nightly")
    assert [i.nodeid for i in dropped] == ["test/hardware/core_vm_test.py::t[s3]"]
    assert dropped[0].marks == ["release"]
    assert len(items) == 3  # unmarked counts as nightly


def farm_file(tmp_path, *entries):
    lines = ["[host]", 'uhubctl = "uhubctl"', ""]
    for e in entries:
        lines.append("[[board]]")
        for k, v in e.items():
            if isinstance(v, str):
                lines.append('%s = "%s"' % (k, v))
            elif isinstance(v, list):
                lines.append("%s = [%s]" % (k, ", ".join('"%s"' % s for s in v)))
        lines.append("")
    path = tmp_path / "farm.toml"
    path.write_text("\n".join(lines))
    return path


def run_list_boards(farm):
    """--farm=PATH as one token: a bare path argument would make pytest take the tmp dir as
    its rootdir and never load the root conftest.py."""
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "--list-boards", "--farm=%s" % farm], cwd=str(HERE),
                          capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def test_list_boards(tmp_path):
    farm = farm_file(tmp_path,
                     {"name": "m0", "board_id": "metro_m0_express", "usb": "3-3.3.2",
                      "run_tests": ["hardware/core_vm_test.py"],
                      "skip_tests": ["hardware/core_vm_test.py::test_vid_pid"]},
                     {"name": "none", "board_id": "x", "usb": "3-3.3.3", "run_tests": []})
    r = run_list_boards(farm)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "    run_tests:  hardware/core_vm_test.py" in r.stdout
    assert "    skip_tests: hardware/core_vm_test.py::test_vid_pid" in r.stdout
    assert "(empty: every test deselected)" in r.stdout


def test_list_boards_reports_every_problem(tmp_path):
    farm = farm_file(tmp_path,
                     {"name": "a", "board_id": "x", "usb": "/dev/ttyACM0"},
                     {"name": "b", "board_id": "y", "usb": "3-3.2", "tty": "/dev/ttyACM1"})
    r = run_list_boards(farm)
    assert r.returncode == 4, r.stdout + r.stderr  # pytest.UsageError
    assert "never a tty or drive" in r.stderr and "tty not allowed" in r.stderr
