#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""cp-regress: run CircuitPython regression fixtures on the HIL farm.

Examples
  ./run.py --list
  ./run.py --tier smoke                       # every board, smoke tests
  ./run.py --tier nightly --boards metro_rp2040,metro_esp32s3
  ./run.py --only usb_enumeration,perf_core --label "11.0.0-alpha.1+pr1234"
  ./run.py --dry-run --tier release           # show what would run and why things skip

Tiers are cumulative: nightly includes smoke, release includes both.
Results land in results/<stamp>/ (results.jsonl, summary.md). Metrics append to
history/metrics.jsonl and are compared to the median of earlier runs.
"""

import argparse
import concurrent.futures as cf
import fcntl
import importlib.util
import json
import os
import statistics
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import farmlib  # noqa: E402

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

TIERS = ["smoke", "nightly", "release"]
HISTORY = os.path.join(HERE, "history", "metrics.jsonl")
GOLDEN = os.path.join(HERE, "history", "golden.json")
_GOLDEN_LOCK = threading.Lock()


class Ctx:
    """Handed to every test's run(board, ctx)."""

    def __init__(self, board, meta, cfg, boards, outdir, label, dry=False):
        self.board = board
        self.meta = meta
        self.cfg = cfg
        self.boards = boards
        self.outdir = outdir
        self.label = label
        self.dry = dry
        self.results = []
        self.logs = []

    # ----- results -----
    def _add(self, name, ok, detail="", value=None, **extra):
        r = {"test": self.meta["id"], "board": self.board.name, "name": name, "ok": ok,
             "detail": str(detail)[:500], "value": value}
        r.update(extra)
        self.results.append(r)
        return ok

    def check(self, name, ok, detail="", value=None):
        return self._add(name, bool(ok), detail, value)

    def skip(self, name, why=""):
        return self._add(name, None, why)

    def log(self, msg):
        self.logs.append(msg)

    def metric(self, name, value, unit="", better="lower", tolerance=0.25):
        """Record a number; fail if it is worse than the running baseline by > tolerance."""
        base = baseline(self.board.name, self.meta["id"] + "." + name)
        ok = True
        detail = "no baseline yet"
        if base is not None and value is not None and base > 0:
            ratio = value / base
            worse = ratio > 1 + tolerance if better == "lower" else ratio < 1 - tolerance
            ok = not worse
            detail = "%.3g %s vs baseline %.3g (x%.2f)" % (value, unit, base, ratio)
        self._add(name, ok, detail, value, kind="metric", unit=unit, better=better)

    def golden(self, name, value):
        """Exact-match a value against the first one recorded for this board (history/golden.json).

        For outputs whose correct value we do not want to recompute on the host (rendered pixel
        checksums, parser results). Delete the key from golden.json to accept an intended change.
        """
        key = "%s|%s.%s" % (self.board.name, self.meta["id"], name)
        with _GOLDEN_LOCK:
            data = {}
            if os.path.exists(GOLDEN):
                with open(GOLDEN) as f:
                    data = json.load(f)
            fresh = key not in data
            if fresh and not self.dry:
                data[key] = value
                os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
                with open(GOLDEN, "w") as f:
                    json.dump(data, f, indent=1, sort_keys=True)
        if fresh:
            return self._add(name, True, "recorded golden value %r" % (value,), value, kind="golden")
        ok = data[key] == value
        return self._add(name, ok, "golden %r, now %r%s" % (data[key], value,
                         "" if ok else " (delete %s from history/golden.json if intended)" % key),
                         value, kind="golden")

    def absorb(self, device_results):
        """Fold @@R lines from the device into this test's results."""
        for r in device_results:
            if r.get("kind") == "metric":
                self.metric(r.get("name"), r.get("value"), r.get("unit", ""), r.get("better", "lower"))
            elif r.get("kind") == "golden":
                self.golden(r.get("name"), r.get("value"))
            else:
                self._add(r.get("name", "?"), r.get("ok"), r.get("detail", ""), r.get("value"))

    # ----- conveniences -----
    def device(self, body, timeout=60):
        """Run a device-side body (devtest helpers available) and absorb its results."""
        with self.board.repl() as repl:
            res, out, err = repl.run_device(body, timeout=timeout)
        self.absorb(res)
        if err.strip():
            self._add("device_exception", False, err.strip()[-400:])
        return res, out, err

    def other(self, cap, exclude_self=True):
        for b in self.boards:
            if cap in b.caps and (b is not self.board or not exclude_self) and b.present():
                return b
        return None

    def secret(self, key):
        env = self.cfg.get("network", {}).get(key)
        return os.environ.get(env) if env else None


