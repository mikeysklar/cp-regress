# Farm idle sketch. Everything breathes: the NeoPixel in a highlighter shade
# chosen from board_id, and the plain LED via PWM in sync with it.
import sys
import time
import board

print("hello world")
print("%s %s" % (sys.implementation.name,
                 ".".join(str(v) for v in sys.implementation.version)))
bid = ""
try:
    bid = board.board_id
    print("board: %s" % bid)
except AttributeError:
    pass

SHADES = {
    "metro_m0_express":          (255,  25,  25),   # highlighter scarlet
    "metro_m4_express":          ( 57, 255,  20),   # neon green
    "metro_m4_airlift_lite":     ( 57, 255,  20),   # neon green
    "adafruit_metro_rp2040":     (  0, 255, 190),   # aqua
    "adafruit_metro_rp2350":     (  0, 170, 255),   # electric blue
    "adafruit_metro_esp32s2":    (150,  60, 255),   # violet
    "adafruit_metro_esp32s3":    (255,   0, 200),   # magenta
    "feather_nrf52840_express":  (255,  40, 110),   # hot pink
    "feather_stm32f405_express": (255, 120,   0),   # neon orange
}
r, g, b = SHADES.get(bid, (255, 255, 255))
print("shade: %d,%d,%d" % (r, g, b))

import digitalio

# Some boards gate NeoPixel power behind a pin. Without this the pixel is dark
# and looks broken -- this is why the nRF52840 showed nothing.
try:
    if hasattr(board, "NEOPIXEL_POWER"):
        npwr = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
        npwr.direction = digitalio.Direction.OUTPUT
        npwr.value = True
        print("neopixel power: on")
except Exception as e:
    print("neopixel power:", e)

# LED: prefer PWM so it breathes, fall back to on/off if the pin cannot PWM
led_pwm = None
led_dig = None
led_name = None
for name in ("LED", "L", "RED_LED", "BLUE_LED", "D13"):
    if hasattr(board, name):
        led_name = name
        try:
            import pwmio
            led_pwm = pwmio.PWMOut(getattr(board, name), frequency=1000, duty_cycle=0)
        except Exception:
            try:
                led_dig = digitalio.DigitalInOut(getattr(board, name))
                led_dig.direction = digitalio.Direction.OUTPUT
            except Exception:
                pass
        break
print("led: %s %s" % (led_name, "pwm" if led_pwm else ("digital" if led_dig else "none")))

pix = None
count = 1
try:
    import neopixel_write
    for name in ("NEOPIXEL", "NEOPIXEL0"):
        if hasattr(board, name):
            pix = digitalio.DigitalInOut(getattr(board, name))
            pix.direction = digitalio.Direction.OUTPUT
            count = getattr(board, "NEOPIXEL_COUNT", 1) or 1
            print("neopixel: %s x%d" % (name, count))
            break
except Exception as e:
    print("neopixel:", e)

LEVELS = list(range(6, 64, 2)) + list(range(64, 6, -2))
i = 0
while True:
    lv = LEVELS[i % len(LEVELS)]
    if pix is not None:
        try:
            neopixel_write.neopixel_write(
                pix, bytearray([g * lv // 255, r * lv // 255, b * lv // 255] * count))
        except Exception:
            pass
    if led_pwm is not None:
        led_pwm.duty_cycle = (lv * lv * 65535) // (64 * 64)   # squared, looks linear to the eye
    elif led_dig is not None:
        led_dig.value = lv > 32
    i += 1
    time.sleep(0.035)
