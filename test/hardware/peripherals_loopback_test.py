# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Peripheral loopback tests with wiring: GPIO, UART, SPI, PWM counter, I2C, DAC/ADC.

pytest version of tests/peripherals_loopback.py. The device code below is identical to the
original (tools/pytest_parity.py checks that). Each section runs only when its wiring key is in
the board's farm.toml entry; the host substitutes the wiring value at @@PINS@@. A section's
device code runs once per board, the first time one of its tests asks for it, then each check
it reports is one test. A FarmError in one section fails that section's test and the next
section still runs, as in run.py.
Targets: #7616 (GPIO), #10022 (UART), #3339 (SPI write_readinto), #717 (PWM/counter),
#1216 (I2C scan).
"""
import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(7616, 10022, 3339, 717, 1216),
]

DEVICE_GPIO_PAIR = r'''
import board
import digitalio
import time

pins = @@PINS@@
used = []
try:
    pin_a = getattr(board, pins[0])
    pin_b = getattr(board, pins[1])

    out_a = digitalio.DigitalInOut(pin_a)
    used.append(out_a)
    out_a.direction = digitalio.Direction.OUTPUT
    in_b = digitalio.DigitalInOut(pin_b)
    used.append(in_b)
    in_b.direction = digitalio.Direction.INPUT
    in_b.pull = digitalio.Pull.DOWN

    # Drive LOW, read LOW
    out_a.value = False
    time.sleep(0.01)
    check("gpio_pair_low", in_b.value == False, "drove LOW, read=%s" % in_b.value)

    # Drive HIGH, read HIGH
    out_a.value = True
    time.sleep(0.01)
    check("gpio_pair_high", in_b.value == True, "drove HIGH, read=%s" % in_b.value)

    # Pulls on pin A, with pin B released so it does not fight them
    in_b.deinit()
    out_a.switch_to_input(pull=digitalio.Pull.UP)
    time.sleep(0.01)
    check("gpio_pair_pullup", out_a.value == True, "pull-up read=%s" % out_a.value)
    out_a.pull = digitalio.Pull.DOWN
    time.sleep(0.01)
    check("gpio_pair_pulldown", out_a.value == False, "pull-down read=%s" % out_a.value)
except Exception as e:
    check("gpio_pair", False, "%s: %s" % (type(e).__name__, e))
finally:
    for o in used:
        o.deinit()
'''

DEVICE_UART_LOOP = r'''
import board
import busio
import time

pins = @@PINS@@
uart = None
try:
    tx = getattr(board, pins[0])
    rx = getattr(board, pins[1])

    test_data = b"Hello UART!" * 100  # 1100 bytes, ~96 ms at 115200
    uart = busio.UART(tx, rx, baudrate=115200, timeout=0.2,
                      receiver_buffer_size=2 * len(test_data))
    uart.reset_input_buffer()

    t0 = time.monotonic()
    uart.write(test_data)
    got = b""
    while len(got) < len(test_data) and time.monotonic() - t0 < 3:
        chunk = uart.read(len(test_data) - len(got))
        if chunk:
            got += chunk
    check("uart_loop_transfer", len(got) == len(test_data),
          "sent %d, read %d bytes" % (len(test_data), len(got)))
    bad = -1
    for i in range(min(len(got), len(test_data))):
        if got[i] != test_data[i]:
            bad = i
            break
    check("uart_loop_data", bad < 0 and len(got) == len(test_data),
          "first mismatch at %d" % bad if bad >= 0 else "%d bytes match" % len(got))
except Exception as e:
    check("uart_loop", False, "%s: %s" % (type(e).__name__, e))
finally:
    if uart is not None:
        uart.deinit()
'''

DEVICE_SPI_LOOP = r'''
import board
import busio

pins = @@PINS@@
spi = None
try:
    mosi = getattr(board, pins[0])
    miso = getattr(board, pins[1])
    sck = getattr(board, pins[2]) if len(pins) > 2 else board.SCK

    spi = busio.SPI(sck, MOSI=mosi, MISO=miso)
    while not spi.try_lock():
        pass
    try:
        spi.configure(baudrate=1000000)
        # write_readinto with 256 bytes
        test_data = bytes(range(256))
        result = bytearray(256)
        spi.write_readinto(test_data, result)
    finally:
        spi.unlock()

    bad = [i for i in range(256) if result[i] != test_data[i]]
    check("spi_loop_xfer", not bad,
          "%d/256 bytes differ, first at %s" % (len(bad), bad[0] if bad else "-"))
except Exception as e:
    check("spi_loop", False, "%s: %s" % (type(e).__name__, e))
finally:
    if spi is not None:
        spi.deinit()
'''

DEVICE_PWM_COUNT = r'''
import board
import time

pins = @@PINS@@
used = []
try:
    import pwmio
    import countio
except ImportError as e:
    pwmio = None
    skip("pwm_count", str(e))

if pwmio is not None:
    try:
        pwm_pin = getattr(board, pins[0])
        cnt_pin = getattr(board, pins[1])

        pwm = pwmio.PWMOut(pwm_pin, frequency=1000, duty_cycle=32768)  # 50%
        used.append(pwm)
        counter = countio.Counter(cnt_pin)  # counts falling edges: one per PWM period
        used.append(counter)

        # Count pulses over ~1 second, scaled by the measured window
        time.sleep(0.05)
        start = counter.count
        t0 = time.monotonic_ns()
        time.sleep(1.0)
        counted = counter.count - start
        secs = (time.monotonic_ns() - t0) / 1e9

        # 1 kHz: one falling edge per period, expect ~1000/s within 2%
        expected = 1000 * secs
        check("pwm_count_rate", abs(counted - expected) <= expected * 0.02,
              "counted %d in %.3f s (expected %d +-2%%)" % (counted, secs, expected),
              value=counted)
        counter.deinit()
        used.remove(counter)

        # Also check the waveform with PulseIn if available (pin is free again)
        try:
            import pulseio
        except ImportError:
            pulseio = None
            skip("pwm_pulseio_duty", "no pulseio")
        if pulseio is not None:
            pulsein = pulseio.PulseIn(cnt_pin, maxlen=20)
            used.append(pulsein)
            time.sleep(0.1)
            pulsein.pause()
            n = len(pulsein)
            if n >= 6:
                # skip the first entry (may be partial); alternating high/low widths
                a = [pulsein[i] for i in range(1, n, 2)]
                b = [pulsein[i] for i in range(2, n, 2)]
                wa = sum(a) / len(a)
                wb = sum(b) / len(b)
                duty_pct = 100 * wa / (wa + wb)
                check("pwm_pulseio_duty", abs(duty_pct - 50) <= 3,
                      "duty %.1f%% (expected 50 +-3%%), widths %d/%d us" % (duty_pct, wa, wb))
                check("pwm_pulseio_period", abs((wa + wb) - 1000) <= 50,
                      "period %d us (expected 1000)" % (wa + wb))
            else:
                check("pwm_pulseio_duty", False, "only %d pulses captured" % n)
    except Exception as e:
        check("pwm_count", False, "%s: %s" % (type(e).__name__, e))
    finally:
        for o in used:
            o.deinit()
'''

DEVICE_I2C_SENSOR = r'''
import board

addr = @@PINS@@
i2c = None
try:
    i2c = board.I2C()
    while not i2c.try_lock():
        pass
    try:
        addrs = i2c.scan()
    finally:
        i2c.unlock()
    check("i2c_scan_found", addr in addrs,
          "want 0x%02x, scan found %s" % (addr, [hex(x) for x in addrs]))
except Exception as e:
    check("i2c_sensor", False, "%s: %s" % (type(e).__name__, e))
'''

DEVICE_DAC_ADC = r'''
import board
import analogio
import time

pins = @@PINS@@
used = []
if not hasattr(analogio, "AnalogOut"):
    skip("dac_adc", "no analogio.AnalogOut on this build")
else:
    try:
        dac_pin = getattr(board, pins[0])
        adc_pin = getattr(board, pins[1])

        dac = analogio.AnalogOut(dac_pin)
        used.append(dac)
        adc = analogio.AnalogIn(adc_pin)
        used.append(adc)

        # Test 3 levels
        levels = [0, 32768, 65535]
        readings = []
        for level in levels:
            dac.value = level
            time.sleep(0.05)
            s = 0
            for _ in range(8):
                s += adc.value
            readings.append(s // 8)

        # Must actually follow the DAC: rising, spanning most of the range, mid near half
        lo, mid, hi = readings
        check("dac_adc_monotonic", lo < mid < hi, "readings: %s" % readings)
        check("dac_adc_span", hi - lo > 40000, "span %d counts, readings %s" % (hi - lo, readings))
        check("dac_adc_mid", abs(mid - 32768) < 6554, "mid %d (expected 32768 +-10%%)" % mid)
    except Exception as e:
        check("dac_adc", False, "%s: %s" % (type(e).__name__, e))
    finally:
        for o in used:
            o.deinit()
'''

SECTIONS = [
    ("gpio_pair", DEVICE_GPIO_PAIR),
    ("uart_loop", DEVICE_UART_LOOP),
    ("spi_loop", DEVICE_SPI_LOOP),
    ("pwm_count", DEVICE_PWM_COUNT),
    ("i2c_sensor", DEVICE_I2C_SENSOR),
    ("dac_adc", DEVICE_DAC_ADC),
]


class _Section:
    """What one wiring section did on this board: skipped, misconfigured, or its DeviceResults."""

    def __init__(self, key, skip=None, fail=None, results=None):
        self.key = key
        self.skip = skip          # why the section did not run (wiring not configured)
        self.fail = fail          # host-side failure detail (bad wiring value, FarmError)
        self.results = results    # DeviceResults when the device code ran


@pytest.fixture(scope="module")
def section(board, device):
    """section(key) -> _Section. Runs that section's device code once per board, on first use.

    Same host logic as run() in tests/peripherals_loopback.py, one section at a time.
    """
    bodies = dict(SECTIONS)
    done = {}

    def get(key):
        if key in done:
            return done[key]
        body = bodies[key]
        if key not in board.wiring:
            done[key] = _Section(key, skip="wiring not configured")
            return done[key]
        value = board.wiring[key]
        try:
            if key == "i2c_sensor":
                try:
                    value = int(value, 0) if isinstance(value, str) else int(value)
                except ValueError:
                    done[key] = _Section(
                        key, fail="bad i2c_sensor address %r in farm.toml" % (value,))
                    return done[key]
            else:
                value = [str(p) for p in value]
                if len(value) < 2:
                    done[key] = _Section(
                        key, fail="need at least two pin names, got %r" % (value,))
                    return done[key]
        except Exception as e:  # pylint: disable=broad-except
            # run.py would abort the fixture here; record it once, on this section's test
            done[key] = _Section(key, fail="bad %s wiring %r in farm.toml: %s: %s"
                                 % (key, board.wiring[key], type(e).__name__, e))
            return done[key]
        if getattr(board, "cpr_crashed", None):
            # an earlier section lost the board; run.py's run() would have stopped there
            done[key] = _Section(key, skip="not reached: the board stopped answering earlier (%s)"
                                 % board.cpr_crashed[:160])
            return done[key]
        res = device.run(body.replace("@@PINS@@", repr(value)), timeout=30)
        if res.crashed and res.crashed.startswith("FarmError:"):
            # run.py records a FarmError as this section's failed check and goes on to the next
            # section, so do not let it skip the rest of the module as "not reached".
            board.cpr_crashed = None
            done[key] = _Section(key, fail=res.crashed)
        else:
            if res.crashed:
                # This run happened inside the current test's body, after its autouse
                # _not_after_crash already counted it, so mark the crash as belonging to this
                # test: the next test then skips as not reached instead of running on a dead board.
                board.cpr_crashed_at = getattr(board, "cpr_started", 1) - 1
            done[key] = _Section(key, results=res)
        return done[key]

    return get


def _section_ok(sec):
    """The section-level check: wiring, host errors, an exception inside the section."""
    if sec.skip:
        pytest.skip(sec.skip)
    assert not sec.fail, sec.fail
    res = sec.results
    res.expect_no_exception()
    if sec.key in res.checks:
        res.expect(sec.key)  # the section's except branch, or its skip()


def _check(sec, name):
    """One check the section's device code reported."""
    if sec.skip:
        pytest.skip(sec.skip)
    if sec.fail:
        pytest.skip("not reached: %s failed (%s)" % (sec.key, sec.fail[:160]))
    res = sec.results
    if sec.key in res.checks and name not in res.checks:
        pytest.skip("not reached: %s: %s" % (sec.key, res.detail(sec.key)))
    if name not in res.checks and not res.crashed and not res.err:
        # run.py has no check at all when the device code never reports one
        # (pwm_pulseio_period after too few pulses)
        pytest.skip("not reported by the device")
    return res.expect(name)


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


