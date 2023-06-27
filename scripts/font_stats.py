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


for fontPath in sys.argv[1:]:
    print("=========================================")
    print("   ", fontPath)
    print("=========================================")
    font = TTFont(fontPath, lazy=True)

    cmap = font.getBestCmap()

    print("num chars:", len(cmap))
    print("num glyphs:", len(font.getGlyphOrder()))

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

    print("num regular glyphs:", len(regularGlyphs))
    print("num composite glyphs:", len(compositeGlyphs))
    print("num var composite glyphs:", len(varCompositeGlyphs))

    print()

    print("glyf table:")
    print("total bytes regular glyphs:", regularGlyphsSize)
    print("total bytes composite glyphs:", compositeGlyphsSize)
    print("total bytes var composite glyphs:", varCompositeGlyphsSize)

    print()
    print("gvar table:")
    print("total bytes regular glyphs:", regularGlyphsVarSize)
    print("total bytes composite glyphs:", compositeGlyphsVarSize)
    print("total bytes var composite glyphs:", varCompositeGlyphsVarSize)
