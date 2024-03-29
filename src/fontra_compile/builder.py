from types import SimpleNamespace
from typing import Any

from fontra.core.classes import VariableGlyph
from fontra.core.path import PackedPath
from fontTools.designspaceLib import AxisDescriptor
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.fixedTools import floatToFixed as fl2fi
from fontTools.misc.timeTools import timestampNow
from fontTools.misc.transform import DecomposedTransform
from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib import TTFont
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
    VariationModelError,
    normalizeLocation,
    piecewiseLinearMap,
)


class Builder:
    def __init__(self, reader, requestedGlyphNames=None):
        self.reader = reader  # a Fontra Backend, such as DesignspaceBackend
        self.requestedGlyphNames = requestedGlyphNames

    async def setup(self) -> None:
        self.glyphMap = await self.reader.getGlyphMap()
        glyphOrder = (
            self.requestedGlyphNames
            if self.requestedGlyphNames
            else sorted(self.glyphMap)  # XXX
        )
        if ".notdef" not in glyphOrder:
            glyphOrder.insert(0, ".notdef")
        self.glyphOrder = glyphOrder

        self.globalAxes = await self.reader.getGlobalAxes()
        self.globalAxisDict = {
            axis.name: applyAxisMapToAxisValues(axis) for axis in self.globalAxes
        }
        self.globalAxisTags = {axis.name: axis.tag for axis in self.globalAxes}
        self.defaultLocation = {k: v[1] for k, v in self.globalAxisDict.items()}

        self.cachedSourceGlyphs: dict[str, VariableGlyph] = {}
        self.cachedComponentBaseInfo: dict = {}

        self.glyphs: dict[str, Glyph] = {}
        self.cmap: dict[int, str] = {}
        self.xAdvances: dict[str, int] = {}
        self.variations: dict[str, list[TupleVariation]] = {}
        self.localAxisTags: set[str] = set()

    async def build(self) -> TTFont:
        await self.buildGlyphs()
        return await self.buildFont()

    async def getSourceGlyph(
        self, glyphName: str, storeInCache: bool = False
    ) -> VariableGlyph:
        sourceGlyph = self.cachedSourceGlyphs.get(glyphName)
        if sourceGlyph is None:
            sourceGlyph = await self.reader.getGlyph(glyphName)
            if storeInCache:
                self.cachedSourceGlyphs[glyphName] = sourceGlyph
        return sourceGlyph

    def ensureGlyphDependency(self, glyphName: str) -> None:
        if glyphName not in self.glyphs and glyphName not in self.glyphOrder:
            self.glyphOrder.append(glyphName)

    async def buildGlyphs(self) -> None:
        for glyphName in self.glyphOrder:
            codePoints = self.glyphMap.get(glyphName)
            self.xAdvances[glyphName] = 500

            glyphInfo = None

            if codePoints is not None:
                self.cmap.update((codePoint, glyphName) for codePoint in codePoints)
                try:
                    glyphInfo = await self.buildOneGlyph(glyphName)
                except KeyboardInterrupt:
                    raise
                except (ValueError, VariationModelError) as e:  # InterpolationError
                    print("warning", glyphName, repr(e))  # TODO: use logging
                else:
                    self.xAdvances[glyphName] = max(glyphInfo.xAdvance, 0)
                    if glyphInfo.variations:
                        self.variations[glyphName] = glyphInfo.variations
                    self.glyphs[glyphName] = glyphInfo.glyph
                    self.localAxisTags.update(glyphInfo.localAxisTags)

            if glyphInfo is None:
                # make .notdef based on UPM
                glyph = TTGlyphPointPen(None).glyph()
                self.xAdvances[glyphName] = 500
                self.glyphs[glyphName] = glyph

    async def buildOneGlyph(self, glyphName: str) -> SimpleNamespace:
        glyph = await self.getSourceGlyph(glyphName, False)
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
        firstSourcePath = None

        glyphSources = filterActiveSources(glyph.sources)

        for sourceIndex, source in enumerate(glyphSources):
            location = {**defaultLocation, **source.location}
            locations.append(normalizeLocation(location, axisDict))
            sourceGlyph = glyph.layers[source.layerName].glyph

            if location == defaultLocation:
                # This is the fefault glyph
                defaultGlyph = sourceGlyph

            coordinates = GlyphCoordinates()

            if componentInfo:
                for compoInfo in componentInfo:
                    location = sortedDict(
                        mapDictKeys(
                            {
                                axisName: values[sourceIndex]
                                for axisName, values in compoInfo.location.items()
                            },
                            compoInfo.baseAxisTags,
                        )
                    )
                    coordinates.extend(getLocationCoords(location, compoInfo.flags))

                    transform = {
                        attrName: values[sourceIndex]
                        for attrName, values in compoInfo.transform.items()
                    }
                    transform = DecomposedTransform(**transform)
                    coordinates.extend(getTransformCoords(transform, compoInfo.flags))
            else:
                assert isinstance(sourceGlyph.path, PackedPath)
                coordinates.array.extend(
                    sourceGlyph.path.coordinates
                )  # shortcut via ._a array
                if firstSourcePath is None:
                    firstSourcePath = sourceGlyph.path
                else:
                    if firstSourcePath.contourInfo != sourceGlyph.path.contourInfo:
                        raise ValueError(
                            f"contours for source {source.name} of {glyphName} are not compatible"
                        )
            # phantom points
            coordinates.append((0, 0))
            coordinates.append((sourceGlyph.xAdvance, 0))
            coordinates.append((0, 0))
            coordinates.append((0, 0))
            sourceCoordinates.append(coordinates)

        model = VariationModel(locations)  # XXX axis order!

        numPoints = len(sourceCoordinates[0])
        for coords in sourceCoordinates[1:]:
            assert len(coords) == numPoints

        deltas, supports = model.getDeltasAndSupports(sourceCoordinates)
        assert len(supports) == len(deltas)

        deltas.pop(0)  # pop the default
        supports.pop(0)  # pop the default
        for d in deltas:
            d.toInt()
            ensureWordRange(d)
        supports = [mapDictKeys(s, axisTags) for s in supports]

        variations = [TupleVariation(s, d) for s, d in zip(supports, deltas)]

        assert defaultGlyph is not None

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
                normLoc = filterDict(normLoc, compoInfo.location)
                ttCompo.location = sortedDict(
                    mapDictKeys(normLoc, compoInfo.baseAxisTags)
                )
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

    async def collectComponentInfo(self, glyph: VariableGlyph) -> list[SimpleNamespace]:
        glyphSources = filterActiveSources(glyph.sources)
        sourceGlyphs = [glyph.layers[source.layerName].glyph for source in glyphSources]

        firstSourceGlyph = sourceGlyphs[0]

        # Collect all used axis names across all sources, per component --
        # we will use that below to make component locations compatible
        allComponentAxisNames = [
            {axisName for compo in compoSources for axisName in compo.location}
            for compoSources in zip(
                *(sourceGlyph.components for sourceGlyph in sourceGlyphs)
            )
        ]

        components = [
            SimpleNamespace(
                name=compo.name,
                transform={
                    attrName: [] for attrName in VAR_COMPONENT_TRANSFORM_MAPPING
                },
                location={axisName: [] for axisName in axisNames},
                **await self.getComponentBaseInfo(compo.name),
            )
            for compo, axisNames in zip(
                firstSourceGlyph.components, allComponentAxisNames
            )
        ]

        for sourceGlyph in sourceGlyphs:
            if len(sourceGlyph.components) != len(components):
                raise ValueError(
                    f"components not compatible {glyph.name}: "
                    f"{len(sourceGlyph.components)} vs. {len(components)}"
                )

            for compoInfo, compo in zip(components, sourceGlyph.components):
                if compo.name != compoInfo.name:
                    raise ValueError(
                        f"components not compatible in {glyph.name}: "
                        f"{compo.name} vs. {compoInfo.name}"
                    )
                for attrName in VAR_COMPONENT_TRANSFORM_MAPPING:
                    compoInfo.transform[attrName].append(
                        getattr(compo.transformation, attrName)
                    )
                normLoc = normalizeLocation(compo.location, compoInfo.baseAxisDict)
                for axisName, axisValue in normLoc.items():
                    if axisName in compoInfo.location:
                        compoInfo.location[axisName].append(axisValue)

        numSources = len(glyphSources)

        for compoInfo in components:
            flags = (
                0
                if compoInfo.respondsToGlobalAxes
                else VarComponentFlags.RESET_UNSPECIFIED_AXES
            )

            for attrName, fieldInfo in VAR_COMPONENT_TRANSFORM_MAPPING.items():
                values = compoInfo.transform[attrName]
                firstValue = values[0]
                if any(v != firstValue or v != fieldInfo.defaultValue for v in values):
                    flags |= fieldInfo.flag

            # Filter out unknown/unused axes
            compoInfo.location = {
                axisName: values
                for axisName, values in compoInfo.location.items()
                if values
            }
            axesAtDefault = []
            for axisName, values in compoInfo.location.items():
                firstValue = values[0]
                if any(v != firstValue for v in values[1:]):
                    flags |= VarComponentFlags.AXES_HAVE_VARIATION
                elif firstValue == 0:
                    axesAtDefault.append(axisName)

            if flags & VarComponentFlags.RESET_UNSPECIFIED_AXES:
                for axisName in axesAtDefault:
                    del compoInfo.location[axisName]
            else:
                for axisName in compoInfo.localAxisNames:
                    if axisName not in compoInfo.location:
                        compoInfo.location[axisName] = [0] * numSources

            compoInfo.flags = flags

            self.ensureGlyphDependency(compoInfo.name)

        return components

    async def getComponentBaseInfo(self, baseGlyphName: str) -> dict[str, Any]:
        baseInfo = self.cachedComponentBaseInfo.get(baseGlyphName)
        if baseInfo is None:
            baseInfo = await self.setupComponentBaseInfo(baseGlyphName)
            self.cachedComponentBaseInfo[baseGlyphName] = baseInfo
        return baseInfo

    async def setupComponentBaseInfo(self, baseGlyphName: str) -> dict[str, Any]:
        baseGlyph = await self.getSourceGlyph(baseGlyphName, True)
        localAxisNames = {axis.name for axis in baseGlyph.axes}

        # To determine the `respondsToGlobalAxes` flag, we take this component and all
        # its child components into account, recursively
        responsiveAxesNames = {
            axisName for source in baseGlyph.sources for axisName in source.location
        }
        respondsToGlobalAxes = bool(
            responsiveAxesNames - localAxisNames
        ) or await asyncAny(
            (await self.getComponentBaseInfo(nestedCompoName))["respondsToGlobalAxes"]
            for nestedCompoName in getComponentBaseNames(baseGlyph)
        )

        baseAxisDict = {axis.name: axisTuple(axis) for axis in baseGlyph.axes}
        baseAxisDict = {**self.globalAxisDict, **baseAxisDict}
        baseAxisTags = {
            **self.globalAxisTags,
            **makeLocalAxisTags(baseAxisDict, self.globalAxisDict),
        }
        return dict(
            localAxisNames=localAxisNames,
            respondsToGlobalAxes=respondsToGlobalAxes,
            baseAxisDict=baseAxisDict,
            baseAxisTags=baseAxisTags,
        )

    async def buildFont(self) -> TTFont:
        builder = FontBuilder(await self.reader.getUnitsPerEm(), glyphDataFormat=1)

        builder.updateHead(created=timestampNow(), modified=timestampNow())
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
        builder.setupHorizontalMetrics(addLSB(builder.font["glyf"], self.xAdvances))
        builder.setupCharacterMap(self.cmap)
        builder.setupOS2()
        builder.setupPost()

        return builder.font


