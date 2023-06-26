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


class Builder:
    def __init__(self, reader):
        self.reader = reader  # a Fontra Backend, such as DesignspaceBackend

    async def setup(self):
        self.glyphMap = await self.reader.getGlyphMap()
        glyphOrder = sorted(self.glyphMap)  # XXX
        if ".notdef" not in glyphOrder:
            glyphOrder.insert(0, ".notdef")
        self.glyphOrder = glyphOrder

        self.globalAxes = await self.reader.getGlobalAxes()
        self.globalAxisDict = {
            axis.name: applyAxisMapToAxisValues(axis) for axis in self.globalAxes
        }
        self.globalAxisTags = {axis.name: axis.tag for axis in self.globalAxes}
        self.defaultLocation = {k: v[1] for k, v in self.globalAxisDict.items()}

        self.glyphs = {}
        self.cmap = {}
        self.hMetrics = {}
        self.variations = {}
        self.localAxisTags = set()

    async def build(self):
        await self.buildGlyphs()
        return await self.buildFont()

    async def buildGlyphs(self):
        for glyphName in self.glyphOrder:
            codePoints = self.glyphMap.get(glyphName)
            self.hMetrics[glyphName] = (500, 0)

            if codePoints is not None:
                self.cmap.update((codePoint, glyphName) for codePoint in codePoints)
                glyphInfo = await self.buildOneGlyph(glyphName)
                self.hMetrics[glyphName] = (glyphInfo.xAdvance, 0)
                if glyphInfo.variations:
                    self.variations[glyphName] = glyphInfo.variations
                self.glyphs[glyphName] = glyphInfo.glyph
                self.localAxisTags.update(glyphInfo.localAxisTags)
            else:
                # make .notdef based on UPM
                glyph = TTGlyphPointPen(None).glyph()
                self.hMetrics[glyphName] = (500, 0)
                self.glyphs[glyphName] = glyph

    async def buildOneGlyph(self, glyphName):
        glyph = await self.reader.getGlyph(glyphName)
        localAxisDict = {axis.name: axisTuple(axis) for axis in glyph.axes}
        localDefaultLocation = {k: v[1] for k, v in localAxisDict.items()}
        defaultLocation = {**self.defaultLocation, **localDefaultLocation}
        axisDict = {**self.globalAxisDict, **localAxisDict}
        localAxisTags = makeLocalAxisTags(axisDict, self.globalAxisDict)
        axisTags = {**self.globalAxisTags, **localAxisTags}

        locations = []
        sourceCoordinates = []
        defaultGlyph = None

        # TODO:
        # - collect components data
        # - ensure compatibility
        # - for each component:
        #   - find which transform fields are non-default OR are variable
        #   - build GlyphVarComponent
        #   - build "coordinate" data for gvar

        for source in glyph.sources:
            location = {**defaultLocation, **source.location}
            locations.append(normalizeLocation(location, axisDict))
            sourceGlyph = glyph.layers[source.layerName].glyph
            if location == defaultLocation:
                defaultGlyph = sourceGlyph
            coordinates = GlyphCoordinates()
            coordinates._a.extend(
                sourceGlyph.path.coordinates
            )  # shortcut via ._a array
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
                baseGlyph = await self.reader.getGlyph(compo.name)
                baseAxisDict = {axis.name: axisTuple(axis) for axis in baseGlyph.axes}
                baseAxisDict = {**self.globalAxisDict, **baseAxisDict}
                baseAxisDict = {
                    name: values
                    for name, values in baseAxisDict.items()
                    if name in compo.location
                }
                baseAxisTags = {
                    **self.globalAxisTags,
                    **makeLocalAxisTags(baseAxisDict, self.globalAxisDict),
                }
                ttCompo = GlyphVarComponent()
                ttCompo.glyphName = compo.name
                ttCompo.transform = compo.transformation
                normLoc = normalizeLocation(compo.location, baseAxisDict)
                ttCompo.location = {
                    baseAxisTags[name]: value for name, value in normLoc.items()
                }
                ttGlyph.components.append(ttCompo)
            variations = []
        else:
            ttGlyphPen = TTGlyphPointPen(None)
            defaultGlyph.path.drawPoints(ttGlyphPen)
            ttGlyph = ttGlyphPen.glyph()
        return SimpleNamespace(
            glyph=ttGlyph,
            xAdvance=defaultGlyph.xAdvance,
            variations=variations,
            localAxisTags=set(localAxisTags.values()),
        )

    async def buildFont(self):
        builder = FontBuilder(await self.reader.getUnitsPerEm(), glyphDataFormat=1)

        builder.setupGlyphOrder(self.glyphOrder)
        builder.setupNameTable(dict())
        builder.setupGlyf(self.glyphs)
        if self.globalAxes or self.localAxisTags:
            dsAxes = makeDSAxes(self.globalAxes, sorted(self.localAxisTags))
            builder.setupFvar(dsAxes, [])
            if any(axis.map for axis in dsAxes):
                builder.setupAvar(dsAxes)
        if self.variations:
            builder.setupGvar(self.variations)
        builder.setupHorizontalHeader()
        builder.setupHorizontalMetrics(self.hMetrics)
        builder.setupCharacterMap(self.cmap)
        builder.setupOS2()
        builder.setupPost()

        return builder.font


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
    builder = Builder(reader)
    await builder.setup()
    ttFont = await builder.build()
    ttFont.save(outputFontPath)


if __name__ == "__main__":
    asyncio.run(main())
