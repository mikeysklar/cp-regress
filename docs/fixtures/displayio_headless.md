# displayio_headless: displayio, vectorio, bitmaptools logic with no display

Tier smoke · automation auto · skips cleanly on builds without displayio (metro_m0)

## Regressions it targets
#8873, #4193, #3657, #10226, #7540, #10363, #10883, #8822, #3169 and the Group / TileGrid / Bitmap API family

## What it checks
- TileGrid and Group sections skip (`tilegrid_group`) on builds without them, such as the unix port
- Bitmap size, corner pixels, rejects negative size, value over value_count, out-of-bounds index
- Palette length and transparency
- TileGrid x/y, flip_x + transpose_xy, tile index set/get
- Group append, duplicate append rejected, empty Group append, insert, index, remove, hidden set/clear, hidden set before append (#4193)
- vectorio Rectangle / Circle / Polygon attributes, Polygon points round trip; duplicate shape append reported (info while #10226 is open)
- bitmaptools fill_region, draw_line, blit with clip, blit at the edge, rotozoom
- `render_checksum`: checksum of the drawn bitmaps compared with the first value recorded for this board (`history/golden.json`). A different build rendering different pixels fails.
- `tilepalettemapper_construct` with the current `(palette, input_color_count)` signature

## Setup
None. Seed the golden checksum on a known-good build first.

## What is hard to automate
Anything about what a real panel shows: init sequences, backlight, refresh glitches. See `display_fixture`.

## Run
`./run.py --only displayio_headless`
