# PS1 color and palette decoding.
#
# The PS1 stores colors as 16-bit ABGR1555 values:
#   bits 0-4:  red (5 bits)
#   bits 5-9:  green (5 bits)
#   bits 10-14: blue (5 bits)
#   bit 15:    semi-transparency flag (ignored here)
# Black (0x0000) is treated as fully transparent.
#
# Textures come in three pixel modes:
#   4bpp  - each byte holds two 4-bit palette indices
#   8bpp  - each byte is one palette index
#   15bpp - each 16-bit word is a direct ABGR1555 color

import struct
from PIL import Image


def abgr1555_to_rgba(color: int) -> tuple:
    """Convert a 16-bit ABGR1555 word to an (R, G, B, A) tuple.
    Black (0) becomes fully transparent, matching PS1 behavior."""
    r = (color & 0x1F) << 3
    g = ((color >> 5) & 0x1F) << 3
    b = ((color >> 10) & 0x1F) << 3
    a = 0 if color == 0 else 255
    return (r, g, b, a)


def expand_palette(clut_bytes: bytes, count: int) -> list:
    """Read 'count' ABGR1555 entries from raw CLUT bytes into a list of RGBA tuples."""
    return [abgr1555_to_rgba(struct.unpack_from('<H', clut_bytes, i * 2)[0])
            for i in range(count)]


def decode_4bpp(raw: bytes, width: int, height: int, palette: list) -> Image.Image:
    """Decode a 4bpp paletted image. Each byte contains two 4-bit indices (lo nibble first)."""
    img = Image.new('RGBA', (width, height))
    pix = img.load()
    bpr = width // 2  # bytes per row
    for y in range(height):
        for xb in range(bpr):
            b = raw[y * bpr + xb]
            if xb * 2 < width:
                pix[xb * 2, y] = palette[b & 0xF]
            if xb * 2 + 1 < width:
                pix[xb * 2 + 1, y] = palette[(b >> 4) & 0xF]
    return img


def decode_8bpp(raw: bytes, width: int, height: int, palette: list) -> Image.Image:
    """Decode an 8bpp paletted image. Each byte is a direct palette index."""
    img = Image.new('RGBA', (width, height))
    pix = img.load()
    for y in range(height):
        for x in range(width):
            idx = raw[y * width + x]
            pix[x, y] = palette[idx] if idx < len(palette) else (0, 0, 0, 0)
    return img


def decode_15bpp(raw: bytes, width: int, height: int) -> Image.Image:
    """Decode a 15bpp direct-color image. Each 16-bit word is one ABGR1555 pixel."""
    img = Image.new('RGBA', (width, height))
    pix = img.load()
    for y in range(height):
        for x in range(width):
            c = struct.unpack_from('<H', raw, (y * width + x) * 2)[0]
            pix[x, y] = abgr1555_to_rgba(c)
    return img
