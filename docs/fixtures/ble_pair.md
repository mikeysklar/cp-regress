# ble_pair: BLE advertising and scanning between paired boards

Tier nightly / automation auto / boards with ble

## Regressions it targets

- [#6012](https://github.com/adafruit/circuitpython/issues/6012) - Hang when advertising while scanning on the same board

## What it checks

- `present`, `vid_pid`: board on the bus
- Skips when no other present board has the `ble` cap
- `partner_advertise_start`: partner starts a non-connectable advertisement named `CPRA`
  (stays on in the background after the raw REPL returns)
- `scan_entry_found`: this board's 10 s scan sees a Local Name of exactly `CPRA`;
  `scan_find_ms` metric (time to first hit), `rssi_sane` (-110 < rssi < 0)
- `advertise_start`, `partner.scan_entry_found`, `partner.scan_find_ms`, `partner.rssi_sane`:
  same with roles swapped, name `CPRB`
- `advertising_while_scanning`: starting an advertisement during a scan either works or
  raises `BluetoothError`/`RuntimeError`; any other exception fails. `scan_finished_after_adv`:
  the scan still ends
- `adv_while_scan_no_hang`: that step returns before the 30 s host timeout (#6012 was a hang)
- `scan_error`, `device_exception`, `partner.device_exception`: unexpected device errors
- Advertising and scanning are stopped on both boards after each phase

## Setup

No additional wiring or host packages required. The test uses two boards from the farm that have BLE capability (feather_nrf52840, metro_esp32s3).

## What is hard to automate

- **True BLE range/distance testing**: This fixture tests BLE at close range only. Testing maximum range, antenna performance, and out-of-range behavior would require controlled RF shielding or an RF chamber.
- **Busy RF**: other farm boards and nearby devices advertise too; the scan matches the exact name, so they only add noise to `scan_find_ms`.
- **Connection establishment**: The test uses advertising and scanning but does not attempt to establish a BLE connection. Testing bonding, pairing, GATT service negotiation, and data transfer over a connected link would require substantial additional code.
- **Coexistence with WiFi**: Boards with both BLE and WiFi (metro_esp32s3) may have interference or scheduler issues that only appear under specific WiFi load. This would require running WiFi traffic simultaneously.
- **Power consumption validation**: Advertising and scanning power draw cannot be measured in an automated test without a power meter connected to the board. The test validates timing and discovery only.

## Run

```bash
./run.py --only ble_pair --boards feather_nrf52840,metro_esp32s3
```

Requires two boards in the farm with the `ble` capability.
