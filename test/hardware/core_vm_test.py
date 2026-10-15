# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Core language VM: integers, strings, collections, exceptions, traceback.

pytest version of tests/core_vm.py. Runs the smallest tests from a CircuitPython checkout's
tests/basics/ (each in fresh globals) and compares the device output with the .exp file or host
python3 output, then checks MemoryError and deep recursion directly. The whole run happens once
per board in a module-scoped fixture; each check there is one test here, in the same order.
Targets: #6214, #3169, #1881, #10259, #8499, #5451, #4101.

The per-file tests are parametrized over the 40 smallest files found on the host at collection
time (CPR_CP_TREE, default ~/circuitpython). Boards with the "small" cap run only the first 15,
as in the original; the rest skip there.
"""
import glob
import logging
import os
import subprocess
import time

import pytest

import farmlib  # same module object run.py uses, so farmlib.FarmError matches
from fixtures.device import DeviceResults

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(6214, 3169, 1881, 10259, 8499, 5451, 4101),
]

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

MAX_TESTS = 40        # boards without the "small" cap
MAX_TESTS_SMALL = 15  # metro_m0 and other "small" boards


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


def _collect_names():
    """Names of the files the largest selection would run. Host filesystem only, no hardware."""
    tree = _get_cp_tree()
    if not tree:
        return []
    return [name for _, name, _ in _get_test_files(tree, max_count=MAX_TESTS)]


BASICS = _collect_names()


def _run_all(board, rec):
    """The body of the original run() after the host checks, recording instead of ctx.*."""
    cp_tree = _get_cp_tree()
    if not cp_tree:
        rec["stop"] = ("circuitpython_tree", "CPR_CP_TREE not set and ~/circuitpython not found")
        return
    rec["cp_tree"] = cp_tree

    # For small boards, run only the first 15 tests
    max_tests = MAX_TESTS_SMALL if "small" in board.caps else MAX_TESTS
    test_files = _get_test_files(cp_tree, max_count=max_tests)
    rec["selected"] = [name for _, name, _ in test_files]

    if not test_files:
        rec["stop"] = ("test_files", "no test files found in %s/tests/basics/" % cp_tree)
        return

    logging.getLogger(__name__).info("selected %d test files from %s/tests/basics" % (len(test_files), cp_tree))

    passed = 0
    failed = 0
    skipped = 0

    t0 = time.time()

    lost = False
    try:
        repl = board.repl()
    except (farmlib.FarmError, OSError) as e:
        rec["repl_open"] = str(e)
        rec["stop"] = ("repl_open", str(e))
        return
    rec["repl_open"] = None
    with repl:
        for test_path, test_name, _ in test_files:
            # Read the test source
            try:
                with open(test_path) as f:
                    test_src = f.read()
            except OSError:
                rec["files"][test_name] = (None, "cannot read test file")
                skipped += 1
                continue

            # Get expected output
            expected = _get_expected_output(test_path, cp_tree)
            if expected is None:
                rec["files"][test_name] = (None, "cannot get expected output")
                skipped += 1
                continue

            # Run on device
            try:
                stdout, stderr = repl.exec_raw(_wrap(test_src), timeout=20)
            except Exception as e:  # pylint: disable=broad-except
                rec["files"][test_name] = (False, "device execution failed: %s" % str(e)[:200])
                failed += 1
                # Recover: interrupt the hung test, leave the raw REPL (prompt() alone cannot,
                # ^C in raw mode never prints '>>> '), then re-sync on the friendly prompt.
                try:
                    repl.write(b"\x03\x03")
                    time.sleep(0.2)
                    repl.write(b"\x02")
                    repl.prompt()
                    rec["recovered"] += 1
                except Exception as e2:  # pylint: disable=broad-except
                    rec["repl_recover"] = "%s: %s" % (type(e2).__name__, e2)
                    lost = True
                    break
                continue

            # Handle special case: device can signal skip with "SKIP"
            if stdout.strip() == "SKIP":
                rec["files"][test_name] = (None, "device skipped")
                skipped += 1
                continue

            # Normalize and compare
            expected_norm = _normalize_output(expected)
            actual_norm = _normalize_output(stdout)

            if actual_norm == expected_norm:
                rec["files"][test_name] = (True, "")
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
                rec["files"][test_name] = (False, detail)
                failed += 1

    if lost:
        # the console went away mid-run: get the board back before the direct checks
        board.wait_ready()

    # Direct checks: memory error and recursion error (what ctx.device did, without marking
    # the board crashed, so a REPL error here fails only its own check)
    for name, body in (("memory_error", DEVICE_MEMORY_ERROR), ("recursion_error", DEVICE_RECURSION_ERROR)):
        try:
            with board.repl() as r:
                res, out, err = r.run_device(body, timeout=30)
            rec["direct"][name] = DeviceResults(board, res, out, err)
        except (farmlib.FarmError, OSError) as e:
            rec["direct"][name] = "REPL: %s" % e

    elapsed = time.time() - t0

    # Report metrics
    total = passed + failed + skipped
    if total > 0:
        pass_ratio = 100.0 * passed / (passed + failed) if (passed + failed) > 0 else 0
        rec["metrics"]["pass_ratio"] = (pass_ratio, "%", "higher")

    rec["metrics"]["test_time"] = (elapsed, "s", "lower")
    rec["summary"] = (passed > 0 and failed == 0,
                      "passed %d/%d, failed %d, skipped %d" % (passed, passed + failed, failed, skipped))


@pytest.fixture(scope="module")
def run_record(board):
    """Run every selected basics test, then the direct checks, once per board.

    Read-only on the board (raw REPL only), so there is nothing to restore. An unexpected
    host-side exception is kept in the record and fails test_summary.
    """
    rec = {"stop": None, "cp_tree": None, "selected": [], "repl_open": False, "files": {},
           "recovered": 0, "repl_recover": None, "direct": {}, "metrics": {}, "summary": None,
           "error": None}
    try:
        _run_all(board, rec)
    except Exception as e:  # pylint: disable=broad-except
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    return rec


def _reached(rec):
    """Skip a test whose step the run never got to (the original returned early)."""
    if rec["stop"]:
        name, why = rec["stop"]
        if name == "repl_open":
            pytest.skip("not reached: repl_open failed (%s)" % why[:160])
        pytest.skip("not reached: %s skipped (%s)" % (name, why))


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_vid_pid(board):
    """The host can read the board's USB VID:PID."""
    vp = board.vid_pid()
    assert vp is not None, str(vp)


