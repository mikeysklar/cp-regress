# repl_console: the friendly REPL a user hits first

Tier smoke · automation auto · all native-USB boards

## Regressions it targets
#417 (pastes over 128 chars), #2686 and #1630 (console stops after lots of output), #3588, #4164, #6480, #10514, #9454

## What it checks
- `prompt_reachable`, `eval` (7*6 -> 42, matched on the result line, not the echo), `underscore_last_result`
- `interrupt_ctrl_c`, metric `interrupt_ms`: Ctrl-C stops `time.sleep(30)` within 3 s
- `paste_4k`: a ~4 KB paste-mode block must compute and print its sum (the echo never contains the number)
- `large_output_count`, `large_output_order`, metric `output_kBps`: 2000 lines of 100 chars from the raw REPL, all present and in order
- `ctrl_d_soft_reboot`, `reboot_banner`: Ctrl-D reboots and the banner appears on the way back to the REPL

## Setup
None.

## What is hard to automate
Host-OS terminal quirks and drops that only appear after hours of idle are not covered.

## Run
`./run.py --only repl_console`