def baseline(board, metric, n=5):
    if not os.path.exists(HISTORY):
        return None
    vals = []
    with open(HISTORY) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("board") == board and r.get("metric") == metric and isinstance(r.get("value"), (int, float)):
                vals.append(r["value"])
    return statistics.median(vals[-n:]) if vals else None


def load_tests():
    tests = []
    tdir = os.path.join(HERE, "tests")
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("cpr_" + fn[:-3], os.path.join(tdir, fn))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            print("!! cannot load %s: %s" % (fn, e), file=sys.stderr)
            continue
        meta = getattr(mod, "META", None)
        if not meta or not hasattr(mod, "run"):
            print("!! %s has no META/run, skipped" % fn, file=sys.stderr)
            continue
        meta.setdefault("id", fn[:-3])
        meta.setdefault("needs", [])
        meta.setdefault("wiring", [])
        meta.setdefault("tier", "nightly")
        meta.setdefault("timeout", 300)
        meta.setdefault("multi_board", False)
        tests.append((meta, mod))
    return tests


def applicable(meta, board):
    missing = [c for c in meta["needs"] if c not in board.caps]
    if missing:
        return "board lacks " + ",".join(missing)
    missing = [w for w in meta["wiring"] if w not in board.wiring]
    if missing:
        return "fixture not wired: " + ",".join(missing)
    if board.port in meta.get("skip_ports", []):
        return "skipped on port " + board.port
    if board.name in meta.get("skip_boards", []):
        return "skipped on board " + board.name
    return None


def board_health(board):
    """(ok, why): on the bus, tty present, REPL evaluates, not in safe mode."""
    if not board.present():
        return False, "off the bus"
    if not board.tty():
        return False, "no tty"
    # Two tries on fresh opens: the first open after a boot can get no reply at all (seen on
    # the Metro M0, 1 of 5), while every later open works.
    for attempt in (1, 2):
        try:
            with board.repl() as r:
                # The safe-mode banner prints at boot, before we open the port, so ask the board.
                out, err = r.exec_raw("import supervisor\nprint('cpr', 6 * 7)\n"
                                      "print('cpr_sm', supervisor.runtime.safe_mode_reason)",
                                      timeout=10)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return False, "REPL: %s: %s" % (type(e).__name__, str(e)[-120:])
            time.sleep(2)
    for line in out.splitlines():
        if line.startswith("cpr_sm ") and not line.rstrip().endswith(".NONE"):
            return False, "safe mode: " + line[7:].strip()
    if "cpr 42" not in out:
        return False, "REPL eval returned %r %r" % (out[-60:], err[-60:])
    return True, ""


def recover(board):
    """Power cycle and re-check, up to power.tries times (default 1). Returns (ok, why)."""
    tries = board.power.get("tries", 1)
    whys = []
    for _ in range(tries):
        try:
            board.power_cycle()
            time.sleep(board.power.get("settle", 12))
        except Exception as e:  # noqa: BLE001
            whys.append("power cycle: %s" % e)
            break
        ok, why = board_health(board)
        if ok:
            return True, ("healthy after %d power cycles (%s)" % (len(whys) + 1, "; ".join(whys))
                          if whys else "")
        whys.append(why)
    # A VBUS cycle does not reset every board (the M4 AirLift sat off the bus until an SWD
    # init; reset run): the probe is the last rung, for boards that have one.
    if (getattr(board, "flash", None) or {}).get("method") == "swd":
        import flash as cpr_flash  # noqa: PLC0415  (pulls in openocd paths only when needed)
        try:
            ran = cpr_flash.swd_reset(board)
        except Exception as e:  # noqa: BLE001
            return False, "; ".join(whys + ["swd reset: %s" % e])
        time.sleep(board.power.get("settle", 12))
        ok, why = board_health(board)
        if ok:
            return True, "healthy after an SWD reset (%s)" % "; ".join(whys)
        whys.append("after an SWD reset%s: %s" % ("" if ran else " (openocd failed)", why))
    return False, "; ".join(whys)


