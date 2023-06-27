import pathlib
import sys

from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_v_a_r import TupleVariation
from fontTools.varLib.models import VariationModel, VariationModelError


def tuplifyLocation(location):
    return tuple(sorted(location.items()))


def glyphSetGetter(font):
    glyphSets = {}

    def getGlyphSet(location):
        locTuple = tuplifyLocation(location)
        glyphSet = glyphSets.get(locTuple)
        if glyphSet is None:
            glyphSet = font.getGlyphSet(location=location, normalized=True)
            glyphSets[locTuple] = glyphSet
        return glyphSet

    return getGlyphSet


def flattenVarComposites(font):
    axisOrder = [axis.axisTag for axis in font["fvar"].axes]
    flattenedGlyphs = {}
    baseGlyphNames = set()
    glyfTable = font["glyf"]
    gvarTable = font["gvar"]
    glyphNames = font.getGlyphOrder()
    encodedGlyphNames = set(font.getBestCmap().values())
    # glyphNames.sort(key=lambda glyphName: glyphName not in encodedGlyphNames)

    getGlyphSet = glyphSetGetter(font)

    for glyphName in glyphNames:
        # if glyphName in baseGlyphNames:
        #     continue
        glyph = glyfTable[glyphName]
        if glyph.isVarComposite():
            for compo in glyph.components:
                baseGlyphNames.add(compo.glyphName)
            variations = gvarTable.variations[glyphName]
            locations = [{}]
            for v in variations:
                loc = {axisName: triple[1] for axisName, triple in v.axes.items()}
                if loc == locations[-1]:
                    # The peaks of these locations are the same, but the tents are
                    # different. This is hard to convert back to a master-based
                    # system. This hack partially works, but not quite.
                    print("fudging ***", glyphName)
                    locations[-1] = {
                        k: v - 0.0001 if v == loc[k] else v
                        for k, v in locations[-1].items()
                    }
                locations.append(loc)

            coordinateVariations = []
            defaultGlyph = None
            for location in locations:
                glyphSet = getGlyphSet(location)
                glyph = glyphSet[glyphName]
                pen = TTGlyphPointPen(None)
                glyph.drawPoints(pen)
                ttGlyph = pen.glyph()
                if not location:
                    defaultGlyph = ttGlyph
                coordinates = ttGlyph.coordinates.copy()
                # Add phantom points
                coordinates.extend([(0, 0), (glyph.width, 0), (0, 0), (0, 0)])
                coordinateVariations.append(coordinates)

            try:
                model = VariationModel(locations, axisOrder)
            except VariationModelError:
                print("****** error in", glyphName, locations)
                variations = []
            else:
                deltas, supports = model.getDeltasAndSupports(
                    [c for c in coordinateVariations]
                )
                deltas.pop(0)
                supports.pop(0)
                for d in deltas:
                    d.toInt()
                variations = [TupleVariation(s, d) for s, d in zip(supports, deltas)]
            flattenedGlyphs[glyphName] = defaultGlyph, variations
    for glyphName, (glyph, variations) in flattenedGlyphs.items():
        glyfTable[glyphName] = glyph
        gvarTable.variations[glyphName] = variations

    hmtxTable = font["hmtx"]
    for glyphName in baseGlyphNames:
        if glyphName in encodedGlyphNames:
            continue
        del glyfTable[glyphName]
        del gvarTable.variations[glyphName]
        del hmtxTable.metrics[glyphName]


fontPath = pathlib.Path(sys.argv[1])
outPath = fontPath.parent / (fontPath.stem + "-flattened" + fontPath.suffix)

font = TTFont(fontPath)
flattenVarComposites(font)
font.save(outPath)
