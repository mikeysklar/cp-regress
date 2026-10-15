# usb_hid_midi: USB HID and MIDI enumeration and data flow

Tier: nightly / automation: auto / boards: all with native_usb

## Regressions it targets

- #1769: USB enumeration issues
- #2048: MIDI data flow regression
- #4895: MIDI message dropout and byte corruption
- #5101: USB HID interface not enumerating
- #5213: HID report descriptor issues
- #6764: MIDI port enumeration
- #7930: USB audio interface issues
- #7944: HID device attribute missing
- #9130: MIDI ports not initializing
- #9306: HID interface class mismatch
- #9847: MIDI data truncation
- #10373: USB enumeration stability
- #4724: HID/MIDI composite device

## What it checks

The device is asked first what it has enabled; host expectations follow from that, so a board
where HID or MIDI is off by default (STM32F405: both; ESP32-S2/S3: MIDI) is not failed for it.

**Device-side:**
- `usb_hid.devices_is_tuple`; skip `usb_hid.devices` when it is empty (disabled by default or
  by boot.py); skip `usb_hid.import` when the module is not in the build
- `usb_hid.device_<i>_usage_page` / `_usage`: int in 1..0xFFFF
- `usb_hid.device_<i>_get_last_received_report`: returns `None` or `bytes` (Linux sends the
  keyboard LED state on enumeration, so the keyboard may return `b'\x00'` once)
- `usb_midi.ports_is_tuple`, `usb_midi.ports_count_is_2`, `usb_midi.ports_0_is_PortIn`,
  `usb_midi.ports_1_is_PortOut` (skip when `ports` is empty)
- `usb_midi.write_returns_int`, `usb_midi.write_all_accepted`: 50 Note On/Off messages
  (150 bytes) to `ports[1]` within 3 s while the host is draining

**Host-side (Linux farm):**
- `board_present`, `board_vid_pid_readable`, `board_has_interfaces`
- `usb_hid_interface_enumerated` (class 03) when the device reports HID devices, or
  `usb_hid_interface_absent_when_disabled` when it reports none
- `hid_sysfs_device`: a `/sys/bus/hid/devices` entry under this board's interfaces
- `hid_report_descriptor_readable`, and `hid_report_descriptor_contains_keyboard_usage`
  (05 01 / 09 06) when the device lists a keyboard
- `usb_midi_interface_enumerated` (class 01) when `usb_midi.ports` is non-empty, or
  `usb_midi_interface_absent_when_disabled`
- `usb_midi_data_flow`: the host opens the board's ALSA rawmidi node
  (`/dev/snd/midiC<card>D0`, found under the board's sysfs interfaces) read-only, discards
  stale bytes, lets the device send, and requires exactly the 150 bytes in order

## Setup

None. `usb_midi_data_flow` needs `snd-usb-audio` loaded and the farm user able to open
`/dev/snd/midiC*D0` (the `audio` group or a seat ACL); otherwise it is skipped with the reason.
No host packages beyond the standard library.

## What is hard to automate

- Custom `boot.py` HID/MIDI configurations: the test follows whatever the device reports,
  so it cannot tell a deliberate `usb_hid.disable()` from a regression that empties `devices`.
- HID IN reports (keyboard/mouse output) are never sent: that would type into the host.
- MIDI host-to-device (`ports[0]` reads) is not exercised.
- Enumeration on macOS/Windows hosts, and boot-protocol keyboard behavior in BIOS.

## Run

```bash
./run.py --only usb_hid_midi --boards metro_rp2040,metro_m4_airlift,metro_m0,feather_nrf52840,metro_esp32s2,metro_rp2350,metro_esp32s3,feather_stm32f405
```

To run on a single board:
```bash
./run.py --only usb_hid_midi --boards metro_rp2040
```

To see what would run:
```bash
./run.py --dry-run --only usb_hid_midi
```
