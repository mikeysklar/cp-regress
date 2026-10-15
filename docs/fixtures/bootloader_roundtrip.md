# bootloader_roundtrip: Enter and exit bootloader via on_next_reset()

Tier: release / automation: auto / boards with uf2 and reset_ok (skip metro_m0, metro_m4_airlift)

## Regressions it targets

- #4728 Bootloader entry fails
- #7578 UF2 volume not detected
- #7060 ROM bootloader VID/PID mismatch
- #739 microcontroller.on_next_reset() not persisted
- #11097 Bootloader detection timing
- #5354 Power cycle after bootloader hangs
- #9047 CircuitPython version changes on bootloader roundtrip

## What it checks

- present, version_before: board on the bus, boot_out.txt version readable
- reset_issued: only reported (as a failure) if `on_next_reset(RunMode.UF2)` + `reset()` returned instead of resetting
- vid_pid_changed: within 40 s the board re-enumerates (new devnum) with a different VID/PID
- uf2_volume_mounted: a block device on the same hub port has INFO_UF2.TXT (found mounted or mounted with udisksctl)
- info_uf2_content: INFO_UF2.TXT contains "UF2 Bootloader" (matches SAMD UF2, RP2040 ROM and TinyUF2)
- bootloader_entry_s metric: time from sending reset() to a readable INFO_UF2.TXT
- entered_bootloader: UF2 volume reached
- returned_to_cp: a power cycle brings back CircuitPython (tty + CIRCUITPY)
- vid_pid_restored: VID/PID is back to the CircuitPython one
- version_unchanged: boot_out.txt version equals the starting version
- recovery: only on a failure, a second power cycle was attempted

## Setup

None on the board. Host needs udisksctl and findmnt (Linux). Board must have uf2 and reset_ok.

## What is hard to automate

- RunMode.UF2 is used, not BOOTLOADER: on espressif BOOTLOADER is ROM download mode with no UF2 volume
- metro_m0 is skipped (reset() hazardous); metro_m4_airlift is skipped because a VBUS cycle does not reset it, so nothing automated can leave the bootloader
- feather_nrf52840 has no uf2 cap (CDC-only bootloader) and does not run this
- Exit relies on a power cycle clearing the bootloader request; a port that cuts power poorly leaves the board in the bootloader
- Cannot test firmware upload (would corrupt the build being tested)
- Issue numbers in the list above were not re-verified against GitHub

## Run

```
./run.py --only bootloader_roundtrip --boards metro_rp2040,metro_esp32s3,feather_nrf52840
```
