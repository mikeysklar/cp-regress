#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Run every fixture's device code on the CircuitPython unix port, before the farm.

The unix coverage build has the core VM, json, gc, Bitmap / Palette / vectorio / bitmaptools,
settings.toml and os, but no board, USB, radio or pins. Each DEVICE* string runs with the same
check / skip / metric helpers the farm uses, inside a RAM FAT disk mounted at "/" that stands in
for CIRCUITPY. Some fixtures get the files their host side would have written (settings.toml,
the usb_msc_rw test file) and their host-side comparisons. core_vm runs its selected
tests/basics files with the same expected-output logic it uses on a board.

Per DEVICE string the result is
  PASS         ran to the end, at least one passing check, no failed check
  SKIPPED      ran to the end but every check skipped (feature not built for unix)
  PARTIAL      some checks ran, then a module or attribute the unix port lacks stopped it
  UNSUPPORTED  stopped by a missing module or attribute before any check
  FAIL         a failed check, or any other uncaught exception
Exit status is 1 if anything FAILs.

  python3 tools/unix_smoke.py --cp-tree ~/circuitpython --build     # build, then run
  python3 tools/unix_smoke.py --micropython path/to/micropython      # existing binary
  python3 tools/unix_smoke.py --only displayio_headless,settings_toml -v

