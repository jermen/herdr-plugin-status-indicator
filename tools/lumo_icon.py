"""Add Lumo Code's logo to the icon font: the head of its pixel cat, sized like the other logos.

Usage: python tools/lumo_icon.py <lumo-code>/crates/lumo-core/assets/cat.svg assets/HerdrAgentIconsMax-Regular.ttf
Needs fontTools. Rewrites the font in place; running it again replaces the glyph.
"""
import re
import sys

from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

CODEPOINT = 0xE1B7  # the slot after Radar's last vendor (qodercli, U+E1B6)
WHITE = "#fdfdfd"


def head(svg):
    """The cat's head as rows of filled pixels: rows 1-16, columns 0-20, eyes left open."""
    px = {}
    for x, y, w, h, fill in re.findall(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)" fill="([^"]+)"', svg):
        for i in range(int(w)):
            for j in range(int(h)):
                px[(int(x) + i, int(y) + j)] = fill
    # The tail starts at column 19 on row 16.
    return [[px.get((x, y), WHITE) != WHITE and not (y == 16 and x > 18) for x in range(21)] for y in range(1, 17)]


def glyph(rows, size=44, top=717):
    """21 x 16 pixels -> 924 x 704 units, centred on the other logos (about -95..825)."""
    pen = TTGlyphPen(None)
    for j, row in enumerate(rows):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            x0, x1 = start * size, x * size
            y1 = top - j * size + (1 if j else 0)  # overlap the row above by a unit: no seams
            y0 = top - (j + 1) * size
            pen.moveTo((x0, y0))
            pen.lineTo((x0, y1))
            pen.lineTo((x1, y1))
            pen.lineTo((x1, y0))
            pen.closePath()
    return pen.glyph()


def main(svg_path, font_path):
    font = TTFont(font_path)
    order = font.getGlyphOrder()
    if "lumo" not in order:
        font.setGlyphOrder(order + ["lumo"])
    font["glyf"]["lumo"] = glyph(head(open(svg_path).read()))
    font["hmtx"]["lumo"] = (600, 0)
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap[CODEPOINT] = "lumo"
    font.save(font_path)


if __name__ == "__main__":
    main(*sys.argv[1:])
