# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Basic peripheral API checks without wiring: I2C/SPI/PWM construction, pin claimed twice,
use-after-deinit, singleton patterns.

pytest version of tests/peripherals_selfcheck.py. The device code below is identical to the
original (tools/pytest_parity.py checks that). It runs once per board; each check it reports is
one test here, in the same order. Targets: #1603 (I2C singleton), #5476 (PWM duty after
frequency change).
"""
import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.targets(1603, 5476),
]

DEVICE = r'''
import board
import busio
import digitalio
import pwmio
import analogio
import microcontroller
import time

# I2C buses on SCL/SDA (most boards have these). Without pull-ups CircuitPython must raise
# a clean RuntimeError, not hang or hard fault.
i2c_has_pullups = False
if hasattr(board, "SCL") and hasattr(board, "SDA"):
    try:
        bus = busio.I2C(board.SCL, board.SDA)
        i2c_has_pullups = True
        check("i2c_construct", True, "I2C bus created (pull-ups present)")
        bus.deinit()
    except RuntimeError as e:
        msg = str(e)
        if "pull up" in msg.lower():
            check("i2c_pullup_err", True, msg)
        else:
            check("i2c_construct", False, msg)
    except Exception as e:
        check("i2c_construct", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("i2c_construct", "board lacks SCL/SDA")

# Claiming the same pin twice should raise ValueError. Pick a plain GPIO by name; never a
# console, display or LED-only pin (the S2's DEBUG_TX/RX carry the farm console).
CANDIDATES = ("D5", "D6", "D9", "D10", "D11", "D12", "D13", "GP5", "IO5")
pin_found = None
for attr in CANDIDATES:
    p = getattr(board, attr, None)
    if isinstance(p, microcontroller.Pin):
        pin_found = p
        break

if pin_found is not None:
    try:
        io1 = digitalio.DigitalInOut(pin_found)
        io1.direction = digitalio.Direction.OUTPUT
        try:
            io2 = digitalio.DigitalInOut(pin_found)
            check("pin_claim_twice", False, "should have raised ValueError")
            io2.deinit()
        except ValueError as e:
            check("pin_claim_twice", True, str(e))
        finally:
            io1.deinit()
    except Exception as e:
        check("pin_claim_twice", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("pin_claim_twice", "no D pins found")

# Use after deinit should raise ValueError
if pin_found is not None:
    try:
        io = digitalio.DigitalInOut(pin_found)
        io.deinit()
        try:
            _ = io.direction
            check("use_after_deinit", False, "should have raised ValueError")
        except ValueError as e:
            check("use_after_deinit", True, str(e))
    except Exception as e:
        check("use_after_deinit", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("use_after_deinit", "no D pins found")

# #1603: board.I2C() called twice should return the same object (needs pull-ups)
if not i2c_has_pullups:
    skip("i2c_singleton", "no I2C pull-ups on this board; add an I2C sensor to wiring")
elif hasattr(board, "SCL") and hasattr(board, "SDA"):
    try:
        i2c1 = board.I2C()
        i2c2 = board.I2C()
        check("i2c_singleton", i2c1 is i2c2, "I2C() should return same object (issue #1603)")
    except Exception as e:
        check("i2c_singleton", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("i2c_singleton", "board lacks SCL/SDA")

# SPI with MOSI only should construct
if hasattr(board, "SCK") and hasattr(board, "MOSI"):
    try:
        spi = busio.SPI(board.SCK, MOSI=board.MOSI)
        check("spi_mosi_only", True, "SPI(SCK, MOSI=...) constructed")
        spi.deinit()
    except Exception as e:
        check("spi_mosi_only", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("spi_mosi_only", "board lacks SCK/MOSI")

# PWM: frequency and duty_cycle round trip at 3 frequencies
pwm_pin = None
for attr in ("D9", "D5", "D10", "D6", "D11"):
    p = getattr(board, attr, None)
    if isinstance(p, microcontroller.Pin):
        pwm_pin = p
        break

if pwm_pin is not None:
    pwm = None
    try:
        # .frequency is only writable with variable_frequency=True
        pwm = pwmio.PWMOut(pwm_pin, frequency=1000, duty_cycle=32768, variable_frequency=True)
        check("pwm_construct", True, "PWM created")
    except Exception as e:
        check("pwm_construct", False, "%s: %s" % (type(e).__name__, e))
    if pwm is not None:
        try:
            bad = []
            for freq in (500, 1000, 5000):
                pwm.frequency = freq
                time.sleep(0.01)
                if abs(pwm.frequency - freq) > freq * 0.1:
                    bad.append("%d->%d" % (freq, pwm.frequency))
            check("pwm_frequency_roundtrip", not bad, "off: %s" % bad if bad else "500/1000/5000 Hz")
        except Exception as e:
            check("pwm_frequency_roundtrip", False, "%s: %s" % (type(e).__name__, e))
        try:
            pwm.duty_cycle = 16384
            time.sleep(0.01)
            dc = pwm.duty_cycle
            check("pwm_duty_roundtrip", abs(dc - 16384) < 100, "duty_cycle=%d (expected ~16384)" % dc)
        except Exception as e:
            check("pwm_duty_roundtrip", False, "%s: %s" % (type(e).__name__, e))
        pwm.deinit()
else:
    skip("pwm_construct", "no suitable PWM pin found")

# AnalogIn on A0
if hasattr(board, "A0"):
    try:
        ain = analogio.AnalogIn(board.A0)
        val = ain.value
        check("analog_read", isinstance(val, int), "A0 read %d (type %s)" % (val, type(val).__name__))
        if isinstance(val, int):
            check("analog_range", 0 <= val <= 65535, "value=%d in [0..65535]" % val)
        ain.deinit()
    except Exception as e:
        check("analog_read", False, "%s: %s" % (type(e).__name__, e))
else:
    skip("analog_read", "board lacks A0")

# microcontroller.cpu.temperature
try:
    temp = microcontroller.cpu.temperature
    if temp is None:
        check("cpu_temp", True, "temperature is None (not supported)")
    elif isinstance(temp, float):
        check("cpu_temp", -40 <= temp <= 125, "temp=%.1f°C" % temp)
    else:
        check("cpu_temp", False, "temperature is %s, not float or None" % type(temp).__name__)
except Exception as e:
    check("cpu_temp", False, "%s: %s" % (type(e).__name__, e))

# neopixel_write if board has NEOPIXEL
if hasattr(board, "NEOPIXEL"):
    try:
        import neopixel_write
        import array
        buf = array.array("B", [255, 0, 0, 0, 255, 0])  # 2 RGB pixels
        try:
            if hasattr(board, "NEOPIXEL_POWER"):
                pwr = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
                pwr.direction = digitalio.Direction.OUTPUT
                pwr.value = True
                time.sleep(0.1)
            npx = digitalio.DigitalInOut(board.NEOPIXEL)
            npx.direction = digitalio.Direction.OUTPUT
            neopixel_write.neopixel_write(npx, buf)
            npx.deinit()
            check("neopixel_write", True, "wrote RGB pixels")
            if hasattr(board, "NEOPIXEL_POWER"):
                pwr.deinit()
        except ValueError as e:
            # Metro M0: NEOPIXEL is PA30 = SWCLK, unclaimable while an SWD probe is attached
            # (DSU DBGPRES latches on attach). Anywhere else "in use" is a real failure.
            if board.board_id == "metro_m0_express" and "in use" in str(e):
                skip("neopixel_write", "M0 NEOPIXEL is SWCLK; blocked while the SWD probe is attached")
            else:
                check("neopixel_write", False, "%s: %s" % (type(e).__name__, e))
        except Exception as e:
            check("neopixel_write", False, "%s: %s" % (type(e).__name__, e))
    except ImportError:
        skip("neopixel_write", "neopixel_write not available")
else:
    skip("neopixel_write", "board lacks NEOPIXEL")
'''


@pytest.fixture(scope="module")
def results(device):
    """Run the device checks once per board."""
    return device.run(DEVICE, timeout=60)


_NO_PWM = "not reported: no PWMOut (no PWM pin, or PWMOut failed)"


def _expect_if_reported(results, name, why):
    """For checks the device only reports on some paths (run.py records nothing otherwise)."""
    if name in results.checks or results.crashed or results.err:
        return results.expect(name)
    pytest.skip(why)
    return None


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_i2c_construct(results):
    """busio.I2C(SCL, SDA) constructs (not reported when the clean pull-up error is raised)."""
    _expect_if_reported(results, "i2c_construct", "not reported: I2C raised the pull-up error")


def test_i2c_pullup_err(results):
    """Without pull-ups, busio.I2C raises a clean RuntimeError naming the pull-ups."""
    _expect_if_reported(results, "i2c_pullup_err", "not reported: I2C bus has pull-ups")


def test_pin_claim_twice(results):
    """Claiming the same pin twice raises ValueError."""
    results.expect("pin_claim_twice")


def test_use_after_deinit(results):
    """Reading a DigitalInOut after deinit() raises ValueError."""
    results.expect("use_after_deinit")


def test_i2c_singleton(results):
    """board.I2C() called twice returns the same object (#1603)."""
    results.expect("i2c_singleton")


def test_spi_mosi_only(results):
    """busio.SPI(SCK, MOSI=...) constructs without MISO."""
    results.expect("spi_mosi_only")


def test_pwm_construct(results):
    """PWMOut constructs with variable_frequency=True."""
    results.expect("pwm_construct")


def test_pwm_frequency_roundtrip(results):
    """PWM frequency reads back within 10% at 500, 1000 and 5000 Hz."""
    _expect_if_reported(results, "pwm_frequency_roundtrip", _NO_PWM)


def test_pwm_duty_roundtrip(results):
    """PWM duty_cycle 16384 reads back within 100 counts after a frequency change (#5476)."""
    _expect_if_reported(results, "pwm_duty_roundtrip", _NO_PWM)


def test_analog_read(results):
    """AnalogIn(A0).value is an int."""
    results.expect("analog_read")


def test_analog_range(results):
    """AnalogIn(A0).value is in 0..65535."""
    _expect_if_reported(results, "analog_range", "not reported: A0 read failed or board lacks A0")


def test_cpu_temp(results):
    """microcontroller.cpu.temperature is None or a float in -40..125 C."""
    results.expect("cpu_temp")


def test_neopixel_write(results):
    """neopixel_write writes to board.NEOPIXEL without error."""
    results.expect("neopixel_write")


def test_device_ran(results):
    """The device code ran to the end without raising (run.py's device_exception check)."""
    results.expect_no_exception()
