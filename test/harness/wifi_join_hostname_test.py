# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""wifi_join_test's hostname device code on CPython, against a radio whose setter applies
hostname_valid()'s rules (shared-bindings/wifi/Radio.c:21-54)."""
import pathlib
import types

import pytest

pytestmark = pytest.mark.smoke

SRC = (pathlib.Path(__file__).resolve().parents[2] / "test" / "hardware" /
       "wifi_join_test.py").read_text()
BLOCK = SRC[SRC.index("def test_hostname():"):]
BLOCK = BLOCK[:BLOCK.index("\n\n", BLOCK.index('check("hostname_default_valid"'))]


def _valid(h):
    return all(0 < len(p) < 64 and p[0] != "-" and all(c.isalnum() or c == "-" for c in p)
               for p in h.split("."))


class Radio:
    def __init__(self, hostname):
        self._h = hostname

    @property
    def hostname(self):
        return self._h

    @hostname.setter
    def hostname(self, value):
        if not _valid(value):
            raise ValueError("invalid hostname")
        self._h = value


def run(default):
    out = {}

    def guard(name, fn):
        try:
            return fn()
        except Exception as e:  # pylint: disable=broad-except
            out[name] = (False, "%s: %s" % (type(e).__name__, e))
            return None

    g = {"wifi": types.SimpleNamespace(radio=Radio(default)), "guard": guard,
         "check": lambda name, ok, detail="", **k: out.__setitem__(name, (ok, detail))}
    exec(BLOCK, g)  # pylint: disable=exec-used
    return out


def test_espressif_default_settable_but_reported_invalid():
    """bravo 2026-10-17: the setter works, and the underscore default is named, not hidden."""
    out = run("cpy-metro_esp32s3-14c19f2fad04")
    assert "hostname_test" not in out
    assert out["hostname_settable"][0] is True
    assert out["hostname_default_valid"] == (
        False, "default cpy-metro_esp32s3-14c19f2fad04: '_' is not allowed")


def test_valid_default_passes():
    out = run("cpy-metro-esp32s3-14c19f2fad04")
    assert out["hostname_settable"][0] is True and out["hostname_default_valid"][0] is True


@pytest.mark.parametrize("bad, why", [("-cpy", "starts with a dash"), ("a..b", "is 0 long"),
                                      ("x" * 64, "is 64 long")])
def test_other_invalid_defaults_are_named(bad, why):
    assert why in run(bad)["hostname_default_valid"][1]
