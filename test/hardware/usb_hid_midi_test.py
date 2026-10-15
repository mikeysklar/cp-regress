# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""USB HID and MIDI enumeration and data flow.

pytest version of tests/usb_hid_midi.py. The device code below is identical to the original
(tools/pytest_parity.py checks that). The host reads the board's USB interfaces first, then the
board reports what usb_hid and usb_midi have enabled, and the host expectations follow from
that, as in the original. Finally the host holds the board's ALSA rawmidi node open while the
board sends 50 MIDI messages, and compares the bytes that arrive.

Targets: #1769, #2048, #5213, #9306, #4724, #4895, #7944, #9130, #9847, #6764, #10373, #7930,
#5101.
"""
import glob
import logging
import os
import re
import time

import pytest

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(1769, 2048, 5213, 9306, 4724, 4895, 7944, 9130, 9847, 6764, 10373, 7930, 5101),
]

log = logging.getLogger(__name__)

DEVICE_HID_CHECK = r'''
try:
    import usb_hid
except ImportError:
    usb_hid = None
if usb_hid is None:
    skip("usb_hid.import", "usb_hid not in this build")
    print("CPR_HID -1 0")
else:
    devs = usb_hid.devices
    check("usb_hid.devices_is_tuple", isinstance(devs, tuple), type(devs).__name__)
    if len(devs) == 0:
        skip("usb_hid.devices", "usb_hid.devices is empty: disabled by default on this port, or by boot.py")
    kbd = 0
    for i, device in enumerate(devs):
        try:
            up = device.usage_page
            check("usb_hid.device_%d_usage_page" % i, isinstance(up, int) and 0 < up <= 0xFFFF, "0x%04x" % up)
        except Exception as e:
            up = None
            check("usb_hid.device_%d_usage_page" % i, False, "%s: %s" % (type(e).__name__, e))
        try:
            u = device.usage
            check("usb_hid.device_%d_usage" % i, isinstance(u, int) and 0 < u <= 0xFFFF, "0x%04x" % u)
        except Exception as e:
            u = None
            check("usb_hid.device_%d_usage" % i, False, "%s: %s" % (type(e).__name__, e))
        if up == 0x01 and u == 0x06:
            kbd = 1
        # Returns None, or bytes if the host sent an OUT report (Linux sends the keyboard LED
        # state on enumeration, so a keyboard may legitimately return b'\x00' once).
        try:
            report = device.get_last_received_report()
            check("usb_hid.device_%d_get_last_received_report" % i,
                  report is None or isinstance(report, bytes), repr(report))
        except ValueError as e:
            skip("usb_hid.device_%d_get_last_received_report" % i, "needs report_id: %s" % e)
        except Exception as e:
            check("usb_hid.device_%d_get_last_received_report" % i, False, "%s: %s" % (type(e).__name__, e))
    print("CPR_HID %d %d" % (len(devs), kbd))
'''

DEVICE_MIDI_CHECK = r'''
try:
    import usb_midi
except ImportError:
    usb_midi = None
if usb_midi is None:
    skip("usb_midi.import", "usb_midi not in this build")
    print("CPR_MIDI -1")
else:
    ports = usb_midi.ports
    check("usb_midi.ports_is_tuple", isinstance(ports, tuple), type(ports).__name__)
    n = len(ports)
    if n == 0:
        skip("usb_midi.ports", "usb_midi.ports is empty: disabled by default on this port, or by boot.py")
    else:
        check("usb_midi.ports_count_is_2", n == 2, n)
        check("usb_midi.ports_0_is_PortIn", isinstance(ports[0], usb_midi.PortIn), type(ports[0]).__name__)
        if n > 1:
            check("usb_midi.ports_1_is_PortOut", isinstance(ports[1], usb_midi.PortOut), type(ports[1]).__name__)
    print("CPR_MIDI %d" % n)
'''

# @@DRAIN@@ is replaced with True when the host holds the ALSA rawmidi input open, so the
# device FIFO is being drained and every byte must be accepted.
DEVICE_MIDI_SEND = r'''
import time
import usb_midi
port_out = usb_midi.ports[1]
# 25 x (Note On, Note Off) for middle C = 50 messages, 150 bytes
msgs = bytes([0x90, 0x3C, 0x40, 0x80, 0x3C, 0x00]) * 25
sent = 0
bad = None
t0 = time.monotonic()
try:
    while sent < len(msgs) and time.monotonic() - t0 < 3:
        n = port_out.write(msgs[sent:sent + 3])
        if n is not None and not isinstance(n, int):
            bad = repr(n)
            break
        if n:
            sent += n
    dt = time.monotonic() - t0
    check("usb_midi.write_returns_int", bad is None, bad or "")
    if @@DRAIN@@:
        check("usb_midi.write_all_accepted", sent == len(msgs), "%d/%d bytes in %.2f s" % (sent, len(msgs), dt), sent)
    else:
        skip("usb_midi.write_all_accepted", "no host reader; %d/%d bytes accepted" % (sent, len(msgs)))
except Exception as e:
    check("usb_midi.write", False, "%s: %s" % (type(e).__name__, e))
'''

MIDI_EXPECT = bytes([0x90, 0x3C, 0x40, 0x80, 0x3C, 0x00]) * 25

DEVICE_TIMEOUT = 30  # the original's _device() default

# Device checks with fixed names, in the original's order. Some are only reported on some
# boards (an import skip, an empty-tuple skip); those skip here when the device did not send them.
HID_CHECKS = ["usb_hid.import", "usb_hid.devices_is_tuple", "usb_hid.devices"]
# usb_hid.device_<i>_<attr>, one per entry in usb_hid.devices; all indexes are checked together
HID_DEVICE_ATTRS = ["usage_page", "usage", "get_last_received_report"]
MIDI_CHECKS = ["usb_midi.import", "usb_midi.ports_is_tuple", "usb_midi.ports",
               "usb_midi.ports_count_is_2", "usb_midi.ports_0_is_PortIn", "usb_midi.ports_1_is_PortOut"]
MIDI_SEND_CHECKS = ["usb_midi.write_returns_int", "usb_midi.write_all_accepted"]


def _tag(out, tag):
    """Ints from a 'CPR_<tag> a b' line printed by the device, or None."""
    for line in out.splitlines():
        parts = line.strip().split()
        if parts and parts[0] == tag:
            try:
                return [int(p) for p in parts[1:]]
            except ValueError:
                return None
    return None


def _hid_sysfs(board):
    """realpaths of /sys/bus/hid/devices entries whose parent is one of this board's interfaces."""
    want = "/%s:1." % board.usb
    return [p for p in (os.path.realpath(d) for d in glob.glob("/sys/bus/hid/devices/*")) if want in p]


