# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""tools/aio_upload.py offline: feeds from junit and metrics, the account guard, the key file,
and the request it would send. Nothing reaches the network."""
import importlib.util
import json
import os
import pathlib

import pytest

pytestmark = pytest.mark.smoke

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("aio_upload", ROOT / "tools" / "aio_upload.py")
aio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aio)

BOARDS = ["metro_esp32s2", "metro_m0"]
KEY = "aio_" + "x" * 28  # shaped like a key, not one

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" timestamp="2026-10-17T07:22:01.5+00:00">
 <testcase classname="test.hardware.a_test" name="test_one[metro_esp32s2]@metro_esp32s2"/>
 <testcase classname="test.hardware.a_test" name="test_one[metro_m0]@metro_m0">
  <failure message="boom"/></testcase>
 <testcase classname="test.hardware.a_test" name="test_two[metro_m0]@metro_m0">
  <error message="setup"/></testcase>
 <testcase classname="test.hardware.a_test" name="test_three[metro_m0]@metro_m0">
  <skipped message="no wifi"/></testcase>
 <testcase classname="test.harness.b_test" name="test_host"/>
</testsuite></testsuites>
"""


@pytest.fixture
def junit(tmp_path):
    p = tmp_path / "run.xml"
    p.write_text(JUNIT)
    return str(p)


@pytest.fixture
def farm(tmp_path):
    p = tmp_path / "farm.toml"
    p.write_text("".join('[[board]]\nname = "%s"\nusb = "1-1"\n' % b for b in BOARDS))
    return str(p)


@pytest.fixture
def creds(tmp_path):
    def make(username="mikeysklar", key=KEY, mode=0o600):
        p = tmp_path / "cpr-aio.env"
        p.write_text("CPR_AIO_USERNAME=%s\nCPR_AIO_KEY=%s\n" % (username, key))
        os.chmod(p, mode)
        return str(p)
    return make


def test_summary_counts_and_per_board(junit):
    assert aio.summary([junit], BOARDS) == [
        ("passed", 2), ("failed", 1), ("errors", 1),
        ("fail-metro-esp32s2", 0), ("fail-metro-m0", 2)]


def test_metric_takes_latest_value_from_this_run(tmp_path):
    m = tmp_path / "metrics.jsonl"
    rows = [("2026-10-16T07:20:29", 1.0), ("2026-10-17T07:30:00", 2.0),
            ("2026-10-17T07:40:00", 3.0)]
    m.write_text("".join(json.dumps({"ts": t, "board": "metro_m0", "metric": "perf.x",
                                     "value": v}) + "\n" for t, v in rows))
    feeds, missing = aio.metric_values(["perf.x@metro_m0", "perf.y@metro_m0"],
                                       "2026-10-17T07:22:01", str(m))
    assert feeds == [("perf-x-metro-m0", 3.0)]
    assert missing == ["perf.y@metro_m0"]


def test_metric_from_an_older_run_is_not_sent(tmp_path):
    m = tmp_path / "metrics.jsonl"
    m.write_text(json.dumps({"ts": "2026-10-16T07:20:29", "board": "metro_m0",
                             "metric": "perf.x", "value": 1.0}) + "\n")
    feeds, missing = aio.metric_values(["perf.x@metro_m0"], "2026-10-17T07:22:01", str(m))
    assert feeds == [] and missing == ["perf.x@metro_m0"]


@pytest.mark.parametrize("user", ["brentru", "", "MikeySklar", "mikeysklar2"])
def test_only_mikeys_account(user):
    assert aio.check_account(user)


def test_mikeys_account_allowed():
    assert aio.check_account("mikeysklar") == []


def test_send_refuses_another_account(junit, farm, creds, monkeypatch):
    monkeypatch.setattr(aio, "post", lambda *a, **k: pytest.fail("posted"))
    with pytest.raises(SystemExit, match="not 'mikeysklar'"):
        aio.main(["--junit", junit, "--farm", farm, "--creds", creds("brentru"), "--send"])


def test_send_refuses_a_world_readable_key_file(junit, farm, creds, monkeypatch):
    monkeypatch.setattr(aio, "post", lambda *a, **k: pytest.fail("posted"))
    with pytest.raises(SystemExit, match="chmod 600"):
        aio.main(["--junit", junit, "--farm", farm, "--creds", creds(mode=0o644), "--send"])


def test_dry_run_needs_no_key_and_sends_nothing(junit, farm, monkeypatch, capsys):
    monkeypatch.setattr(aio, "post", lambda *a, **k: pytest.fail("posted"))
    assert aio.main(["--junit", junit, "--farm", farm, "--creds", "/nonexistent"]) == 0
    assert "not sent" in capsys.readouterr().out


def test_too_many_feeds_is_refused_not_trimmed(junit, farm):
    with pytest.raises(SystemExit, match="more than --max-feeds 4"):
        aio.main(["--junit", junit, "--farm", farm, "--max-feeds", "4"])


def test_post_request(junit, farm, creds, monkeypatch, capsys):
    sent = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        sent["url"], sent["key"] = req.full_url, req.get_header("X-aio-key")
        sent["body"] = json.loads(req.data)
        return Resp()

    real = aio.post
    monkeypatch.setattr(aio, "post", lambda u, k, f: real(u, k, f, opener=opener))
    assert aio.main(["--junit", junit, "--farm", farm, "--creds", creds(), "--send"]) == 0
    assert sent["url"] == "https://io.adafruit.com/api/v2/mikeysklar/groups/cp-regress/data"
    assert sent["key"] == KEY
    assert {"key": "fail-metro-m0", "value": 2} in sent["body"]["feeds"]
    assert KEY not in capsys.readouterr().out
