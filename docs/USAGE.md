# cp-regress

Hardware regression fixtures for CircuitPython, built from ten years of issues
(a sweep of adafruit/circuitpython issues; results in [FIRST-RUN.md](FIRST-RUN.md)). Each fixture targets a family of real regressions and
names the issues it would have caught.

## Install on the farm host

```sh
rsync -a cp-regress/ <farm-host>:~/cp-regress/
ssh <farm-host>
cd ~/cp-regress
python3 -m pip install --user pyserial          # only host dependency
python3 tools/check_fixtures.py --cp-tree ~/circuitpython   # static gate: compile, META, API names vs shared-bindings
python3 tools/selftest.py                       # REPL protocol against a fake CircuitPython on a pty
python3 tools/unix_smoke.py --build             # every fixture's device code on the unix port
python3 run.py --list
python3 run.py --dry-run --tier release         # what would run where, and why things skip
```

Optional host tools some fixtures use: `openssl` (net_tls), `amidi` from alsa-utils
(usb_hid_midi), nss-mdns (web_workflow mDNS check), a CircuitPython checkout at
`~/circuitpython` or `CPR_CP_TREE` (core_vm).

## Run

Flash the build under test first with the farm's own tools (`fastcp.sh`), then:

```sh
python3 run.py --tier smoke --expect-version 11.0.0-alpha --label "main@$(git -C ~/circuitpython rev-parse --short HEAD)"
CPR_WIFI_SSID=... CPR_WIFI_PASSWORD=... python3 run.py --tier nightly
python3 run.py --tier release --boards metro_esp32s3,metro_rp2040
python3 run.py --only usb_enumeration,perf_core
```

Tiers are cumulative: `smoke` (every merge, a few minutes per board), `nightly`, `release`.
`--expect-version` refuses to run if any board's `boot_out.txt` does not match.

Output: `results/<stamp>/summary.md` (board x test grid and failures) and `results.jsonl`.
Every metric also lands in `history/metrics.jsonl`; later runs fail a metric that is more than
25% worse than the median of the last five runs on that board. Exact values (for example the
displayio render checksum) are recorded once per board in `history/golden.json` and must match
afterwards; delete a key to accept an intended change. Run once on a known-good build first to
seed both, or pass `--no-history` for throwaway runs.

## Layout

| Path | What |
|---|---|
| `run.py` | runner, tiers, board selection, summary, metric baselines |
| `farm.toml` | boards by USB path, caps, wiring, power-cycle timings |
| `lib/farmlib.py` | board resolution by USB path, mount, REPL driver, preserve/restore |
| `lib/devtest.py` | on-device `check` / `skip` / `metric` helpers |
| `tests/*.py` | one fixture per file (`META` + `run(board, ctx)`) |
| `docs/fixtures/*.md` | what each fixture targets, checks, needs, and cannot catch |
| `CONTRACT.md` | how to write a new fixture |
| `tools/check_fixtures.py` | static gate for every fixture |
| `tools/selftest.py` | hardware-free test of the REPL protocol |
| `tools/unix_smoke.py` | runs every fixture's device code on the unix coverage port; CI-friendly, exit 1 on failure |
| `history/` | metric baselines and golden values (created on first run) |

## Before the farm: unix smoke

`tools/unix_smoke.py` builds (`--build`) or uses (`--micropython`) the unix coverage port of
the tree under test and runs every DEVICE string with the farm's helpers, inside a RAM FAT
disk mounted at `/`. settings_toml and usb_msc_rw get the files their host side would write;
core_vm runs its 40 tests/basics files. Code that needs `board`, USB, wifi or BLE reports
UNSUPPORTED rather than failing. Seven negative controls run first; if any is misclassified
the tool exits 2 without running fixtures. On main (2026-10-13): 8 PASS, 4 SKIPPED, 22 UNSUPPORTED.
Not exercised on unix: TileGrid / Group, TilePaletteMapper, `supervisor.get_setting()`.

## Safety

- Files a fixture rewrites (code.py, boot.py, settings.toml) are backed up and restored with
  an md5 check.
- `microcontroller.reset()` is only used on boards with `reset_ok` (not the M0).
- No sudo. uhubctl runs unprivileged.
- A lock per USB path stops two runs touching one board.
- Fixtures that need wiring skip until the wiring is declared in `farm.toml`.
