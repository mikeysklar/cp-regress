# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Peripheral loopback tests with wiring: GPIO, UART, SPI, PWM counter, I2C, DAC/ADC.

Requires jumpers configured in farm.toml wiring keys.
Targets regressions: GPIO, UART, SPI, PWM, counter, I2C scanning, DAC/ADC.

The device has no access to farm.toml: the host substitutes each wiring value into the
device body at @@PINS@@ before running it.
"""

import farmlib

META = {
    "id": "peripherals_loopback",
    "title": "Peripheral loopback tests (requires wiring)",
    "category": "Peripherals",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": [],  # tests skip until specific wiring is present
    "skip_boards": [],
    "automation": "fixture",
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [7616, 10022, 3339, 717, 1216],
}

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


def run(board, ctx):
    ctx.check("present", board.present(), board.usb)

    for key, body in SECTIONS:
        if key not in board.wiring:
            ctx.skip(key, "wiring not configured")
            continue
        value = board.wiring[key]
        if key == "i2c_sensor":
            try:
                value = int(value, 0) if isinstance(value, str) else int(value)
            except ValueError:
                ctx.check(key, False, "bad i2c_sensor address %r in farm.toml" % (value,))
                continue
        else:
            value = [str(p) for p in value]
            if len(value) < 2:
                ctx.check(key, False, "need at least two pin names, got %r" % (value,))
                continue
        try:
            ctx.device(body.replace("@@PINS@@", repr(value)), timeout=30)
        except farmlib.FarmError as e:
            ctx.check(key, False, "FarmError: %s" % e)