--build runs `make -C mpy-cross` and `make -C ports/unix VARIANT=coverage`. A fresh clone
needs tags (`make fetch-tags`) and these submodules: lib/berkeley-db-1.xx lib/micropython-lib
lib/mp3 extmod/ulab lib/mbedtls lib/axtls lib/AnimatedGIF lib/tjpgd lib/uzlib lib/quirc
lib/libffi lib/adafruit_floppy (`--fetch-submodules` runs `git submodule update` for them), and
the Python packages huffman, cascadetoml, jinja2, typer, sh, click, intelhex, tomlkit.
"""

import argparse
import glob
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))
import farmlib  # noqa: E402

SUBMODULES = ["lib/berkeley-db-1.xx", "lib/micropython-lib", "lib/mp3", "extmod/ulab",
              "lib/mbedtls", "lib/axtls", "lib/AnimatedGIF", "lib/tjpgd", "lib/uzlib",
              "lib/quirc", "lib/libffi", "lib/adafruit_floppy"]

# Runs before the fixture body. time.monotonic* do not exist on unix, and a builtin module
# cannot gain attributes, so a copy goes into sys.modules (import checks it first).
PRELUDE = r'''
import sys as _cpr_sys
import time as _cpr_rt
if not hasattr(_cpr_rt, "monotonic"):
    class _CprTime:
        pass
    _cpr_t = _CprTime()
    for _cpr_k in dir(_cpr_rt):
        setattr(_cpr_t, _cpr_k, getattr(_cpr_rt, _cpr_k))
    _cpr_t.monotonic_ns = _cpr_rt.time_ns
    _cpr_t.monotonic = lambda: _cpr_rt.time_ns() / 1e9
    _cpr_sys.modules["time"] = _cpr_t
import os as _cpr_os


class _CprRam:
    def __init__(self, blocks):
        self.data = bytearray(blocks * 512)
        self.mv = memoryview(self.data)

    def readblocks(self, block, buf, off=0):
        a = block * 512 + off
        buf[:] = self.mv[a:a + len(buf)]

    def writeblocks(self, block, buf, off=None):
        a = block * 512 + (off or 0)
        self.mv[a:a + len(buf)] = buf

    def ioctl(self, op, arg):
        if op == 4:
            return len(self.data) // 512
        if op == 5:
            return 512
        if op == 6:
            return 0


_cpr_bdev = _CprRam(@@BLOCKS@@)
_cpr_os.VfsFat.mkfs(_cpr_bdev)
try:
    _cpr_os.umount("/")
except OSError:
    pass
_cpr_os.mount(_cpr_os.VfsFat(_cpr_bdev), "/")
_cpr_os.chdir("/")
for _cpr_path, _cpr_data in @@FILES@@:
    _cpr_parts = _cpr_path.strip("/").split("/")
    for _cpr_i in range(1, len(_cpr_parts)):
        try:
            _cpr_os.mkdir("/" + "/".join(_cpr_parts[:_cpr_i]))
        except OSError:
            pass
    with open(_cpr_path, "wb") as _cpr_f:
        _cpr_f.write(_cpr_data)
'''

# Host placeholders, filled the way each fixture's run() fills them on the farm.
PLACEHOLDERS = {
    "@@EXPECT_OUT@@": "False", "@@RUN_TIME@@": "10", "@@SCALE@@": "1.0", "@@DRAIN@@": "False",
    "@@NAME@@": "CPRU", "@@PINS@@": "None", "@@LIGHT@@": "None", "@@SLEEP@@": "1",
}

MISSING = re.compile(r"^(ImportError: no module named '([^']+)'"
                     r"|AttributeError: (?:module|type object) '([^']+)' has no attribute '([^']+)')",
                     re.M)


def load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fill(dict):
    def __missing__(self, key):
        return repr("cpr")


def fill(body):
    for k, v in PLACEHOLDERS.items():
        body = body.replace(k, v)
    if re.search(r"(?<!\{)\{[A-Za-z_]+\}(?!\})", body):
        try:
            body = body.format_map(Fill())
        except (ValueError, IndexError, KeyError):
            pass
    return body


# Per-fixture setup: files on the RAM disk, and host-side checks on the device results.

MSC_DATA = random.Random(42).randbytes(32 * 1024)


def msc_files(mod):
    files = [("/cpr_test_file.bin", MSC_DATA)]
    files += [("/cpr_burst/file_%02d.txt" % i, b"burst %d\n" % i) for i in range(20)]
    return files


def msc_after(results):
    vals = {r["name"]: r.get("value") for r in results}
    want = zlib.adler32(MSC_DATA) & 0xFFFFFFFF
    got = vals.get("device_adler32")
    ok = got == want
    return [{"name": "device_matches_host", "ok": ok,
             "detail": "device %s host %08x" % (("%08x" % got) if isinstance(got, int) else got, want)}]


HOOKS = {
    "settings_toml": {"files": lambda m: [("/settings.toml", m.TEST_SETTINGS.encode())]},
    "usb_msc_rw": {"files": msc_files, "after": msc_after},
}


def run_body(mp, body, files, heap, blocks, timeout):
    src = (farmlib.device_source("") + PRELUDE.replace("@@BLOCKS@@", str(blocks))
           .replace("@@FILES@@", repr(files)) + "\n" + body)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run([mp, "-X", "heapsize=%s" % heap, path], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return -1, (e.stdout or b"").decode(errors="replace") + "\nTIMEOUT after %d s" % timeout
    finally:
        os.unlink(path)


def classify(rc, out, results):
    fails = [r for r in results if r.get("ok") is False]
    lines = [ln for ln in out.splitlines() if not ln.startswith(farmlib.RESULT_TAG)]
    tail = "\n".join(lines[-3:]).strip()
    if rc == 0:
        if fails:
            return "FAIL", fails, ""
        if not any(r.get("ok") is True for r in results):
            return "SKIPPED", fails, "only skips: the feature is not in the unix build"
        return "PASS", fails, ""
    m = None
    for m in MISSING.finditer(out):
        pass
    if m and not fails and "TIMEOUT" not in out:
        what = m.group(2) or "%s.%s" % (m.group(3), m.group(4))
        return ("PARTIAL" if results else "UNSUPPORTED"), fails, "unix lacks " + what
    return "FAIL", fails, tail[-300:]


def core_vm(mp, mod, cp_tree, heap, verbose):
    """core_vm's own selection and expected output, run on unix instead of a board."""
    rows = []
    with tempfile.TemporaryDirectory() as d:
        for test_path, name, _ in mod._get_test_files(cp_tree, max_count=40):
            expected = mod._get_expected_output(test_path, cp_tree)
            with open(test_path) as f:
                src = mod._wrap(f.read())
            p = os.path.join(d, "t.py")
            with open(p, "w") as f:
                f.write(src)
            try:
                r = subprocess.run([mp, "-X", "heapsize=%s" % heap, p], capture_output=True,
                                   text=True, timeout=30, cwd=os.path.dirname(test_path))
                got, err = r.stdout, r.stderr
            except subprocess.TimeoutExpired:
                got, err = None, "timeout"
            if got is not None and got.strip() == "SKIP":
                rows.append((name, None, "SKIP"))
                continue
            ok = got is not None and expected is not None and \
                mod._normalize_output(got) == mod._normalize_output(expected)
            detail = "" if ok else (err.strip().splitlines() or ["output differs from expected"])[-1]
            rows.append((name, ok, detail[:160]))
    return rows


CONTROLS = [
    ("control_pass", 'check("x", True, "")', "PASS"),
    ("control_failed_check", 'check("x", False, "boom")', "FAIL"),
    ("control_exception", 'check("x", True, "")\nraise ValueError("boom")', "FAIL"),
    ("control_missing_module", 'import board', "UNSUPPORTED"),
    ("control_partial", 'check("x", True, "")\nimport wifi', "PARTIAL"),
    ("control_only_skips", 'skip("x", "why")', "SKIPPED"),
    ("control_fs_seeded", 'check("x", open("/a/b.txt").read() == "hi", "")', "PASS"),
]


