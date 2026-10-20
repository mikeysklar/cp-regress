# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Finding each board from its USB path, and reading its hub port's power state, after the
read-only half of Brent's hil/tests_hil_board_utils/test_power.py @ 90cd5af
(test_find_port_matches_location, test_find_port_empty_location, test_uhubctl_reports_board_port).

His find_port caches the tty per board; we never cache (a ttyACM number moves on every reset),
so his cache test becomes "the console resolves to the device at farm.toml's USB path, now".
Nothing here switches power: uhubctl is only asked for status. His power_off_and_on and
power_cycle_board are the opt-in power_boot module (-m power_boot).
"""
import os
import re
import subprocess

import pytest

import farmlib

pytestmark = [
    pytest.mark.smoke,
]


def _iface_of(tty):
    """The USB interface a tty belongs to, from sysfs: e.g. '3-3.3.4.2:1.0'."""
    name = os.path.basename(os.path.realpath(tty))
    dev = os.path.realpath("/sys/class/tty/%s/device" % name)
    return os.path.basename(dev)


def test_console_resolves_from_usb_path(board):
    ttys = board.ttys()
    assert ttys, "%s: no tty under /dev/serial/by-path for %s" % (board.name, board.usb)
    console = ttys[0]
    assert _iface_of(console) == board.usb + ":1.0", (
        "%s: console %s -> %s, not interface 0 of %s"
        % (board.name, console, os.path.realpath(console), board.usb))
    for t in ttys:
        assert _iface_of(t).startswith(board.usb + ":1."), "%s belongs to another device" % t
        assert os.access(t, os.R_OK | os.W_OK), "no permission to open %s" % t


def test_empty_usb_path_has_no_tty():
    nowhere = farmlib.Board({"name": "nowhere", "board_id": "none", "usb": "9-9.9.4"}, {})
    assert not nowhere.present()
    assert nowhere.ttys() == []


def test_uhubctl_reports_board_port_powered(board):
    """uhubctl, unprivileged, sees the board's hub port with power on and a device connected.
    Status only: no -a, so nothing is switched."""
    hub, port = board.usb.rsplit(".", 1)
    res = subprocess.run([board.uhubctl, "-l", hub, "-p", port], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, "uhubctl failed: %s" % res.stderr.strip()
    # uhubctl also lists the USB3 companion hub; keep the section for this hub only.
    section = re.search(r"^Current status for hub %s .*?(?=^Current|\Z)" % re.escape(hub),
                        res.stdout, re.M | re.S)
    assert section, "hub %s not in uhubctl output:\n%s" % (hub, res.stdout)
    m = re.search(r"^\s*Port %s: [0-9a-f]{4} (.*)$" % re.escape(port), section.group(0), re.M)
    assert m, "port %s not in uhubctl output:\n%s" % (port, res.stdout)
    words = m.group(1).split(" [", 1)[0].split()
    assert "power" in words and "connect" in words, "port %s.%s: %s" % (hub, port, words)
