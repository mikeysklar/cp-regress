# sleep_alarm: Deep sleep and light sleep with TimeAlarm

Tier nightly / automation auto / boards with native_usb (excluding stm)

## Regressions it targets

- [#5462](https://github.com/adafruit/circuitpython/issues/5462) - Sleep memory corruption across wake cycles
- [#2875](https://github.com/adafruit/circuitpython/issues/2875) - Deep sleep wake timing incorrect
- [#10573](https://github.com/adafruit/circuitpython/issues/10573) - Light sleep does not wake on timeout
- [#9464](https://github.com/adafruit/circuitpython/issues/9464) - TimeAlarm exception on invalid time
- [#10934](https://github.com/adafruit/circuitpython/issues/10934) - wake_alarm type mismatch after deep sleep
- [#6484](https://github.com/adafruit/circuitpython/issues/6484) - Sleep memory initialization
- [#2855](https://github.com/adafruit/circuitpython/issues/2855) - alarm module missing on some ports
- [#5839](https://github.com/adafruit/circuitpython/issues/5839) - Deep sleep with USB connected does not work as expected

## What it checks

Light sleep, run in the raw REPL first (it also detects whether `alarm` exists; without it the
deep sleep part is skipped):

- **alarm_import**: `import alarm` works (skip, not fail, when the build has no alarm module)
- **light_sleep_s** (metric) / **light_sleep_timing**: `light_sleep_until_alarms()` with a 2 s TimeAlarm takes 1.9 to 3.0 s
- **light_sleep_woke**: the returned alarm is a `TimeAlarm` (#10573)
- **light_sleep_wake_alarm**: `alarm.wake_alarm` is the returned alarm
- **past_alarm_raises**: `TimeAlarm(monotonic_time=<past>)` raises ValueError (#9464)
- **light_sleep_error**: any unexpected exception during light sleep

Deep sleep loop: code.py increments `alarm.sleep_memory[0]`, prints `CPR wake <count> <wake_alarm type>`
and calls `exit_and_deep_sleep_until_alarms()` with a 5 s TimeAlarm. The host soft reboots once
and listens for 27 s on the same serial connection (no keystrokes, which would end the pretend sleep).

- **wake_count**: at least 4 wake lines in 27 s
- **sleep_memory_persists**: each count is the previous one + 1 (mod 256) (#5462, #6484)
- **first_wake_type**: first run after the soft reboot has `wake_alarm` None
- **later_wake_types**: every later run has `wake_alarm` of type TimeAlarm (#10934)
- **deep_sleep_interval_s** (metric) / **deep_sleep_interval**: median host-timed gap between wake lines is 4.5 to 8 s (#2875)
- **ready_after_restore**: board back after code.py is restored and soft rebooted (power cycle fallback)

## Setup

None. Note that with USB connected CircuitPython does a **simulated (fake) deep sleep**: it prints
"Pretending to deep sleep until alarm, CTRL-C or file write." and restarts the VM on the alarm. The
board stays powered, so this checks sleep_memory, wake_alarm and alarm timing, not real deep sleep.

## What is hard to automate

- **True deep sleep current measurement**: Cannot verify micro-amp sleep current without external power supply and current meter.
- **Wake on external pin**: Testing wake from a GPIO pin requires another board or function generator to drive a pin at the correct time.
- **Brown-out detection**: Cannot safely test by dropping supply voltage without risking board damage.
- **RTC accuracy over long sleep**: Testing multi-hour sleep durations requires a real-time clock or external oscillator validation.
- **Sleep with different clock configurations**: Testing sleep with different main clocks, prescalers, and RTC sources would require board-specific configuration.
- **Memory leaks across wake cycles**: Detecting heap growth over 100+ cycles requires detailed memory tracking.

## Run

```bash
./run.py --only sleep_alarm --boards metro_rp2040,metro_m4_airlift,feather_nrf52840
```

Skips feather_stm32f405 (STM32 deep sleep with USB is unreliable).
