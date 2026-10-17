#!/bin/bash
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
#
# Overnight run on bravo: the tests that track Brent's add-hil-ci-cd work first, then the smoke
# tier on the same boards as a baseline, then one Markdown report (tools/hil_report.py, the
# PR #930 proof format) for the morning.
#
#   tools/overnight.sh                     # the six boards below
#   BOARDS=metro_esp32s2,metro_esp32s3 tools/overnight.sh
#   DRY=1 tools/overnight.sh               # --cpr-dry-run: what would run where, no hardware
#
# Never flashes (no --firmware), never erases, never resets the shared broker, no power_boot,
# no BLE pairs. Each phase has a wall-clock cap so a wedged board cannot hold the farm past
# the morning. Board locks are the harness's own (/tmp/cp-regress-*.lock), so another session
# that honours them waits; one that does not (see the hil-farm notes) must be stopped first.
set -u

HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HOME/cpr-pytest-venv/bin/python}
ENV_FILE=${ENV_FILE:-$HERE/.env}
BUNDLE=${BUNDLE:-$HOME/cpr-bundles/adafruit-circuitpython-bundle-10.x-mpy-20261015}
# RP2040 and nRF52840 are held out: other sessions use them (2026-10-16).
BOARDS=${BOARDS:-metro_esp32s2,metro_esp32s3,metro_m0,metro_m4_airlift,metro_rp2350,feather_stm32f405}
NBOARDS=$(echo "$BOARDS" | tr ',' '\n' | grep -c .)
STAMP=$(date +%Y%m%d-%H%M)
OUT=${OUT:-$HERE/results/overnight-$STAMP}
PHASE1_CAP=${PHASE1_CAP:-14400}   # 4 h
PHASE2_CAP=${PHASE2_CAP:-7200}    # 2 h

# Brent-aligned modules, in the order his work goes: provisioning (settings scenarios), device
# identity, WiFi (his network/test_wifi.py), then the MQTT broker connection, his next step.
BRENT_MODULES=(
  test/hardware/settings_scenarios_test.py
  test/hardware/identity_crash_test.py
  test/hardware/net_wifi_test.py
  test/hardware/wifi_join_test.py
  test/hardware/mqtt_broker_test.py
  test/hardware/mqtt_catcher_test.py
)

[ -x "$PY" ] || { echo "no python at $PY" >&2; exit 1; }
[ -d "$BUNDLE" ] || { echo "no library bundle at $BUNDLE" >&2; exit 1; }
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  echo "warning: no $ENV_FILE, WiFi tests will skip" >&2
fi
mkdir -p "$OUT"
cd "$HERE" || exit 1

common=(-n "$NBOARDS" --dist loadgroup -p no:cacheprovider --boards "$BOARDS"
        --bundle "$BUNDLE" --serial-log-dir "$OUT/serial")
[ -n "${DRY:-}" ] && common+=(--cpr-dry-run)

run_phase() {  # name cap args...
  local name=$1 cap=$2; shift 2
  local cmd=("$PY" -m pytest "${common[@]}" --junitxml="$OUT/$name.xml" "$@")
  echo "$(date '+%F %T') start $name: ${cmd[*]}" | tee -a "$OUT/runs.log"
  timeout --signal=INT --kill-after=120 "$cap" "${cmd[@]}" >"$OUT/$name.out" 2>&1
  local rc=$?
  echo "$(date '+%F %T') end $name rc=$rc" | tee -a "$OUT/runs.log"
  tail -3 "$OUT/$name.out" | tee -a "$OUT/runs.log"
  "$PY" tools/hil_report.py --junit "$OUT/$name.xml" --serial-dir "$OUT/serial" \
      --title "cp-regress $name, $STAMP ($BOARDS)" --invocation "${cmd[*]}" \
      -o "$OUT/$name.md" || true
}

run_phase brent "$PHASE1_CAP" --tier nightly "${BRENT_MODULES[@]}"
run_phase smoke "$PHASE2_CAP" --tier smoke test/hardware

cat "$OUT/brent.md" "$OUT/smoke.md" > "$OUT/comment.md" 2>/dev/null
echo "$(date '+%F %T') done: $OUT/comment.md" | tee -a "$OUT/runs.log"