def run_board(board, tests, cfg, boards, outdir, label, dry):
    out = []
    down = None
    lock_path = "/tmp/cp-regress-%s.lock" % board.usb
    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [{"test": "*", "board": board.name, "name": "lock", "ok": False,
                     "detail": "board busy: " + lock_path}]
        if not dry and not board.present():
            return [{"test": "*", "board": board.name, "name": "present", "ok": False,
                     "detail": "not on the bus at " + board.usb}]
        if not dry:
            # Start from a healthy board: one left in safe mode by an earlier crash would
            # otherwise fail the first test for something it did not do.
            ok, why = board_health(board)
            if not ok:
                ok, rwhy = recover(board)
                out.append({"test": "*", "board": board.name, "name": "_preflight_recovered",
                            "ok": ok, "detail": "%s -> %s" % (why, rwhy or "healthy")})
                if not ok:
                    down = "unhealthy before the run: " + why
        for meta, mod in tests:
            why = applicable(meta, board)
            ctx = Ctx(board, meta, cfg, boards, outdir, label, dry)
            if why:
                ctx.skip("applicable", why)
            elif down:
                ctx.check("board_down", False, down)
            elif dry:
                ctx.skip("dry_run", "would run")
            else:
                t0 = time.time()
                try:
                    mod.run(board, ctx)
                except Exception as e:  # noqa: BLE001
                    ctx.check("harness", False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-600:]))
                    # leave the board usable for the next test
                    try:
                        board.wait_ready(timeout=30)
                    except Exception:  # noqa: BLE001
                        pass
                ctx.results.append({"test": meta["id"], "board": board.name, "name": "_duration",
                                    "ok": True, "value": round(time.time() - t0, 1)})
                # A test that kills the board is charged with it, once; later tests get a clean
                # board or are marked board_down instead of failing in confusing ways.
                ok, hwhy = board_health(board)
                if not ok:
                    ctx.check("board_survived", False, hwhy)
                    ok, rwhy = recover(board)
                    ctx.results.append({"test": meta["id"], "board": board.name,
                                        "name": "_recovered", "ok": ok, "detail": rwhy})
                    if not ok:
                        down = "board did not recover after %s: %s" % (meta["id"], rwhy)
            out.extend(ctx.results)
            status = summarize_one(ctx.results)
            print("  %-18s %-24s %s" % (board.name, meta["id"], status), flush=True)
    return out


def summarize_one(results):
    real = [r for r in results if not r["name"].startswith("_")]
    if any(r["ok"] is False for r in real):
        bad = [r["name"] for r in real if r["ok"] is False]
        return "FAIL (" + ", ".join(bad[:4]) + ")"
    if real and all(r["ok"] is None for r in real):
        return "skip: " + (real[0].get("detail") or "")
    return "pass"


def write_summary(outdir, results, tests, boards, label, versions):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "results.jsonl"), "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    ids = [m["id"] for m, _ in tests]
    groups = {}
    for r in results:
        if not r["name"].startswith("_"):
            groups.setdefault((r["test"], r["board"]), []).append(r["ok"])
    dry = {(r["test"], r["board"]) for r in results if r["name"] == "dry_run"}
    grid = {}
    for k, oks in groups.items():
        if k in dry:
            grid[k] = "would run"
        elif False in oks:
            grid[k] = "**FAIL**"
        elif all(o is None for o in oks):
            grid[k] = "skip"
        else:
            grid[k] = "pass"
    lines = ["# cp-regress run %s" % os.path.basename(outdir), "", "Label: %s" % (label or "-"), ""]
    lines.append("| Board | Version |")
    lines.append("|---|---|")
    for b in boards:
        lines.append("| %s | %s |" % (b.name, versions.get(b.name) or "?"))
    lines += ["", "| Test | " + " | ".join(b.name for b in boards) + " |",
              "|---|" + "---|" * len(boards)]
    for t in ids:
        lines.append("| %s | " % t + " | ".join(grid.get((t, b.name), "-") for b in boards) + " |")
    fails = [r for r in results if r["ok"] is False]
    if fails:
        lines += ["", "## Failures", ""]
        for r in fails:
            lines.append("- `%s` on %s: **%s** %s" % (r["test"], r["board"], r["name"], r.get("detail", "")[:300]))
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return len(fails)


