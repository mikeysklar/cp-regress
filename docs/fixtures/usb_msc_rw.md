# usb_msc_rw: host writes over MSC, device reads back the same bytes

Tier smoke · automation auto · all native-USB boards (64 KB file on `small` boards)

## Regressions it targets
- #9956, #10390, #11097, #9971, #10901: drive present but broken after an update
- #4670: CIRCUITPY volume broken by one commit on macOS
- #9384, #10547: drive mounts read-only or drops

## What it checks
- `host_write`, metric `host_write_kBps`: 256 KB deterministic file written and synced
- `host_burst_created`: 20 small files in a directory
- `host_readback`: host Adler-32 matches (page cache; weak on its own)
- `device_read`, `device_matches_host`: the board reads the file and its Adler-32 equals the host's. This is the end-to-end check that bytes reached flash.
- `device_burst_count`: the board sees all 20 burst files
- `free_space_agree`: host and device `statvfs` within 2%
- `cleanup`, `mount_after`, `devnum_unchanged`: no re-enumeration during the test

## Setup
None.

## What is hard to automate
- Host page cache hides device-side corruption from a host-only readback, which is why the device computes the checksum. Dropping caches needs root.
- Corruption that appears only after power loss mid-write is not covered.
- macOS-specific MSC behaviour (#7523 mount loops, #8918 Sonoma slow writes) needs a Mac runner.

## Run
`./run.py --only usb_msc_rw`
