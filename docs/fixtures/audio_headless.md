# audio_headless: Audio module tests (no sound verification)

**Tier / automation / boards:** nightly / auto / all with native_usb

## Regressions it targets

- #1623: Repeated play() calls crash or corrupt state
- #6200: Audio module initialization failures
- #4979: synthio press/release state machine issues

## What it checks

### audioio / audiopwmio

- **audio_modules**: on atmel-samd, raspberrypi, nordic and stm boards audioio or audiopwmio must be built in; elsewhere it is a skip that lists what exists
- **audio_play_start**: AudioOut on A0 (audioio) or PWMAudioOut on D5/D9/first Dn pin (audiopwmio) reports `playing` right after `play()` of a 0.5 s 1 kHz square wave (8-bit RawSample)
- **audio_play_duration**: `playing` goes False within 1.5 s, and not before 0.25 s
- **audio_repeat_play**: 5 back-to-back `play()` calls do not raise, the output is still playing afterwards, and `stop()` stops it (#1623 regression)
- **audio_reinit**: after `deinit()` the same pin can be claimed again
- **audio_play**: only reported (as a failure) if construction or playback raises

### synthio (if present)

- **synthio_press**: after `press(60)`, `pressed == (60,)`
- **synthio_release**: after `release(60)`, `pressed` is empty
- **synthio**: only reported (as a failure) if construction raises, or as a skip when synthio is not built in

### host

- **present**: board on the bus
- **device_run**: the device body ran to its last section (or the raw REPL error if it did not)

## Setup

- None (no external hardware required)
- Tests skip gracefully if audio modules are not built in (boards with CIRCUITPY_AUDIOIO=0)

## What is hard to automate

- **No sound verification**: Cannot listen to output or measure frequency/distortion without hardware loopback
  - Would need: audio out → low-pass filter → ADC input, measure via FFT
  - Cannot be done without extra wiring and post-processing
  
- **Audio timing**: Precise timing of audio playback dependent on CPU load and interrupt latency
  - The duration check only bounds it loosely (0.25 s to 1.5 s for a 0.5 s sample); tighter bounds would flake
  
- **Synthio voice stealing**: Cannot verify that overlapping notes are handled correctly
  - Would need to record output and analyze waveform
  
- **Multiple audio outputs**: Test only verifies one output pin
  - Cannot test simultaneous I2S and PWM audio without hardware validation

## Run

```bash
./run.py --tier nightly --only audio_headless --boards metro_rp2040,metro_esp32s3

# Dry-run to see which boards it would run on (nightly tier, so --tier nightly is required):
./run.py --dry-run --tier nightly --only audio_headless
```

## Notes

- Uses an 8-bit unsigned bytearray square wave so no float math or large buffers are needed (4 KB on a SAMD21)
- Uses A0 for audioio, D5, D9 or the first numbered D pin for audiopwmio
- The audio object is always deinit()ed, so the pin is free for later tests in the same session
- No dependency on external sound files or pre-computed samples
