import argparse
import asyncio
import pathlib
from importlib.metadata import entry_points
from types import SimpleNamespace

# from fontra.core.classes import LocalAxis, from_dict
from fontTools.designspaceLib import AxisDescriptor
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.transform import DecomposedTransform
from fontTools.misc.fixedTools import floatToFixed as fl2fi
from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib.tables._g_l_y_f import (
    VAR_COMPONENT_TRANSFORM_MAPPING,
    Glyph,
    GlyphCoordinates,
    GlyphVarComponent,
    VarComponentFlags,
)
from fontTools.ttLib.tables._g_v_a_r import TupleVariation
from fontTools.varLib.models import (
    VariationModel,
    normalizeLocation,
    piecewiseLinearMap,
)


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
        self.xAndvances = {}
        self.variations = {}
        self.localAxisTags = set()

    async def build(self):
        await self.buildGlyphs()
        return await self.buildFont()

    async def buildGlyphs(self):
        for glyphName in self.glyphOrder:
            codePoints = self.glyphMap.get(glyphName)
            self.xAndvances[glyphName] = 500

            if codePoints is not None:
                self.cmap.update((codePoint, glyphName) for codePoint in codePoints)
                glyphInfo = await self.buildOneGlyph(glyphName)
                self.xAndvances[glyphName] = glyphInfo.xAdvance
                if glyphInfo.variations:
                    self.variations[glyphName] = glyphInfo.variations
                self.glyphs[glyphName] = glyphInfo.glyph
                self.localAxisTags.update(glyphInfo.localAxisTags)
            else:
                # make .notdef based on UPM
                glyph = TTGlyphPointPen(None).glyph()
                self.xAndvances[glyphName] = 500
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

        componentInfo = await self.collectComponentInfo(glyph)

        for sourceIndex, source in enumerate(glyph.sources):
            location = {**defaultLocation, **source.location}
            locations.append(normalizeLocation(location, axisDict))
            sourceGlyph = glyph.layers[source.layerName].glyph

            if location == defaultLocation:
                # This is the fefault glyph
                defaultGlyph = sourceGlyph

            coordinates = GlyphCoordinates()

            if componentInfo:
                for compoInfo in componentInfo:
                    transform = {
                        attrName: values[sourceIndex]
                        for attrName, values in compoInfo.transform.items()
                    }
                    transform = DecomposedTransform(**transform)
                    coordinates.extend(getTransformCoords(transform, compoInfo.flags))
            else:
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
        supports = [mapDictKeys(s, axisTags) for s in supports]

        variations = [TupleVariation(s, d) for s, d in zip(supports, deltas)]

        if componentInfo:
            ttGlyph = Glyph()
            ttGlyph.numberOfContours = -2
            ttGlyph.components = []
            ttGlyph.xMin = ttGlyph.yMin = ttGlyph.xMax = ttGlyph.yMax = 0
            for compo, compoInfo in zip(defaultGlyph.components, componentInfo):
                ttCompo = GlyphVarComponent()
                ttCompo.flags = compoInfo.flags
                ttCompo.glyphName = compo.name
                ttCompo.transform = compo.transformation
                normLoc = normalizeLocation(compo.location, compoInfo.baseAxisDict)
                ttCompo.location = mapDictKeys(normLoc, compoInfo.baseAxisTags)
                ttGlyph.components.append(ttCompo)
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

    async def collectComponentInfo(self, glyph):
        firstSource = glyph.sources[0]
        firstSourceGlyph = glyph.layers[firstSource.layerName].glyph

        components = [
            SimpleNamespace(
                name=compo.name,
                transform={
                    attrName: [] for attrName in VAR_COMPONENT_TRANSFORM_MAPPING
                },
                location={axisName: [] for axisName in compo.location},
                **await self.setupComponentBaseAxes(compo),
            )
            for compo in firstSourceGlyph.components
        ]

        for source in glyph.sources:
            sourceGlyph = glyph.layers[source.layerName].glyph

            if len(sourceGlyph.components) != len(components):
                raise ValueError(f"components not compatible {glyph.name}")

            for compoInfo, compo in zip(components, sourceGlyph.components):
                if compo.name != compoInfo.name:
                    raise ValueError(
                        f"components not compatible in {glyph.name}: "
                        f"{compo.name} vs. {compoInfo.name}"
                    )
                if sorted(compoInfo.location) != sorted(compo.location):
                    raise ValueError(
                        f"component locations not compatible in {glyph.name}: "
                        f"{compo.name}"
                    )
                for attrName in VAR_COMPONENT_TRANSFORM_MAPPING:
                    compoInfo.transform[attrName].append(
                        getattr(compo.transformation, attrName)
                    )
                normLoc = normalizeLocation(compo.location, compoInfo.baseAxisDict)
                for axisName, axisValue in normLoc.items():
                    compoInfo.location[axisName].append(axisValue)

        for compoInfo in components:
            flags = 0

            for attrName, fieldInfo in VAR_COMPONENT_TRANSFORM_MAPPING.items():
                values = compoInfo.transform[attrName]
                firstValue = values[0]
                if any(v != firstValue or v != fieldInfo.defaultValue for v in values):
                    flags |= fieldInfo.flag

            for axisName, values in compoInfo.location.items():
                firstValue = values[0]
                if any(v != firstValue for v in values[1:]):
                    flags |= VarComponentFlags.AXES_HAVE_VARIATION
                    break

            compoInfo.flags = flags

        return components

    async def setupComponentBaseAxes(self, compo):
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
        return dict(baseAxisDict=baseAxisDict, baseAxisTags=baseAxisTags)

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
        builder.setupHorizontalMetrics(addLSB(builder.font["glyf"], self.xAndvances))
        builder.setupCharacterMap(self.cmap)
        builder.setupOS2()
        builder.setupPost()

        return builder.font


def addLSB(glyfTable, metrics):
    return {
        glyphName: (xAdvance, glyfTable[glyphName].xMin)
        for glyphName, xAdvance in metrics.items()
    }


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


def mapDictKeys(d, mapping):
    return {mapping[k]: v for k, v in d.items()}


def getTransformCoords(transform, flags):
    # This is mostly taken from _g_l_y_f.py, would be nice if we could
    # reuse that code somehow.
    coords = []
    if flags & (
        VarComponentFlags.HAVE_TRANSLATE_X | VarComponentFlags.HAVE_TRANSLATE_Y
    ):
        coords.append((transform.translateX, transform.translateY))
    if flags & VarComponentFlags.HAVE_ROTATION:
        coords.append((fl2fi(transform.rotation / 180, 12), 0))
    if flags & (VarComponentFlags.HAVE_SCALE_X | VarComponentFlags.HAVE_SCALE_Y):
        coords.append((fl2fi(transform.scaleX, 10), fl2fi(transform.scaleY, 10)))
    if flags & (VarComponentFlags.HAVE_SKEW_X | VarComponentFlags.HAVE_SKEW_Y):
        coords.append(
            (
                fl2fi(transform.skewX / -180, 12),
                fl2fi(transform.skewY / 180, 12),
            )
        )
    if flags & (VarComponentFlags.HAVE_TCENTER_X | VarComponentFlags.HAVE_TCENTER_Y):
        coords.append((transform.tCenterX, transform.tCenterY))
    return coords


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