def controls(mp, heap):
    """Known inputs with known outcomes; if one is misclassified no other result means anything."""
    bad = []
    for name, body, want in CONTROLS:
        rc, out = run_body(mp, body, [("/a/b.txt", b"hi")], heap, 256, 30)
        got = classify(rc, out, farmlib.parse_results(out))[0]
        if got != want:
            bad.append("%s: expected %s, got %s\n%s" % (name, want, got, out[-300:]))
    return bad


def build(tree, fetch):
    if fetch:
        subprocess.run(["git", "-C", tree, "submodule", "update", "--init", "--depth", "1"]
                       + SUBMODULES, check=True)
    jobs = "-j%d" % (os.cpu_count() or 4)
    subprocess.run(["make", "-C", os.path.join(tree, "mpy-cross"), jobs], check=True)
    subprocess.run(["make", "-C", os.path.join(tree, "ports", "unix"), "VARIANT=coverage", jobs],
                   check=True)
    return os.path.join(tree, "ports", "unix", "build-coverage", "micropython")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--micropython", default=os.environ.get("CPR_UNIX_MP"),
                    help="unix coverage binary (default: <cp-tree>/ports/unix/build-coverage/micropython)")
    ap.add_argument("--cp-tree", default=os.environ.get("CPR_CP_TREE", os.path.expanduser("~/circuitpython")))
    ap.add_argument("--build", action="store_true", help="build mpy-cross and the unix coverage port first")
    ap.add_argument("--fetch-submodules", action="store_true", help="with --build: fetch the needed submodules")
    ap.add_argument("--only", help="comma-separated fixture ids")
    ap.add_argument("--heap", default="32M")
    ap.add_argument("--json", help="write all results to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every check")
    a = ap.parse_args()

    mp = a.micropython
    if a.build:
        mp = build(a.cp_tree, a.fetch_submodules)
    if not mp:
        mp = os.path.join(a.cp_tree, "ports", "unix", "build-coverage", "micropython")
    if not os.access(mp, os.X_OK):
        print("no unix micropython at %s (use --build or --micropython)" % mp)
        return 2
    only = set(a.only.split(",")) if a.only else None

    bad = controls(mp, a.heap)
    if bad:
        print("negative controls misclassified, results would be meaningless:")
        print("\n".join(bad))
        return 2
    print("controls     %d/%d classified correctly" % (len(CONTROLS), len(CONTROLS)))

    report, nfail = [], 0
    for path in sorted(glob.glob(os.path.join(HERE, "tests", "*.py"))):
        fid = os.path.basename(path)[:-3]
        if fid.startswith("_") or (only and fid not in only):
            continue
        mod = load(path)
        hook = HOOKS.get(fid, {})
        files = hook.get("files", lambda m: [])(mod)
        bodies = [(k, v) for k, v in vars(mod).items() if k.startswith("DEVICE") and isinstance(v, str)]
        for name, body in bodies:
            rc, out = run_body(mp, fill(body), files, a.heap, 4096, 180)
            results = farmlib.parse_results(out)
            if rc == 0 and "after" in hook:
                results += hook["after"](results)
            status, fails, why = classify(rc, out, results)
            nfail += status == "FAIL"
            report.append({"fixture": fid, "device": name, "status": status,
                           "results": results, "note": why})
            print("%-11s %-22s %-24s checks=%-3d %s" % (status, fid, name, len(results), why))
            for r in (results if a.verbose else fails):
                mark = {True: "ok", False: "FAIL", None: "skip"}[r.get("ok")]
                print("    %-4s %-34s %s" % (mark, r["name"], str(r.get("detail", ""))[:120]))
        if fid == "core_vm" and os.path.isdir(os.path.join(a.cp_tree, "tests", "basics")):
            rows = core_vm(mp, mod, a.cp_tree, a.heap, a.verbose)
            bad = [r for r in rows if r[1] is False]
            status = "FAIL" if bad else "PASS"
            nfail += bool(bad)
            report.append({"fixture": fid, "device": "tests/basics", "status": status,
                           "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in rows]})
            print("%-11s %-22s %-24s checks=%-3d %d skipped" % (
                status, fid, "tests/basics", len(rows), sum(r[1] is None for r in rows)))
            for n, ok, d in (rows if a.verbose else bad):
                print("    %-4s %-34s %s" % ({True: "ok", False: "FAIL", None: "skip"}[ok], n, d))

    counts = {}
    for r in report:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + "  ".join("%s %d" % kv for kv in sorted(counts.items())))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
