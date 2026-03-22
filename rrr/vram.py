# PS1 VRAM simulation.
#
# The PS1 has a 1024x512 pixel VRAM, where each pixel is a 16-bit halfword.
# Everything - textures, palettes (CLUTs), and framebuffers - lives in this
# single address space.  We replicate it here as a flat byte array so we can
# load BIG*.TMS and CRS texture data then sample it exactly as the hardware
# would when rendering.

import struct
from rrr.color import expand_palette, decode_4bpp, decode_8bpp, decode_15bpp
from PIL import Image

VRAM_WIDTH = 1024   # halfwords per row
VRAM_HEIGHT = 512   # rows


class VramSim:
    """In-memory copy of the PS1 VRAM, populated by loading game files."""

    def __init__(self):
        # Two bytes per halfword, 1024 halfwords wide, 512 rows tall.
        self.mem = bytearray(VRAM_WIDTH * VRAM_HEIGHT * 2)

    # -- low-level helpers --------------------------------------------------

    def _offset(self, x: int, y: int) -> int:
        return (y * VRAM_WIDTH + x) * 2

    def load_rect(self, x: int, y: int, w: int, h: int, data: bytes):
        """Copy a rectangle of raw halfword data into VRAM at (x, y)."""
        for row in range(h):
            src = row * w * 2
            dst = self._offset(x, y + row)
            self.mem[dst: dst + w * 2] = data[src: src + w * 2]

    # -- higher-level loaders -----------------------------------------------

    def load_tms_block(self, blk):
        """Upload one TmsBlock (CLUT + image data) into VRAM."""
        if blk.has_clut and blk.clut_bytes:
            self.load_rect(blk.clut_x, blk.clut_y,
                           blk.clut_w, blk.clut_h, blk.clut_bytes)
        if blk.img_bytes:
            self.load_rect(blk.img_x, blk.img_y,
                           blk.img_w, blk.img_h, blk.img_bytes)

    def load_pct_block(self, x: int, y: int, w: int, h: int, data: bytes):
        """Upload one PCT/CT CLUT record into VRAM."""
        self.load_rect(x, y, w, h, data)

    # -- texture sampling ---------------------------------------------------

    def extract_texture(self, tx: int, ty: int, cx: int, cy: int,
                        mode: int,
                        u_min: int, v_min: int, u_max: int, v_max: int,
                        pad: int = 2) -> tuple:
        """
        Extract a cropped sub-image from a texture page.

        tx, ty  - VRAM top-left of the texture page (halfword coords).
        cx, cy  - VRAM position of the CLUT row (mode 0/1 only).
        mode    - 0=4bpp, 1=8bpp, 2=15bpp.
        u_min..v_max - UV bounding box of the polys that use this texture.
        pad     - extra pixels added around the bounding box.

        Returns (PIL.Image, u_offset, v_offset) where u/v_offset is the
        top-left pixel of the returned image in texture-page UV space.
        """
        u0 = max(0, u_min - pad)
        v0 = max(0, v_min - pad)
        u1 = min(255, u_max + pad)
        v1 = min(255, v_max + pad)
        ch = max(v1 - v0 + 1, 1)

        if ty + v1 >= VRAM_HEIGHT or tx >= VRAM_WIDTH or cy >= VRAM_HEIGHT:
            return Image.new('RGBA', (max(u1 - u0 + 1, 1), ch), (128, 0, 128, 255)), u0, v0

        if mode == 0:
            try:
                cr = bytes(self.mem[self._offset(cx, cy): self._offset(cx, cy) + 32])
                pal = expand_palette(cr, 16)
            except Exception:
                pal = [(128, 128, 128, 255)] * 16
            rows = b''.join(
                bytes(self.mem[self._offset(tx, ty + r): self._offset(tx, ty + r) + 128])
                for r in range(v0, v1 + 1))
            img = decode_4bpp(rows, 256, ch, pal)

        elif mode == 1:
            try:
                cr = bytes(self.mem[self._offset(cx, cy): self._offset(cx, cy) + 512])
                pal = expand_palette(cr, 256)
            except Exception:
                pal = [(128, 128, 128, 255)] * 256
            rows = b''.join(
                bytes(self.mem[self._offset(tx, ty + r): self._offset(tx, ty + r) + 256])
                for r in range(v0, v1 + 1))
            img = decode_8bpp(rows, 256, ch, pal)

        else:
            rows = b''.join(
                bytes(self.mem[self._offset(tx, ty + r): self._offset(tx, ty + r) + 512])
                for r in range(v0, v1 + 1))
            img = decode_15bpp(rows, 256, ch)

        return img.crop((u0, 0, u1 + 1, ch)), u0, v0
