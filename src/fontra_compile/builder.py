from dataclasses import dataclass, field
from typing import Any

from fontra.core.classes import VariableGlyph
from fontra.core.path import PackedPath
from fontTools.designspaceLib import AxisDescriptor
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.fixedTools import floatToFixed as fl2fi
from fontTools.misc.timeTools import timestampNow
from fontTools.misc.transform import DecomposedTransform
from fontTools.misc.vector import Vector
from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates
from fontTools.ttLib.tables._g_v_a_r import TupleVariation
from fontTools.ttLib.tables.otTables import VAR_TRANSFORM_MAPPING, VarComponentFlags
from fontTools.varLib.models import (
    VariationModel,
    VariationModelError,
    normalizeLocation,
    piecewiseLinearMap,
)
from fontTools.varLib.multiVarStore import OnlineMultiVarStoreBuilder


class InterpolationError(Exception):
    pass


class MissingBaseGlyphError(Exception):
    pass


# If a component transformation has variations in any of the following fields, the
# component can not be a classic component, and should be compiled as a variable
# component, even if there are no axis variations
VARCO_IF_VARYING = {
    "rotation",
    "scaleX",
    "scaleY",
    "skewX",
    "skewY",
    "tCenterX",
    "tCenterY",
}


@dataclass
class GlyphInfo:
    glyph: Glyph
    xAdvance: float = 500
    variations: list = field(default_factory=list)
    variableComponents: list = field(default_factory=list)
    localAxisTags: set = field(default_factory=set)
    model: VariationModel | None = None


