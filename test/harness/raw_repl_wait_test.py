# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Repl.raw_repl_wait(): WiFi boards get room for two failed supervisor joins on Ctrl-A."""
import types

import pytest

import farmlib

pytestmark = pytest.mark.smoke


def _repl(caps):
    r = farmlib.Repl.__new__(farmlib.Repl)  # no port opened
    r.board = types.SimpleNamespace(caps=set(caps))
    return r


def test_wifi_board_waits_past_two_failed_joins():
    assert _repl({"wifi", "native_usb"}).raw_repl_wait() > 2 * 9.7  # 19.3 s seen on the S3


def test_other_boards_keep_the_short_wait():
    assert _repl({"native_usb"}).raw_repl_wait() == 5
