import argparse
import asyncio
import pathlib
from types import SimpleNamespace

from importlib.metadata import entry_points

from fontTools.designspaceLib import AxisDescriptor
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates, GlyphVarComponent
from fontTools.ttLib.tables._g_v_a_r import TupleVariation
from fontTools.varLib.models import (
    normalizeLocation,
    piecewiseLinearMap,
    VariationModel,
)

from fontra.core.classes import LocalAxis, from_dict


async def buildTTF(reader):
    glyphMap = await reader.getGlyphMap()
    glyphOrder = sorted(glyphMap)  # XXX
    if ".notdef" not in glyphOrder:
        glyphOrder.insert(0, ".notdef")

    globalAxes = await reader.getGlobalAxes()
    globalAxisDict = {axis.name: applyAxisMapToAxisValues(axis) for axis in globalAxes}
    globalAxisTags = {axis.name: axis.tag for axis in globalAxes}
    defaultLocation = {k: v[1] for k, v in globalAxisDict.items()}

    glyphs = {}
    cmap = {}
    hMetrics = {}
    variations = {}
    localAxisTags = set()

    for glyphName in glyphOrder:
        codePoints = glyphMap.get(glyphName)
        hMetrics[glyphName] = (500, 0)

        if codePoints is not None:
            cmap.update((codePoint, glyphName) for codePoint in codePoints)
            glyphInfo = await buildTTGlyph(
                reader, glyphName, defaultLocation, globalAxisDict, globalAxisTags
            )
            hMetrics[glyphName] = (glyphInfo.xAdvance, 0)
            if glyphInfo.variations:
                variations[glyphName] = glyphInfo.variations
            glyphs[glyphName] = glyphInfo.glyph
            localAxisTags.update(glyphInfo.localAxisTags)
        else:
            # make .notdef based on UPM
            glyph = TTGlyphPointPen(None).glyph()
            hMetrics[glyphName] = (500, 0)
            glyphs[glyphName] = glyph

    builder = FontBuilder(await reader.getUnitsPerEm(), glyphDataFormat=1)

    builder.setupGlyphOrder(glyphOrder)
    builder.setupNameTable(dict())
    builder.setupGlyf(glyphs)
    if globalAxes or localAxisTags:
        dsAxes = makeDSAxes(globalAxes, sorted(localAxisTags))
        builder.setupFvar(dsAxes, [])
        if any(axis.map for axis in dsAxes):
            builder.setupAvar(dsAxes)
    if variations:
        builder.setupGvar(variations)
    builder.setupHorizontalHeader()
    builder.setupHorizontalMetrics(hMetrics)
    builder.setupCharacterMap(cmap)
    builder.setupOS2()
    builder.setupPost()

    return builder.font


