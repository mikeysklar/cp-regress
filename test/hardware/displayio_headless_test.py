# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Displayio API and logic invariants with no display attached.

pytest version of tests/displayio_headless.py. The device code below is identical to the
original (tools/pytest_parity.py checks that). It runs once per board; each check it reports
is one test here, in the same order. When a whole module is not in the build (displayio,
TileGrid/Group, vectorio, bitmaptools, tilepalettemapper) the device skips that section and
its tests skip with the device's reason.
Targets: #8873, #4193, #3657, #1957, #3129, #6710, #7824, #10363, #10305, #8499, #8113, #10226,
#1744, #7540, #8426, #6507, #9707, #8822, #10883.
"""
import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.needs("native_usb"),
    pytest.mark.targets(8873, 4193, 3657, 1957, 3129, 6710, 7824, 10363, 10305, 8499, 8113,
                        10226, 1744, 7540, 8426, 6507, 9707, 8822, 10883),
]

DEVICE = r'''
def expect_raise(name, excs, fn):
    try:
        fn()
        check(name, False, "no exception")
    except excs:
        check(name, True, "")
    except Exception as e:
        check(name, False, "wrong exception %s: %s" % (type(e).__name__, e))


def setpx(bm, x, y, v):
    bm[x, y] = v


def attempt(name, fn):
    try:
        fn()
        return True
    except Exception as e:
        check(name, False, "%s: %s" % (type(e).__name__, e))
        return False


# One function per section: small boards receive each top-level statement separately.
def cpr_basic():
    try:
        import displayio
    except ImportError:
        skip("displayio", "displayio not in this build")
        return None

    # Bitmap
    b = displayio.Bitmap(8, 8, 16)
    check("bitmap_size", b.width == 8 and b.height == 8, "%dx%d" % (b.width, b.height))
    b[0, 0] = 5
    b[7, 7] = 15
    check("bitmap_corner_pixels", b[0, 0] == 5 and b[7, 7] == 15, "%d %d" % (b[0, 0], b[7, 7]))
    expect_raise("bitmap_rejects_negative_size", (ValueError,), lambda: displayio.Bitmap(-1, 8, 16))
    expect_raise("bitmap_rejects_value_over_count", (ValueError, OverflowError), lambda: setpx(b, 0, 0, 16))
    expect_raise("bitmap_rejects_out_of_bounds", (IndexError, ValueError), lambda: setpx(b, 8, 0, 1))

    # Palette
    p = displayio.Palette(4)
    p[0] = 0xFF0000
    p[1] = 0x00FF00
    p.make_transparent(0)
    check("palette_len", len(p) == 4, len(p))
    check("palette_transparency", p.is_transparent(0) and not p.is_transparent(1), "")
    return displayio, p


def cpr_groups(displayio, p):
    # TileGrid and Group (the unix port builds Bitmap/Palette/vectorio but not these)
    if not (hasattr(displayio, "TileGrid") and hasattr(displayio, "Group")):
        skip("tilegrid_group", "displayio.TileGrid/Group not in this build")
    else:
        # TileGrid
        tiles = displayio.Bitmap(16, 16, 4)
        tg = displayio.TileGrid(tiles, pixel_shader=p, width=2, height=2, tile_width=8, tile_height=8)
        tg.x = 10
        tg.y = 20
        check("tilegrid_xy", tg.x == 10 and tg.y == 20, "%d,%d" % (tg.x, tg.y))
        tg.flip_x = True
        tg.transpose_xy = True
        check("tilegrid_flip_transpose", tg.flip_x and tg.transpose_xy, "")
        tg[1, 1] = 3
        check("tilegrid_tile_index", tg[1, 1] == 3, tg[1, 1])

        # Group
        g = displayio.Group()
        g.append(tg)
        check("group_append_tilegrid", len(g) == 1 and g[0] is tg, len(g))
        expect_raise("group_rejects_duplicate_tilegrid", (ValueError,), lambda: g.append(tg))
        inner = displayio.Group()
        if attempt("group_append_empty_group", lambda: g.append(inner)):
            check("group_append_empty_group", len(g) == 2, len(g))
        g2 = displayio.Group()
        if attempt("group_insert", lambda: g2.insert(0, displayio.Group())):
            check("group_insert", len(g2) == 1, len(g2))
        check("group_index", g.index(inner) == 1, "")
        g.remove(inner)
        check("group_remove", len(g) == 1, len(g))
        g.hidden = True
        check("group_hidden_set", g.hidden, "")
        g.hidden = False
        check("group_hidden_clear", not g.hidden, "")
        hidden_first = displayio.Group()
        hidden_first.hidden = True
        hidden_first.append(displayio.Group())
        check("group_hidden_before_append", hidden_first.hidden, "#4193")


def cpr_vectorio(displayio, p):
    # vectorio
    try:
        import vectorio
    except ImportError:
        skip("vectorio", "vectorio not in this build")
    else:
        pts = [(0, 0), (10, 0), (10, 10), (0, 10)]
        r = vectorio.Rectangle(pixel_shader=p, width=10, height=20, x=5, y=5)
        check("rectangle_size", r.width == 10 and r.height == 20, "")
        c = vectorio.Circle(pixel_shader=p, radius=5, x=10, y=10)
        check("circle_radius", c.radius == 5, c.radius)
        poly = vectorio.Polygon(pixel_shader=p, points=pts, x=0, y=0)
        check("polygon_points_roundtrip", list(poly.points) == pts, repr(poly.points))
        if hasattr(displayio, "Group"):
            vg = displayio.Group()
            vg.append(r)
            try:
                vg.append(r)
                skip("vectorio_duplicate_append", "accepted twice (#10226 open behaviour)")
            except ValueError:
                check("vectorio_duplicate_append", True, "rejected")


def cpr_bitmaptools(displayio):
    # bitmaptools and a render checksum compared against this board's golden value
    try:
        import bitmaptools
    except ImportError:
        skip("bitmaptools", "bitmaptools not in this build")
        return
    dst = displayio.Bitmap(32, 32, 16)
    src = displayio.Bitmap(8, 8, 16)
    for y in range(8):
        for x in range(8):
            src[x, y] = (x + 2 * y) % 16
    ok = attempt("bitmaptools_fill_region", lambda: bitmaptools.fill_region(dst, 0, 0, 16, 16, 3))
    ok = attempt("bitmaptools_draw_line", lambda: bitmaptools.draw_line(dst, 0, 31, 31, 0, 5)) and ok
    ok = attempt("bitmaptools_blit_clip", lambda: bitmaptools.blit(dst, src, 20, 20, x1=2, y1=2, x2=8, y2=8)) and ok
    ok = attempt("bitmaptools_blit_edge", lambda: bitmaptools.blit(dst, src, 28, 28)) and ok
    rz = displayio.Bitmap(16, 16, 16)
    ok = attempt("bitmaptools_rotozoom", lambda: bitmaptools.rotozoom(rz, src, angle=0.5, scale=1.5)) and ok
    if ok:
        acc = 0
        for bm in (dst, rz):
            for y in range(bm.height):
                for x in range(bm.width):
                    acc = (acc * 31 + bm[x, y] + 1) % 2147483647
        golden("render_checksum", acc)


def cpr_tpm(displayio):
    # tilepalettemapper (#10363 changed its constructor)
    try:
        import tilepalettemapper
    except ImportError:
        skip("tilepalettemapper", "not in this build")
        return
    tp = displayio.Palette(8)
    if attempt("tilepalettemapper_construct", lambda: tilepalettemapper.TilePaletteMapper(tp, 4)):
        check("tilepalettemapper_construct", True, "")


cpr_d = cpr_basic()
if cpr_d:
    cpr_groups(*cpr_d)
if cpr_d:
    cpr_vectorio(*cpr_d)
if cpr_d:
    cpr_bitmaptools(cpr_d[0])
if cpr_d:
    cpr_tpm(cpr_d[0])
'''

# Device-side section skips, and the checks that are never reported when each one fires.
# "displayio" gates everything; "tilegrid_group" also gates vectorio_duplicate_append, which the
# device only runs when displayio.Group exists.
SECTIONS = {
    "bitmap_size": ("displayio",),
    "bitmap_corner_pixels": ("displayio",),
    "bitmap_rejects_negative_size": ("displayio",),
    "bitmap_rejects_value_over_count": ("displayio",),
    "bitmap_rejects_out_of_bounds": ("displayio",),
    "palette_len": ("displayio",),
    "palette_transparency": ("displayio",),
    "tilegrid_xy": ("displayio", "tilegrid_group"),
    "tilegrid_flip_transpose": ("displayio", "tilegrid_group"),
    "tilegrid_tile_index": ("displayio", "tilegrid_group"),
    "group_append_tilegrid": ("displayio", "tilegrid_group"),
    "group_rejects_duplicate_tilegrid": ("displayio", "tilegrid_group"),
    "group_append_empty_group": ("displayio", "tilegrid_group"),
    "group_insert": ("displayio", "tilegrid_group"),
    "group_index": ("displayio", "tilegrid_group"),
    "group_remove": ("displayio", "tilegrid_group"),
    "group_hidden_set": ("displayio", "tilegrid_group"),
    "group_hidden_clear": ("displayio", "tilegrid_group"),
    "group_hidden_before_append": ("displayio", "tilegrid_group"),
    "rectangle_size": ("displayio", "vectorio"),
    "circle_radius": ("displayio", "vectorio"),
    "polygon_points_roundtrip": ("displayio", "vectorio"),
    "vectorio_duplicate_append": ("displayio", "vectorio", "tilegrid_group"),
    "bitmaptools_fill_region": ("displayio", "bitmaptools"),
    "bitmaptools_draw_line": ("displayio", "bitmaptools"),
    "bitmaptools_blit_clip": ("displayio", "bitmaptools"),
    "bitmaptools_blit_edge": ("displayio", "bitmaptools"),
    "bitmaptools_rotozoom": ("displayio", "bitmaptools"),
    "render_checksum": ("displayio", "bitmaptools"),
    "tilepalettemapper_construct": ("displayio", "tilepalettemapper"),
}

DEVICE_CHECKS = [
    "bitmap_size", "bitmap_corner_pixels", "bitmap_rejects_negative_size",
    "bitmap_rejects_value_over_count", "bitmap_rejects_out_of_bounds",
    "palette_len", "palette_transparency",
    "tilegrid_xy", "tilegrid_flip_transpose", "tilegrid_tile_index",
    "group_append_tilegrid", "group_rejects_duplicate_tilegrid", "group_append_empty_group",
    "group_insert", "group_index", "group_remove", "group_hidden_set", "group_hidden_clear",
    "group_hidden_before_append",
    "rectangle_size", "circle_radius", "polygon_points_roundtrip", "vectorio_duplicate_append",
    "bitmaptools_fill_region", "bitmaptools_draw_line", "bitmaptools_blit_clip",
    "bitmaptools_blit_edge", "bitmaptools_rotozoom",
]

BITMAPTOOLS_CALLS = ["bitmaptools_fill_region", "bitmaptools_draw_line", "bitmaptools_blit_clip",
                     "bitmaptools_blit_edge", "bitmaptools_rotozoom"]


def _skip_if_section_skipped(results, name):
    """Skip when the device skipped the section that holds this check and never reported it.

    A check the device did report is always judged on its own record, so a section skip can
    never hide it (vectorio_duplicate_append runs when Group exists even if TileGrid does not).
    """
    if name in results.checks or name in results.goldens:
        return
    for section in SECTIONS.get(name, ()):
        rs = results.checks.get(section)
        if rs and all(r.get("ok") is None for r in rs):
            pytest.skip(rs[0].get("detail") or "%s not in this build" % section)


@pytest.fixture(scope="module")
def results(device):
    """Run every displayio check once per board."""
    return device.run(DEVICE, timeout=30)


def test_present(board):
    """The board is on the bus at its hub port."""
    assert board.present(), board.usb


@pytest.mark.parametrize("name", DEVICE_CHECKS)
def test_device_check(results, name):
    """One device-side check (a skip means the module or class is not in this build)."""
    _skip_if_section_skipped(results, name)
    results.expect(name)


def test_render_checksum(results, golden):
    """Checksum of the drawn bitmaps matches the first value recorded for this board."""
    _skip_if_section_skipped(results, "render_checksum")
    if "render_checksum" not in results.goldens:
        failed = [n for n in BITMAPTOOLS_CALLS
                  if any(r.get("ok") is False for r in results.checks.get(n, []))]
        if failed:
            pytest.skip("not drawn: %s failed" % ", ".join(failed))
    golden("render_checksum", results.golden_value("render_checksum"))


def test_tilepalettemapper_construct(results):
    """TilePaletteMapper takes the current (palette, input_color_count) signature (#10363)."""
    _skip_if_section_skipped(results, "tilepalettemapper_construct")
    results.expect("tilepalettemapper_construct")


def test_device_ran(results):
    """The device code ran to the end without raising (device_exception in run.py)."""
    results.expect_no_exception()
