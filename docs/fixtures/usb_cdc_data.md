# usb_cdc_data: USB CDC data echo with storage recovery

**Tier:** nightly / **Automation:** auto / **Boards:** all with native_usb and reset_ok, except metro_m0 (no reset_ok) and the espressif / stm ports

## Regressions it targets

- [#4364](https://github.com/adafruit/circuitpython/issues/4364) — USB CDC secondary interface stability
- [#5381](https://github.com/adafruit/circuitpython/issues/5381) — CDC data echo under load
- [#6018](https://github.com/adafruit/circuitpython/issues/6018) — USB interface count regression
- [#7456](https://github.com/adafruit/circuitpython/issues/7456) — MSC interface enumeration
- [#8417](https://github.com/adafruit/circuitpython/issues/8417) — storage.disable_usb_drive state
- [#10396](https://github.com/adafruit/circuitpython/issues/10396) — REPL responsiveness after CDC activity
- [#4417](https://github.com/adafruit/circuitpython/issues/4417) — Multiple CDC interfaces
- [#5640](https://github.com/adafruit/circuitpython/issues/5640) — CDC interface recovery
- [#6471](https://github.com/adafruit/circuitpython/issues/6471) — Storage remount after disable
- [#4986](https://github.com/adafruit/circuitpython/issues/4986) — Safe mode recovery from storage state

## What it checks

boot.py only runs on a hard reset, so every USB configuration change is applied with
`microcontroller.reset()` (power cycle if the REPL cannot be reached), never a soft reboot.

### Phase 1: USB CDC Data Echo
boot.py enables console + data (and disables usb_hid / usb_midi to stay inside the endpoint budget); code.py echoes usb_cdc.data.
- **cdc_boot_ready** - board comes back after the hard reset
- **ttys_count** - two serial ports exist on the board (console + data)
- **data_tty_exists** - board.data_tty() returns a path
- **device_ready** - a PING written to the data tty is echoed back (host-driven sync, no banner)
- **echo_received** - all 64 KB (512-byte chunks) echoed back without loss
- **echo_data_match** - echoed bytes match sent bytes exactly (detail gives first mismatch)
- **data_echo_kBps** - throughput metric (KB/s, higher is better)
- **usb_cdc_data_available**, **usb_cdc_console_available** - from the console REPL after the echo loop is interrupted
- **console_responsive** - the console raw REPL ran the device body
- **cdc_restore_ready**, **ttys_restored** - after restoring files and a hard reset the tty count matches the start

### Phase 2: Storage Disable and Recovery
- **disabled_boot_ready** - console tty is back after a reset with `storage.disable_usb_drive()` in boot.py
- **msc_disabled** - no class 08 interface in the descriptor (and the interface list is not empty)
- **no_block_dev** - no block device for the board
- **console_after_disable** - `print(1+1)` in the raw REPL prints `2`
- **fs_writable_when_drive_disabled** - CIRCUITPY is writable from Python while the drive is disabled
- **boot_py_removed_on_device** - recovery step: boot.py removed from the device side
- **recovery_success** - CIRCUITPY mounts again after a hard reset
- **recovery_safe_mode** - only if the above failed: reset into safe mode (skips boot.py) to get MSC back
- **msc_restored** - class 08 interface is present again
- **final_ready** - board ready after the final hard reset with the original files

## Setup

**Hardware:** Requires a board with native USB support and safe microcontroller.reset():
- metro_rp2040, metro_m4_airlift, feather_nrf52840, metro_rp2350
- Not: metro_m0 (no reset_ok cap); metro_esp32s2 / metro_esp32s3 (console + data + MSC needs 6 IN endpoints, the chip allows 5); feather_stm32f405 (needs 6 endpoint numbers, has 4). Those would boot into safe mode.

**Wiring:** None

**Environment:** None

**Host packages:** pyserial (pip install pyserial)

## What is hard to automate

1. **Endpoint exhaustion** — Some boards have limited USB endpoints. boot.py disables usb_hid and usb_midi to make room for the data CDC; if a board still runs out it boots into safe mode and the test reports it as `ttys_count` / `device_ready` failures without naming safe mode.

2. **Host-side USB enumeration timing** — Re-enumeration takes 10-12 seconds on the farm (bravo). The test waits board.wait_ready() but timing races can occur if the host polls the device during the USB reconfiguration. This is hard to automate without better USB protocol-level instrumentation.

3. **Filesystem coherency with MSC mounted** — After disable_usb_drive(), the MSC volume is not presented to the host, but CircuitPython must safely remount its own partition. If the host had write access while this happens, corruption can occur. This test runs on an isolated farm so cannot test real-world USB hot-plug races.

4. **Storage state persistence across resets** — Configuration changes are applied with `microcontroller.reset()`, not a hub power cycle, so a regression that only shows after a cold power-on is not covered.

5. **CDC backpressure and flow control** — The test sends one 512-byte chunk at a time and waits for its echo, so it does not test USB bulk transfer underruns, stalls, or host-side flow control scenarios. Real-world regressions in CDC flow control would need a USB analyzer or more sophisticated host-side protocol handling.

6. **Safe mode fallback** — Safe mode is only used when the device-side recovery fails; that path has not been exercised on hardware. If both fail the board is left with the USB drive disabled and needs a person (REPL: `import os; os.remove("/boot.py")`, then reset).

## Run

```bash
./run.py --tier nightly --only usb_cdc_data --boards metro_rp2040,metro_m4_airlift,feather_nrf52840
```

Or on a single board:
```bash
./run.py --tier nightly --only usb_cdc_data --boards metro_rp2040
```

For a dry run to see what would execute:
```bash
./run.py --dry-run --tier nightly --only usb_cdc_data --boards metro_rp2040
```
