# code_py_lifecycle: code.py runs once, errors stay errors, boot.py output lands

Tier smoke · automation auto · destructive (rewrites code.py and boot.py, restored by md5)

## Regressions it targets
#3502 (code.py started twice), #7459, #7578, #7060 (safe mode or boot loops instead of a traceback), #11287, #1537, #4728

## What it checks
- `code_py_runs`, `no_double_start`: marker printed exactly once after Ctrl-D
- `exception_traceback`, `code_done_message`, `no_safe_mode`
- `run_reason`, `reset_reason`: printed as `supervisor.RunReason.*` / `microcontroller.ResetReason.*`
- `recursion_raises`, `memoryerror_raises`, `survives_exceptions`
- `hard_reset_back`, `boot_py_in_boot_out` (reset_ok boards): boot.py print reaches boot_out.txt after `microcontroller.reset()` sent from the REPL (never from code.py, which would loop)

## Setup
None.

## What is hard to automate
A true hard fault is seen only indirectly (no output, board re-enumerates into safe mode). The safe-mode reason is not decoded yet.

## Run
`./run.py --only code_py_lifecycle`
