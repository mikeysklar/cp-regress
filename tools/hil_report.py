#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Markdown report of one pytest run, in the WipperSnapper HIL proof format.

After append_proof in tyeth's .github/workflows/hil-lib.sh (adafruit/Adafruit_Wippersnapper_Arduino
PR #930, hil-test-additions) and append_comment in .github/scripts/hil_display_test.py (on
brentru add-hil-ci-cd @ 90cd5af): a result table, then per test a collapsible serial.log window
from boot to just after the evidence line, and a protomq window over the SAME wall-clock span,
so the broker traffic lines up with the console instead of drifting. A log that was not
captured is said so, never silently dropped.

Ours differs in what feeds it. The serial windows come from the run's --serial-log-dir (Brent's
"[HH:MM:SS.mmm] text" dump format, test/fixtures/console.py), the broker side from the catcher's
results/catcher/<board>.jsonl records for the same test id, and the verdicts from --junitxml.
Eight boards share one run, so the table is board x test, not one target.

  python3 tools/hil_report.py --junit results/junit.xml --serial-dir results/pytest-X/serial
  python3 tools/hil_report.py ... --all          # proof for passing tests too
  python3 tools/hil_report.py ... --evidence 'Connected|ERROR'

HIL_PROOF_BEFORE (lines before the evidence line, -1 = from boot, the default) and
HIL_PROOF_AFTER (default 6) work as in hil-lib.sh. Reads files only; touches no board.
"""
import argparse
import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [HERE, os.path.join(HERE, "test"), os.path.join(HERE, "lib")]
from fixtures.console import log_name  # noqa: E402  pylint: disable=wrong-import-position

PROOF_BEFORE = int(os.environ.get("HIL_PROOF_BEFORE", "-1"))
PROOF_AFTER = int(os.environ.get("HIL_PROOF_AFTER", "6"))
TAIL = 25
MAX_SECTION = 14000  # hil_display_test.py: keeps a GitHub comment under 65536 chars
DEFAULT_EVIDENCE = r"Traceback|Error|error|--- cpr:|safe mode|Hard fault"
STAMP = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\.(\d{3})\] ")
ICON = {"passed": "✅", "failed": "❌", "error": "❌", "dnf": "⚠️", "dns": "🚫", "skipped": "⏭"}
WORST_FIRST = ("failed", "error", "dnf", "dns", "passed", "skipped")
# test/fixtures/farm.py DNS and DNF: a board's dns_tests, never started because it does not come
# up reliably; and a test that started, then lost the board, so it never finished
DNS_RE = re.compile(r"DNS \(did not start\)")
DNF_RE = re.compile(r"DNF \(did not finish\)")


# ------------------------------------------------------------------ junit

def read_junit(path, boards):
    """[{nodeid, board, test, outcome, message, time}] from a pytest --junitxml file."""
    out = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "")
        group = re.search(r"@([^/:\[\]]*)$", name)  # xdist loadgroup's "@<board>" suffix
        name = re.sub(r"@[^/:\[\]]*$", "", name)
        nodeid = cls.replace(".", "/") + ".py::" + name
        params = re.search(r"\[(.*)\]$", name)
        parts = params.group(1).split("-") if params else []
        board = group.group(1) if group and group.group(1) in boards else None
        board = board or next((b for b in boards if b in parts), None)
        if board is None and params:
            board = next((b for b in boards if b in params.group(1)), None)
        # The row label: the test with the board taken out of its parameters, so the table
        # reads test x board (test_scenario[good], not one row per board).
        rest = [x for x in parts if x != board]
        test = re.sub(r"\[.*\]$", "", name) + ("[%s]" % "-".join(rest) if rest else "")
        outcome, message = "passed", ""
        for kind in ("failure", "error", "skipped"):
            el = tc.find(kind)
            if el is not None:
                outcome = "failed" if kind == "failure" else kind
                message = (el.get("message") or "") + ("\n" + el.text if el.text else "")
                break
        if outcome == "skipped" and DNS_RE.search(message):
            outcome = "dns"
        elif outcome != "passed" and DNF_RE.search(message):
            outcome = "dnf"
        out.append({"nodeid": nodeid, "board": board or "host", "test": test,
                    "name": name, "outcome": outcome, "message": message.strip(),
                    "time": float(tc.get("time") or 0)})
    return out


# ------------------------------------------------------------------ serial logs

def serial_lines(path):
    """[(epoch or None, line)] from a "[HH:MM:SS.mmm] text" dump. The dump has no date, so
    each line's date is worked back from the file's mtime (its last write): walking from the
    end, a time of day later than the line after it means midnight was crossed."""
    try:
        with open(path, errors="replace") as f:
            raw = f.read().splitlines()
        end = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return []
    day = end.date()
    out, later = [], None
    for line in reversed(raw):
        m = STAMP.match(line)
        if not m:
            out.append((None, line))
            continue
        h, mi, s, ms = (int(x) for x in m.groups())
        tod = datetime.time(h, mi, s, ms * 1000)
        if later is not None and tod > later:
            day -= datetime.timedelta(days=1)
        later = tod
        out.append((datetime.datetime.combine(day, tod).timestamp(), line))
    out.reverse()
    return out


def proof_window(lines, regex):
    """hil-lib.sh proof_window: the lines up to and just after the LAST line matching regex.
    Returns (window, t_start, t_end, matched); a tail of TAIL lines when nothing matches."""
    hits = [i for i, (_, l) in enumerate(lines) if re.search(regex, l)]
    if not hits:
        win = lines[-TAIL:]
        matched = False
    else:
        ln = hits[-1]
        start = 0 if PROOF_BEFORE < 0 else max(0, ln - PROOF_BEFORE)
        win = lines[start:ln + PROOF_AFTER + 1]
        matched = True
    stamps = [t for t, _ in win if t is not None]
    return win, (stamps[0] if stamps else None), (stamps[-1] if stamps else None), matched


# ------------------------------------------------------------------ catcher records

def catcher_records(directory):
    """nodeid -> [record] from every results/catcher/<board>.jsonl."""
    out = defaultdict(list)
    if not directory or not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                out[re.sub(r"@[^/:\[\]]*$", "", r.get("nodeid") or "")].append(r)
    return out


def record_line(r):
    payload = r.get("payload")
    if not isinstance(payload, str):
        payload = json.dumps(payload, sort_keys=True, default=repr)
    if len(payload) > 300:
        payload = payload[:300] + "..."
    return "%s %-4s %s %s" % (r.get("ts", "?"), r.get("box", "?"), r.get("topic") or "-", payload)


def broker_window(recs, t_a, t_b):
    """Records in [t_a, t_b] (the serial window's span), else the test's last TAIL."""
    if t_a is not None and t_b is not None:
        win = [r for r in recs if t_a <= float(r.get("t", 0)) <= t_b]
        if win:
            return win, "⏱ aligned to the serial window (%s … %s)" % (_hms(t_a), _hms(t_b))
        return recs[-TAIL:], "⚠️ no broker traffic in the serial window — last records shown"
    return recs[-TAIL:], "serial gave no time anchor — last records shown"


def _hms(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


# ------------------------------------------------------------------ markdown

def section(board, label, logtype, note, body):
    text = "\n".join(body).strip() or "(empty)"
    if len(text) > MAX_SECTION:
        text = "…(truncated — full log in the run folder)…\n" + text[-MAX_SECTION:]
    return ("\n<details><summary>📜 `%s` %s · %s.log (%s)</summary>\n\n```\n%s\n```\n</details>\n"
            % (board, label, logtype, note, text))


def proof(case, serial_dir, records, evidence):
    out = []
    path = os.path.join(serial_dir, case["board"], log_name(case["nodeid"])) if serial_dir else ""
    lines = serial_lines(path) if path else []
    t_a = t_b = None
    if lines:
        win, t_a, t_b, matched = proof_window(lines, evidence)
        note = ("✓ from boot to just after the detected test phrase" if matched and PROOF_BEFORE < 0
                else "✓ around the detected test phrase" if matched
                else "⚠️ test phrase not found — tail shown")
        out.append(section(case["board"], case["test"], "serial", note, [l for _, l in win]))
    else:
        out.append("\n> ⚠️ `%s` %s — `serial.log` not captured (device-only test, or no "
                   "--serial-log-dir)\n" % (case["board"], case["test"]))
    recs = records.get(case["nodeid"], [])
    if recs:
        win, note = broker_window(recs, t_a, t_b)
        out.append(section(case["board"], case["test"], "protomq", note,
                           [record_line(r) for r in win]))
    return out


def report(cases, serial_dir, records, evidence, show_all, title, invocation):
    boards = sorted({c["board"] for c in cases})
    tests = list(OrderedDict.fromkeys(c["test"] for c in cases))
    grid = defaultdict(list)
    for c in cases:
        grid[c["test"], c["board"]].append(c["outcome"])
    totals = defaultdict(int)
    for c in cases:
        totals[c["outcome"]] += 1

    out = ["## 🔌 %s" % title, "",
           " · ".join(["%d passed" % totals["passed"], "%d failed" % totals["failed"],
                       "%d errors" % totals["error"]]
                      + ["%d %s" % (totals[k], k.upper()) for k in ("dnf", "dns") if totals[k]]
                      + ["%d skipped" % totals["skipped"]]), "",
           "| test | " + " | ".join("`%s`" % b for b in boards) + " |",
           "|---|" + "---|" * len(boards)]
    for t in tests:
        cells = []
        for b in boards:
            got = grid.get((t, b))
            if not got:
                cells.append("")
                continue
            worst = next((o for o in WORST_FIRST if o in got), got[0])
            n = sum(1 for o in got if o == worst)
            cells.append("%s%s" % (ICON[worst], "" if len(got) == 1 else " %d/%d" % (n, len(got))))
        out.append("| `%s` | %s |" % (t, " | ".join(cells)))
    out.append("")

    shown = [c for c in cases if c["outcome"] in ("failed", "error", "dnf")
             or (show_all and c["outcome"] == "passed")]
    for c in shown:
        out += ["", "### %s `%s` on `%s`" % (ICON[c["outcome"]], c["name"], c["board"]), ""]
        if c["message"]:
            out.append(section(c["board"], c["test"], "pytest", c["outcome"],
                               c["message"].splitlines()[-60:]))
        out += proof(c, serial_dir, records, evidence)
    if invocation:
        out.append("\n<details><summary>🧾 invocation</summary>\n\n```\n$ %s\n```\n</details>\n"
                   % invocation)
    return "\n".join(out) + "\n"


def farm_boards(path):
    try:
        import tomllib  # pylint: disable=import-outside-toplevel
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # pylint: disable=import-outside-toplevel
    with open(path, "rb") as f:
        return [b["name"] for b in tomllib.load(f).get("board", [])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n", 2)[2])
    ap.add_argument("--junit", required=True, help="pytest --junitxml file")
    ap.add_argument("--serial-dir", default="", help="the run's --serial-log-dir")
    ap.add_argument("--catcher-dir", default=os.path.join(HERE, "results", "catcher"))
    ap.add_argument("--farm", default=os.path.join(HERE, "farm.toml"))
    ap.add_argument("--evidence", default=DEFAULT_EVIDENCE,
                    help="regex for the detection line (default: errors and cpr markers)")
    ap.add_argument("--all", action="store_true", help="proof sections for passing tests too")
    ap.add_argument("--title", default="cp-regress HIL run")
    ap.add_argument("--invocation", default="", help="the pytest command line, for the record")
    ap.add_argument("-o", "--out", default="-", help="comment.md path (default stdout)")
    args = ap.parse_args(argv)

    cases = read_junit(args.junit, farm_boards(args.farm))
    text = report(cases, args.serial_dir, catcher_records(args.catcher_dir), args.evidence,
                  args.all, args.title, args.invocation)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
    return 1 if any(c["outcome"] in ("failed", "error", "dnf") for c in cases) else 0


if __name__ == "__main__":
    sys.exit(main())
