# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""tools/brent_sync.py offline: the docs/BRENT-SYNC.md tables, how a changed upstream file or
symbol is mapped to the cp-regress file that ports it, symbol fingerprints, citations and
--record, with a fake source in place of gh api / git. Nothing is fetched."""
import types

import pytest

import tools.brent_sync as bs

pytestmark = pytest.mark.smoke

DOC = """# Brent sync

## Last synced

| Key | Repo | Branch | SHA | Synced | Watch |
|---|---|---|---|---|---|
| brent | brentru/x | add-hil-ci-cd | aaaaaaa | 2026-10-01 | `hil/* !*.pyc` |

text between tables ends nothing; a second table under the same heading is ignored:

| Key | Repo |
|---|---|
| ignored | ignored |

## Mapping

| Repo | Brent | Line | Ours | Status | Notes |
|---|---|---|---|---|---|
| brent | `hil/utils.py::SerialMonitor` | 10 | `lib/serialmon.py::SerialMonitor` | ported | |
| brent | `hil/utils.py::get_port` | 3 | `lib/farmlib.py` | equivalent | |
| brent | `hil/utils.py` | - | `lib/farmlib.py` | watch | everything else in it |
| brent | `hil/tests/*.py` | - | `test/harness/` | watch | |
| brent | `-` | - | concept row | equivalent | |

## Deliberately not ported

