#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Static gate for cp-regress fixtures. Run before taking fixtures to the farm.

  python3 tools/check_fixtures.py [--cp-tree PATH]

Checks each tests/*.py:
  * imports cleanly, has META + run()
  * META has the required keys with legal values
  * every device code string (module-level str containing check( / metric( / skip()
    compiles with CPython's parser (CircuitPython syntax is a subset)
  * device code avoids constructs CircuitPython lacks (walrus, f"{x=}", typing, dataclasses)
  * every `import X` in device code is a CircuitPython module that exists in shared-bindings
    (or a core module), and every `X.name` attribute used is mentioned in that module's
    shared-bindings sources (heuristic: reports, does not fix)
  * microcontroller.reset() appears only behind a reset_ok guard
"""

import argparse
import ast
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ["id", "title", "category", "tier", "needs", "automation", "targets"]
TIERS = {"smoke", "nightly", "release"}
AUTOMATION = {"auto", "fixture", "assisted"}
CAPS = {"native_usb", "uf2", "wifi", "ble", "esp32spi", "usb_host", "reset_ok", "small"}
CORE = {"sys", "time", "gc", "os", "math", "struct", "json", "re", "random", "errno", "array",
        "collections", "io", "binascii", "hashlib", "zlib", "select", "builtins", "micropython",
        "asyncio", "traceback", "ssl", "socket", "_bleio", "board", "microcontroller", "supervisor",
        "storage", "usb_cdc", "usb_hid", "usb_midi", "ulab"}
EXTMOD = {"json", "re", "random", "errno", "array", "collections", "io", "binascii", "hashlib",
          "zlib", "select", "struct", "math", "sys", "gc", "time", "os", "builtins", "micropython",
          "asyncio", "traceback"}


def device_strings(mod):
    for k, v in vars(mod).items():
        if isinstance(v, str) and re.search(r"\b(check|metric|skip)\(", v) and "\n" in v:
            yield k, v


def check_device(name, src, bindings, problems):
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        # templates: str.format placeholders ({x}, {x!r}) or @@X@@ markers. Fill them the way
        # the test would, so doubled braces collapse exactly as at runtime.
        class _Fill(dict):
            def __missing__(self, k):
                return 0
        filled = re.sub(r"@@[A-Z_]+@@", "0", src)
        try:
            filled = filled.format_map(_Fill())
        except (ValueError, IndexError, KeyError, AttributeError):
            pass
        try:
            tree = ast.parse(filled)
        except SyntaxError as e2:
            problems.append("%s: device code does not compile: %s" % (name, e2))
            return
    for node in ast.walk(tree):
        if isinstance(node, ast.NamedExpr):
            problems.append("%s: walrus operator in device code" % name)
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and getattr(v, "conversion", -1) == -1:
                    pass
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                top = m.split(".")[0]
                if top in ("typing", "dataclasses", "abc", "enum", "threading", "subprocess"):
                    problems.append("%s: imports %s, not on CircuitPython" % (name, top))
                elif bindings and top not in EXTMOD and not os.path.isdir(os.path.join(bindings, top)) \
                        and top not in ("adafruit_esp32spi", "adafruit_ble", "adafruit_requests"):
                    problems.append("%s: module %s not found in shared-bindings" % (name, top))
    if "=}" in src and re.search(r"f[\"'][^\"']*\{[^}]*=\}", src):
        problems.append("%s: f-string '=' specifier" % name)
    if bindings:
        # drop string literals so check names like "usb_hid.devices_ok" are not read as API use
        import io
        import tokenize
        try:
            code = " ".join(t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
                            if t.type not in (tokenize.STRING, tokenize.COMMENT)
                            and not t.type == getattr(tokenize, "FSTRING_MIDDLE", -1))
            code = code.replace(" . ", ".")
        except (tokenize.TokenError, SyntaxError, IndentationError):
            code = src
        for m, attr in set(re.findall(r"\b([a-z_][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", code)):
            d = os.path.join(bindings, m)
            if not os.path.isdir(d) or m in EXTMOD or m == "board":  # board pins live in each board's pins.c
                continue
            blob = ""
            for fn in os.listdir(d):
                if fn.endswith((".c", ".h")):
                    with open(os.path.join(d, fn), errors="replace") as f:
                        blob += f.read()
            if not re.search(r"\b%s\b" % re.escape(attr), blob):
                problems.append("%s: %s.%s not found in shared-bindings/%s" % (name, m, attr, m))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cp-tree", default=os.environ.get("CPR_CP_TREE", os.path.expanduser("~/circuitpython")))
    a = ap.parse_args()
    bindings = os.path.join(a.cp_tree, "shared-bindings")
    if not os.path.isdir(bindings):
        print("note: no shared-bindings at %s, API checks skipped" % bindings)
        bindings = None
    sys.path.insert(0, os.path.join(HERE, "lib"))
    total = 0
    tdir = os.path.join(HERE, "tests")
    ids = set()
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        problems = []
        path = os.path.join(tdir, fn)
        try:
            spec = importlib.util.spec_from_file_location("chk_" + fn[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            print("FAIL %-24s import: %s" % (fn, e))
            total += 1
            continue
        meta = getattr(mod, "META", None)
        if not isinstance(meta, dict) or not callable(getattr(mod, "run", None)):
            problems.append("missing META or run()")
            meta = meta or {}
        for k in REQUIRED:
            if k not in meta:
                problems.append("META missing %s" % k)
        if meta.get("id") != fn[:-3]:
            problems.append("META id %r != file name" % meta.get("id"))
        if meta.get("tier") not in TIERS:
            problems.append("bad tier %r" % meta.get("tier"))
        if meta.get("automation") not in AUTOMATION:
            problems.append("bad automation %r" % meta.get("automation"))
        bad_caps = [c for c in meta.get("needs", []) if c not in CAPS]
        if bad_caps:
            problems.append("unknown caps %s" % bad_caps)
        if not all(isinstance(t, int) for t in meta.get("targets", [])):
            problems.append("targets must be issue numbers")
        ids.add(meta.get("id"))
        for name, src in device_strings(mod):
            check_device("%s.%s" % (fn[:-3], name), src, bindings, problems)
        text = open(path).read()
        if "microcontroller.reset()" in text or "m.reset()" in text:
            if "reset_ok" not in text:
                problems.append("calls microcontroller.reset() without a reset_ok guard")
        if "sudo" in text:
            problems.append("mentions sudo")
        doc = os.path.join(HERE, "docs", "fixtures", fn[:-3] + ".md")
        if not os.path.exists(doc):
            problems.append("no docs/fixtures/%s.md" % fn[:-3])
        status = "ok  " if not problems else "WARN"
        print("%s %-24s tier=%-8s auto=%-9s targets=%d" % (status, fn[:-3], meta.get("tier"),
                                                          meta.get("automation"), len(meta.get("targets", []))))
        for p in problems:
            print("       - " + p)
        total += len(problems)
    print("\n%d problems" % total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
