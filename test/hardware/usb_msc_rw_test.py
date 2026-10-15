# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""USB MSC read/write: large file integrity, speed, free space, burst writes.

pytest version of tests/usb_msc_rw.py. The host writes a 256 KB file (64 KB on "small" boards)
and a burst of 20 small files to CIRCUITPY over MSC, then the board reads them back and computes
the same Adler-32. The whole sequence (including cleanup of the files it wrote) runs once per
board in the module-scoped `flow` fixture, using the original's run() unchanged with a small
recorder that has the Ctx check/skip/metric/device methods. Each check is then one test, in the
original's order. The original's fixture timeout is 120 s; the device run keeps timeout=120.

Targets: #9956, #10390, #4670, #9971, #10901, #11097.
"""

import logging
import os
import time
import traceback

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(9956, 10390, 4670, 9971, 10901, 11097),
]

log = logging.getLogger(__name__)

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



def _run_flow(board, ctx):
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


class _Record:
    """Stands in for run.py's Ctx: keeps every check and metric the flow records.

    Checks keep their order; a name recorded twice keeps both (a test fails if any failed).
    """

    def __init__(self, board):
        self.board = board
        self.checks = {}   # name -> [(ok, detail)]
        self.metrics = {}  # name -> (value, unit, better)

    def check(self, name, ok, detail="", value=None):  # pylint: disable=unused-argument
        self.checks.setdefault(name, []).append((bool(ok), str(detail)[:500]))
        log.info("%s %s: %s %s", self.board.name, name, "ok" if ok else "FAIL", str(detail)[:200])
        return bool(ok)

    def skip(self, name, why=""):
        self.checks.setdefault(name, []).append((None, str(why)[:500]))

    def metric(self, name, value, unit="", better="lower", tolerance=0.25):  # pylint: disable=unused-argument
        self.metrics[name] = (value, unit, better)

    def device(self, body, timeout=60):
        """Same as Ctx.device: run devtest code, fold its records in, return (res, out, err)."""
        with self.board.repl() as repl:
            res, out, err = repl.run_device(body, timeout=timeout)
        for r in res:
            if r.get("kind") == "metric":
                self.metric(r.get("name"), r.get("value"), r.get("unit", ""), r.get("better", "lower"))
            elif r.get("kind") != "golden":
                ok = r.get("ok")
                self.checks.setdefault(r.get("name", "?"), []).append(
                    (None if ok is None else bool(ok), str(r.get("detail", ""))[:500]))
        if err.strip():
            self.check("device_exception", False, err.strip()[-400:])
        return res, out, err


@pytest.fixture(scope="module")
def flow(board):
    """Run the original sequence once per board. Its own finally removes the test file and
    the burst directory; an unexpected exception is kept as a "harness" failure, and the board
    is given 30 s to come back, as run.py does."""
    rec = _Record(board)
    try:
        _run_flow(board, rec)
    except Exception as e:  # pylint: disable=broad-except
        rec.check("harness", False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-600:]))
        try:
            board.wait_ready(timeout=30)
        except Exception:  # pylint: disable=broad-except
            pass
    return rec


def _not_reached(flow, name):
    if "rest" in flow.checks:  # ctx.skip("rest", ...): not mounted, or too little free space
        pytest.skip(flow.checks["rest"][0][1] or "skipped")
    if "harness" in flow.checks:
        pytest.skip("not reached: %s" % flow.checks["harness"][0][1][:200])
    pytest.skip("not reached: %s was not recorded on this run" % name)


def _expect(flow, name):
    rs = flow.checks.get(name)
    if not rs:
        _not_reached(flow, name)
    if all(ok is None for ok, _ in rs):
        pytest.skip(rs[0][1] or "skipped")
    bad = [d for ok, d in rs if ok is False]
    assert not bad, "%s: %s" % (name, "; ".join(bad)[:500])


def _expect_if_recorded(flow, name):
    """For checks the original records only on failure (msc_io, device_exception)."""
    if name not in flow.checks:
        if "rest" in flow.checks or "harness" in flow.checks:
            _not_reached(flow, name)
        return
    _expect(flow, name)


def test_mounted(flow):
    """CIRCUITPY mounts on the host (the "rest" skip follows if it does not, or if it is full)."""
    _expect(flow, "mounted")


def test_host_write(flow):
    """The host writes the deterministic file and it has the full size."""
    _expect(flow, "host_write")


def test_host_write_kBps(flow, metric):  # pylint: disable=invalid-name
    """Host MSC write throughput (KB/s, higher is better)."""
    if "host_write_kBps" not in flow.metrics:
        _not_reached(flow, "host_write_kBps")
    value, unit, better = flow.metrics["host_write_kBps"]
    metric("host_write_kBps", value, unit, better=better)


def test_host_burst_created(flow):
    """20 small files written to /cpr_burst."""
    _expect(flow, "host_burst_created")


def test_host_readback(flow):
    """Host read-back Adler-32 matches (page cache; weak on its own)."""
    _expect(flow, "host_readback")


DEVICE_CHECKS = ["device_read", "device_adler32", "statvfs_read", "device_burst_count"]


@pytest.mark.parametrize("name", DEVICE_CHECKS)
def test_device(flow, name):
    """The board reads the file, reports free space, and sees all 20 burst files."""
    _expect(flow, name)


def test_device_exception(flow):
    """The device code raised nothing (recorded only if it did)."""
    _expect_if_recorded(flow, "device_exception")


def test_device_matches_host(flow):
    """The board's Adler-32 equals the host's: bytes reached flash intact."""
    _expect(flow, "device_matches_host")


def test_free_space_agree(flow):
    """Host and device statvfs agree within 2% (compared only when both gave a number)."""
    if "free_space_agree" not in flow.checks and "device_matches_host" in flow.checks:
        pytest.skip("not compared: no device free space or host free space is 0")
    _expect(flow, "free_space_agree")


def test_msc_io(flow):
    """No OSError/ValueError during the host I/O (recorded only if one happened)."""
    _expect_if_recorded(flow, "msc_io")


def test_cleanup(flow):
    """The test file and burst directory are gone afterwards."""
    _expect(flow, "cleanup")


def test_mount_after(flow):
    """CIRCUITPY is still mounted at the end."""
    _expect(flow, "mount_after")


def test_devnum_unchanged(flow):
    """Same USB device number: the board did not re-enumerate."""
    _expect(flow, "devnum_unchanged")


def test_harness(flow):
    """The sequence ran to the end without an unexpected host exception (charged once here)."""
    _expect_if_recorded(flow, "harness")
