# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""USB HID and MIDI enumeration and basic functionality tests.

Targets regressions in USB HID/MIDI interface enumeration, report descriptor
handling, and MIDI data flow. Checks host-side device detection and device-side
API availability.
"""

import glob
import os
import time

import farmlib  # same module object run.py uses, so farmlib.FarmError matches

META = {
    "id": "usb_hid_midi",
    "title": "USB HID and MIDI enumeration and data flow",
    "category": "USB",
    "tier": "nightly",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [1769, 2048, 5213, 9306, 4724, 4895, 7944, 9130, 9847, 6764, 10373, 7930, 5101],
}

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


def _device(ctx, name, body, timeout=30):
    """ctx.device that turns a host-side REPL failure into a failed check. Returns stdout."""
    try:
        _, out, _ = ctx.device(body, timeout=timeout)
        return out
    except (farmlib.FarmError, OSError) as e:
        ctx.check(name, False, "REPL: %s" % e)
        return ""


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


def run(board, ctx):
    # Host-side checks: USB enumeration
    ctx.check("board_present", board.present(), board.usb)
    vp = board.vid_pid()
    ctx.check("board_vid_pid_readable", vp is not None, str(vp))
    interfaces = board.interfaces()
    ctx.check("board_has_interfaces", len(interfaces) > 0, "%d interfaces" % len(interfaces))
    classes = sorted(interfaces.values())
    hid_found = "03" in classes
    midi_found = "01" in classes  # AudioControl + MIDIStreaming are both class 01

    # Device-side first: what the board says is enabled decides what the host must see.
    hid = _tag(_device(ctx, "usb_hid_device_check", DEVICE_HID_CHECK), "CPR_HID")
    midi = _tag(_device(ctx, "usb_midi_device_check", DEVICE_MIDI_CHECK), "CPR_MIDI")

    if not hid:
        ctx.skip("usb_hid_interface_enumerated", "device did not report usb_hid state")
    elif hid[0] < 0:
        ctx.skip("usb_hid_interface_enumerated", "usb_hid not in this build")
    elif hid[0] == 0:
        ctx.check("usb_hid_interface_absent_when_disabled", not hid_found, "classes %s" % classes)
    else:
        ctx.check("usb_hid_interface_enumerated", hid_found, "classes %s" % classes)
        nodes = _hid_sysfs(board)
        ctx.check("hid_sysfs_device", len(nodes) > 0, ", ".join(nodes) or "no /sys/bus/hid device under " + board.usb)
        desc = b""
        for node in nodes:
            try:
                with open(os.path.join(node, "report_descriptor"), "rb") as f:
                    desc = f.read()
                break
            except OSError as e:
                ctx.log("report_descriptor %s: %s" % (node, e))
        if nodes:
            ctx.check("hid_report_descriptor_readable", len(desc) > 0, "%d bytes" % len(desc))
        if hid[1] and desc:
            # Generic Desktop (05 01) / Keyboard (09 06), as in Device.KEYBOARD
            ctx.check("hid_report_descriptor_contains_keyboard_usage",
                      b"\x05\x01" in desc and b"\x09\x06" in desc, desc[:24].hex())
        elif not hid[1]:
            ctx.skip("hid_report_descriptor_contains_keyboard_usage", "no keyboard in usb_hid.devices")

    if not midi:
        ctx.skip("usb_midi_interface_enumerated", "device did not report usb_midi state")
        return
    if midi[0] < 0:
        ctx.skip("usb_midi_interface_enumerated", "usb_midi not in this build")
        return
    if midi[0] == 0:
        ctx.check("usb_midi_interface_absent_when_disabled", not midi_found, "classes %s" % classes)
        return
    ctx.check("usb_midi_interface_enumerated", midi_found, "classes %s" % classes)
    if midi[0] < 2:
        return

    # MIDI data flow: hold the ALSA rawmidi input open so snd-usb-audio drains the device,
    # send 50 messages from the board, and compare the bytes that arrive.
    node = _midi_node(board)
    fd = None
    why = None
    if not node:
        why = "no ALSA midiC*D0 under %s (snd-usb-audio not bound?)" % board.usb
    else:
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            why = "cannot open %s: %s" % (node, e)
    try:
        if fd is not None:
            time.sleep(0.5)
            stale = _read_quiet(fd, quiet=0.3, limit=2)  # bytes left in the device FIFO by earlier runs
            if stale:
                ctx.log("discarded %d stale MIDI bytes" % len(stale))
        _device(ctx, "usb_midi_send", DEVICE_MIDI_SEND.replace("@@DRAIN@@", "True" if fd is not None else "False"))
        if fd is None:
            ctx.skip("usb_midi_data_flow", why)
        else:
            got = _read_quiet(fd)
            ctx.check("usb_midi_data_flow", got == MIDI_EXPECT,
                      "%d/%d bytes from %s; head %s" % (len(got), len(MIDI_EXPECT), node, got[:12].hex()),
                      len(got))
    finally:
        if fd is not None:
            os.close(fd)
