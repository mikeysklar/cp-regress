# peripherals_selfcheck: Basic peripheral API checks without wiring

**Tier / automation / boards:** smoke / auto / all

## Regressions it targets

- #1603: I2C singleton crash (board.I2C() called twice must return same object)
- #5476: PWM duty cycle incorrect after frequency change

## What it checks

- **i2c_construct**: board has I2C (SCL/SDA) and constructs busio.I2C
  - Records "pull up" error if no pull-ups detected (some boards have them on-chip)
- **i2c_singleton**: board.I2C() called twice returns the same object (regression #1603)
- **pin_claim_twice**: Claiming the same pin twice with DigitalInOut raises ValueError
- **use_after_deinit**: Reading from a pin after deinit() raises ValueError
- **spi_mosi_only**: SPI(SCK, MOSI=...) constructs (MISO optional)
- **pwm_construct**: PWMOut constructs on an available D-pin
- **pwm_frequency_roundtrip**: frequency set/get at 500 Hz, 1 kHz, 5 kHz
- **pwm_duty_roundtrip**: duty_cycle set to 16384 and read back (within 100 counts)
- **analog_read**: analogio.AnalogIn(A0).value returns an int in [0..65535]
- **cpu_temp**: microcontroller.cpu.temperature is float [-40..125°C] or None
- **neopixel_write**: neopixel_write writes to NEOPIXEL without error if present

## Setup

- None (no wiring required)
- All tests use hasattr guards for optional pins (SCL/SDA, A0, NEOPIXEL, etc.)

## What is hard to automate

- Cannot verify pin actually drives a load (open-drain test)
- Cannot validate actual PWM frequency with a scope
- Cannot listen to audio output without hardware
- Pull-up value (kΩ) not verified, only presence of the pull-up error

## Run

```bash
./run.py --only peripherals_selfcheck --boards metro_rp2040,metro_m4_airlift,metro_esp32s3
```
