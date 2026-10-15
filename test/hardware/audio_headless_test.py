# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Headless audio tests: wave generation, play, multi-play, synthio.

pytest version of tests/audio_headless.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). It runs once per board; each check it reports is one test
here, in the same order. There is no sound verification (no loopback ADC).
Targets: #1623 (repeated play), #6200 (audio module init), #4979 (synthio press/release).
"""
import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(1623, 6200, 4979),
]

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


# Checks reported inside a guarded section, and the section that gates them. The device skips
# the section when the module is not built in, and guard() reports a failure under the section
# name if the section raised; either way the checks after that point are never sent.
SECTIONS = {
    "audio_play_start": "audio_play",
    "audio_play_duration": "audio_play",
    "audio_repeat_play": "audio_play",
    "audio_reinit": "audio_play",
    "synthio_press": "synthio",
    "synthio_release": "synthio",
}

AUDIO_PLAY_CHECKS = ["audio_play_start", "audio_play_duration", "audio_repeat_play", "audio_reinit"]
SYNTHIO_CHECKS = ["synthio_press", "synthio_release"]


def _expect_in_section(results, name):
    """Assert a check in a guarded section; skip it if the device never got that far."""
    if name in results.checks:
        return results.expect(name)
    section = SECTIONS.get(name)
    rs = results.checks.get(section) if section else None
    if rs and all(r.get("ok") is None for r in rs):
        pytest.skip(rs[0].get("detail") or "%s skipped on the device" % section)
    if rs and any(r.get("ok") is False for r in rs):
        pytest.skip("not reached: %s raised (%s)" % (section, rs[-1].get("detail", "")))
    # audio_play_start skipped ("no suitable output pin"): the rest of the section returns early
    if section == "audio_play":
        start = results.checks.get("audio_play_start")
        if start and all(r.get("ok") is None for r in start):
            pytest.skip(start[0].get("detail") or "audio_play_start skipped")
    return results.expect(name)  # skips if the run crashed, fails otherwise


def _expect_if_reported(results, name):
    """A guard() section name: only reported as a failure (raised) or a skip (not built in)."""
    if name not in results.checks:
        if results.crashed or results.err:
            results.expect(name)  # skips as "not reached"
        return
    results.expect(name)


@pytest.fixture(scope="module")
def results(board, device):
    """Run the audio body once per board, with the port's audio-output expectation filled in."""
    body = DEVICE.replace("@@EXPECT_OUT@@", str(board.port in EXPECT_OUT_PORTS))
    res = device.run(body, timeout=60)
    if res.crashed:
        # run.py charges a lost board to device_run. The test that sets this fixture up is
        # test_audio_modules, which only skips on a crash, and farm.py would then skip every
        # later test (test_device_run included) as not reached, so the crash would never fail.
        # No later test in this module touches the board, so clear the module-wide skip; the
        # checks still skip as not reached and test_device_run fails with the reason. The
        # board teardown still health-checks and power-cycles.
        board.cpr_crashed = None
    return res


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


def test_audio_modules(results):
    """audioio or audiopwmio is built in on ports that should have it; a skip lists what exists."""
    results.expect("audio_modules")


@pytest.mark.parametrize("name", AUDIO_PLAY_CHECKS)
def test_audio_play_check(results, name):
    """One audio playback check (a skip means no audio output module or pin on this board)."""
    _expect_in_section(results, name)


def test_audio_play(results):
    """Construction and playback did not raise (skips when audioio/audiopwmio is not built in)."""
    _expect_if_reported(results, "audio_play")


@pytest.mark.parametrize("name", SYNTHIO_CHECKS)
def test_synthio_check(results, name):
    """One synthio press/release check (a skip means synthio is not built in)."""
    _expect_in_section(results, name)


def test_synthio(results):
    """synthio construction did not raise (skips when synthio is not built in)."""
    _expect_if_reported(results, "synthio")


def test_device_run(results):
    """The device body ran to its last section (or the raw REPL error if it did not)."""
    assert not results.crashed, results.crashed
    names = [r.get("name") for r in results.records]
    assert "synthio" in names or "synthio_release" in names or "synthio_press" in names, \
        "device results: %s" % names


def test_device_ran(results):
    """The device code ran to the end without raising (device_exception in run.py)."""
    if results.crashed:
        pytest.skip("charged to test_device_run")  # run.py adds no device_exception then
    results.expect_no_exception()
