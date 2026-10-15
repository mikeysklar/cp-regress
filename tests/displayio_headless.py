# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
"""Displayio API and logic invariants with no display attached.

Verifies Bitmap, Palette, Group, TileGrid, and vectorio shape construction and
basic operations without requiring a physical display.
"""

META = {
    "id": "displayio_headless",
    "title": "Displayio APIs without physical display",
    "category": "Display",
    "tier": "smoke",
    "needs": ["native_usb"],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": False,
    "multi_board": False,
    "timeout": 60,
    "targets": [
        8873,  # appending empty Group inside Group works
        4193,  # Group.hidden toggles
        3657,  # Group visibility
        1957,  # Bitmap value set/get
        3129,  # Polygon construction
        6710,  # Rectangle construction
        7824,  # TileGrid attributes
        10363, # TilePaletteMapper signature
        10305, # Bitmap rejects out-of-range value
        8499,  # Palette transparency
        8113,  # Bitmap boundary pixels
        10226, # appending same object twice raises ValueError
        1744,  # Group indexing
        7540,  # Bitmap rejects negative sizes
        8426,  # TileGrid indexing
        6507,  # Palette transparency
        9707,  # bitmaptools.fill_region
        8822,  # bitmaptools.blit with boundaries
        10883, # rotozoom on small bitmap
    ],
}

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


def run(board, ctx):
    """Run displayio API tests on device without a physical display."""
    ctx.check("present", board.present(), board.usb)

    # Run all device-side checks
    ctx.device(DEVICE, timeout=30)
