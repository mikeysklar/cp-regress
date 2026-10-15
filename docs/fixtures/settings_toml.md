# settings_toml: os.getenv and supervisor.get_setting against a known settings.toml

Tier smoke · automation auto · destructive (rewrites settings.toml, restored by md5)

## Regressions it targets
- #11118: unicode in settings.toml values
- #7677: escapes and long keys in values
- 11.x typed settings: `supervisor.get_setting()` (str / int / bool / float), `os.getenv()` returning str

## What it checks
- `getenv_*`: plain string, `\n` escape, `\u` escape, direct UTF-8, int returned as text, missing key, default, lookups after a bad value
- `gs_*` (11.x only, skipped if `supervisor.get_setting` is absent): str, int, negative int, hex int, bool, float, exponent float, `+inf`, `nan`
- spec rejections (toml.io): `Inf` (case), bare `+`, an unquoted word all raise `ValueError`
- `spec_leading_zero`: `017` should be invalid per the spec. main parses ints with `strtol(..., 0)` and reads it as octal 15, so this is reported as info (skip with detail), not a failure

## Setup
None.

## What is hard to automate
Nothing hardware-specific. Parser edge cases are better covered on the unix port in CI as well.

## Run
`./run.py --only settings_toml`
