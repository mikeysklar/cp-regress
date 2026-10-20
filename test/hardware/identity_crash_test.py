# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Identity and crash checks on every board: device_uid, the soft-reset banner and identity,
the crash window, and the crash watch itself.

These are Brent's checks (device_uid in hil/firmware/conftest.py:424, expect_banner and
expect_identity in hil_checks.py, assert_none(CRASH_RE) in test_end_to_end.py:47 @ cf64e03)
that no committed test used until now. The negative test proves the crash watch fails when a
Traceback is printed, so the monitor is tested and not just trusted.

Only soft_reset() (Ctrl-D) resets the board here: no VBUS cut, so SAMD boards keep their USB
enumeration and nothing needs the SWD rescue. Smoke tier: two short sessions per board.
"""
import json
import os

import pytest

import hil_checks
from fixtures.identity import REPL_UID, UID_FILE

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.destructive,
]


def test_uid_matches_boot_out_and_repl(board, device_uid):
    """device_uid is non-empty and the right length for the port, and the UID boot_out.txt and
    microcontroller.cpu.uid both report."""
    uid = device_uid()
    assert uid, "device_uid returned nothing"
    problem = hil_checks.check_uid(uid, board.port)
    assert problem is None, problem
    boot_uid = hil_checks.parse_boot_out(board.boot_out()).uid
    assert boot_uid == uid, "boot_out.txt UID %r, device_uid %r" % (boot_uid, uid)
    with board.repl() as r:
        out, err = r.exec_raw(REPL_UID, timeout=30)
    assert not err.strip(), "REPL raised: %s" % err.strip()[-200:]
    repl_uid = None
    for line in out.splitlines():
        if line.startswith("cpr_uid "):
            repl_uid = line.split(None, 1)[1].strip().upper()
    assert repl_uid == uid, "REPL microcontroller.cpu.uid %r, device_uid %r" % (repl_uid, uid)


def test_uid_matches_history(board, device_uid):
    """device_uid agrees with the UID recorded for this board in history/device_uid.json."""
    uid = device_uid()
    if not os.path.exists(UID_FILE):
        pytest.skip("no %s (--no-history)" % UID_FILE)
    with open(UID_FILE) as f:
        rec = json.load(f).get(board.name)
    if not rec:
        pytest.skip("%s has no entry for %s (--no-history)" % (UID_FILE, board.name))
    assert rec.get("uid") == uid, "history has %r for %s, device_uid %r" % (
        rec.get("uid"), board.name, uid)


def test_soft_reset_no_crash_banner_and_identity(board, device_uid, serial):
    """After a soft reset: no crash or safe-mode line in the CRASH_WINDOW, the console banner
    matches boot_out.txt, boot_out.txt names the farm.toml board, and its UID is device_uid."""
    uid = device_uid()
    boot = serial.soft_reset()
    hil_checks.assert_no_crash(serial, since=boot, duration=hil_checks.CRASH_WINDOW,
                               port=board.port)
    banner = hil_checks.expect_banner(serial, timeout=20, poke=True)
    ident = hil_checks.expect_identity(board, board)  # boot_out.txt against farm.toml
    assert banner == ident.banner, "console banner %s, boot_out.txt %s" % (banner, ident.banner)
    assert ident.uid == uid, "after soft reset boot_out.txt UID %r, device_uid %r" % (
        ident.uid, uid)


def test_crash_watch_catches_traceback(board, serial):
    """A Python exception prints 'Traceback' on the console and the VM carries on. The crash
    watch with python_errors must fail on that line, so a silent monitor cannot pass a test."""
    since = serial.mark()
    serial.send("raise ValueError('cpr identity_crash negative test')")
    serial.expect(r"^ValueError: ", timeout=10)
    with pytest.raises(pytest.fail.Exception, match="traceback"):
        hil_checks.assert_no_crash(serial, since=since, duration=2, port=board.port,
                                   python_errors=True)
