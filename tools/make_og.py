#!/usr/bin/env python3
"""Render static/og.png: the landing ASCII donut on the project blue.

Dev-only helper (needs Pillow). The runtime app stays stdlib-only.

Font: put SourceCodePro.ttf next to this script, e.g.
  https://github.com/google/fonts/raw/main/ofl/sourcecodepro/SourceCodePro%5Bwght%5D.ttf

Usage:
  python tools/make_og.py
"""
import math
import os

from PIL import Image, ImageDraw, ImageFont

COLS, ROWS = 80, 22
CHARS = ".,-~:;=!*#$@"
BG = "#61b4ff"
FG = "#666666"
W, H = 1200, 630
FONT_SIZE = 22
LINE_H = 26


def donut(t=0.0):
    """Classic donut.c torus, same math as the landing page."""
    zbuf = [0.0] * (COLS * ROWS)
    out = [" "] * (COLS * ROWS)
    A = t * 0.0012
    B = t * 0.0006
    sin_a, cos_a = math.sin(A), math.cos(A)
    sin_b, cos_b = math.sin(B), math.cos(B)
    j = 0.0
    while j < 6.283:
        sin_j, cos_j = math.sin(j), math.cos(j)
        i = 0.0
        while i < 6.283:
            sin_i, cos_i = math.sin(i), math.cos(i)
            h = cos_j + 2
            d = 1 / (sin_i * h * sin_a + sin_j * cos_a + 5)
            l, m, n = cos_i, cos_b, sin_b
            tt = sin_i * h * cos_a - sin_j * sin_a
            x = math.floor(COLS / 2 + 30 * d * (l * h * m - tt * n))
            y = math.floor(ROWS / 2 + 1 + 15 * d * (l * h * n + tt * m))
            o = x + COLS * y
            lum = math.floor(8 * ((sin_j * sin_a - sin_i * cos_j * cos_a) * m
                                  - sin_i * cos_j * sin_a - sin_j * cos_a
                                  - l * cos_j * n))
            if 0 <= x < COLS and 0 <= y < ROWS and d > zbuf[o]:
                zbuf[o] = d
                out[o] = CHARS[lum if 0 < lum < len(CHARS) else 0]
            i += 0.02
        j += 0.07
    return ["".join(out[r * COLS:(r + 1) * COLS]) for r in range(ROWS)]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    font_path = os.path.join(here, "SourceCodePro.ttf")
    if not os.path.exists(font_path):
        raise SystemExit(
            f"missing {font_path}\n"
            "download it from https://github.com/google/fonts/raw/main/"
            "ofl/sourcecodepro/SourceCodePro%5Bwght%5D.ttf"
        )

    font = ImageFont.truetype(font_path, FONT_SIZE)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    lines = donut(0.0)
    char_w = font.getlength("M")
    block_w = char_w * COLS
    block_h = LINE_H * ROWS
    x0 = (W - block_w) / 2
    y0 = (H - block_h) / 2
    for r, line in enumerate(lines):
        draw.text((x0, y0 + r * LINE_H), line, font=font, fill=FG)

    out = os.path.join(root, "static", "og.png")
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out} ({W}x{H}, block {block_w:.0f}x{block_h})")


if __name__ == "__main__":
    main()