def _midi_node(board):
    hits = sorted(glob.glob(os.path.join(board.sysfs(), board.usb + ":1.*", "sound", "card*", "midiC*D0")))
    return "/dev/snd/" + os.path.basename(hits[0]) if hits else None


def _read_quiet(fd, quiet=0.5, limit=3.0):
    buf = b""
    end = time.time() + limit
    last = time.time()
    while time.time() < end and time.time() - last < quiet:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            chunk = b""
        if chunk:
            buf += chunk
            last = time.time()
        else:
            time.sleep(0.05)
    return buf


def _assert_ran(results):
    """What the original's _device() and ctx.device() charge: a REPL failure, a device raise."""
    assert not results.crashed, "REPL: %s" % results.crashed
    assert not results.err, "device_exception: %s" % results.err[-400:]


def _expect_if_reported(results, name):
    """results.expect(name), or a skip when this board's device code never reaches that check."""
    if results.crashed or results.err or name in results.checks:
        results.expect(name)
    else:
        pytest.skip("%s not reported on this board" % name)


# ---------- module-scoped steps, run once per board in the original's order ----------

@pytest.fixture(scope="module")
def host(board):
    """Host-side USB enumeration, read before any device code runs."""
    present = board.present()  # same read order as the original
    vid_pid = board.vid_pid()
    interfaces = board.interfaces()
    classes = sorted(interfaces.values())
    return {
        "present": present,
        "vid_pid": vid_pid,
        "interfaces": interfaces,
        "classes": classes,
        "hid_found": "03" in classes,
        "midi_found": "01" in classes,  # AudioControl + MIDIStreaming are both class 01
    }


