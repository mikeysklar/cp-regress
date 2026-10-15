# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""CircuitPython language regression test: core VM, type coercion, collections, exceptions.

Runs the smallest MicroPython/CircuitPython tests from tests/basics/ (each in fresh globals),
comparing device output to expected (from .exp files
or host python3 execution). Targets language regressions across integer/float/string/list/
dict/set/exception handling and basic VM semantics.
"""

import os
import glob
import subprocess
import time

import farmlib  # same module object run.py uses, so farmlib.FarmError matches

META = {
    "id": "core_vm",
    "title": "Core language VM: integers, strings, collections, exceptions, traceback",
    "category": "Core",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 600,  # 10 min: running ~40 tests per board
    "targets": [6214, 3169, 1881, 10259, 8499, 5451, 4101],
}

# Device code for the two direct checks
DEVICE_MEMORY_ERROR = r'''
# 256 MB: more than any board's RAM + PSRAM, so a MemoryError is the only correct outcome
try:
    x = bytearray(1 << 28)
    check("memory_error", False, "256 MB allocation succeeded unexpectedly")
except MemoryError:
    check("memory_error", True)
except Exception as e:
    check("memory_error", False, "wrong exception: " + type(e).__name__)
'''

DEVICE_RECURSION_ERROR = r'''
def deep_recursion(n):
    if n > 0:
        return deep_recursion(n - 1)
    return 0

try:
    deep_recursion(100000)
    check("recursion_error", False, "deep recursion succeeded unexpectedly")
except RuntimeError as e:
    # "maximum recursion depth exceeded" (C stack check) or "pystack_space_exhausted"
    check("recursion_error", True, str(e))
except Exception as e:
    check("recursion_error", False, "wrong exception: " + type(e).__name__)
'''


def _get_cp_tree():
    """Return CircuitPython checkout path, or None if not available."""
    tree = os.environ.get("CPR_CP_TREE", os.path.expanduser("~/circuitpython"))
    if os.path.isdir(tree):
        return tree
    return None


def _get_test_files(cp_tree, max_count=None):
    """Find and filter test files from tests/basics/.

    Excludes files that mention 'skip' or import micropython-only modules.
    Sorts by size and returns the first max_count, which are the smallest.
    """
    testdir = os.path.join(cp_tree, "tests", "basics")
    if not os.path.isdir(testdir):
        return []

    test_files = sorted(glob.glob(os.path.join(testdir, "*.py")))
    skip_patterns = ["skip", "micropython", "machine", "uctypes", "_thread"]

    candidates = []
    for f in test_files:
        fname = os.path.basename(f)
        if fname.startswith("_"):
            continue

        try:
            with open(f) as fp:
                content = fp.read()
                if any(p in content for p in skip_patterns):
                    continue
        except (OSError, UnicodeDecodeError):
            continue

        size = os.path.getsize(f)
        candidates.append((f, fname, size))

    candidates.sort(key=lambda x: x[2])
    selected = candidates[:max_count] if max_count else candidates
    return selected


def _get_expected_output(test_file, cp_tree):
    """Get expected output for a test file.

    First tries .exp file; if not present, runs the test with host python3.
    Returns the output as a string, or None if unable to determine.
    """
    exp_file = test_file + ".exp"

    # Try .exp file first
    if os.path.isfile(exp_file):
        try:
            with open(exp_file) as f:
                return f.read()
        except OSError:
            pass

    # Try running with python3
    try:
        result = subprocess.run(
            ["python3", test_file],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=os.path.dirname(test_file)
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _wrap(src):
    """Run a test in fresh globals so names do not leak between tests, and catch SystemExit
    (tests do print('SKIP'); raise SystemExit), which would otherwise leave the raw REPL and
    soft-reboot the board."""
    return ("try:\n    exec(%r, {'__name__': '__main__'})\nexcept SystemExit:\n    pass\n"
            "import gc\ngc.collect()\n" % src)


def _normalize_output(text):
    """Normalize output for comparison: strip, normalize line endings."""
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def run(board, ctx):
    """Run core_vm test on the board."""

    # Host-side checks
    ctx.check("present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("vid_pid", vp is not None, str(vp))

    cp_tree = _get_cp_tree()
    if not cp_tree:
        ctx.skip("circuitpython_tree", "CPR_CP_TREE not set and ~/circuitpython not found")
        return

    # For small boards, run only the first 15 tests
    max_tests = 15 if "small" in board.caps else 40
    test_files = _get_test_files(cp_tree, max_count=max_tests)

    if not test_files:
        ctx.skip("test_files", "no test files found in %s/tests/basics/" % cp_tree)
        return

    ctx.log("selected %d test files from %s/tests/basics" % (len(test_files), cp_tree))

    # Run tests on device
    passed = 0
    failed = 0
    skipped = 0

    t0 = time.time()

    lost = False
    try:
        repl = board.repl()
    except (farmlib.FarmError, OSError) as e:
        ctx.check("repl_open", False, str(e))
        return
    with repl:
        for test_path, test_name, _ in test_files:
            # Read the test source
            try:
                with open(test_path) as f:
                    test_src = f.read()
            except OSError:
                ctx.skip(test_name, "cannot read test file")
                skipped += 1
                continue

            # Get expected output
            expected = _get_expected_output(test_path, cp_tree)
            if expected is None:
                ctx.skip(test_name, "cannot get expected output")
                skipped += 1
                continue

            # Run on device
            try:
                stdout, stderr = repl.exec_raw(_wrap(test_src), timeout=20)
            except Exception as e:
                ctx.check(test_name, False, "device execution failed: %s" % str(e)[:200])
                failed += 1
                # Recover: interrupt the hung test, leave the raw REPL (prompt() alone cannot,
                # ^C in raw mode never prints '>>> '), then re-sync on the friendly prompt.
                try:
                    repl.write(b"\x03\x03")
                    time.sleep(0.2)
                    repl.write(b"\x02")
                    repl.prompt()
                except Exception as e2:
                    ctx.check("repl_recover", False, "%s: %s" % (type(e2).__name__, e2))
                    lost = True
                    break
                continue

            # Handle special case: device can signal skip with "SKIP"
            if stdout.strip() == "SKIP":
                ctx.skip(test_name, "device skipped")
                skipped += 1
                continue

            # Normalize and compare
            expected_norm = _normalize_output(expected)
            actual_norm = _normalize_output(stdout)

            if actual_norm == expected_norm:
                ctx.check(test_name, True)
                passed += 1
            else:
                # Report failure with a useful diff snippet
                exp_lines = expected_norm.split("\n")
                act_lines = actual_norm.split("\n")
                detail = "mismatch (exp %d lines, got %d)" % (len(exp_lines), len(act_lines))
                for i, (e_l, a_l) in enumerate(zip(exp_lines, act_lines)):
                    if e_l != a_l:
                        detail += "; line %d: exp %r got %r" % (i + 1, e_l[:60], a_l[:60])
                        break
                if stderr.strip():
                    detail += "; stderr: %s" % stderr.strip()[-200:]
                ctx.check(test_name, False, detail)
                failed += 1

    if lost:
        # the console went away mid-run: get the board back before the direct checks
        board.wait_ready()

    # Direct checks: memory error and recursion error
    for name, body in (("memory_error", DEVICE_MEMORY_ERROR), ("recursion_error", DEVICE_RECURSION_ERROR)):
        try:
            ctx.device(body, timeout=30)
        except (farmlib.FarmError, OSError) as e:
            ctx.check(name, False, "REPL: %s" % e)

    elapsed = time.time() - t0

    # Report metrics
    total = passed + failed + skipped
    if total > 0:
        pass_ratio = 100.0 * passed / (passed + failed) if (passed + failed) > 0 else 0
        ctx.metric("pass_ratio", pass_ratio, "%", better="higher")

    ctx.metric("test_time", elapsed, "s", better="lower")
    ctx.check("summary", passed > 0 and failed == 0,
              "passed %d/%d, failed %d, skipped %d" % (passed, passed + failed, failed, skipped))