def record_history(results, label, versions):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "a") as f:
        for r in results:
            if r.get("kind") == "metric" and isinstance(r.get("value"), (int, float)):
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "board": r["board"],
                                    "metric": r["test"] + "." + r["name"], "value": r["value"],
                                    "unit": r.get("unit", ""), "label": label,
                                    "version": versions.get(r["board"])}) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "farm.toml"))
    ap.add_argument("--boards", default="all")
    ap.add_argument("--tier", default="smoke", choices=TIERS)
    ap.add_argument("--only", default="")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--expect-version", default="", help="substring every boot_out.txt must contain, e.g. 11.0.0-alpha")
    ap.add_argument("--jobs", type=int, default=4, help="boards tested in parallel")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-history", action="store_true", help="do not add this run's metrics to the baseline")
    a = ap.parse_args()

    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    boards = [farmlib.Board(b, cfg.get("host", {})) for b in cfg.get("board", [])]
    if a.boards != "all":
        want = set(a.boards.split(","))
        boards = [b for b in boards if b.name in want]
    tests = load_tests()
    max_tier = TIERS.index(a.tier)
    tests = [(m, mod) for m, mod in tests if TIERS.index(m["tier"]) <= max_tier]
    if a.only:
        keep = set(a.only.split(","))
        tests = [(m, mod) for m, mod in tests if m["id"] in keep]
    if a.exclude:
        drop = set(a.exclude.split(","))
        tests = [(m, mod) for m, mod in tests if m["id"] not in drop]

    if a.list:
        print("%-24s %-8s %-14s %-10s %s" % ("id", "tier", "automation", "category", "targets"))
        for m, _ in tests:
            print("%-24s %-8s %-14s %-10s %s" % (m["id"], m["tier"], m.get("automation", "?"),
                                                 m.get("category", ""), ",".join("#%s" % t for t in m.get("targets", [])[:6])))
        return 0

    if farmlib.serial is None and not a.dry_run:
        print("pyserial missing: pip install --user pyserial", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(HERE, "results", stamp)
    versions = {}
    if not a.dry_run:
        for b in boards:
            try:
                versions[b.name] = b.version() if b.present() else None
            except farmlib.FarmError as e:
                versions[b.name] = "ERROR " + str(e)
    if a.expect_version:
        wrong = {k: v for k, v in versions.items() if not v or a.expect_version not in v}
        if wrong:
            print("version mismatch, refusing to run: %s" % wrong, file=sys.stderr)
            return 3

    single = [(m, mod) for m, mod in tests if not m["multi_board"]]
    multi = [(m, mod) for m, mod in tests if m["multi_board"]]
    print("cp-regress %s: %d tests x %d boards, tier=%s" % (stamp, len(tests), len(boards), a.tier))
    results = []
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = [ex.submit(run_board, b, single, cfg, boards, outdir, a.label, a.dry_run) for b in boards]
        for fu in futs:
            results.extend(fu.result())
    # multi-board tests run after, one board at a time (they grab a partner via ctx.other)
    for b in boards:
        if multi:
            results.extend(run_board(b, multi, cfg, boards, outdir, a.label, a.dry_run))

    nfail = write_summary(outdir, results, tests, boards, a.label, versions)
    if not a.dry_run and not a.no_history:
        record_history(results, a.label, versions)
    print("\nsummary: %s  (%d failing checks)" % (os.path.join(outdir, "summary.md"), nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
