#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Check that each test/hardware/<id>_test.py covers its tests/<id>.py original.

For every fixture it checks:
  - markers: tier, needs, wiring, skip_boards, skip_ports, targets, destructive, multi_board
    match META
  - device code: every top-level string constant in the original that calls check()/metric()
    exists in the pytest module under the same name, byte for byte
  - check names: every literal name passed to check/skip/metric/golden/guard (host or device
    side) appears in the pytest module, as test_<name> or as a quoted string

Names built at run time ("gs_%s" % k, f-strings) cannot be checked statically; they are counted
and listed so a reviewer can look.

  python3 tools/pytest_parity.py            # every fixture
  python3 tools/pytest_parity.py perf_core  # one
Exit status is 1 if anything is missing.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIG = os.path.join(HERE, "tests")
NEW = os.path.join(HERE, "test", "hardware")
TIERS = ("smoke", "nightly", "release")
NAME_CALL = re.compile(r"""\b(?:ctx\.)?(?:check|skip|metric|golden|guard)\(\s*(['"])([A-Za-z0-9_.\-]+)\1""")
DYN_CALL = re.compile(r"""\b(?:ctx\.)?(?:check|skip|metric|golden|guard)\(\s*(?:f['"]|['"][^'"]*%|[A-Za-z_])""")


def module_info(path):
    src = open(path).read()
    tree = ast.parse(src)
    consts, meta, pytestmark = {}, None, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "META":
                meta = ast.literal_eval(node.value)
            elif name == "pytestmark":
                pytestmark = node.value
            elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                consts[name] = node.value.value
    return src, consts, meta, pytestmark


def marks(node):
    """{'smoke': [], 'needs': ['native_usb'], ...} from a pytestmark list."""
    out = {}
    items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node] if node is not None else []
    for it in items:
        call = it if isinstance(it, ast.Call) else None
        attr = call.func if call else it
        if isinstance(attr, ast.Attribute):
            args = []
            if call:
                for a in call.args:
                    try:
                        args.append(ast.literal_eval(a))
                    except ValueError:
                        args.append("?")
            out[attr.attr] = args
    return out


def check_one(fid):
    problems, notes = [], []
    opath = os.path.join(ORIG, fid + ".py")
    npath = os.path.join(NEW, fid + "_test.py")
    if not os.path.exists(npath):
        return ["missing %s" % os.path.relpath(npath, HERE)], notes
    osrc, oconsts, meta, _ = module_info(opath)
    nsrc, nconsts, _, nmark = module_info(npath)
    m = marks(nmark)

    tier = meta.get("tier", "nightly")
    got = [t for t in TIERS if t in m]
    if got != [tier]:
        problems.append("tier marker %s, META says %s" % (got or "none", tier))
    for key in ("needs", "wiring", "skip_boards", "skip_ports", "targets"):
        want = list(meta.get(key, []) or [])
        have = m.get(key, [])
        if sorted(map(str, want)) != sorted(map(str, have)):
            problems.append("%s marker %s, META says %s" % (key, have, want))
    for flag in ("destructive", "multi_board"):
        if bool(meta.get(flag)) != (flag in m):
            problems.append("%s marker %s, META says %s" % (flag, flag in m, bool(meta.get(flag))))

    for name, val in oconsts.items():
        if "check(" in val or "metric(" in val or "skip(" in val:
            if name not in nconsts:
                problems.append("device constant %s missing" % name)
            elif nconsts[name] != val:
                problems.append("device constant %s differs from the original" % name)

    names = sorted({mm.group(2) for mm in NAME_CALL.finditer(osrc)})
    for n in names:
        if ("test_" + n.replace("-", "_").replace(".", "_")) not in nsrc and ('"%s"' % n) not in nsrc \
                and ("'%s'" % n) not in nsrc:
            problems.append("check %r not covered" % n)
    dyn = len(DYN_CALL.findall(osrc))
    if dyn:
        notes.append("%d dynamically named check/skip/metric calls (review by hand)" % dyn)
    return problems, notes


def main(argv):
    ids = argv or sorted(f[:-3] for f in os.listdir(ORIG) if f.endswith(".py") and not f.startswith("_"))
    bad = 0
    for fid in ids:
        problems, notes = check_one(fid)
        status = "ok" if not problems else "MISSING %d" % len(problems)
        print("%-24s %s%s" % (fid, status, ("  (" + "; ".join(notes) + ")") if notes else ""))
        for p in problems:
            print("    - " + p)
        bad += bool(problems)
    print("\n%d of %d fixtures have gaps" % (bad, len(ids)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
