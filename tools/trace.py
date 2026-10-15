#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Join the ten-year sweep, the fixtures and a hardware run into docs/FINDINGS-TO-TESTS.md.

Inputs (all in this repo):
  data/sweep-classified.jsonl   one row per bug issue / fix PR from the adafruit/circuitpython sweep
  tests/*.py                    each fixture's META (tier, targets)
  data/first-run-results.jsonl  results.jsonl of a run.py run (default: the first full pass)

usage: python3 tools/trace.py [--results data/first-run-results.jsonl] [--out docs/FINDINGS-TO-TESTS.md]
"""
import argparse
import collections
import glob
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

BOARDS = ["metro_rp2040", "metro_m4_airlift", "metro_m0", "feather_nrf52840", "metro_esp32s2",
          "metro_rp2350", "metro_esp32s3", "feather_stm32f405"]
SHORT = dict(zip(BOARDS, ["RP2040", "M4", "M0", "nRF52840", "S2", "RP2350", "S3", "F405"]))

AREA_NAME = {
    "usb_device": "USB device (drive, CDC, HID)", "display": "Display / displayio",
    "repl_console_boot": "REPL / console / boot", "filesystem_storage": "Filesystem / storage",
    "crash_safemode_hardfault": "Crash / safe mode / hard fault", "wifi_network": "WiFi / networking",
    "bus_i2c_spi_uart": "I2C / SPI / UART", "core_vm_language": "Core VM / language", "ble": "BLE",
    "analog_digital_touch_keypad": "Analog / digital / touch", "pwm_pulse_timing": "PWM / pulse / timing",
    "build_ci_tooling": "Build / CI", "memory_gc": "Memory / GC", "board_definition_pins": "Board definitions / pins",
    "sleep_alarm_power": "Sleep / alarm / power", "audio": "Audio", "web_ble_workflow": "Web / BLE workflow",
    "neopixel_led": "NeoPixel / LED", "time_rtc": "Time / RTC", "docs_other": "Docs / other",
    "camera": "Camera", "toolchain_or_build": "Toolchain",
}
# Which fixtures answer each sweep area. "every run" fixtures plus the post-test health check.
AREA_FIXTURES = {
    "usb_device": ["usb_enumeration", "usb_msc_rw", "usb_cdc_data", "usb_hid_midi", "bootloader_roundtrip"],
    "display": ["displayio_headless", "display_fixture"],
    "repl_console_boot": ["repl_console", "code_py_lifecycle", "reset_cycles"],
    "filesystem_storage": ["usb_msc_rw", "settings_toml"],
    "crash_safemode_hardfault": ["code_py_lifecycle", "memory_soak", "(health check after every fixture)"],
    "wifi_network": ["wifi_join", "net_sockets", "net_tls"],
    "bus_i2c_spi_uart": ["peripherals_selfcheck", "peripherals_loopback"],
    "core_vm_language": ["core_vm", "perf_core"],
    "ble": ["ble_pair"],
    "analog_digital_touch_keypad": ["peripherals_selfcheck", "peripherals_loopback"],
    "pwm_pulse_timing": ["peripherals_selfcheck", "peripherals_loopback"],
    "build_ci_tooling": [],
    "memory_gc": ["memory_soak", "perf_core"],
    "board_definition_pins": ["usb_enumeration", "peripherals_selfcheck"],
    "sleep_alarm_power": ["sleep_alarm"],
    "audio": ["audio_headless"],
    "web_ble_workflow": ["web_workflow"],
    "neopixel_led": ["peripherals_selfcheck"],
    "time_rtc": ["perf_core"],
    "docs_other": [], "camera": [], "toolchain_or_build": [],
}
CAUSE_NAME = {
    "refactor_shared_code": "Refactor of shared code", "sdk_or_idf_upgrade": "SDK / ESP-IDF upgrade",
    "new_feature_side_effect": "Side effect of a new feature", "toolchain_or_build": "Toolchain / build",
    "api_change": "API change", "timing_or_race": "Timing / race", "pin_or_board_config": "Pin / board config",
    "resource_exhaustion": "Resource exhaustion", "upstream_micropython_merge": "Upstream MicroPython merge",
    "memory_gc": "Memory / GC", "unknown": "Not stated",
}
# Target numbers the sweep classifier did not cover (not labelled as bugs).
EXTRA = {
    1623: {"title": "Allow looping audio samples to be stopped after a complete sample", "kind": "issue", "year": 2019},
    6214: {"title": "don't gc_free() ringbuf if no heap; cleanup ringbuf", "kind": "PR", "year": 2022},
    1216: {"title": "NRF52 - SPI baud rate too high??", "kind": "issue", "year": 2018},
}


def load_fixtures():
    fx = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "tests", "*.py"))):
        if os.path.basename(f).startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("t", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fx[mod.META["id"]] = mod.META
    return fx


def load_results(path):
    by = collections.defaultdict(list)
    for line in open(path):
        r = json.loads(line)
        if r["test"] != "*" and not r["name"].startswith("_"):
            by[(r["test"], r["board"])].append(r)
    return by


def fixture_status(fid, meta, res):
    """One short status string for a fixture from the run."""
    if meta["tier"] != "smoke":
        if meta.get("wiring"):
            return "needs wiring; not run yet"
        return "written, %s tier; not run on hardware yet" % meta["tier"]
    passed, failed, skipped, notes = [], [], [], []
    for b in BOARDS:
        rs = res.get((fid, b), [])
        if not rs:
            continue
        if any(r["ok"] is False for r in rs):
            why = [r for r in rs if r["ok"] is False and r["name"] == "board_survived"]
            failed.append("%s (%s)" % (SHORT[b], why[0]["detail"].split(".")[-1] if why else "fail"))
        elif all(r["ok"] is not True for r in rs if r["name"] not in ("mounted", "present")) and \
                any(r["ok"] is None and r["name"] == "rest" for r in rs):
            skipped.append(SHORT[b])
        else:
            passed.append(SHORT[b])
    s = "pass %d/8" % len(passed)
    if failed:
        s += "; **fail: %s**" % ", ".join(failed)
    if skipped:
        s += "; skipped: %s" % ", ".join(skipped)
    return s


def issue_checks(res):
    """Checks whose detail cites an issue number: {issue: (check, Counter)} for check-level status."""
    import re
    out = {}
    for (fid, b), rs in res.items():
        for r in rs:
            for n in re.findall(r"#(\d{3,5})", r.get("detail") or ""):
                c = out.setdefault(int(n), (r["name"], collections.Counter()))[1]
                c["pass" if r["ok"] else ("skip" if r["ok"] is None else "fail")] += 1
    return out


def notable_skips(res):
    """Informational skips worth surfacing (spec deviations, not missing features)."""
    out = collections.defaultdict(list)
    for (fid, b), rs in res.items():
        for r in rs:
            if r["ok"] is None and r["name"] == "spec_leading_zero":
                out[(fid, r["name"], r["detail"])].append(SHORT[b])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(ROOT, "data", "first-run-results.jsonl"))
    ap.add_argument("--sweep", default=os.path.join(ROOT, "data", "sweep-classified.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "FINDINGS-TO-TESTS.md"))
    a = ap.parse_args()

    sweep = {}
    for line in open(a.sweep):
        x = json.loads(line)
        sweep[x["n"]] = x
    fx = load_fixtures()
    res = load_results(a.results)
    status = {k: fixture_status(k, m, res) for k, m in fx.items()}
    ichk = issue_checks(res)

    issues = [x for x in sweep.values() if x["kind"] == "issue"]
    regs = [x for x in issues if x["is_regression"]]
    area_regs = collections.Counter(x["area"] for x in regs)
    cause_regs = collections.Counter(x["cause"] for x in regs)
    targets = collections.defaultdict(list)
    for k, m in fx.items():
        for n in m["targets"]:
            targets[n].append(k)
    tgt_regs = [n for n in targets if sweep.get(n, {}).get("is_regression")]

    L = []
    w = L.append
    w("# From the sweep to the tests to the results")
    w("")
    w("Generated by `tools/trace.py` from `data/sweep-classified.jsonl` (%d classified bug issues and fix PRs "
      "from adafruit/circuitpython, 2016 to 2026), each fixture's `META`, and `data/first-run-results.jsonl` "
      "(smoke tier, 8 boards, CircuitPython main at 55488150be, 11.0.0-alpha.1-86)." % len(sweep))
    w("")
    w("Each row reads left to right: what the sweep found, what was built for it, what the first hardware "
      "run showed. Only the smoke tier has run on hardware so far; nightly and release fixtures are written "
      "and statically checked.")
    w("")

    # ---------- 1. by finding ----------
    smoke = [k for k, m in fx.items() if m["tier"] == "smoke"]
    boot_areas = ["usb_device", "repl_console_boot", "filesystem_storage", "crash_safemode_hardfault"]
    boot = sum(area_regs[x] for x in boot_areas)
    lz = notable_skips(res)
    lz_boards = next(iter(lz.values()), [])
    w("## 1. Sweep findings → what was built → what the first run showed")
    w("")
    w("| Sweep finding | Built in response | First run (main @ 55488150be) |")
    w("|---|---|---|")
    w("| %d bug issues are regressions (\"worked in X, broke in Y\") | %d fixtures targeting %d past issues, %d of them "
      "regressions by number | smoke tier: 63 of 64 board × fixture results pass, 1 real firmware bug |"
      % (len(regs), len(fx), len(targets), len(tgt_regs)))
    w("| The boot path (USB, REPL, filesystem, safe mode) holds %d of %d regressions (%d%%) | 5 of the 8 smoke "
      "fixtures run it on every board: usb_enumeration, usb_msc_rw, repl_console, code_py_lifecycle, settings_toml "
      "| all pass on 8 boards (S2 MSC skipped: drive full) |" % (boot, len(regs), round(100 * boot / len(regs))))
    w("| Median 7.5 days from merge to first user report; 76% within 30 days | smoke tier sized to run per merge "
      "| about 2 min of tests per board, all 8 in parallel, about 5 min wall clock |")
    w("| Crash / safe mode / hard fault: %d regressions | a health check after every fixture reads "
      "`supervisor.runtime.safe_mode_reason`; code_py_lifecycle checks recursion and MemoryError raise cleanly "
      "| **found: STM32F405 hard-faults when a list grows past 4096 items in a fragmented heap** (deterministic; "
      "other boards raise MemoryError) |" % area_regs["crash_safemode_hardfault"])
    w("| Top known cause: refactor of shared code (%d), breaking ports the author did not test | every run covers "
      "five port families (atmel-samd ×2, raspberrypi ×2, espressif ×2, nordic, stm) | the STM32 bug is port-only; "
      "whether it is new in 11.0 needs a 10.x control |" % cause_regs["refactor_shared_code"])
    w("| SDK / ESP-IDF upgrades: %d regressions | ESP32-S2 and ESP32-S3 in every run | both pass all smoke fixtures |"
      % cause_regs["sdk_or_idf_upgrade"])
    w("| Upstream MicroPython merges: %d regressions | core_vm (40 tests/basics files on the board) and perf_core "
      "| perf_core passes 7/8 with 114 timing metrics recorded; core_vm not run yet |"
      % cause_regs["upstream_micropython_merge"])
    w("| Slowdowns go unreported for months (#10733, CircuitPython 10 3× slower) | perf_core metrics against "
      "the median of the last 5 runs | values recorded; baselines not seeded yet, so no slowdown verdicts |")
    w("| atmel-samd has the highest regression rate among large ports (20%); small boards get broken by changes "
      "tested on bigger chips | Metro M0 (SAMD21) on the farm, `small` cap, sizes scaled down | M0 passes 8/8; "
      "the harness itself needed chunked code for its 15 KB heap |")
    w("| settings.toml regressions (#11118, #7677) and a TOML spec rule | settings_toml checks types, escapes, "
      "unicode and toml.io rejections | **found: `017` read as 15 (octal) on %d boards; toml.io says invalid** |"
      % len(lz_boards))
    w("| Display / displayio: %d regressions | displayio_headless (Bitmap, Group, TileGrid, vectorio, bitmaptools, "
      "render checksum); display_fixture needs a TFT | headless passes 8/8; checksum golden not seeded yet |"
      % area_regs["display"])
    w("| WiFi / networking: %d regressions; web workflow: %d | wifi_join, net_sockets, net_tls, web_workflow "
      "(S2, S3) | nightly tier, not run yet |" % (area_regs["wifi_network"], area_regs["web_ble_workflow"]))
    w("| I2C / SPI / UART: %d, analog/digital: %d, PWM/timing: %d | peripherals_selfcheck (no wiring); "
      "peripherals_loopback (jumpers + sensor) | selfcheck passes 8/8; loopback needs wiring |"
      % (area_regs["bus_i2c_spi_uart"], area_regs["analog_digital_touch_keypad"], area_regs["pwm_pulse_timing"]))
    w("| BLE: %d regressions | ble_pair (nRF52840 ↔ S3) | nightly, not run yet |" % area_regs["ble"])
    w("| 85%% of regressions judged catchable by a scripted hardware test | %d fixtures: %d smoke, %d nightly, %d "
      "release | 8 smoke fixtures run; the rest are next |"
      % (len(fx), len(smoke), sum(1 for m in fx.values() if m["tier"] == "nightly"),
         sum(1 for m in fx.values() if m["tier"] == "release")))
    w("")

    # ---------- 2. by area ----------
    w("## 2. By sweep area")
    w("")
    w("Regression counts are the sweep's (issues only). \"Targeted issues\" counts every issue in that area "
      "a fixture names by number, regressions or not, so it can exceed the regression count (WiFi). An area "
      "can have fixtures but 0 targeted issues: the fixture checks the behaviour without naming an issue there.")
    w("")
    w("| Area | Regressions | Targeted issues | Fixtures | First run |")
    w("|---|---|---|---|---|")
    for area, n in area_regs.most_common():
        names = AREA_FIXTURES.get(area, [])
        tg = sum(1 for t in targets if sweep.get(t, {}).get("area") == area)
        if not names:
            st = "no fixture (outside hardware testing)" if area in ("build_ci_tooling", "docs_other", "toolchain_or_build") else "no fixture yet"
        else:
            run = [x for x in names if x in fx and fx[x]["tier"] == "smoke"]
            st = "; ".join("%s %s" % (x, status[x]) for x in run) or "not run yet"
            if any("fail" in status[x] for x in run):
                st += " (F405 bug)"
        w("| %s | %d | %d | %s | %s |" % (AREA_NAME.get(area, area), n, tg, ", ".join(names) or "-", st))
    w("")

    # ---------- 3. by fixture ----------
    w("## 3. By fixture")
    w("")
    w("| Fixture | Tier | Targets | of which regressions | First run |")
    w("|---|---|---|---|---|")
    order = {"smoke": 0, "nightly": 1, "release": 2}
    for k, m in sorted(fx.items(), key=lambda kv: (order[kv[1]["tier"]], kv[0])):
        nreg = sum(1 for t in m["targets"] if sweep.get(t, {}).get("is_regression"))
        w("| [%s](fixtures/%s.md) | %s | %d | %d | %s |" % (k, k, m["tier"], len(m["targets"]), nreg, status[k]))
    w("")

    # ---------- 4. by issue ----------
    w("## 4. Every targeted issue")
    w("")
    w("Status is the fixture's first-run result. A fixture can pass while the one check aimed at an issue "
      "skips on some boards, so where a check cites its issue number the status is that check's own result "
      "(marked *check*). Each fixture's checks are listed in `docs/fixtures/<id>.md`.")
    w("")
    w("| # | Year | Title | Area | Regression | Cause | Fixture | First run |")
    w("|---|---|---|---|---|---|---|---|")
    for n in sorted(targets):
        x = sweep.get(n) or dict(EXTRA.get(n, {}), area="", is_regression=None, cause="")
        title = (x.get("title") or "").replace("|", "\\|")
        if len(title) > 70:
            title = title[:67] + "..."
        reg = {True: "yes", False: "no", None: "n/a"}[x.get("is_regression")]
        f = targets[n]
        w("| [#%d](https://github.com/adafruit/circuitpython/issues/%d) | %s | %s | %s | %s | %s | %s | %s |" % (
            n, n, x.get("year", ""), title, AREA_NAME.get(x.get("area"), x.get("area") or "-"), reg,
            CAUSE_NAME.get(x.get("cause"), x.get("cause") or "-"), ", ".join(f),
            ("*check* %s: pass %d/8%s" % (ichk[n][0], ichk[n][1]["pass"],
                                           "; skipped or absent on %d" % (8 - ichk[n][1]["pass"] - ichk[n][1]["fail"]) if ichk[n][1]["pass"] + ichk[n][1]["fail"] < 8 else ""))
            if n in ichk else
            "; ".join(sorted({status[k] if fx[k]["tier"] == "smoke" else "not run yet" for k in f}))))
    w("")
    w("## 5. Not covered yet")
    w("")
    w("- **Signals on pins** (GPIO, UART, SPI, I2C traffic, PWM measured by another pin): peripherals_loopback "
      "is written and waits for jumpers and an I2C sensor.")
    w("- **Real displays**: display_fixture waits for a TFT and a light sensor.")
    w("- **Networking, BLE, USB HID/MIDI, sleep, audio, soak, core VM on target**: nightly fixtures are written "
      "and statically checked, not yet run on the farm.")
    w("- **Host-OS-specific USB bugs (macOS, Windows), true deep-sleep current, visual glitches**: need other "
      "rigs or a person.")
    w("- **Build / CI and docs regressions**: outside hardware testing.")
    w("")
    with open(a.out, "w") as f:
        f.write("\n".join(L))
    print("wrote %s: %d targeted issues, %d fixtures" % (os.path.relpath(a.out, ROOT), len(targets), len(fx)))


if __name__ == "__main__":
    main()
