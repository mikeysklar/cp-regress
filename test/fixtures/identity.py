# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""device_uid, after Brent's device_uid fixture (hil/firmware/conftest.py:424 @ cf64e03).

    def test_same_board(device_uid):
        uid = device_uid()

His reads the "Device UID:" line WipperSnapper prints in connect(). CircuitPython writes the
chip UID to boot_out.txt instead (main.c:918-922, "UID:" + upper-case hex), so ours reads it
from there, resolved from the board's USB path at the moment of use, and falls back to asking
the REPL for microcontroller.cpu.uid when the line is missing (older builds, or a board whose
port prints zeros).

The UID of every board is kept in history/device_uid.json, keyed by farm.toml board name. A
board that answers with a different UID than last time fails loudly and is marked down for the
rest of the run: someone swapped the board on that hub port, or the USB path in farm.toml now
points at another board. Delete the board's key from the file to accept an intended swap.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager

import pytest

import farmlib
import hil_checks
import run as cpr_run  # HISTORY: device_uid.json sits next to metrics.jsonl

UID_FILE = os.path.join(os.path.dirname(cpr_run.HISTORY), "device_uid.json")

# %02X per byte rather than .hex(): the same form as boot_out.txt, and it needs nothing a
# small build (Metro M0) might leave out.
REPL_UID = ("import microcontroller\n"
            "print('cpr_uid', ''.join('%02X' % b for b in microcontroller.cpu.uid))\n")


@contextmanager
def _locked(path):
    """The same lock-file scheme as fixtures.history, so xdist workers can write at once."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_uid(board, timeout=30):
    """(uid, source) for a farmlib.Board: boot_out.txt first, then the REPL.

    Raises farmlib.FarmError when neither gives a plausible UID for the board's port.
    """
    why = []
    try:
        uid = hil_checks.parse_boot_out(board.boot_out()).uid
        bad = hil_checks.check_uid(uid, board.port)
        if not bad:
            return uid, "boot_out.txt"
        why.append("boot_out.txt: " + bad)
    except farmlib.FarmError as e:  # a different board's drive is a hard stop, not a fallback
        raise farmlib.FarmError("%s: boot_out.txt: %s" % (board.name, e)) from e
    with board.repl() as r:
        out, err = r.exec_raw(REPL_UID, timeout=timeout)
    uid = None
    for line in out.splitlines():
        if line.startswith("cpr_uid "):
            uid = line.split(None, 1)[1].strip().upper()
    bad = hil_checks.check_uid(uid, board.port)
    if not bad:
        return uid, "repl"
    why.append("REPL: %s %s" % (bad, err.strip()[-160:]))
    raise farmlib.FarmError("%s: no UID: %s" % (board.name, "; ".join(why)))


def check_and_record(board, uid, source, write=True, path=UID_FILE):
    """Compare uid with the one stored for board.name, then store it. Returns a list of
    problems (empty when the board is the one seen before, or is new)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _locked(path):
        data = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
        problems = []
        prev = data.get(board.name)
        if prev and prev.get("uid") != uid:
            problems.append("UID %s, but %s had %s (first seen %s at %s). Board swapped or "
                            "farm.toml usb wrong; delete %r from %s if intended"
                            % (uid, board.name, prev.get("uid"), prev.get("first_seen"),
                               prev.get("usb"), board.name, path))
        for other, rec in data.items():
            if other != board.name and rec.get("uid") == uid:
                problems.append("UID %s is also recorded for %s (at %s): two farm.toml entries "
                                "reach one board" % (uid, other, rec.get("usb")))
        if write and not problems:
            rec = dict(prev or {}, uid=uid, board_id=board.board_id, usb=board.usb,
                       source=source, last_seen=now)
            rec.setdefault("first_seen", now)
            data[board.name] = rec
            with open(path, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
        return problems


@pytest.fixture
def device_uid(request, board):
    """device_uid(timeout) -> the board's chip UID, upper-case hex, as boot_out.txt prints it.

    Read once per board per run (cached on the config), checked against history/device_uid.json.
    A changed UID fails the test and marks the board down for the rest of the run. With
    --no-history the check still runs but nothing is written.
    """
    config = request.config
    if not hasattr(config, "cpr_uids"):
        config.cpr_uids = {}  # board -> uid read this run

    def read(timeout=30):
        if board.name in config.cpr_uids:
            return config.cpr_uids[board.name]
        try:
            uid, source = read_uid(board, timeout=timeout)
        except farmlib.FarmError as e:
            pytest.fail(str(e), pytrace=False)
        problems = check_and_record(board, uid, source,
                                    write=not config.getoption("no_history"))
        if problems:
            why = "identity: " + "; ".join(problems)
            config.cpr_down[board.name] = why
            pytest.fail("%s: %s" % (board.name, why), pytrace=False)
        config.cpr_uids[board.name] = uid
        return uid

    return read
