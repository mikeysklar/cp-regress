# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""The WiFi SSID and password never reach a report. results/smoke-20261018-0545 had the password
in smoke.out, smoke.xml and smoke.md: a failing write_file()'s arguments are printed, and one was
the bytes of settings.toml. Made-up values only."""
import types

import pytest

from fixtures import secrets as s

pytestmark = pytest.mark.smoke

CFG = {"network": {"ssid_env": "CPR_WIFI_SSID", "password_env": "CPR_WIFI_PASSWORD"}}
ENV = {"CPR_WIFI_SSID": "fake-net", "CPR_WIFI_PASSWORD": "pa'ss\"wörd\\1"}


def test_secret_values_come_from_the_environment_names():
    assert s.secret_values(CFG, ENV) == [ENV["CPR_WIFI_PASSWORD"], "fake-net"]
    assert s.secret_values(CFG, {}) == []
    assert s.secret_values({}, ENV) == []


@pytest.mark.parametrize("shown", [
    lambda v: v,                                          # as is
    lambda v: s.settings_toml({"CIRCUITPY_WIFI_PASSWORD": v}),   # TOML-escaped
    lambda v: repr(v),                                    # str repr
    lambda v: "data = %r" % s.settings_toml({"P": v}).encode(),  # pytest's bytes argument line
])
def test_redact_every_form(shown):
    values = s.secret_values(CFG, ENV)
    text = shown(ENV["CPR_WIFI_PASSWORD"]) + " on " + shown("fake-net")
    out = s.redact(text, values)
    assert "wörd" not in out and "w\\xc3\\xb6rd" not in out and "fake-net" not in out
    assert s.REDACTED in out


class Rep:
    def __init__(self, longrepr, sections=()):
        self.longrepr, self.sections = longrepr, list(sections)


def run_hook(rep, env, monkeypatch):
    monkeypatch.setattr(s.os, "environ", env)
    item = types.SimpleNamespace(config=types.SimpleNamespace(cpr_cfg=CFG))
    gen = s.pytest_runtest_makereport.__wrapped__(item, None) \
        if hasattr(s.pytest_runtest_makereport, "__wrapped__") else \
        s.pytest_runtest_makereport(item, None)
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(types.SimpleNamespace(get_result=lambda: rep))
    return rep


def test_hook_cleans_longrepr_and_sections(monkeypatch):
    pw = ENV["CPR_WIFI_PASSWORD"]
    rep = run_hook(Rep("E  data = %r" % pw.encode(), [("Captured stdout call", "ssid fake-net")]),
                   ENV, monkeypatch)
    assert "wörd" not in rep.longrepr and "\\xc3\\xb6" not in rep.longrepr
    assert rep.sections == [("Captured stdout call", "ssid " + s.REDACTED)]


def test_hook_leaves_skips_and_clean_reports_alone(monkeypatch):
    skip = ("f.py", 3, "Skipped: why")
    assert run_hook(Rep(skip), ENV, monkeypatch).longrepr is skip
    obj = object()
    assert run_hook(Rep(obj), {}, monkeypatch).longrepr is obj   # no secrets set: untouched