async def buildTTGlyph(
    reader, glyphName, defaultLocation, globalAxisDict, globalAxisTags
):
    glyph = await reader.getGlyph(glyphName)
    localAxisDict = {axis.name: axisTuple(axis) for axis in glyph.axes}
    localDefaultLocation = {k: v[1] for k, v in localAxisDict.items()}
    defaultLocation = {**defaultLocation, **localDefaultLocation}
    axisDict = {**globalAxisDict, **localAxisDict}
    localAxisTags = makeLocalAxisTags(axisDict, globalAxisDict)
    axisTags = {**globalAxisTags, **localAxisTags}

    locations = []
    sourceCoordinates = []
    defaultGlyph = None
    for source in glyph.sources:
        location = {**defaultLocation, **source.location}
        locations.append(normalizeLocation(location, axisDict))
        sourceGlyph = glyph.layers[source.layerName].glyph
        if location == defaultLocation:
            defaultGlyph = sourceGlyph
        coordinates = GlyphCoordinates()
        coordinates._a.extend(sourceGlyph.path.coordinates)  # shortcut via ._a array
        # phantom points
        coordinates.append((0, 0))
        coordinates.append((sourceGlyph.xAdvance, 0))
        coordinates.append((0, 0))
        coordinates.append((0, 0))
        sourceCoordinates.append(coordinates)

    model = VariationModel(locations)  # XXX axis order!
    deltas, supports = model.getDeltasAndSupports(sourceCoordinates)
    assert len(supports) == len(deltas)

    deltas.pop(0)  # pop the default
    supports.pop(0)  # pop the default
    for d in deltas:
        d.toInt()
    supports = [
        {axisTags[name]: values for name, values in s.items()} for s in supports
    ]

    variations = [TupleVariation(s, d) for s, d in zip(supports, deltas)]
    if defaultGlyph.components:
        ttGlyph = Glyph()
        ttGlyph.numberOfContours = -2
        ttGlyph.components = []
        ttGlyph.xMin = ttGlyph.yMin = ttGlyph.xMax = ttGlyph.yMax = 0
        for compo in defaultGlyph.components:
            # Ideally we need the full "made of" graph, so we can normalize
            # nested var composites, but then again, our local axis name -> fvar tag name
            # mechanism doesn't account for that, either.
            baseGlyph = await reader.getGlyph(compo.name)
            baseAxisDict = {axis.name: axisTuple(axis) for axis in baseGlyph.axes}
            baseAxisDict = {**globalAxisDict, **baseAxisDict}
            baseAxisDict = {
                name: values
                for name, values in baseAxisDict.items()
                if name in compo.location
            }
            baseAxisTags = {
                **globalAxisTags,
                **makeLocalAxisTags(baseAxisDict, globalAxisDict),
            }
            ttCompo = GlyphVarComponent()
            ttCompo.glyphName = compo.name
            ttCompo.transform = compo.transformation
            normLoc = normalizeLocation(compo.location, baseAxisDict)
            ttCompo.location = {
                baseAxisTags[name]: value for name, value in normLoc.items()
            }
            if ttCompo.location:
                print(glyphName, compo.name, ttCompo.location)
            ttGlyph.components.append(ttCompo)
        variations = []
    else:
        # if glyphName == "A":
        #     print(variations)
        ttGlyphPen = TTGlyphPointPen(None)
        defaultGlyph.path.drawPoints(ttGlyphPen)
        ttGlyph = ttGlyphPen.glyph()
    return SimpleNamespace(
        glyph=ttGlyph,
        xAdvance=defaultGlyph.xAdvance,
        variations=variations,
        localAxisTags=set(localAxisTags.values()),
    )


def applyAxisMapToAxisValues(axis):
    mappingDict = {k: v for k, v in axis.mapping}
    minValue = piecewiseLinearMap(axis.minValue, mappingDict)
    defaultValue = piecewiseLinearMap(axis.defaultValue, mappingDict)
    maxValue = piecewiseLinearMap(axis.maxValue, mappingDict)
    return (minValue, defaultValue, maxValue)


def axisTuple(axis):
    return (axis.minValue, axis.defaultValue, axis.maxValue)


def newAxisDescriptor(
    *, name, tag, minValue, defaultValue, maxValue, mapping=(), hidden=False
):
    dsAxis = AxisDescriptor()
    dsAxis.minimum = minValue
    dsAxis.default = defaultValue
    dsAxis.maximum = maxValue
    dsAxis.name = name
    dsAxis.tag = tag
    dsAxis.hidden = hidden
    if mapping:
        dsAxis.map = filterDuplicates([tuple(m) for m in mapping])
    return dsAxis


def makeDSAxes(axes, localAxisTags):
    return [
        newAxisDescriptor(
            name=axis.name,
            tag=axis.tag,
            minValue=axis.minValue,
            defaultValue=axis.defaultValue,
            maxValue=axis.maxValue,
            mapping=axis.mapping,
        )
        for axis in axes
    ] + [
        newAxisDescriptor(
            name=localAxisTag,
            tag=localAxisTag,
            minValue=-1,
            defaultValue=0,
            maxValue=1,
            hidden=True,
        )
        for localAxisTag in localAxisTags
    ]


def filterDuplicates(seq):
    return list(dict.fromkeys(seq))


def makeLocalAxisTags(axisDict, globalAxes):
    axisTags = {}
    for name in axisDict:
        # Sort axis names, to match current Fontra and RoboCJK behavior.
        # TOD: This should be changed to something more controllable.
        if name in globalAxes:
            continue
        numNames = len(axisTags)
        axisTags[name] = f"V{numNames:03}"
    return axisTags


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_font")
    parser.add_argument("output_font")

    args = parser.parse_args()
    sourceFontPath = pathlib.Path(args.source_font).resolve()
    outputFontPath = pathlib.Path(args.output_font).resolve()

    fileType = sourceFontPath.suffix.lstrip(".").lower()
    backendEntryPoints = entry_points(group="fontra.filesystem.backends")
    entryPoint = backendEntryPoints[fileType]
    backendClass = entryPoint.load()
    reader = backendClass.fromPath(sourceFontPath)
    ttFont = await buildTTF(reader)
    ttFont.save(outputFontPath)


if __name__ == "__main__":
    asyncio.run(main())
