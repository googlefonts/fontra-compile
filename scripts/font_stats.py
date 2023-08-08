import os
import sys

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_v_a_r import GVAR_HEADER_SIZE


def getGvarSizes(font):
    gvarData = font.getTableData("gvar")
    gvarTable = font["gvar"]
    glyphOrder = font.getGlyphOrder()

    offsets = gvarTable.decompileOffsets_(
        gvarData[GVAR_HEADER_SIZE:],
        tableFormat=(gvarTable.flags & 1),
        glyphCount=gvarTable.glyphCount,
    )
    sizes = {}
    for gid, glyphName in enumerate(glyphOrder):
        sizes[glyphName] = offsets[gid + 1] - offsets[gid]
    return sizes


header = [
    "file",
    "file size",
    "num chars",
    "num glyphs",
    "num outline glyphs",
    "num composite glyphs",
    "num var composite glyphs",
    "glyf table size",
    "gvar table size",
    "glyf outline glyphs size",
    "glyf composite glyphs size",
    "glyf var composite glyphs size",
    "gvar outline glyphs size",
    "gvar composite glyphs size",
    "gvar var composite glyphs size",
]

separator = ";"
print(separator.join(header))

for fontPath in sys.argv[1:]:
    font = TTFont(fontPath, lazy=True)

    cmap = font.getBestCmap()

    regularGlyphs = set()
    compositeGlyphs = set()
    varCompositeGlyphs = set()

    regularGlyphsSize = 0
    compositeGlyphsSize = 0
    varCompositeGlyphsSize = 0

    regularGlyphsVarSize = 0
    compositeGlyphsVarSize = 0
    varCompositeGlyphsVarSize = 0

    glyfTable = font["glyf"]

    gvarSizes = getGvarSizes(font)

    for glyphName, glyph in glyfTable.glyphs.items():
        if glyph.isVarComposite():
            varCompositeGlyphs.add(glyphName)
            varCompositeGlyphsSize += len(glyph.data)
            varCompositeGlyphsVarSize += gvarSizes[glyphName]

        elif glyph.isComposite():
            compositeGlyphs.add(glyphName)
            compositeGlyphsSize += len(glyph.data)
            compositeGlyphsVarSize += gvarSizes[glyphName]
        else:
            regularGlyphs.add(glyphName)
            if hasattr(glyph, "data"):
                regularGlyphsSize += len(glyph.data)
            regularGlyphsVarSize += gvarSizes[glyphName]

    row = []
    row.append(fontPath)
    row.append(os.stat(fontPath).st_size)
    row.append(len(cmap))
    row.append(len(font.getGlyphOrder()))
    row.append(len(regularGlyphs))
    row.append(len(compositeGlyphs))
    row.append(len(varCompositeGlyphs))

    row.append(font.reader.tables["glyf"].length)
    row.append(font.reader.tables["gvar"].length)

    row.append(regularGlyphsSize)
    row.append(compositeGlyphsSize)
    row.append(varCompositeGlyphsSize)

    row.append(regularGlyphsVarSize)
    row.append(compositeGlyphsVarSize)
    row.append(varCompositeGlyphsVarSize)

    print(separator.join(str(cell) for cell in row))