# gpio_pair: wiring gpio_pair = [pin_a, pin_b], A jumpered to B

def test_gpio_pair(section):
    """gpio_pair section: wired, ran, raised nothing."""
    _section_ok(section("gpio_pair"))


def test_gpio_pair_low(section):
    """A driven LOW reads LOW on B (B pulled down)."""
    _check(section("gpio_pair"), "gpio_pair_low")


def test_gpio_pair_high(section):
    """A driven HIGH reads HIGH on B."""
    _check(section("gpio_pair"), "gpio_pair_high")


def test_gpio_pair_pullup(section):
    """With B released, A as input with pull-up reads HIGH."""
    _check(section("gpio_pair"), "gpio_pair_pullup")


def test_gpio_pair_pulldown(section):
    """With B released, A as input with pull-down reads LOW."""
    _check(section("gpio_pair"), "gpio_pair_pulldown")


# uart_loop: wiring uart_loop = [TX, RX], TX jumpered to RX

def test_uart_loop(section):
    """uart_loop section: wired, ran, raised nothing."""
    _section_ok(section("uart_loop"))


def test_uart_loop_transfer(section):
    """1100 bytes at 115200 baud all come back within 3 s."""
    _check(section("uart_loop"), "uart_loop_transfer")


def test_uart_loop_data(section):
    """The bytes that came back match byte for byte."""
    _check(section("uart_loop"), "uart_loop_data")


