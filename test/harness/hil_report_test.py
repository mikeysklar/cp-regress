# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""tools/hil_report.py on a made-up run: junit, a serial dump that crosses midnight, catcher
records. Checks the hil-lib.sh semantics it ports (PR #930): the window runs from boot to just
after the LAST evidence line, the protomq window covers the same wall-clock span, and a log
that is missing is said so."""
import datetime
import importlib.util
import json
import os
import pathlib

import pytest

from fixtures.console import log_name

pytestmark = pytest.mark.smoke

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hil_report", ROOT / "tools" / "hil_report.py")
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)

BOARDS = ["metro_esp32s2", "metro_esp32s3", "metro_m0"]
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
 <testcase classname="test.hardware.mqtt_catcher_test" name="test_echo[metro_esp32s3]" time="3.2"/>
 <testcase classname="test.hardware.mqtt_catcher_test" name="test_echo[metro_esp32s2]" time="9.1">
  <failure message="CatcherTimeout: no cpr/metro_esp32s2/results">trace line</failure></testcase>
 <testcase classname="test.hardware.settings_scenarios_test" name="test_scenario[metro_esp32s3-good]@metro_esp32s3" time="1"/>
 <testcase classname="test.hardware.net_wifi_test" name="test_ssid_found[metro_m0]" time="0">
  <skipped message="board lacks wifi"/></testcase>
</testsuite></testsuites>
"""
FAIL_ID = "test/hardware/mqtt_catcher_test.py::test_echo[metro_esp32s2]"


def at(day, h, m, s, ms=0):
    return datetime.datetime.combine(day, datetime.time(h, m, s, ms * 1000)).timestamp()


@pytest.fixture
def run(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT)
    today = datetime.date(2026, 10, 17)
    yday = today - datetime.timedelta(days=1)
    serial = tmp_path / "serial" / "metro_esp32s2" / log_name(FAIL_ID)
    serial.parent.mkdir(parents=True)
    serial.write_text("\n".join([
        "[23:59:58.000] --- cpr: hard_reset",
        "[23:59:59.500] Adafruit CircuitPython 11.0.0-alpha.1",
        "[00:00:01.000] code.py output:",
        "[00:00:02.000] Error: first",
        "[00:00:03.000] Error: last",
        "[00:00:04.000] after 1",
        "[00:00:30.000] much later",
    ]) + "\n")
    mtime = at(today, 0, 0, 31)
    os.utime(serial, (mtime, mtime))
    cat = tmp_path / "catcher"
    cat.mkdir()
    recs = [
        {"t": at(yday, 23, 59, 59, 600), "ts": "a", "box": "in", "topic": "cpr/s2/log",
         "payload": "booted", "nodeid": FAIL_ID + "@metro_esp32s2"},
        {"t": at(today, 0, 0, 25), "ts": "b", "box": "in", "topic": "cpr/s2/log",
         "payload": "outside", "nodeid": FAIL_ID},
        {"t": at(today, 0, 0, 2), "ts": "c", "box": "in", "topic": "cpr/s3/log",
         "payload": "other test", "nodeid": "x::y"},
    ]
    (cat / "metro_esp32s2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return tmp_path, yday


def test_junit_maps_boards_and_outcomes(run):
    tmp, _ = run
    cases = hr.read_junit(str(tmp / "junit.xml"), BOARDS)
    assert [(c["board"], c["outcome"]) for c in cases] == [
        ("metro_esp32s3", "passed"), ("metro_esp32s2", "failed"), ("metro_esp32s3", "passed"),
        ("metro_m0", "skipped")]
    assert cases[1]["nodeid"] == FAIL_ID
    assert cases[2]["test"] == "test_scenario[good]"  # board out of the row label, @ stripped
    assert cases[2]["nodeid"].endswith("test_scenario[metro_esp32s3-good]")


def test_serial_dates_cross_midnight(run):
    tmp, yday = run
    lines = hr.serial_lines(str(tmp / "serial" / "metro_esp32s2" / log_name(FAIL_ID)))
    assert lines[0][0] == at(yday, 23, 59, 58)
    assert lines[2][0] == at(yday + datetime.timedelta(days=1), 0, 0, 1)
    assert [t for t, _ in lines] == sorted(t for t, _ in lines)


def test_window_is_boot_to_after_last_hit(run, monkeypatch):
    tmp, _ = run
    monkeypatch.setattr(hr, "PROOF_AFTER", 1)
    lines = hr.serial_lines(str(tmp / "serial" / "metro_esp32s2" / log_name(FAIL_ID)))
    win, t_a, t_b, matched = hr.proof_window(lines, r"Error")
    assert matched and [l for _, l in win][0].endswith("hard_reset")
    assert [l for _, l in win][-1].endswith("after 1")  # last hit + 1, not the first hit
    assert t_b - t_a == pytest.approx(6.0)


def test_report_aligns_protomq_and_flags_missing_logs(run, monkeypatch):
    tmp, _ = run
    monkeypatch.setattr(hr, "PROOF_AFTER", 1)
    cases = hr.read_junit(str(tmp / "junit.xml"), BOARDS)
    md = hr.report(cases, str(tmp / "serial"), hr.catcher_records(str(tmp / "catcher")),
                   r"Error", False, "t", "pytest x")
    assert "2 passed · 1 failed · 0 errors · 1 skipped" in md
    assert "| `test_echo` | ❌ | ✅ |  |" in md
    assert "### ❌ `test_echo[metro_esp32s2]` on `metro_esp32s2`" in md
    assert "booted" in md          # inside the serial span, xdist @board suffix stripped
    assert "outside" not in md     # same test, outside the span
    assert "other test" not in md  # another test's record
    assert "aligned to the serial window" in md
    assert "CatcherTimeout" in md
    assert "test_echo[metro_esp32s3]` on" not in md  # passes need --all


def test_missing_serial_log_is_said(run):
    tmp, _ = run
    cases = hr.read_junit(str(tmp / "junit.xml"), BOARDS)
    md = hr.report(cases, str(tmp / "nowhere"), {}, r"Error", True, "t", "")
    assert "`serial.log` not captured" in md
    assert "test_echo[metro_esp32s3]` on" in md  # --all shows passes


def test_main_exit_status(run):
    tmp, _ = run
    out = tmp / "comment.md"
    rc = hr.main(["--junit", str(tmp / "junit.xml"), "--serial-dir", str(tmp / "serial"),
                  "--catcher-dir", str(tmp / "catcher"), "-o", str(out)])
    assert rc == 1 and out.read_text().startswith("## 🔌")
