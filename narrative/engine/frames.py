"""Render an observation frame PNG purely from one committed tile snapshot.

The artist only understands the Facet tile channel format (f64 depth, f32
linear color, u32 object id) plus a pending hatch. It never invents content
for a pending tile: those cells stay a neutral dark hatch so the viewer
cannot confuse un-rendered canvas with reviewed imagery.
"""
from facetproof.png import encode_rgb
from facetproof.pigments import linear_to_srgb

# Minimal 5x7 bitmap font, sufficient for tile ordinals and worker labels.
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "?": ["01110", "10001", "00001", "00010", "00100", "00000", "00100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}

PRODUCER_TINT = {  # border color per worker label
    "A": (214, 102, 47),    # interrupted worker: burnt orange
    "B": (47, 133, 214),    # takeover worker: blue
    "C": (200, 110, 110),   # cancelled-run worker: muted red
    "D": (214, 178, 78),    # failed first attempt: ochre
    "E": (138, 196, 132),   # retry worker: green
    "OTHER": (150, 156, 166),
}
PENDING_BASE = (26, 28, 34)
PENDING_HATCH = (58, 62, 73)
GRID = (14, 15, 18)
LABEL_SHADOW = (0, 0, 0)
LABEL_TEXT = (255, 255, 255)
NO_FRAME = (120, 126, 138)


def _set(buf, width, x, y, color):
    if 0 <= x < width and 0 <= y < len(buf) // width // 3:
        i = (y * width + x) * 3
        buf[i:i + 3] = bytes(color)


def _rect(buf, width, height, x0, y0, x1, y1, color):
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            _set(buf, width, x, y, color)


def _glyph(buf, width, height, gx, gy, char, color, scale=1):
    pattern = FONT.get(char, FONT["?"])
    for row, line in enumerate(pattern):
        for col, bit in enumerate(line):
            if bit == "1":
                for sy in range(scale):
                    for sx in range(scale):
                        _set(buf, width, gx + col * scale + sx, gy + row * scale + sy, color)


def _text(buf, width, height, gx, gy, text, color, scale=1):
    cursor = gx
    for char in text:
        if char == " ":
            cursor += 4 * scale
            continue
        _glyph(buf, width, height, cursor, gy, char, LABEL_SHADOW, scale)
        _glyph(buf, width, height, cursor - 1, gy - 1, char, color, scale)
        cursor += 6 * scale


def _hatch_tile(buf, width, height, x0, y0, x1, y1):
    _rect(buf, width, height, x0, y0, x1, y1, PENDING_BASE)
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            if (xx - x0 + (yy - y0)) % 10 < 2:
                _set(buf, width, xx, yy, PENDING_HATCH)


def render_observation_frame(width, height, tiles, blobs, token_labels, *, planned=True):
    """tiles: ordered records from the single snapshot (complete carry payload)."""
    buf = bytearray(width * height * 3)
    # Background/pending base for the whole canvas first.
    _rect(buf, width, height, 0, 0, width, height, PENDING_BASE)
    if not planned:
        # No tile manifest exists yet: show an explicit empty-canvas banner so
        # the frame is never read as rendered imagery.
        _text(buf, width, height, max(8, width // 2 - 90), height // 2 - 8,
              "NO TILE FRAME", NO_FRAME, scale=2)
        _text(buf, width, height, max(8, width // 2 - 70), height // 2 + 16,
              "QUEUED - NO TILES", PENDING_HATCH, scale=1)
        return encode_rgb(width, height, bytes(buf))
    complete = [t for t in tiles if t["state"] == "complete"]
    for tile in tiles:
        x0, y0, x1, y1 = tile["bounds"]
        if tile["state"] != "complete":
            _hatch_tile(buf, width, height, x0, y0, x1, y1)
            continue
        decoded = _decode_tile(tile, blobs)
        local_w = x1 - x0
        colors = decoded["colors"]
        for y in range(y0, y1):
            for x in range(x0, x1):
                local = (y - y0) * local_w + (x - x0)
                r = round(linear_to_srgb(colors[local * 3]) * 255)
                g = round(linear_to_srgb(colors[local * 3 + 1]) * 255)
                b = round(linear_to_srgb(colors[local * 3 + 2]) * 255)
                _set(buf, width, x, y, (r, g, b))
        label = token_labels.get(tile["producer_token"], "OTHER")
        border = PRODUCER_TINT.get(label, PRODUCER_TINT["OTHER"])
        _rect(buf, width, height, x0, y0, x1, y0 + 2, border)
        _rect(buf, width, height, x0, y1 - 2, x1, y1, border)
        _rect(buf, width, height, x0, y0, x0 + 2, y1, border)
        _rect(buf, width, height, x1 - 2, y0, x1, y1, border)
    # Grid lines on every tile boundary (drawn last, over content edges).
    for tile in tiles:
        x0, y0, x1, y1 = tile["bounds"]
        _rect(buf, width, height, x0, y0, x1, y0 + 1, GRID)
        _rect(buf, width, height, x0, y1 - 1, x1, y1, GRID)
        _rect(buf, width, height, x0, y0, x0 + 1, y1, GRID)
        _rect(buf, width, height, x1 - 1, y0, x1, y1, GRID)
    # Labels: ordinal for every tile; producer letter for complete tiles.
    for tile in tiles:
        x0, y0, _, _ = tile["bounds"]
        _text(buf, width, height, x0 + 4, y0 + 4, str(tile["ordinal"]), LABEL_TEXT, scale=1)
        if tile["state"] == "complete":
            label = token_labels.get(tile["producer_token"], "OTHER")
            _text(buf, width, height, x0 + 4, y0 + 14, label,
                  PRODUCER_TINT.get(label, PRODUCER_TINT["OTHER"]), scale=2)
    return encode_rgb(width, height, bytes(buf))


def _decode_tile(tile, blobs):
    from facetproof.tile_protocol import decode
    return decode(tile["payload"], tile["bounds"], tile["fingerprint"], blobs)


def render_plate_reference(width, height, color_rgb):
    """The delivered color channel, framed identically for the final chapter."""
    return encode_rgb(width, height, color_rgb)
