# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Offline self-tests for the harness utilities: python3 -m pytest test/harness -q

After Brent's tests_hil_board_utils/ (@ cf64e03), minus the board: SerialMonitor runs against a
pty pair, the catcher against a fake ProtoMQ on 127.0.0.1, flash with subprocess and the board
mocked. Every module is marked smoke so the default --tier keeps it; no test takes `board`, so
no farm board is ever selected, opened, mounted, flashed or power-cycled.
"""
import pytest

from harness.fakes import FakeProtoMQ, PtyBoard


@pytest.fixture
def pty_board():
    """A board whose console is a pty; see fakes.PtyBoard."""
    b = PtyBoard()
    yield b
    b.close()


@pytest.fixture
def fake_protomq():
    """ProtoMQ's HTTP API on 127.0.0.1; see fakes.FakeProtoMQ."""
    f = FakeProtoMQ()
    yield f
    f.close()
