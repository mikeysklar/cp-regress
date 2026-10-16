# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""tools/power_cycle_monitor.py SWD rescue with openocd mocked: a hung or missing openocd must
come back as a failed rescue (a PortError, a FarmError), never as a raw TimeoutExpired or
OSError that would abort the CLI loop over the remaining boards. No tool runs and no board is
touched."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import flash

pytestmark = pytest.mark.smoke

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import power_cycle_monitor as pcm  # noqa: E402  pylint: disable=wrong-import-position


def _m4():
    """A Metro M4 stub: SWD at 500 kHz, no tty after the cut."""
    return SimpleNamespace(
        name="metro_m4", port="atmel-samd", usb="3-3.3", flash={"method": "swd", "speed": 500},
        tty=lambda: None)


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="openocd", timeout=60),
    FileNotFoundError(2, "No such file or directory", "/usr/bin/openocd"),
])
def test_swd_rescue_failure_returns_false(monkeypatch, exc):
    def hang(*_a, **_k):
        raise exc
    monkeypatch.setattr(flash, "_openocd", hang)
    assert pcm.swd_rescue(_m4(), 500, log=lambda _m: None) is False


def test_swd_rescue_success_returns_true(monkeypatch):
    monkeypatch.setattr(flash, "_openocd",
                        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""))
    assert pcm.swd_rescue(_m4(), 500, log=lambda _m: None) is True


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="openocd", timeout=60),
    OSError("openocd missing"),
])
def test_rescue_that_cannot_run_is_a_port_error(monkeypatch, exc):
    """The tty never comes back and openocd hangs or is missing: PortError, not the raw error."""
    def wait(board, timeout, **_k):
        raise pcm.PortError("%s: no tty at %s after %.3g s" % (board.name, board.usb, timeout))

    def hang(*_a, **_k):
        raise exc
    monkeypatch.setattr(pcm, "wait_for_port", wait)
    monkeypatch.setattr(flash, "_openocd", hang)
    with pytest.raises(pcm.PortError):
        pcm.wait_for_port_or_rescue(_m4(), timeout=1)