def test_circuitpython_tree(run_record):
    """A CircuitPython checkout is on the host (CPR_CP_TREE or ~/circuitpython)."""
    if run_record["stop"] and run_record["stop"][0] == "circuitpython_tree":
        pytest.skip(run_record["stop"][1])
    if run_record["error"] and not run_record["cp_tree"]:
        pytest.skip("not reached: %s" % run_record["error"][:200])


def test_test_files(run_record):
    """tests/basics/ in that checkout has files to run."""
    stop = run_record["stop"]
    if stop and stop[0] == "test_files":
        pytest.skip(stop[1])
    if stop and stop[0] == "circuitpython_tree":
        pytest.skip("not reached: %s skipped (%s)" % stop)
    if run_record["error"] and not run_record["selected"]:
        pytest.skip("not reached: %s" % run_record["error"][:200])


def test_repl_open(run_record):
    """The raw REPL opens for the run."""
    stop = run_record["stop"]
    if stop and stop[0] in ("circuitpython_tree", "test_files"):
        pytest.skip("not reached: %s skipped (%s)" % stop)
    if run_record["repl_open"] is False:
        pytest.skip("not reached: %s" % (run_record["error"] or "the run stopped earlier")[:200])
    assert run_record["repl_open"] is None, run_record["repl_open"]


@pytest.mark.parametrize("name", BASICS)
def test_basics(run_record, name):
    """One tests/basics/ file: device output matches the expected output."""
    _reached(run_record)
    if name not in run_record["selected"]:
        pytest.skip("not selected on this board (first %d files only)" % len(run_record["selected"]))
    if name not in run_record["files"]:
        if run_record["repl_recover"]:
            pytest.skip("not reached: repl_recover failed (%s)" % run_record["repl_recover"][:160])
        if run_record["error"]:
            pytest.skip("not reached: %s" % run_record["error"][:200])
        pytest.fail("%s was selected but never ran" % name, pytrace=False)
    ok, detail = run_record["files"][name]
    if ok is None:
        pytest.skip(detail)
    assert ok, detail


def test_repl_recover(run_record):
    """After a hung or failed test, Ctrl-C and Ctrl-B get the REPL back."""
    _reached(run_record)
    assert run_record["repl_recover"] is None, run_record["repl_recover"]
    if not run_record["recovered"]:
        pytest.skip("no test needed recovery")


@pytest.mark.parametrize("name", ["memory_error", "recursion_error"])
def test_direct(run_record, name):
    """memory_error: 256 MB bytearray raises MemoryError. recursion_error: 100000-deep
    recursion raises RuntimeError, not a hard fault."""
    _reached(run_record)
    res = run_record["direct"].get(name)
    if res is None:
        pytest.skip("not reached: %s" % (run_record["error"] or "direct checks did not run")[:200])
    assert not isinstance(res, str), res
    if res.checks.get(name):
        res.expect(name)
    assert not res.err, "device_exception: " + res.err[-400:]
    res.expect(name)


def test_pass_ratio(run_record, metric):
    """Percentage of run tests that passed, compared with this board's history."""
    _reached(run_record)
    if "pass_ratio" not in run_record["metrics"]:
        pytest.skip(run_record["error"] or "no tests ran")
    value, unit, better = run_record["metrics"]["pass_ratio"]
    metric("pass_ratio", value, unit, better=better)


def test_test_time(run_record, metric):
    """Total time for all the tests and the direct checks."""
    _reached(run_record)
    if "test_time" not in run_record["metrics"]:
        pytest.skip("not reached: %s" % (run_record["error"] or "run did not finish")[:200])
    value, unit, better = run_record["metrics"]["test_time"]
    metric("test_time", value, unit, better=better)


def test_summary(run_record):
    """At least one test passed and none failed."""
    _reached(run_record)
    assert not run_record["error"], "run failed: " + run_record["error"]
    ok, detail = run_record["summary"]
    assert ok, detail
