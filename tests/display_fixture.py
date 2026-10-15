# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Display fixture test: ST7789 240x240 via fourwire.FourWire + busdisplay.BusDisplay.

Measures refresh performance and verifies backlight brightness control with
an optional light sensor.
"""

import farmlib

META = {
    "id": "display_fixture",
    "title": "ST7789 240x240 display via fourwire and busdisplay",
    "category": "Display",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": ["display"],  # farm.toml must have a 'display' wiring entry
    "skip_boards": [],
    "automation": "fixture",  # requires external display hardware
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [
        10728,  # backlight brightness regression
        9443,   # brightness control issue
        6264,   # backlight regression
    ],
}

DEVICE_SETUP = r'''
import board
import displayio
import time

PINS = @@PINS@@
LIGHT = @@LIGHT@@

def _pin(name):
    return getattr(board, name, None) if name else None

pins = {}
for key in PINS:
    pins[key] = _pin(PINS[key])
missing = [k for k in ("sck", "mosi", "dc") if pins.get(k) is None]
missing += [k for k in PINS if PINS[k] and pins.get(k) is None and k not in missing]

mods_ok = True
try:
    import busio
    import fourwire
    import busdisplay
except ImportError as e:
    mods_ok = False
    skip("display_modules", str(e))

if not mods_ok:
    pass
elif missing:
    check("display_pins_found", False, "no board pin for: " + str(missing) + " in " + str(PINS))
else:
    check("display_pins_found", True, str(PINS))
    spi = None
    light = None
    try:
        displayio.release_displays()
        spi = busio.SPI(clock=pins["sck"], MOSI=pins["mosi"])
        bus = fourwire.FourWire(
            spi,
            command=pins["dc"],
            chip_select=pins.get("cs"),
            reset=pins.get("rst"),
            baudrate=24000000
        )
        # Adafruit ST7789 init: SWRESET, SLPOUT, COLMOD 16-bit, MADCTL, INVON, NORON, DISPON
        init_seq = (b"\x01\x80\x96" b"\x11\x80\xff" b"\x3a\x81\x55\x0a" b"\x36\x01\x08"
                    b"\x21\x80\x0a" b"\x13\x80\x0a" b"\x36\x01\xc0" b"\x29\x80\xff")
        display = busdisplay.BusDisplay(bus, init_seq, width=240, height=240, rotation=0,
                                        backlight_pin=pins.get("bl"), auto_refresh=False)
        check("busdisplay_create", True)

        # Full-screen bitmap; flipping every pixel makes each refresh push a whole frame.
        bmp = displayio.Bitmap(240, 240, 2)
        pal = displayio.Palette(2)
        pal[0] = 0x000000
        pal[1] = 0xFFFFFF
        g = displayio.Group()
        g.append(displayio.TileGrid(bmp, pixel_shader=pal))
        display.root_group = g
        check("display_root_group_set", display.root_group is g)

        n_ok = 0
        t0 = time.monotonic_ns()
        for i in range(20):
            bmp.fill(i & 1)
            if display.refresh():
                n_ok += 1
        per_ms = (time.monotonic_ns() - t0) / 1e6 / 20
        check("display_refresh_20x", n_ok == 20 and per_ms < 250,
              "%d/20 refreshed, %.1f ms each" % (n_ok, per_ms), value=per_ms)
        metric("display_refresh_ms", per_ms, "ms", "lower")

        if pins.get("bl") is None:
            skip("display_brightness", "no bl pin in display_pins")
        else:
            for v in (1.0, 0.5, 0.0):
                display.brightness = v
                got = display.brightness
                check("display_brightness_%d" % int(v * 100), abs(got - v) < 0.01,
                      "set %.2f read %.2f" % (v, got))
            if LIGHT is None:
                skip("light_sensor", "light_sensor wiring not configured")
            elif _pin(LIGHT) is None:
                check("light_sensor", False, "no board pin " + LIGHT)
            else:
                import analogio
                light = analogio.AnalogIn(_pin(LIGHT))

                def _avg():
                    time.sleep(0.1)
                    s = 0
                    for _ in range(16):
                        s += light.value
                    return s // 16

                display.brightness = 1.0
                on_value = _avg()
                display.brightness = 0.0
                off_value = _avg()
                display.brightness = 1.0
                metric("light_sensor_on", on_value, "counts", "higher")
                check("light_tracks_backlight", on_value - off_value > 2000,
                      "on=%d off=%d" % (on_value, off_value))
    except Exception as e:
        check("display_setup", False, str(type(e).__name__) + ": " + str(e)[:120])
    finally:
        if light is not None:
            light.deinit()
        displayio.release_displays()
        if spi is not None:
            spi.deinit()
'''

DEFAULT_PINS = {"sck": "SCK", "mosi": "MOSI", "cs": "D9", "dc": "D7", "rst": "D5", "bl": "D6"}


def run(board, ctx):
    """Test ST7789 display with fourwire and busdisplay."""
    ctx.check("present", board.present(), board.usb)

    # Check for display wiring
    display_config = board.wiring.get("display", "")
    if not display_config:
        ctx.skip("display_fixture", "display wiring not configured")
        return

    # Parse display config (format: "st7789_240x240")
    if not str(display_config).startswith("st7789"):
        ctx.skip("display_fixture", f"unsupported display type: {display_config}")
        return

    # Get display pins from wiring or use defaults
    display_pins = dict(DEFAULT_PINS)
    display_pins.update(board.wiring.get("display_pins", {}) or {})
    light_sensor_pin = board.wiring.get("light_sensor") or None

    device_code = (DEVICE_SETUP.replace("@@PINS@@", repr(display_pins))
                   .replace("@@LIGHT@@", repr(light_sensor_pin)))

    try:
        ctx.device(device_code, timeout=90)
    except farmlib.FarmError as e:
        ctx.check("display_device", False, "FarmError: %s" % e)