@pytest.fixture(scope="module")
def hid_run(host, device):  # pylint: disable=unused-argument,redefined-outer-name
    """DEVICE_HID_CHECK, after the host snapshot. Its CPR_HID line is [n_devices, has_keyboard]."""
    return device.run(DEVICE_HID_CHECK, timeout=DEVICE_TIMEOUT)


@pytest.fixture(scope="module")
def midi_run(hid_run, device):  # pylint: disable=unused-argument,redefined-outer-name
    """DEVICE_MIDI_CHECK, after the HID run. Its CPR_MIDI line is [n_ports]."""
    return device.run(DEVICE_MIDI_CHECK, timeout=DEVICE_TIMEOUT)


def _hid(hid_run):  # pylint: disable=redefined-outer-name
    return _tag(hid_run.out, "CPR_HID")


def _midi(midi_run):  # pylint: disable=redefined-outer-name
    return _tag(midi_run.out, "CPR_MIDI")


def _need_hid_devices(hid_run):  # pylint: disable=redefined-outer-name
    """Skip with the original's reason unless the device reported at least one HID device."""
    hid = _hid(hid_run)
    if not hid:
        pytest.skip("device did not report usb_hid state")
    if hid[0] < 0:
        pytest.skip("usb_hid not in this build")
    if hid[0] == 0:
        pytest.skip("usb_hid.devices is empty (see usb_hid_interface_absent_when_disabled)")
    return hid


def _need_midi_ports(midi_run):  # pylint: disable=redefined-outer-name
    midi = _midi(midi_run)
    if not midi:
        pytest.skip("device did not report usb_midi state")
    if midi[0] < 0:
        pytest.skip("usb_midi not in this build")
    if midi[0] == 0:
        pytest.skip("usb_midi.ports is empty (see usb_midi_interface_absent_when_disabled)")
    return midi


@pytest.fixture(scope="module")
def hid_host(board, hid_run, midi_run):  # pylint: disable=unused-argument,redefined-outer-name
    """sysfs HID nodes and the first readable report descriptor (only when HID is enabled)."""
    hid = _hid(hid_run)
    if not hid or hid[0] <= 0:
        return None
    nodes = _hid_sysfs(board)
    desc = b""
    for node in nodes:
        try:
            with open(os.path.join(node, "report_descriptor"), "rb") as f:
                desc = f.read()
            break
        except OSError as e:
            log.info("report_descriptor %s: %s", node, e)
    return {"nodes": nodes, "desc": desc}


@pytest.fixture(scope="module")
def midi_send(board, device, midi_run, hid_host):  # pylint: disable=unused-argument,redefined-outer-name
    """Hold the ALSA rawmidi input open, send 50 messages from the board, read what arrives.

    Runs only when the board reports two MIDI ports. The rawmidi fd is always closed.
    """
    midi = _midi(midi_run)
    if not midi or midi[0] < 2:
        return None
    rec = {"node": None, "why": None, "drain": False, "results": None, "got": None, "error": None}
    fd = None
    node = _midi_node(board)
    rec["node"] = node
    if not node:
        rec["why"] = "no ALSA midiC*D0 under %s (snd-usb-audio not bound?)" % board.usb
    else:
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            rec["why"] = "cannot open %s: %s" % (node, e)
    try:
        if fd is not None:
            time.sleep(0.5)
            stale = _read_quiet(fd, quiet=0.3, limit=2)  # bytes left in the device FIFO by earlier runs
            if stale:
                log.info("discarded %d stale MIDI bytes", len(stale))
        rec["drain"] = fd is not None
        rec["results"] = device.run(DEVICE_MIDI_SEND.replace("@@DRAIN@@", "True" if fd is not None else "False"),
                                    timeout=DEVICE_TIMEOUT)
        if fd is not None:
            rec["got"] = _read_quiet(fd)
    except Exception as e:  # pylint: disable=broad-except
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if fd is not None:
            os.close(fd)
    return rec


