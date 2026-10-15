# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""USB MSC read/write: large file integrity, speed, free space, burst writes."""

import os
import time

META = {
    "id": "usb_msc_rw",
    "title": "USB MSC read/write: 256 KB file, speed, free space, burst small files",
    "category": "USB",
    "tier": "smoke",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 120,
    "targets": [9956, 10390, 4670, 9971, 10901, 11097],
}

# Device-side code: Adler-32 of the host-written file (no hashlib needed: CircuitPython's
# hashlib.Hash has digest() only and is missing on some boards), free space, burst listing.
DEVICE = r'''
import os
file_path = "/cpr_test_file.bin"
a = 1
b = 0
n = 0
try:
    buf = bytearray(2048)
    with open(file_path, "rb") as f:
        while True:
            k = f.readinto(buf)
            if not k:
                break
            for i in range(k):
                a = (a + buf[i]) % 65521
                b = (b + a) % 65521
            n += k
    check("device_read", True, "%d bytes" % n, n)
    check("device_adler32", True, "", (b << 16) | a)
except OSError as e:
    check("device_read", False, str(e))

try:
    st = os.statvfs("/")
    check("statvfs_read", True, "free %d bytes" % (st[0] * st[3]), st[0] * st[3])
except OSError as e:
    check("statvfs_read", False, str(e))

try:
    files = os.listdir("/cpr_burst")
    check("device_burst_count", len(files) == 20, "expected 20, got %d" % len(files), len(files))
except OSError as e:
    check("device_burst_count", False, str(e))
'''


def adler32(data):
    import zlib
    return zlib.adler32(data) & 0xFFFFFFFF


def run(board, ctx):
    """Host writes a file and a burst of small files over MSC; the device must read them back."""
    mp = board.mount(timeout=30, check_id=True)
    ctx.check("mounted", mp is not None, mp)
    if not mp:
        ctx.skip("rest", "CIRCUITPY not mounted")
        return

    initial_devnum = board.devnum()
    file_size = 64 * 1024 if "small" in board.caps else 256 * 1024
    # Fit the user's drive: leave room for the 20 burst files (one cluster each) and slack.
    st = os.statvfs(mp)
    free = st.f_frsize * st.f_bavail
    room = (free - 24 * st.f_frsize - 4096) // 4096 * 4096
    if room < 8192:
        ctx.skip("rest", "CIRCUITPY has %d bytes free, too little for the MSC test; "
                 "clear files on the drive to run it" % free)
        return
    file_size = min(file_size, room)
    test_file_path = os.path.join(mp, "cpr_test_file.bin")
    burst_dir = os.path.join(mp, "cpr_burst")

    try:
        # Phase 1: host writes a deterministic pseudo-random file
        import random
        rnd = random.Random(42)
        test_data = bytes(rnd.getrandbits(8) for _ in range(file_size))
        host_adler = adler32(test_data)
        t0 = time.time()
        with open(test_file_path, "wb") as f:
            f.write(test_data)
            f.flush()
            os.fsync(f.fileno())
        os.sync()
        write_time = max(time.time() - t0, 1e-6)
        ctx.check("host_write", os.path.getsize(test_file_path) == file_size,
                  "%d bytes" % os.path.getsize(test_file_path))
        ctx.metric("host_write_kBps", file_size / 1024 / write_time, "KB/s", better="higher")

        # Phase 2: burst of 20 small files
        os.makedirs(burst_dir, exist_ok=True)
        for i in range(20):
            with open(os.path.join(burst_dir, "file_%02d.txt" % i), "w") as f:
                f.write("data %d\n" % i)
        os.sync()
        ctx.check("host_burst_created", len(os.listdir(burst_dir)) == 20,
                  "%d files" % len(os.listdir(burst_dir)))

        # Host read-back. This mostly proves the page cache; the device read below is
        # the real end-to-end check that bytes reached flash intact.
        with open(test_file_path, "rb") as f:
            ctx.check("host_readback", adler32(f.read()) == host_adler, "adler32 %08x" % host_adler)

        host_stat = os.statvfs(mp)
        host_free = host_stat.f_frsize * host_stat.f_bfree

        # Writes can trigger an autoreload; give it time to settle before the REPL
        time.sleep(3)

        # Phase 3: device reads the same bytes
        res, out, err = ctx.device(DEVICE, timeout=120)
        vals = {r.get("name"): r.get("value") for r in res}
        dev_adler = vals.get("device_adler32")
        ctx.check("device_matches_host", dev_adler == host_adler,
                  "device %s host %08x" % (("%08x" % dev_adler) if isinstance(dev_adler, int) else dev_adler, host_adler))
        dev_free = vals.get("statvfs_read")
        if isinstance(dev_free, int) and host_free > 0:
            diff = abs(dev_free / host_free - 1)
            ctx.check("free_space_agree", diff < 0.02,
                      "device %d vs host %d bytes, diff %.1f%%" % (dev_free, host_free, diff * 100))
    except (OSError, ValueError) as e:
        ctx.check("msc_io", False, "%s: %s" % (type(e).__name__, e))
    finally:
        # Phase 4: clean up
        try:
            if os.path.isdir(burst_dir):
                for name in os.listdir(burst_dir):
                    os.remove(os.path.join(burst_dir, name))
                os.rmdir(burst_dir)
            if os.path.exists(test_file_path):
                os.remove(test_file_path)
            os.sync()
            ctx.check("cleanup", not os.path.exists(burst_dir) and not os.path.exists(test_file_path), "")
        except OSError as e:
            ctx.check("cleanup", False, str(e))

    # Phase 5: still the same USB session, still mounted
    time.sleep(3)
    mp_after = board.mount(timeout=15)
    ctx.check("mount_after", mp_after is not None, mp_after)
    ctx.check("devnum_unchanged", board.devnum() == initial_devnum,
              "before %s, after %s (a change means the board re-enumerated)" % (initial_devnum, board.devnum()))
