# display_fixture: ST7789 240x240 display via fourwire and busdisplay
Nightly tier / fixture / all boards

## Regressions it targets
- #10728: backlight brightness control regression
- #9443: brightness control issue with display
- #6264: backlight brightness regression

## What it checks
- **Wiring presence**: `display` wiring entry exists in farm.toml and starts with `st7789`
- `display_modules` (skip): `busio`, `fourwire` or `busdisplay` missing from the build
- `display_pins_found`: every configured pin name exists on `board` (sck, mosi, dc required)
- `busdisplay_create`: `displayio.release_displays()`, `busio.SPI`, `fourwire.FourWire`
  (24 MHz) and `busdisplay.BusDisplay` (240x240, Adafruit ST7789 init, `auto_refresh=False`,
  `backlight_pin`) all construct; any exception becomes a failed `display_setup`
- `display_root_group_set`: a full-screen 1-bit Bitmap/TileGrid group reads back as `root_group`
- `display_refresh_20x`: 20 manual `refresh()` calls, each flipping every pixel, all return
  True and average under 250 ms. Metric `display_refresh_ms` (ms per full frame, vs baseline)
- `display_brightness_100/50/0`: `brightness` set through the backlight pin reads back the
  same value (skipped when `bl` is empty)
- `light_tracks_backlight`: with `light_sensor` wired, the averaged AnalogIn reading with
  brightness 1.0 is at least 2000 counts above brightness 0.0. Metric `light_sensor_on`
- The display is released and SPI deinitialized at the end, pass or fail

## Setup
Requires a **farm.toml wiring entry** (TOML inline tables must stay on one line):
```toml
wiring = { display = "st7789_240x240", display_pins = { sck = "SCK", mosi = "MOSI", cs = "D9", dc = "D7", rst = "D5", bl = "D6" }, light_sensor = "A2" }
```

`display_pins` and `light_sensor` are optional. Missing `display_pins` keys default to the
values above; set a key to `""` to leave that pin unconnected. Pin names are `board`
attribute names (`D5` -> `board.D5`). Use the board's hardware SPI pins for sck/mosi;
RP2040 and others reject arbitrary pin pairs.

**Hardware**:
- ST7789 display module 240x240 (1.54" or 2.0" TFT displays common)
- Wiring: SPI (CLK, MOSI) + control (DC, CS, RST) + backlight (BL)
- Optional: an analog light sensor (phototransistor or photocell divider) facing the panel on an ADC pin

**Environment**: None; all configuration in farm.toml.

## What is hard to automate
- **Display rendering verification**: test measures refresh timing and backlight control but does NOT verify that images are actually rendered correctly. Visual correctness requires:
  - Optical camera or light sensor monitoring specific regions
  - Expected image checksums (would need reference images for each board)
  - Display glitch or corruption detection (requires pixel-by-pixel reading)

- **Optical sensor integration**: the on/off light check needs the sensor shielded from room
  light; the 2000-count threshold is a guess until the first runs set a real one. On a
  non-PWM backlight pin, 0.5 brightness is simply off, so partial levels are not measured.

- **Color rendering**: color values are set in palette but actual RGB output is not verified. Would need:
  - Color reference images or checksums
  - Display calibration per board variant
  - Sensor for color temperature and gamma

- **Panel persistence**: long-term burn-in or image ghosting detection would need:
  - Multiple frames of history
  - Temporal analysis of sensor readings
  - Expected variance metrics

## Run
```bash
./run.py --only display_fixture --boards metro_rp2040,metro_m4_airlift,metro_rp2350,metro_esp32s3
```

Display wiring must exist in farm.toml for boards that will run this test; others will skip.
