# BIG*.TMS texture container format.
#
# Each BIG file is a sequence of blocks.  The game loads them at startup in
# order: BIG4 -> BIG0 -> BIG3 -> BIG1 -> BIG2.  Each block uploads a CLUT
# and/or an image rectangle into VRAM.
#
# Block layout (all little-endian):
#   [+0x00] u32  block_total_size   (including this header)
#   [+0x04] u32  unknown
#   [+0x08] u32  flags
#               bits 0-2: pixel mode  (0=4bpp, 1=8bpp, 2=15bpp)
#               bit    3: has_clut
#
# If has_clut:
#   [+0x0C] u32  clut_section_size
#   [+0x10] u16  clut_vram_x
#   [+0x12] u16  clut_vram_y
#   [+0x14] u16  clut_width   (number of ABGR1555 entries per row)
#   [+0x16] u16  clut_height  (number of rows)
#   [+0x18] ...  raw ABGR1555 CLUT data
#
# Image section (immediately after the optional CLUT section):
#   [+0x00] u32  image_section_size
#   [+0x04] u16  img_vram_x
#   [+0x06] u16  img_vram_y
#   [+0x08] u16  img_width    (halfwords per row)
#   [+0x0A] u16  img_height
#   [+0x0C] ...  raw pixel data
#
# Pixel widths in pixels (not halfwords):
#   4bpp  -> halfwords * 4
#   8bpp  -> halfwords * 2
#   15bpp -> halfwords * 1

import struct
from dataclasses import dataclass, field
from typing import Optional
from PIL import Image
from rrr.color import expand_palette, decode_4bpp, decode_8bpp, decode_15bpp


@dataclass
class TmsBlock:
    index: int = 0
    pixel_mode: int = 0    # 0=4bpp 1=8bpp 2=15bpp
    has_clut: bool = False
    clut_x: int = 0
    clut_y: int = 0
    clut_w: int = 0
    clut_h: int = 0
    clut_bytes: bytes = b''
    img_x: int = 0
    img_y: int = 0
    img_w: int = 0         # halfwords per row
    img_h: int = 0
    img_bytes: bytes = b''
    image: Optional[Image.Image] = field(default=None, repr=False)

    @property
    def pixel_width(self) -> int:
        """Actual pixel width (converts halfwords to pixels by mode)."""
        if self.pixel_mode == 0:
            return self.img_w * 4
        if self.pixel_mode == 1:
            return self.img_w * 2
        return self.img_w


def parse_tms(data: bytes, label: str = '') -> list:
    """Parse a BIG*.TMS file and return a list of TmsBlock objects."""
    blocks = []
    pos = 4     # skip file header word
    idx = 0

    while pos + 12 <= len(data):
        block_size = struct.unpack_from('<I', data, pos)[0]
        if block_size < 1:
            break
        flags = struct.unpack_from('<I', data, pos + 8)[0]
        blk = TmsBlock()
        blk.index = idx
        blk.pixel_mode = flags & 7
        blk.has_clut = bool(flags & 8)
        inner = pos + 12

        if blk.has_clut:
            csz = struct.unpack_from('<I', data, inner)[0]
            blk.clut_x = struct.unpack_from('<H', data, inner + 4)[0]
            blk.clut_y = struct.unpack_from('<H', data, inner + 6)[0]
            blk.clut_w = struct.unpack_from('<H', data, inner + 8)[0]
            blk.clut_h = struct.unpack_from('<H', data, inner + 10)[0]
            blk.clut_bytes = data[inner + 12: inner + csz]
            inner += csz

        isz = struct.unpack_from('<I', data, inner)[0]
        blk.img_x = struct.unpack_from('<H', data, inner + 4)[0]
        blk.img_y = struct.unpack_from('<H', data, inner + 6)[0]
        blk.img_w = struct.unpack_from('<H', data, inner + 8)[0]
        blk.img_h = struct.unpack_from('<H', data, inner + 10)[0]
        blk.img_bytes = data[inner + 12: inner + isz]

        blocks.append(blk)
        idx += 1
        pos += (block_size & 0xFFFFFFFC) + 4

    if label:
        print(f'  {label}: {len(blocks)} blocks')
    return blocks


def render_block(blk: TmsBlock) -> Image.Image:
    """Decode a TmsBlock into a PIL RGBA image using its embedded CLUT (if any)."""
    w = max(blk.pixel_width, 1)
    h = max(blk.img_h, 1)

    if blk.pixel_mode == 0:
        if not blk.clut_bytes:
            return Image.new('RGBA', (w, h), (0, 0, 0, 0))
        pal = expand_palette(blk.clut_bytes, blk.clut_w)
        return decode_4bpp(blk.img_bytes, w, h, pal)

    if blk.pixel_mode == 1:
        pal = expand_palette(blk.clut_bytes, blk.clut_w)
        return decode_8bpp(blk.img_bytes, w, h, pal)

    if blk.pixel_mode == 2:
        return decode_15bpp(blk.img_bytes, w, h)

    return Image.new('RGBA', (w, h), (128, 0, 128, 255))