| Repo | Brent | What | Why |
|---|---|---|---|
| brent | `hil/utils.py::run_uhubctl` | sudo -n uhubctl | never sudo |
| brent | `hil/utils.py::SerialMonitor.board_reset` | DTR/RTS reset | native USB |
"""

OLD = '''"""utils"""
import os

TIMEOUT = 5
lower_case = 1


def get_port(board):
    return board  # resolved


class SerialMonitor:
    BAUD = 115200

    def expect(self, pattern):
        return pattern

    def board_reset(self):
        pass


def run_uhubctl(sudo=True):
    pass
'''

# Comments and formatting change nothing; expect's body, get_port's position, a new function
# and a removed one do.
NEW = '''"""utils, reformatted"""
import os

TIMEOUT = 5  # seconds
lower_case = 2


def helper():
    pass


def get_port(board):
    return (board)


class SerialMonitor:
    BAUD = 115200

    def expect(self, pattern, timeout=10):
        return pattern

    def board_reset(self):
        pass
'''


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "BRENT-SYNC.md"
    p.write_text(DOC)
    return p


def test_read_tables(doc):
    t = bs.read_tables(doc.read_text())
    assert set(t) == {bs.SEC_SYNC, bs.SEC_MAP, bs.SEC_SKIP}
    assert [r["key"] for r in t[bs.SEC_SYNC]] == ["brent"]  # first table only
    row = t[bs.SEC_MAP][0]
    assert row["brent"] == "hil/utils.py::SerialMonitor"     # backticks stripped
    assert doc.read_text().splitlines()[row["_line"]].startswith("| brent | `hil/utils.py::Se")


def test_load_doc(doc):
    _, repos, rules = bs.load_doc(str(doc))
    assert repos["brent"]["watch"] == ["hil/*"] and repos["brent"]["ignore"] == ["*.pyc"]
    assert len(rules) == 6  # the `-` concept row maps no symbol
    assert {r["kind"] for r in rules} == {"map", "skip"}


@pytest.mark.parametrize("broken,problem", [
    (DOC.replace("## Deliberately not ported", "## Something else"), "no '## Deliberately"),
    (DOC.replace("| brent | brentru/x |", "| brent | |"), "need Key, Repo, Branch and SHA"),
    (DOC.replace("| brent | `hil/tests/*.py`", "| nobody | `hil/tests/*.py`"), "repo 'nobody'"),
])
def test_load_doc_refuses(tmp_path, broken, problem):
    p = tmp_path / "bad.md"
    p.write_text(broken)
    with pytest.raises(bs.SyncError, match=problem):
        bs.load_doc(str(p))


def test_the_real_doc_loads_and_points_at_real_files():
    _, repos, rules = bs.load_doc()
    assert {"brent", "protomq", "protobuf"} <= set(repos)
    missing = {p for r in rules for p in bs.missing_targets(r)}
    assert not missing, "BRENT-SYNC.md names files that do not exist: %s" % sorted(missing)


def test_watched():
    repo = {"watch": ["hil/*", ".github/*"], "ignore": ["*/__pycache__/*", "*.pyc"]}
    assert bs.watched(repo, "hil/firmware/conftest.py")
    assert not bs.watched(repo, "hil/__pycache__/x.pyc")
    assert not bs.watched(repo, "src/Wippersnapper.cpp")


def test_lookup_most_specific_wins(doc):
    _, _, rules = bs.load_doc(str(doc))

    def ours(key):
        r = bs.lookup(rules, "brent", key)
        return None if r is None else (r["kind"], r["key"])

    assert ours("hil/utils.py::SerialMonitor") == ("map", "hil/utils.py::SerialMonitor")
    assert ours("hil/utils.py::SerialMonitor.expect") == ("map", "hil/utils.py::SerialMonitor")
    # An exact not-ported row beats the broader class row.
    assert ours("hil/utils.py::SerialMonitor.board_reset") == (
        "skip", "hil/utils.py::SerialMonitor.board_reset")
    assert ours("hil/utils.py::run_uhubctl") == ("skip", "hil/utils.py::run_uhubctl")
    assert ours("hil/utils.py::helper") == ("map", "hil/utils.py")      # file-level row
    assert ours("hil/tests/sub/test_x.py::test_a") == ("map", "hil/tests/*.py")  # * crosses /
    assert ours("hil/other.py::x") is None
    assert bs.lookup(rules, "protomq", "hil/utils.py") is None
    assert bs.loose(bs.lookup(rules, "brent", "hil/utils.py::helper"), "hil/utils.py::helper")
    assert not bs.loose(bs.lookup(rules, "brent", "hil/utils.py::get_port"),
                        "hil/utils.py::get_port")


def test_rule_matches():
    assert bs.rule_matches("hil/x.py::Cls", "hil/x.py::Cls.method")
    assert not bs.rule_matches("hil/x.py::Cls", "hil/x.py::Class")
    assert not bs.rule_matches("hil/x.py::Cls", "hil/x.py")
    assert bs.rule_matches("hil/x.py::test_*", "hil/x.py::test_power")


def test_ours_paths():
    assert bs.ours_paths({"ours": "`lib/flash.py::flash`, test/fixtures/farm.py; `-`"}) == [
        "lib/flash.py", "test/fixtures/farm.py"]
    assert bs.ours_paths({"ours": "farm.toml [[board]] flash"}) == ["farm.toml"]
    assert bs.ours_paths({"ours": "concept row"}) == []


def test_py_symbols_fingerprints():
    old, new = bs.py_symbols(OLD), bs.py_symbols(NEW)
    assert set(old) == {"TIMEOUT", "get_port", "SerialMonitor", "SerialMonitor.expect",
                        "SerialMonitor.board_reset", "run_uhubctl"}
    assert old["TIMEOUT"][2] == new["TIMEOUT"][2]          # a comment is not a change
    assert old["get_port"][2] == new["get_port"][2]        # nor are redundant parentheses
    assert old["SerialMonitor"][2] == new["SerialMonitor"][2]  # methods are their own symbols
    assert old["SerialMonitor.expect"][2] != new["SerialMonitor.expect"][2]
    assert bs.diff_symbols(old, new) == [("+", "helper", 8), ("~", "SerialMonitor.expect", 19),
                                         ("-", "run_uhubctl", 22)]


def test_symbols_by_file_type():
    js = (b"router.post('/echo', h)\n/* router.get('/draft', h) */\n"
          b"// router.delete('/old', h)\nexport default async function run() {}\n")
    assert set(bs.symbols("api/routes.js", js)) == {"POST /echo", "run"}
    proto = b"syntax = \"proto3\";\nmessage Foo {}\n  enum Bar { A = 0; }\nservice Baz {}\n"
    assert {k: v[0] for k, v in bs.symbols("x.proto", proto).items()} == {
        "Foo": 2, "Bar": 3, "Baz": 4}
    js_syms = bs.symbols("hil/hil-boards.json", b'[{"name": "a", "chip": "esp32"}]')
    assert set(js_syms) == {"name", "chip"}
    assert bs.symbols("x.py", b"def broken(:\n") is None
    assert bs.symbols("x.cpp", b"int main;") is None
    assert bs.symbols("x.py", None) == {}


def test_symbol_at_prefers_the_method():
    syms = bs.py_symbols(OLD)
    assert bs.symbol_at(syms, syms["SerialMonitor.expect"][0] + 1) == "SerialMonitor.expect"
    assert bs.symbol_at(syms, syms["SerialMonitor"][0]) == "SerialMonitor"
    assert bs.symbol_at(syms, 2) is None


@pytest.mark.parametrize("text,hit", [
    ("after Brent's X, hil_board_utils.py:836 @ 242a162", ("hil_board_utils.py", "836")),
    ("(hil/firmware/conftest.py:252-366 @ cf64e03)", ("hil/firmware/conftest.py", "252")),
    ("api/echo.js:12 @ 9e2e1bf", ("api/echo.js", "12")),
    ("supervisor/shared/safe_mode.c:153 @ 29e9b60", None),  # not one of his file types
    ("hil_board_utils.py:757 (no sha)", None),
])
def test_citations(text, hit):
    m = bs.CITE.search(text)
    assert (m.group(1), m.group(2)) == hit if hit else m is None


class FakeSource:
    """gh api / git in memory: one changed file between two shas."""

    def __init__(self, files):
        self.files_ = files  # {sha: {path: text}}

    def head(self):
        return "bbbbbbb"

    def resolve(self, rev):
        return rev

    def commits(self, base, head):
        return [{"sha": head, "msg": "change utils"}], 1

    def dropped(self, base, head):
        return []

    def truncated(self, base, head):
        return False

    def files(self, base, head):
        return [("M", "hil/utils.py", None), ("A", "hil/new.py", None),
                ("M", "src/Wippersnapper.cpp", None)]

    def show(self, sha, path):
        text = self.files_.get(sha, {}).get(path)
        return None if text is None else text.encode()


def test_check_repo_maps_every_change(doc, monkeypatch):
    _, repos, rules = bs.load_doc(str(doc))
    src = FakeSource({"aaaaaaa": {"hil/utils.py": OLD},
                      "bbbbbbb": {"hil/utils.py": NEW, "hil/new.py": "def fresh():\n    pass\n"}})
    monkeypatch.setattr(bs, "open_source", lambda repo, args: src)
    args = types.SimpleNamespace(head={}, since={})
    cites = [("lib/serialmon.py", 3, "utils.py", 16, "aaaaaaa"),  # inside expect: changed
             ("lib/farmlib.py", 9, "utils.py", 8, "aaaaaaa")]    # get_port: moved
    res, _ = bs.check_repo(repos["brent"], rules, args, cites)
    assert res["other"] == [("M", "src/Wippersnapper.cpp")]  # outside the watch list
    by_path = {e["path"]: e for e in res["files"]}
    ops = {(s["op"], s["name"]): bs.describe(s["rule"])
           for s in by_path["hil/utils.py"]["symbols"]}
    assert ops == {("+", "helper"): "-> lib/farmlib.py  [watch]",
                   ("~", "SerialMonitor.expect"): "-> lib/serialmon.py::SerialMonitor  [ported]",
                   ("-", "run_uhubctl"): "not ported: sudo -n uhubctl"}
    # hil/new.py matches no row: its new function and the file are unmapped.
    assert res["unmapped"] == 1 and res["new_loose"] == 1
    assert sorted(c[4:6] for c in res["cites"]) == [("SerialMonitor.expect", "changed"),
                                                    ("get_port", "moved")]
    new_line = {c[4]: c[6] for c in res["cites"]}
    # The cited line follows the symbol to its new place, keeping its offset into it.
    assert new_line == {"get_port": 12, "SerialMonitor.expect": 20}

    # --record moves the sha and the exact rows' Line cells, keeping the table's shape.
    res["lines"] = {("hil/utils.py", "SerialMonitor"): 17, ("hil/utils.py", "get_port"): 12}
    text = bs.record(doc.read_text(), repos, {"brent": res}, "2026-10-16")
    t = bs.read_tables(text)
    assert (t[bs.SEC_SYNC][0]["sha"], t[bs.SEC_SYNC][0]["synced"]) == ("bbbbbbb", "2026-10-16")
    assert [r["line"] for r in t[bs.SEC_MAP]] == ["17", "12", "-", "-", "-"]
    assert len(text.splitlines()) == len(DOC.splitlines())
    assert "`hil/utils.py::SerialMonitor`" in text