@dataclass
class ComponentInfo:
    name: str
    transform: dict[str, list[float]]
    location: dict[str, list[float]]
    localAxisNames: list
    respondsToGlobalAxes: bool
    baseAxisDict: dict
    baseAxisTags: dict
    isVariableComponent: bool = False
    flags: int = 0
    defaultSourceIndex: int = 0

    def addTransformationToComponent(self, compo, storeBuilder):
        compo.transform = DecomposedTransform(
            **{k: v[self.defaultSourceIndex] for k, v in self.transform.items()}
        )

        if not self.flags & VarComponentFlags.TRANSFORM_HAS_VARIATION:
            return

        transformValues = [
            [
                fl2fi(
                    v / fieldMappingValues.scale,
                    fieldMappingValues.fractionalBits,
                )
                for v in self.transform[fieldName]
            ]
            for fieldName, fieldMappingValues in VAR_TRANSFORM_MAPPING.items()
            if fieldMappingValues.flag & self.flags
        ]

        masterValues = [Vector(vec) for vec in zip(*transformValues)]
        assert masterValues

        _, varIdx = storeBuilder.storeMasters(masterValues)
        compo.transformVarIndex = varIdx

    def addLocationToComponent(self, compo, axisIndicesMapping, axisTags, storeBuilder):
        if not self.flags & VarComponentFlags.HAVE_AXES:
            return

        assert self.location

        location = sorted(mapDictKeys(self.location, self.baseAxisTags).items())
        axisIndices = tuple(axisTags.index(k) for k, v in location)
        axisIndicesIndex = axisIndicesMapping.get(axisIndices)
        if axisIndicesIndex is None:
            axisIndicesIndex = len(axisIndicesMapping)
            axisIndicesMapping[axisIndices] = axisIndicesIndex

        compo.axisIndicesIndex = axisIndicesIndex
        compo.axisValues = [v[self.defaultSourceIndex] for k, v in location]

        if self.flags & VarComponentFlags.AXIS_VALUES_HAVE_VARIATION:
            locationValues = [[fl2fi(v, 14) for v in values] for k, values in location]
            masterValues = [Vector(vec) for vec in zip(*locationValues)]
            _, varIdx = storeBuilder.storeMasters(masterValues)
            compo.axisValuesVarIndex = varIdx


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

        self.axes = await self.reader.getAxes()
        self.globalAxes = self.axes.axes
        self.globalAxisDict = {
            axis.name: applyAxisMapToAxisValues(axis) for axis in self.globalAxes
        }
        self.globalAxisTags = {axis.name: axis.tag for axis in self.globalAxes}
        self.defaultLocation = {k: v[1] for k, v in self.globalAxisDict.items()}

        self.cachedSourceGlyphs: dict[str, VariableGlyph] = {}
        self.cachedComponentBaseInfo: dict = {}

        self.glyphInfos: dict[str, GlyphInfo] = {}
        self.cmap: dict[int, str] = {}

    async def build(self) -> TTFont:
        await self.prepareGlyphs()
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
        if glyphName not in self.glyphInfos and glyphName not in self.glyphOrder:
            self.glyphOrder.append(glyphName)

    async def prepareGlyphs(self) -> None:
        for glyphName in self.glyphOrder:
            codePoints = self.glyphMap.get(glyphName)

            glyphInfo = None

            if codePoints is not None:
                self.cmap.update((codePoint, glyphName) for codePoint in codePoints)
                try:
                    glyphInfo = await self.prepareOneGlyph(glyphName)
                except KeyboardInterrupt:
                    raise
                except (
                    InterpolationError,
                    MissingBaseGlyphError,
                    VariationModelError,
                ) as e:
                    print("warning", glyphName, repr(e))  # TODO: use logging

            if glyphInfo is None:
                # make .notdef based on UPM
                glyphInfo = GlyphInfo(glyph=TTGlyphPointPen(None).glyph(), xAdvance=500)

            self.glyphInfos[glyphName] = glyphInfo

    async def prepareOneGlyph(self, glyphName: str) -> GlyphInfo:
        glyph = await self.getSourceGlyph(glyphName, False)

        localAxisDict = {axis.name: axisTuple(axis) for axis in glyph.axes}
        localDefaultLocation = {k: v[1] for k, v in localAxisDict.items()}
        defaultLocation = {**self.defaultLocation, **localDefaultLocation}
        axisDict = {**self.globalAxisDict, **localAxisDict}
        localAxisTags = makeLocalAxisTags(axisDict, self.globalAxisDict)
        axisTags = {**self.globalAxisTags, **localAxisTags}

        glyphSources = filterActiveSources(glyph.sources)

        sourceCoordinates, locations = prepareSourceCoordinates(
            glyph, glyphSources, defaultLocation, axisDict
        )

        locations = [mapDictKeys(s, axisTags) for s in locations]

        model = (
            VariationModel(locations) if len(locations) >= 2 else None
        )  # XXX axis order!

        variations = (
            prepareGvarVariations(sourceCoordinates, model) if model is not None else []
        )

        defaultSourceIndex = model.reverseMapping[0] if model is not None else 0
        defaultGlyph = glyph.layers[glyphSources[defaultSourceIndex].layerName].glyph

        ttGlyphPen = TTGlyphPointPen(None)
        defaultGlyph.path.drawPoints(ttGlyphPen)
        ttGlyph = ttGlyphPen.glyph()

        componentInfo = await self.collectComponentInfo(glyph, defaultSourceIndex)

        return GlyphInfo(
            glyph=ttGlyph,
            xAdvance=max(defaultGlyph.xAdvance or 0, 0),
            variations=variations,
            variableComponents=componentInfo,
            localAxisTags=set(localAxisTags.values()),
            model=model,
        )

    async def collectComponentInfo(
        self, glyph: VariableGlyph, defaultSourceIndex: int
    ) -> list[ComponentInfo]:
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
            ComponentInfo(
                name=compo.name,
                transform={attrName: [] for attrName in VAR_TRANSFORM_MAPPING},
                location={axisName: [] for axisName in axisNames},
                **await self.getComponentBaseInfo(compo.name),
                defaultSourceIndex=defaultSourceIndex,
            )
            for compo, axisNames in zip(
                firstSourceGlyph.components, allComponentAxisNames
            )
        ]

        for sourceGlyph in sourceGlyphs:
            if len(sourceGlyph.components) != len(components):
                raise InterpolationError(
                    f"components not compatible {glyph.name}: "
                    f"{len(sourceGlyph.components)} vs. {len(components)}"
                )

            for compoInfo, compo in zip(components, sourceGlyph.components):
                if compo.name != compoInfo.name:
                    raise InterpolationError(
                        f"components not compatible in {glyph.name}: "
                        f"{compo.name} vs. {compoInfo.name}"
                    )
                for attrName in VAR_TRANSFORM_MAPPING:
                    compoInfo.transform[attrName].append(
                        getattr(compo.transformation, attrName)
                    )
                normLoc = normalizeLocation(compo.location, compoInfo.baseAxisDict)
                for axisName, axisValue in normLoc.items():
                    if axisName in compoInfo.location:
                        compoInfo.location[axisName].append(axisValue)

        numSources = len(glyphSources)

        for compoInfo in components:
            # Filter out unknown/unused axes
            compoInfo.location = {
                axisName: values
                for axisName, values in compoInfo.location.items()
                if values
            }

            isVariableComponent = bool(compoInfo.location)

            flags = 0

            if not compoInfo.respondsToGlobalAxes:
                flags |= VarComponentFlags.RESET_UNSPECIFIED_AXES

            if isVariableComponent:
                flags |= VarComponentFlags.HAVE_AXES

            for attrName, fieldInfo in VAR_TRANSFORM_MAPPING.items():
                values = compoInfo.transform[attrName]
                if any(v != fieldInfo.defaultValue for v in values):
                    flags |= fieldInfo.flag
                    firstValue = values[0]
                    if any(v != firstValue for v in values[1:]):
                        flags |= VarComponentFlags.TRANSFORM_HAS_VARIATION
                        if attrName in VARCO_IF_VARYING:
                            isVariableComponent = True

            axesAtDefault = []
            for axisName, values in compoInfo.location.items():
                firstValue = values[0]
                if any(v != firstValue for v in values[1:]):
                    flags |= VarComponentFlags.AXIS_VALUES_HAVE_VARIATION
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
            compoInfo.isVariableComponent = isVariableComponent

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
        if baseGlyph is None:
            raise MissingBaseGlyphError(
                f"a required base glyph is not available: {baseGlyphName!r}"
            )

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
        builder.setupGlyf(getGlyphInfoAttributes(self.glyphInfos, "glyph"))

        localAxisTags = set()
        for glyphInfo in self.glyphInfos.values():
            localAxisTags.update(glyphInfo.localAxisTags)

        axisTags = []

        if self.globalAxes or localAxisTags:
            dsAxes = makeDSAxes(self.globalAxes, sorted(localAxisTags))
            axisTags = [axis.tag for axis in dsAxes]
            builder.setupFvar(dsAxes, [])
            if any(axis.map for axis in dsAxes):
                builder.setupAvar(dsAxes)

        variations = getGlyphInfoAttributes(self.glyphInfos, "variations")
        if variations:
            builder.setupGvar(variations)

        if any(glyphInfo.variableComponents for glyphInfo in self.glyphInfos.values()):
            varcTable = self.buildVARC(axisTags)
            builder.font["VARC"] = varcTable

        builder.setupHorizontalHeader()
        builder.setupHorizontalMetrics(
            addLSB(
                builder.font["glyf"],
                getGlyphInfoAttributes(self.glyphInfos, "xAdvance"),
            )
        )
        builder.setupCharacterMap(self.cmap)
        builder.setupOS2()
        builder.setupPost()

        return builder.font

    def buildVARC(self, axisTags):
        axisIndicesMapping = {}
        storeBuilder = OnlineMultiVarStoreBuilder(axisTags)

        glyphNames = [
            glyphName
            for glyphName in self.glyphOrder
            if self.glyphInfos[glyphName].variableComponents
        ]
        coverage = ot.Coverage()
        coverage.glyphs = glyphNames

        varcSubtable = ot.VARC()
        varcSubtable.Version = 0x00010000
        varcSubtable.Coverage = coverage

        variableComposites = []

        for glyphName in varcSubtable.Coverage.glyphs:
            components = []
            model = self.glyphInfos[glyphName].model
            if model is not None:
                storeBuilder.setModel(model)

            for compoInfo in self.glyphInfos[glyphName].variableComponents:
                if model is None:
                    assert (
                        not compoInfo.flags & VarComponentFlags.TRANSFORM_HAS_VARIATION
                    )
                    assert (
                        not compoInfo.flags
                        & VarComponentFlags.AXIS_VALUES_HAVE_VARIATION
                    )

                compo = ot.VarComponent()
                compo.flags = compoInfo.flags
                compo.glyphName = compoInfo.name

                compoInfo.addTransformationToComponent(compo, storeBuilder)
                compoInfo.addLocationToComponent(
                    compo, axisIndicesMapping, axisTags, storeBuilder
                )

                components.append(compo)

            if self.glyphInfos[glyphName].glyph.numberOfContours:
                # Add a component for the outline section, so we can effectively
                # mix outlines and components. This is a special case in the spec.
                compo = ot.VarComponent()
                compo.glyphName = glyphName
                components.append(compo)

            compositeGlyph = ot.VarCompositeGlyph(components)
            variableComposites.append(compositeGlyph)

        compoGlyphs = ot.VarCompositeGlyphs()
        compoGlyphs.VarCompositeGlyph = variableComposites
        varcSubtable.VarCompositeGlyphs = compoGlyphs

        axisIndicesList = ot.AxisIndicesList()
        axisIndicesList.Item = [list(k) for k in axisIndicesMapping.keys()]
        varcSubtable.AxisIndicesList = axisIndicesList

        varcSubtable.MultiVarStore = storeBuilder.finish()

        varcTable = newTable("VARC")
        varcTable.table = varcSubtable
        return varcTable


