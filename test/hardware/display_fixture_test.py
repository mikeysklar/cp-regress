# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Display fixture test: ST7789 240x240 via fourwire.FourWire + busdisplay.BusDisplay.

pytest version of tests/display_fixture.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). It measures refresh performance and verifies backlight
brightness control with an optional light sensor. The device code runs once per board; each
check it reports is one test here, in the same order.
Targets: #10728 (backlight brightness regression), #9443 (brightness control issue),
#6264 (backlight regression).
"""
import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.wiring("display"),
    pytest.mark.targets(10728, 9443, 6264),
]

# display_brightness_%d for v in (1.0, 0.5, 0.0), in the device's order
BRIGHTNESS_CHECKS = ["display_brightness_100", "display_brightness_50", "display_brightness_0"]

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


@pytest.fixture(scope="module")
def display_config(board):
    """The display wiring for this board: (display_pins, light_sensor_pin), or a module skip.

    Host-side only (reads farm.toml wiring), so the run.py "display_fixture" skips happen here.
    """
    display_config = board.wiring.get("display", "")
    if not display_config:
        pytest.skip("display wiring not configured")

    # Parse display config (format: "st7789_240x240")
    if not str(display_config).startswith("st7789"):
        pytest.skip(f"unsupported display type: {display_config}")

    # Get display pins from wiring or use defaults
    display_pins = dict(DEFAULT_PINS)
    display_pins.update(board.wiring.get("display_pins", {}) or {})
    light_sensor_pin = board.wiring.get("light_sensor") or None
    return display_pins, light_sensor_pin


@pytest.fixture(scope="module")
def results(device, display_config):
    """Run the display code once per board. A FarmError lands in results.crashed."""
    display_pins, light_sensor_pin = display_config
    device_code = (DEVICE_SETUP.replace("@@PINS@@", repr(display_pins))
                   .replace("@@LIGHT@@", repr(light_sensor_pin)))
    return device.run(device_code, timeout=90)


def _gate(results, name, why):
    """For checks the device only reports on some paths (run.py records nothing otherwise).

    A check the device did report is asserted (a device skip() becomes a pytest skip). One it
    never reached skips, naming the earlier step that stopped it.
    """
    if name in results.checks or results.crashed or results.err:
        return results.expect(name)
    if "display_modules" in results.checks:
        pytest.skip("not reached: " + str(results.detail("display_modules") or "display modules missing"))
    if results.checks.get("display_pins_found") and results.checks["display_pins_found"][-1].get("ok") is False:
        pytest.skip("not reached: display pins missing")
    if "display_setup" in results.checks:
        pytest.skip("not reached: display_setup failed (%s)" % results.detail("display_setup"))
    if "display_brightness" in results.checks:
        pytest.skip("not reached: " + str(results.detail("display_brightness")))
    pytest.skip(why)
    return None


def _device_metric(results, metric, name, after):
    """Compare a device metric with its baseline; `after` is the check it is sent beside."""
    if name not in results.metrics:
        _gate(results, after, "not reported: %s not reached" % name)
        pytest.fail("%s ran but sent no %s" % (after, name), pytrace=False)
    rec = results.metric_record(name)
    metric(name, rec["value"], rec.get("unit", ""), rec.get("better", "lower"))


def _light_sensor_gate(results):
    """light_sensor is only reported when it stops the light checks (a skip, or no board pin).

    Then run.py records nothing for light_sensor_on / light_tracks_backlight, so they skip;
    a missing pin is charged once, by test_light_sensor.
    """
    if "light_sensor" in results.checks:
        pytest.skip("not reached: light_sensor: %s" % results.detail("light_sensor"))


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_display_fixture(display_config):
    """farm.toml wires an st7789 display to this board (otherwise the module skips)."""
    display_pins, _ = display_config
    assert display_pins


def test_display_device(results):
    """The raw REPL run finished; run.py's display_device check on a FarmError.

    run.py records this after the device checks, but it must be the first test that uses
    `results`: once a run loses the board, every later test in the module skips as not
    reached (test/fixtures/farm.py), so a crash is only charged by the test whose setup ran it.
    """
    assert not results.crashed, results.crashed  # "FarmError: ..." as in run.py


def test_display_modules(results):
    """busio, fourwire and busdisplay are built in (a skip names the missing one)."""
    if "display_modules" in results.checks or results.crashed or results.err:
        results.expect("display_modules")


def test_display_pins_found(results):
    """Every configured display pin name exists on `board` (sck, mosi, dc required)."""
    _gate(results, "display_pins_found", "not reported")


def test_busdisplay_create(results):
    """SPI, FourWire at 24 MHz and BusDisplay with the ST7789 init sequence construct."""
    _gate(results, "busdisplay_create", "not reported")


def test_display_root_group_set(results):
    """A full-screen bitmap group reads back as display.root_group."""
    _gate(results, "display_root_group_set", "not reported")


def test_display_refresh_20x(results):
    """20 full-frame refresh() calls all return True, averaging under 250 ms."""
    _gate(results, "display_refresh_20x", "not reported")


def test_display_refresh_ms(results, metric):
    """Milliseconds per full-frame refresh, compared with this board's baseline."""
    _device_metric(results, metric, "display_refresh_ms", "display_refresh_20x")


@pytest.mark.parametrize("name", BRIGHTNESS_CHECKS)
def test_display_brightness(results, name):
    """brightness set through the backlight pin reads back (skips when bl is empty)."""
    if "display_brightness" in results.checks:
        results.expect("display_brightness")  # the device's "no bl pin" skip
    _gate(results, name, "not reported")


def test_light_sensor(results):
    """The light_sensor pin exists (skips when light_sensor is not wired)."""
    _gate(results, "light_sensor", "not reported: light sensor wired and found")


def test_light_sensor_on(results, metric):
    """Averaged light sensor counts with the backlight on, compared with the baseline."""
    _light_sensor_gate(results)
    _device_metric(results, metric, "light_sensor_on", "light_tracks_backlight")


def test_light_tracks_backlight(results):
    """The light sensor reads at least 2000 counts more with brightness 1.0 than 0.0."""
    _light_sensor_gate(results)
    _gate(results, "light_tracks_backlight", "not reported")


def test_display_setup(results):
    """No exception while creating or driving the display (only reported when one is raised)."""
    if "display_setup" in results.checks:
        results.expect("display_setup")


def test_device_exception(results):
    """The device code ran to the end without raising (run.py's device_exception check)."""
    if results.crashed:
        pytest.skip("charged to test_display_device")
    assert not results.err, results.err[-400:]
