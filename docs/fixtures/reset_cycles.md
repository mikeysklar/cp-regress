# reset_cycles: Soft reboot, hard reset, and power cycle recovery

Tier: nightly / automation: auto / all boards

## Regressions it targets

- #10934 Soft reboot instability
- #11380 Reset recovery timing issues

## What it checks

- present, version_before: board on the bus, boot_out.txt version readable
- soft_reboot_0..9: with a one-line code.py (inside board.preserved), Ctrl-D prints "soft reboot", code.py output appears, and the board reaches "Press any key to enter the REPL"
- soft_reboot_ms metric: median time from "soft reboot" to "Press any key"
- soft_restore_ready: after the user's code.py is restored, a soft reboot and wait_ready succeed
- hard_reset_0..2_returned (reset_ok boards only): microcontroller.reset() does not return, the board leaves the bus (devnum changes) and comes back with tty and CIRCUITPY
- hard_reset_0..2_version: boot_out.txt version equals the starting version
- hard_reset_to_tty_s metric: median time from disconnect to tty + mount
- power_cycle_0..1_ready, power_cycle_0..1_version: board.power_cycle() comes back with the same version
- power_cycle_0..1_reset_reason: microcontroller.cpu.reset_reason is POWER_ON; skipped (not failed) otherwise, since some farm ports do not really cut power
- power_cycle_ready_s metric: median time to tty + mount after the uhubctl cycle
- *_recovery: only on a failure, a recovery power cycle was attempted

## Setup

None. Hard reset checks also need reset_ok.

## What is hard to automate

- metro_m4_airlift: VBUS cycling does not reset it, so power_cycle_*_reset_reason is a skip there
- reset_reason is not POWER_ON on every port after a VBUS cycle; a skip is informational, not a pass
- Hard reset is not run without reset_ok (metro_m0)
- Soft reboot timing includes the trivial code.py run and serial overhead
- A board that wedges and does not come back after a power cycle still needs a person

## Run

```
./run.py --only reset_cycles --boards metro_rp2040,metro_esp32s3
```
