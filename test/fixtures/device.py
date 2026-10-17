# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Device fixtures: run CircuitPython code on the board and assert on what it reported.

Device code calls check(), skip(), metric() and golden() from lib/devtest.py, exactly as in
tests/*.py. Run it once per board in a module-scoped fixture, then give each check its own
test function:

    @pytest.fixture(scope="module")
    def results(device):
        return device.run(DEVICE, timeout=60)

    def test_bitmap_size(results):
        results.expect("bitmap_size")
"""
import pytest

import run as cpr_run  # recover(): the same power cycle and health check run.py uses


class DeviceResults:
    """What one device run reported. Checks and metrics with the same name are kept apart."""

    def __init__(self, board, records, out, err, crashed=None):
        self.board = board
        self.records = records
        self.out = out
        self.err = err.strip()
        self.crashed = crashed  # the host-side exception if the run never finished
        self.retried = []  # earlier attempts that lost the board (boards with power.tries > 1)
        self.checks = {}
        self.metrics = {}
        self.goldens = {}
        for r in records:
            kind = r.get("kind")
            bucket = self.metrics if kind == "metric" else self.goldens if kind == "golden" else self.checks
            bucket.setdefault(r.get("name", "?"), []).append(r)

    def _missing(self, name, what):
        # A run that died or raised partway is charged once, by test_device_ran /
        # expect_no_exception. Checks it never reached skip instead of failing again.
        if self.crashed:
            pytest.skip("not reached: the device run failed (%s)" % self.crashed[:200])
        if self.err:
            pytest.skip("not reached: the device code raised (%s)" % self.err[-200:])
        pytest.fail("device never reported %s %r" % (what, name), pytrace=False)

    def expect(self, name):
        """Assert every check called `name` passed. A device-side skip() becomes a pytest skip."""
        rs = self.checks.get(name)
        if not rs:
            self._missing(name, "check")
        if all(r.get("ok") is None for r in rs):
            pytest.skip(rs[0].get("detail") or "skipped on the device")
        bad = [r for r in rs if r.get("ok") is False]
        assert not bad, "%s: %s" % (name, "; ".join(str(r.get("detail", "")) for r in bad)[:500])
        return rs[-1]

    def detail(self, name):
        """The detail text of the last check called `name`, or None."""
        rs = self.checks.get(name)
        return rs[-1].get("detail") if rs else None

    def value(self, name):
        """The value of the last check called `name`, or None."""
        rs = self.checks.get(name)
        return rs[-1].get("value") if rs else None

    def metric_record(self, name):
        """The last metric record called `name`; fails the test if the device never sent it."""
        rs = self.metrics.get(name)
        if not rs:
            self._missing(name, "metric")
        return rs[-1]

    def golden_value(self, name):
        """The value of the last golden() called `name`; fails if the device never sent it."""
        rs = self.goldens.get(name)
        if not rs:
            self._missing(name, "golden")
        return rs[-1].get("value")

    def expect_no_exception(self):
        """The device code ran to the end without raising, and the board kept talking."""
        assert not self.crashed, "device run failed: " + self.crashed[-400:]
        assert not self.err, "device raised: " + self.err[-400:]


class Device:
    """Runs device code on one board through the raw REPL (chunked on "small" boards)."""

    def __init__(self, board):
        self.board = board

    def run(self, body, timeout=60):
        """Run body with the devtest helpers prepended. Returns DeviceResults.

        If the board stops answering (a crash, a reset, USB dropping), the results so far are
        kept and `crashed` says why, so one crash fails one test rather than every check.

        A board with power.tries > 1 (one that does not always come up) is power-cycled and the
        run repeated, up to that many runs in all. The failed attempts are printed and kept in
        `retried`, so a pass that needed a retry still shows in the log.
        """
        tries = self.board.power.get("tries", 1)
        retried = []
        while True:
            try:
                res = self._run_once(body, timeout)
            except Exception as e:  # pylint: disable=broad-except
                why = "%s: %s" % (type(e).__name__, e)
                if len(retried) + 1 < tries:
                    ok, rwhy = cpr_run.recover(self.board)
                    if ok:
                        retried.append("attempt %d: %s" % (len(retried) + 1, why[:200]))
                        print("%s: device run %s; power-cycled, running again"
                              % (self.board.name, retried[-1]))
                        continue
                    why += "; power cycle: " + rwhy[:200]
                res = self._crashed(why, retried)
            res.retried = retried
            return res

    def _run_once(self, body, timeout):
        with self.board.repl() as repl:
            records, out, err = repl.run_device(body, timeout=timeout)
        return DeviceResults(self.board, records, out, err)

    def _crashed(self, why, retried):
        if retried:
            why = "%s (after %d earlier attempts: %s)" % (why, len(retried), " | ".join(retried))
        self.board.cpr_crashed = why  # later tests in this module skip (see farm.py)
        # In a module fixture's setup, the current test has not been counted yet; inside a
        # test body it has. Either way the crash belongs to the current test.
        started = getattr(self.board, "cpr_started", 0)
        self.board.cpr_crashed_at = started - 1 if getattr(self.board, "cpr_in_test", False) else started
        return DeviceResults(self.board, [], "", "", crashed=why)


@pytest.fixture(scope="module")
def device(board):
    """Device(board): .run(body, timeout) -> DeviceResults."""
    return Device(board)
