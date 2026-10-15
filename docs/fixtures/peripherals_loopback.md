# peripherals_loopback: Peripheral loopback tests (requires wiring)

**Tier / automation / boards:** nightly / fixture / all with native_usb

## Regressions it targets

- #7616: GPIO pin state transitions
- #10022: UART transmission errors
- #3339: SPI write_readinto data corruption
- #717: PWM/counter integration
- #1216: I2C scan and enumeration

## What it checks

Each section runs only when its wiring key is in farm.toml (otherwise a skip with that key's
name). The host substitutes the wiring value into the device code; an exception inside a
section becomes a failed check named after the key, and pins are deinitialized either way.

### gpio_pair (wiring `gpio_pair = [pin_a, pin_b]`)

- `gpio_pair_low` / `gpio_pair_high`: A driven LOW/HIGH reads the same on B (B pulled down)
- `gpio_pair_pullup` / `gpio_pair_pulldown`: with B released, A as input reads its own pull

### uart_loop (wiring `uart_loop = [TX, RX]`)

- `uart_loop_transfer`: 1100 bytes at 115200 baud all come back within 3 s
  (receiver buffer sized to hold them all)
- `uart_loop_data`: byte-for-byte match, detail gives the first mismatch

### spi_loop (wiring `spi_loop = [MOSI, MISO]` or `[MOSI, MISO, SCK]`)

- `spi_loop_xfer`: locked, 1 MHz `write_readinto` of bytes 0x00-0xFF reads back exactly.
  SCK defaults to `board.SCK`

### pwm_count (wiring `pwm_count = [PWM_pin, counter_pin]`)

- `pwm_count_rate`: PWMOut 1 kHz 50% into `countio.Counter` (falling edges) counts
  1000 per second within 2%, scaled by the measured window
- `pwm_pulseio_duty` / `pwm_pulseio_period`: after the counter is released, `pulseio.PulseIn`
  sees 50% +-3 duty and a 1000 +-50 us period (skipped when pulseio is not in the build)

### i2c_sensor (wiring `i2c_sensor = "0x77"` or `119`)

- `i2c_scan_found`: `board.I2C()` scan (under lock) includes the address

### dac_adc (wiring `dac_adc = [DAC_pin, ADC_pin]`)

- DAC set to 0, 32768, 65535 and ADC averaged over 8 reads at each level
- `dac_adc_monotonic`: readings strictly rise
- `dac_adc_span`: top minus bottom is over 40000 counts (an open jumper fails here)
- `dac_adc_mid`: middle reading within 10% of 32768
- Skipped on builds without `analogio.AnalogOut`

## Setup

### Wiring per board

Add to the board's `[[board]]` section in farm.toml. TOML inline tables must stay on one
line. Pin names are `board` attribute names (`D5` -> `board.D5`):

```toml
wiring = { gpio_pair = ["D5", "D6"], uart_loop = ["TX", "RX"], spi_loop = ["MOSI", "MISO"], pwm_count = ["D9", "D10"], i2c_sensor = "0x77", dac_adc = ["A0", "A1"] }
```

### Jumper connections (Metro RP2040 example)

- **gpio_pair**: D5 to D6 (direct wire)
- **uart_loop**: TX to RX (direct wire)
- **spi_loop**: MOSI to MISO (direct wire); SCK needs no jumper
- **pwm_count**: D9 (PWM output) to D10 (counter input), direct wire
- **i2c_sensor**: a device at the given address on the board's default I2C bus (STEMMA QT)
- **dac_adc**: A0 (DAC) to A1 (ADC), direct wire. Only boards with a DAC (SAMD21/SAMD51,
  ESP32-S2); the RP2040 has none, so leave `dac_adc` out there

## What is hard to automate

- A jumper that is in farm.toml but has fallen off is a failed check, indistinguishable from a
  firmware regression until someone looks at the board
- The pin pairs must be valid for each peripheral on that chip (UART TX/RX, SPI, PWM, countio,
  PulseIn); an invalid pair shows up as a failed check with ValueError, not a skip
- SPI clock skew, phase, and jitter not measured
- PWM frequency stability over time not checked
- I2C bus noise, clock stretching edge cases
- DAC settling time and noise floor not characterized
- No way to verify actual voltage levels without a multimeter

## Run

```bash
# After configuring wiring in farm.toml:
./run.py --only peripherals_loopback --boards metro_rp2040,metro_esp32s3

# Dry-run to see which tests skip:
./run.py --dry-run --only peripherals_loopback --boards metro_rp2040
```
