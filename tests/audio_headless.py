# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Headless audio tests: wave generation, play, multi-play, synthio.

Tests basic audio APIs without hardware validation (no loopback ADC).
Targets regressions: #1623 (repeated play), audio module APIs.
"""

import farmlib

META = {
    "id": "audio_headless",
    "title": "Audio module tests (no sound verification)",
    "category": "Audio",
    "tier": "nightly",
    "needs": [],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [1623, 6200, 4979],
}

# Ports whose builds in this checkout enable audioio or audiopwmio for the farm boards
# (atmel-samd/stm: audioio on A0; raspberrypi/nordic: audiopwmio). espressif is left out:
# the S2 has audioio, the S3 has neither.
EXPECT_OUT_PORTS = ("atmel-samd", "raspberrypi", "nordic", "stm")

DEVICE = r'''
import board

have_audiopwmio = False
have_audioio = False
have_synthio = False

try:
    import audiopwmio
    have_audiopwmio = True
except ImportError:
    pass

try:
    import audioio
    have_audioio = True
except ImportError:
    pass

try:
    import synthio
    have_synthio = True
except ImportError:
    pass

expect_out = @@EXPECT_OUT@@
mods = "audiopwmio=%s audioio=%s synthio=%s" % (have_audiopwmio, have_audioio, have_synthio)
if expect_out:
    check("audio_modules", have_audioio or have_audiopwmio, mods)
elif not (have_audiopwmio or have_audioio or have_synthio):
    skip("audio_modules", "no audio modules available")
else:
    skip("audio_modules", "no expectation for this port; " + mods)

def _pick_out():
    # (constructor, pin) for one output; audioio wants a DAC pin, PWM takes any digital pin
    if have_audioio and hasattr(board, "A0"):
        return audioio.AudioOut, board.A0
    if have_audiopwmio:
        for name in ("D5", "D9"):
            if hasattr(board, name):
                return audiopwmio.PWMAudioOut, getattr(board, name)
        for name in sorted(dir(board)):
            if len(name) > 1 and name[0] == "D" and name[1:].isdigit():
                return audiopwmio.PWMAudioOut, getattr(board, name)
    return None, None

def _audio_play_test():
    import time
    import audiocore
    sample_rate = 8000
    duration = 0.5
    # 1 kHz square wave, unsigned 8-bit: small enough for a SAMD21, no float math needed
    wave = bytearray(int(sample_rate * duration))
    for i in range(len(wave)):
        wave[i] = 192 if (i // 4) & 1 else 64
    raw = audiocore.RawSample(wave, channel_count=1, sample_rate=sample_rate)
    ctor, out_pin = _pick_out()
    if ctor is None:
        skip("audio_play_start", "no suitable output pin")
        return
    audio = ctor(out_pin)
    try:
        audio.play(raw, loop=False)
        check("audio_play_start", audio.playing, "playing=%s right after play()" % audio.playing)
        start = time.monotonic()
        while audio.playing and (time.monotonic() - start) < duration + 1.0:
            time.sleep(0.01)
        elapsed = time.monotonic() - start
        check("audio_play_duration", not audio.playing and elapsed >= duration * 0.5,
              "playing=%s after %.2f s (sample is %.2f s)" % (audio.playing, elapsed, duration), elapsed)
        try:
            for i in range(5):
                audio.play(raw, loop=False)
                time.sleep(0.01)
            ok = audio.playing
            audio.stop()
            check("audio_repeat_play", ok and not audio.playing,
                  "5 back-to-back play() calls, playing after=%s, after stop()=%s" % (ok, audio.playing))
        except Exception as e:
            check("audio_repeat_play", False, "repeat play failed: %s: %s" % (type(e).__name__, e))
    finally:
        audio.deinit()
    try:
        audio = ctor(out_pin)
        audio.deinit()
        check("audio_reinit", True, "re-init on same pin after deinit()")
    except Exception as e:
        check("audio_reinit", False, "%s: %s" % (type(e).__name__, e))

def _synthio_test():
    synth = synthio.Synthesizer(sample_rate=8000, channel_count=1)
    try:
        synth.press(60)
        check("synthio_press", tuple(synth.pressed) == (60,), "pressed=%r" % (synth.pressed,))
        synth.release(60)
        check("synthio_release", len(synth.pressed) == 0, "pressed=%r" % (synth.pressed,))
    finally:
        synth.deinit()

if have_audioio or have_audiopwmio:
    guard("audio_play", _audio_play_test)
else:
    skip("audio_play", "no audioio or audiopwmio")

if have_synthio:
    guard("synthio", _synthio_test)
else:
    skip("synthio", "synthio not available")
'''


def run(board, ctx):
    ctx.check("present", board.present(), board.usb)
    body = DEVICE.replace("@@EXPECT_OUT@@", str(board.port in EXPECT_OUT_PORTS))
    try:
        res, out, err = ctx.device(body, timeout=60)
    except farmlib.FarmError as e:
        ctx.check("device_run", False, str(e))
        return
    names = [r.get("name") for r in res]
    ctx.check("device_run", "synthio" in names or "synthio_release" in names or "synthio_press" in names,
              "device results: %s" % names)