# spi_loop: wiring spi_loop = [MOSI, MISO] or [MOSI, MISO, SCK], MOSI jumpered to MISO

def test_spi_loop(section):
    """spi_loop section: wired, ran, raised nothing."""
    _section_ok(section("spi_loop"))


def test_spi_loop_xfer(section):
    """A 1 MHz write_readinto of bytes 0x00-0xFF reads back exactly (#3339)."""
    _check(section("spi_loop"), "spi_loop_xfer")


# pwm_count: wiring pwm_count = [PWM_pin, counter_pin], jumpered

def test_pwm_count(section):
    """pwm_count section: wired, pwmio/countio built in, ran, raised nothing."""
    _section_ok(section("pwm_count"))


def test_pwm_count_rate(section):
    """1 kHz PWM into countio.Counter counts 1000 per second within 2%."""
    _check(section("pwm_count"), "pwm_count_rate")


def test_pwm_pulseio_duty(section):
    """PulseIn sees 50% +-3 duty (skipped without pulseio)."""
    _check(section("pwm_count"), "pwm_pulseio_duty")


def test_pwm_pulseio_period(section):
    """PulseIn sees a 1000 +-50 us period."""
    _check(section("pwm_count"), "pwm_pulseio_period")


# i2c_sensor: wiring i2c_sensor = "0x77" or 119, a device on board.I2C()

def test_i2c_sensor(section):
    """i2c_sensor section: address valid, ran, raised nothing."""
    _section_ok(section("i2c_sensor"))


def test_i2c_scan_found(section):
    """board.I2C() scan includes the configured address."""
    _check(section("i2c_sensor"), "i2c_scan_found")


# dac_adc: wiring dac_adc = [DAC_pin, ADC_pin], jumpered

def test_dac_adc(section):
    """dac_adc section: wired, AnalogOut built in, ran, raised nothing."""
    _section_ok(section("dac_adc"))


def test_dac_adc_monotonic(section):
    """ADC readings at DAC 0, 32768, 65535 strictly rise."""
    _check(section("dac_adc"), "dac_adc_monotonic")


def test_dac_adc_span(section):
    """Top minus bottom reading is over 40000 counts (an open jumper fails here)."""
    _check(section("dac_adc"), "dac_adc_span")


def test_dac_adc_mid(section):
    """The middle reading is within 10% of 32768."""
    _check(section("dac_adc"), "dac_adc_mid")