def _need_send(midi_send, midi_run):  # pylint: disable=redefined-outer-name
    _need_midi_ports(midi_run)
    if midi_send is None:
        pytest.skip("usb_midi.ports has fewer than 2 ports; no send")
    return midi_send


# ---------- host-side enumeration ----------

def test_board_present(board, host):
    """The board is on the bus at its hub port."""
    assert host["present"], board.usb


def test_board_vid_pid_readable(host):
    """The host can read the board's USB VID:PID."""
    vp = host["vid_pid"]
    assert vp is not None, str(vp)


def test_board_has_interfaces(host):
    """The board exposes at least one USB interface."""
    interfaces = host["interfaces"]
    assert len(interfaces) > 0, "%d interfaces" % len(interfaces)


# ---------- device-side usb_hid ----------

def test_usb_hid_device_check(hid_run):
    """DEVICE_HID_CHECK ran over the REPL without a host-side failure or a device exception."""
    _assert_ran(hid_run)


@pytest.mark.parametrize("name", HID_CHECKS)
def test_usb_hid(hid_run, name):
    """usb_hid imports, devices is a tuple (a skip when it is empty or not built)."""
    _expect_if_reported(hid_run, name)


@pytest.mark.parametrize("attr", HID_DEVICE_ATTRS)
def test_usb_hid_device_attr(hid_run, attr):
    """usb_hid.device_<i>_<attr> passed for every device in usb_hid.devices."""
    pat = re.compile(r"usb_hid\.device_\d+_%s$" % re.escape(attr))
    names = [n for n in hid_run.checks if pat.match(n)]
    if not names:
        if hid_run.crashed or hid_run.err:
            hid_run.expect("usb_hid.device_0_%s" % attr)  # skips as not reached
        pytest.skip("no usb_hid devices reported")
    recs = [r for n in names for r in hid_run.checks[n]]
    if all(r.get("ok") is None for r in recs):
        pytest.skip(recs[0].get("detail") or "skipped on the device")
    bad = [r for r in recs if r.get("ok") is False]
    assert not bad, "; ".join("%s: %s" % (r.get("name"), r.get("detail", "")) for r in bad)[:500]


# ---------- device-side usb_midi ----------

def test_usb_midi_device_check(midi_run):
    """DEVICE_MIDI_CHECK ran over the REPL without a host-side failure or a device exception."""
    _assert_ran(midi_run)


@pytest.mark.parametrize("name", MIDI_CHECKS)
def test_usb_midi(midi_run, name):
    """usb_midi.ports is a tuple of PortIn, PortOut (a skip when it is empty or not built)."""
    _expect_if_reported(midi_run, name)


# ---------- host-side HID, following what the device reported ----------

def test_usb_hid_interface_enumerated(host, hid_run):
    """With usb_hid devices enabled, the host sees a class 03 interface."""
    _need_hid_devices(hid_run)
    assert host["hid_found"], "classes %s" % host["classes"]


def test_usb_hid_interface_absent_when_disabled(host, hid_run):
    """With usb_hid.devices empty, the host sees no class 03 interface."""
    hid = _hid(hid_run)
    if not hid or hid[0] != 0:
        pytest.skip("usb_hid.devices is not empty, or usb_hid state not reported")
    assert not host["hid_found"], "classes %s" % host["classes"]