def addLSB(glyfTable, metrics: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {
        glyphName: (xAdvance, glyfTable[glyphName].xMin)
        for glyphName, xAdvance in metrics.items()
    }


def applyAxisMapToAxisValues(axis) -> tuple[float, float, float]:
    mappingDict = {k: v for k, v in axis.mapping}
    minValue = piecewiseLinearMap(axis.minValue, mappingDict)
    defaultValue = piecewiseLinearMap(axis.defaultValue, mappingDict)
    maxValue = piecewiseLinearMap(axis.maxValue, mappingDict)
    return (minValue, defaultValue, maxValue)


def axisTuple(axis) -> tuple[float, float, float]:
    return (axis.minValue, axis.defaultValue, axis.maxValue)


def newAxisDescriptor(
    *, name, tag, minValue, defaultValue, maxValue, mapping=(), hidden=False
) -> AxisDescriptor:
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


def makeDSAxes(axes, localAxisTags) -> list[AxisDescriptor]:
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


def sortedDict(d):
    return dict(sorted(d.items()))


def filterDict(d, keys):
    return {k: v for k, v in d.items() if k in keys}


def getLocationCoords(location, flags):
    coords = []
    if flags & VarComponentFlags.AXES_HAVE_VARIATION:
        for tag, value in location.items():
            coords.append((fl2fi(value, 14), 0))
    return coords


def ensureWordRange(d):
    for v in d.array:
        if not (-0x8000 <= v < 0x8000):
            raise ValueError("delta value out of range")


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


def getComponentBaseNames(glyph):
    glyphSources = filterActiveSources(glyph.sources)
    firstSourceGlyph = glyph.layers[glyphSources[0].layerName].glyph
    return {compo.name for compo in firstSourceGlyph.components}


def filterActiveSources(sources):
    return [source for source in sources if not source.inactive]


async def asyncAny(aiterable):
    async for item in aiterable:
        if item:
            return True
    return False