def prepareSourceCoordinates(
    glyph: VariableGlyph, glyphSources, defaultLocation, axisDict
):
    sourceCoordinates = []
    locations = []
    firstSourcePath = None

    for sourceIndex, source in enumerate(glyphSources):
        location = {**defaultLocation, **source.location}
        locations.append(normalizeLocation(location, axisDict))
        sourceGlyph = glyph.layers[source.layerName].glyph

        coordinates = GlyphCoordinates()

        assert isinstance(sourceGlyph.path, PackedPath)
        coordinates.array.extend(sourceGlyph.path.coordinates)  # shortcut via ._a array
        if firstSourcePath is None:
            firstSourcePath = sourceGlyph.path
        else:
            if firstSourcePath.contourInfo != sourceGlyph.path.contourInfo:
                raise InterpolationError(
                    f"contours for source {source.name} of {glyph.name} are not compatible"
                )
        # phantom points
        coordinates.append((0, 0))
        coordinates.append((sourceGlyph.xAdvance, 0))
        coordinates.append((0, 0))
        coordinates.append((0, 0))
        sourceCoordinates.append(coordinates)

    return sourceCoordinates, locations


def prepareGvarVariations(sourceCoordinates, model):
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

    return [TupleVariation(s, d) for s, d in zip(supports, deltas)]


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
        # TODO: This should be changed to something more controllable.
        if name in globalAxes:
            continue
        numNames = len(axisTags)
        axisTags[name] = f"V{numNames:03}"
    return axisTags


def mapDictKeys(d, mapping):
    return {mapping[k]: v for k, v in d.items()}


def ensureWordRange(d):
    for v in d.array:
        if not (-0x8000 <= v < 0x8000):
            raise ValueError("delta value out of range")


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


def getGlyphInfoAttributes(glyphInfos, attrName):
    return {
        glyphName: getattr(glyphInfo, attrName)
        for glyphName, glyphInfo in glyphInfos.items()
    }