def test_hid_sysfs_device(board, hid_run, hid_host):
    """A /sys/bus/hid/devices entry sits under one of this board's interfaces."""
    _need_hid_devices(hid_run)
    nodes = hid_host["nodes"]
    assert len(nodes) > 0, ", ".join(nodes) or "no /sys/bus/hid device under " + board.usb


def test_hid_report_descriptor_readable(hid_run, hid_host):
    """The HID report descriptor can be read from sysfs."""
    _need_hid_devices(hid_run)
    if not hid_host["nodes"]:
        pytest.skip("no /sys/bus/hid device to read")
    desc = hid_host["desc"]
    assert len(desc) > 0, "%d bytes" % len(desc)


def test_hid_report_descriptor_contains_keyboard_usage(hid_run, hid_host):
    """When the device lists a keyboard, the descriptor has Generic Desktop (05 01) / Keyboard (09 06)."""
    hid = _need_hid_devices(hid_run)
    desc = hid_host["desc"]
    if not hid[1]:
        pytest.skip("no keyboard in usb_hid.devices")
    if not desc:
        pytest.skip("no report descriptor was read")
    # Generic Desktop (05 01) / Keyboard (09 06), as in Device.KEYBOARD
    assert b"\x05\x01" in desc and b"\x09\x06" in desc, desc[:24].hex()


# ---------- host-side MIDI ----------

def test_usb_midi_interface_enumerated(host, midi_run):
    """With usb_midi ports enabled, the host sees a class 01 interface."""
    _need_midi_ports(midi_run)
    assert host["midi_found"], "classes %s" % host["classes"]


def test_usb_midi_interface_absent_when_disabled(host, midi_run):
    """With usb_midi.ports empty, the host sees no class 01 interface."""
    midi = _midi(midi_run)
    if not midi or midi[0] != 0:
        pytest.skip("usb_midi.ports is not empty, or usb_midi state not reported")
    assert not host["midi_found"], "classes %s" % host["classes"]


# ---------- MIDI data flow ----------

def test_usb_midi_send(midi_send, midi_run):
    """DEVICE_MIDI_SEND ran over the REPL without a host-side failure or a device exception."""
    rec = _need_send(midi_send, midi_run)
    assert not rec["error"], "REPL: %s" % rec["error"]
    _assert_ran(rec["results"])


@pytest.mark.parametrize("name", MIDI_SEND_CHECKS)
def test_usb_midi_write_checks(midi_send, midi_run, name):
    """write() returns an int, and all 150 bytes are accepted while the host drains."""
    rec = _need_send(midi_send, midi_run)
    res = rec["results"]
    if res is None:
        pytest.skip("not reached: %s" % rec["error"])
    if name not in res.checks and "usb_midi.write" in res.checks:
        # write() raised; the original reports only usb_midi.write (see test_usb_midi_write)
        pytest.skip("not reached: write() raised (see usb_midi.write)")
    res.expect(name)


def test_usb_midi_write(midi_send, midi_run):
    """write() did not raise ("usb_midi.write" is only reported on an exception)."""
    rec = _need_send(midi_send, midi_run)
    res = rec["results"]
    if res is None:
        pytest.skip("not reached: %s" % rec["error"])
    if res.crashed or res.err or "usb_midi.write" in res.checks:
        res.expect("usb_midi.write")


def test_usb_midi_data_flow(midi_send, midi_run):
    """The host's rawmidi node receives exactly the 150 bytes, in order."""
    rec = _need_send(midi_send, midi_run)
    if rec["node"] is None or rec["why"]:
        pytest.skip(rec["why"])
    assert not rec["error"], rec["error"]
    got = rec["got"] or b""
    assert got == MIDI_EXPECT, "%d/%d bytes from %s; head %s" % (
        len(got), len(MIDI_EXPECT), rec["node"], got[:12].hex())
