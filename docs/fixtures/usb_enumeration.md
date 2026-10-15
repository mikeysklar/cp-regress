# usb_enumeration: USB enumeration and basic mount

Smoke test / Auto / All boards with native_usb

## Regressions it targets

- #9888: USB bus reset causes disconnection
- #9956: VID/PID not reported correctly
- #10390: MSC interface missing after enumeration
- #8134: Mount fails or races with enumeration

## What it checks

- Board present at its USB hub path (sysfs)
- VID/PID readable and non-zero
- Interface class codes include CDC (02) and MSC (08) by default
- HID (03) and audio (01) reported as informational, not failures
- Serial console tty exists and is reachable by path
- CIRCUITPY filesystem mounts via udisks
- boot_out.txt file exists and names the expected board_id
- boot_out.txt includes Adafruit CircuitPython version line
- Drive is writable from the host (write and delete a test file)
- Metric: time_to_mount (seconds from test start to successful mount)
- Device-side: board.board_id matches boot_out.txt
- Device-side: CircuitPython version >= 9.x

## Setup

None. The boards are as-is with CDC+MSC enabled in firmware.

## What is hard to automate

- Checking that all expected pins are present (would need a per-board pin list)
- Verifying that USB descriptor length and other low-level details are correct
  (requires sniffing the wire or parsing descriptors from the device)
- Detecting if the device _ever_ disconnects during the test (relies on observing sysfs)
- Power cycling between enumeration attempts to clear transient USB glitches
  (possible but slow; the test uses a single steady-state mount)

## Run

```bash
./run.py --only usb_enumeration --boards metro_rp2040,metro_esp32s3
./run.py --tier smoke --only usb_enumeration
```
