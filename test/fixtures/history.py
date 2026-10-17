# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Metric and golden-value fixtures, sharing history/ with run.py.

metric() compares a number with the median of this board's last five runs and fails the test
when it is worse by more than the tolerance. golden() stores the first value seen on a board
and fails later runs that differ. Both lock their files, so xdist workers can write at once.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager

import pytest

import run as cpr_run  # HISTORY, GOLDEN paths and baseline()


@contextmanager
def _locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _test_id(request):
    """perf_core for test/hardware/perf_core_test.py, the id run.py uses."""
    name = request.node.module.__name__.rsplit(".", 1)[-1]
    return name[:-5] if name.endswith("_test") else name


@pytest.fixture
def metric(request, board):
    """metric(name, value, unit="", better="lower", tolerance=0.25, slack=0): record and compare.

    slack is an absolute margin in the metric's unit: a change smaller than it never fails,
    for values whose resolution or host-side noise is large next to the value (compare()).
    The value is attached to the test report (user_properties, shown in --junitxml) and, unless
    --no-history, appended to history/metrics.jsonl for later runs to compare against.
    """
    def record(name, value, unit="", better="lower", tolerance=0.25, slack=0):
        key = _test_id(request) + "." + name
        request.node.user_properties.append((name, value))
        assert isinstance(value, (int, float)), "%s: no numeric value (%r)" % (name, value)
        base = cpr_run.baseline(board.name, key)
        if not request.config.getoption("no_history"):
            with _locked(cpr_run.HISTORY):
                with open(cpr_run.HISTORY, "a") as f:
                    f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "board": board.name,
                                        "metric": key, "value": value, "unit": unit,
                                        "label": request.config.getoption("label"),
                                        "runner": "pytest"}) + "\n")
        if base is None or base <= 0:
            return "no baseline yet"
        worse, detail = compare(value, base, unit, better, tolerance, slack)
        assert not worse, "%s slower than baseline: %s" % (name, detail)
        return detail

    return record


def compare(value, base, unit="", better="lower", tolerance=0.25, slack=0):
    """(worse, detail): worse when the value is past the baseline by more than `tolerance` as a
    ratio AND by more than `slack` in absolute terms."""
    ratio = value / base
    if better == "lower":
        worse = ratio > 1 + tolerance and value - base > slack
    else:
        worse = ratio < 1 - tolerance and base - value > slack
    detail = "%.3g %s vs baseline %.3g (x%.2f)" % (value, unit, base, ratio)
    if slack:
        detail += ", slack %g %s" % (slack, unit)
    return worse, detail


@pytest.fixture
def golden(request, board):
    """golden(name, value): the first value recorded for this board must match from then on.

    Delete the key from history/golden.json to accept an intended change.
    """
    def check(name, value):
        key = "%s|%s.%s" % (board.name, _test_id(request), name)
        dry = request.config.getoption("cpr_dry_run")
        with _locked(cpr_run.GOLDEN):
            data = {}
            if os.path.exists(cpr_run.GOLDEN):
                with open(cpr_run.GOLDEN) as f:
                    data = json.load(f)
            if key not in data:
                if not dry:
                    data[key] = value
                    with open(cpr_run.GOLDEN, "w") as f:
                        json.dump(data, f, indent=1, sort_keys=True)
                return "recorded golden value %r" % (value,)
        assert data[key] == value, ("golden %r, now %r (delete %s from history/golden.json if intended)"
                                    % (data[key], value, key))
        return "golden %r, now %r" % (data[key], value)

    return check
