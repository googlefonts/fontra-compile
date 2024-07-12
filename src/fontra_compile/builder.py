from dataclasses import dataclass, field
from typing import Any

from fontra.core.classes import VariableGlyph
from fontra.core.path import PackedPath
from fontra.core.protocols import ReadableFontBackend
from fontTools.designspaceLib import AxisDescriptor
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.fixedTools import floatToFixed as fl2fi
from fontTools.misc.roundTools import noRound, otRound
from fontTools.misc.timeTools import timestampNow
from fontTools.misc.transform import DecomposedTransform
from fontTools.misc.vector import Vector
from fontTools.pens.boundsPen import BoundsPen, ControlBoundsPen
from fontTools.pens.pointPen import PointToSegmentPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPointPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._g_l_y_f import Glyph as TTGlyph
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
from fontTools.ttLib.tables._g_v_a_r import TupleVariation
from fontTools.ttLib.tables.otTables import VAR_TRANSFORM_MAPPING, VarComponentFlags
from fontTools.varLib import HVAR_FIELDS, VVAR_FIELDS
from fontTools.varLib.builder import buildVarData, buildVarIdxMap
from fontTools.varLib.cff import CFF2CharStringMergePen, addCFFVarStore
from fontTools.varLib.models import (
    VariationModel,
    VariationModelError,
    normalizeLocation,
    piecewiseLinearMap,
)
from fontTools.varLib.multiVarStore import OnlineMultiVarStoreBuilder
from fontTools.varLib.varStore import OnlineVarStoreBuilder


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
    hasContours: bool
    xAdvance: float
    xAdvanceVariations: list
    leftSideBearing: int
    ttGlyph: TTGlyph | None = None
    gvarVariations: list | None = None
    charString: Any | None = None
    charStringSupports: list | None = None
    variableComponents: list = field(default_factory=list)
    localAxisTags: set = field(default_factory=set)
    model: VariationModel | None = None

    def __post_init__(self) -> None:
        if self.ttGlyph is None:
            assert self.gvarVariations is None
            assert self.charString is not None
        else:
            assert self.charString is None


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


@dataclass(kw_only=True)
class Builder:
    reader: ReadableFontBackend  # a Fontra Backend, such as DesignspaceBackend
    requestedGlyphNames: list = field(default_factory=list)
    buildCFF2: bool = False

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

        self.cachedSourceGlyphs: dict[str, VariableGlyph | None] = {}
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
            assert sourceGlyph is not None
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
                glyphInfo = GlyphInfo(
                    ttGlyph=(
                        TTGlyphPointPen(None).glyph() if not self.buildCFF2 else None
                    ),
                    charString=(
                        T2CharStringPen(None, None, CFF2=True).getCharString()
                        if self.buildCFF2
                        else None
                    ),
                    hasContours=False,
                    xAdvance=500,
                    leftSideBearing=0,  # TODO: fix when actual notdef shape is added
                    xAdvanceVariations=[500],
                    gvarVariations=None if self.buildCFF2 else [],
                )

            self.glyphInfos[glyphName] = glyphInfo

    async def prepareOneGlyph(self, glyphName: str) -> GlyphInfo:
        glyph = await self.getSourceGlyph(glyphName, False)

        glyphSources = filterActiveSources(glyph.sources)
        checkInterpolationCompatibility(glyph, glyphSources)

        localAxisDict = {axis.name: axisTuple(axis) for axis in glyph.axes}
        localDefaultLocation = {k: v[1] for k, v in localAxisDict.items()}
        defaultLocation = {**self.defaultLocation, **localDefaultLocation}
        axisDict = {**self.globalAxisDict, **localAxisDict}
        localAxisTags = makeLocalAxisTags(axisDict, self.globalAxisDict)
        axisTags = {**self.globalAxisTags, **localAxisTags}

        locations = prepareLocations(glyphSources, defaultLocation, axisDict)
        locations = [mapDictKeys(s, axisTags) for s in locations]

        model = (
            VariationModel(locations) if len(locations) >= 2 else None
        )  # XXX axis order!

        xAdvanceVariations = prepareXAdvanceVariations(glyph, glyphSources)

        defaultSourceIndex = model.reverseMapping[0] if model is not None else 0
        defaultLayerGlyph = glyph.layers[
            glyphSources[defaultSourceIndex].layerName
        ].glyph

        ttGlyph = None
        gvarVariations = None
        charString = None
        charStringSupports = None

        if not self.buildCFF2:
            ttGlyph, gvarVariations = buildTTGlyph(
                glyph, glyphSources, defaultLayerGlyph, model
            )
        else:
            charString = buildCharString(glyph, glyphSources, defaultLayerGlyph, model)
            charStringSupports = (
                tuple(tuple(sorted(sup.items())) for sup in model.supports[1:])
                if model is not None
                else None
            )

        componentInfo = await self.collectComponentInfo(glyph, defaultSourceIndex)

        boundsPen = (BoundsPen if self.buildCFF2 else ControlBoundsPen)(None)
        defaultLayerGlyph.path.drawPoints(PointToSegmentPen(boundsPen))
        leftSideBearing = boundsPen.bounds[0] if boundsPen.bounds is not None else 0

        return GlyphInfo(
            ttGlyph=ttGlyph,
            gvarVariations=gvarVariations,
            charString=charString,
            charStringSupports=charStringSupports,
            hasContours=not defaultLayerGlyph.path.isEmpty(),
            xAdvance=max(defaultLayerGlyph.xAdvance or 0, 0),
            xAdvanceVariations=xAdvanceVariations,
            leftSideBearing=leftSideBearing,
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
        builder = FontBuilder(
            await self.reader.getUnitsPerEm(),
            glyphDataFormat=1,
            isTTF=not self.buildCFF2,
        )

        builder.updateHead(created=timestampNow(), modified=timestampNow())
        builder.setupGlyphOrder(self.glyphOrder)
        builder.setupNameTable(dict())

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

        if not self.buildCFF2:
            builder.setupGlyf(getGlyphInfoAttributes(self.glyphInfos, "ttGlyph"))
            gvarVariations = getGlyphInfoAttributes(self.glyphInfos, "gvarVariations")
            if gvarVariations:
                builder.setupGvar(gvarVariations)
        else:
            charStrings = getGlyphInfoAttributes(self.glyphInfos, "charString")
            charStringSupports = getGlyphInfoAttributes(
                self.glyphInfos, "charStringSupports"
            )
            varDataList, regionList = prepareCFFVarData(charStrings, charStringSupports)
            builder.setupCFF2(charStrings)
            addCFFVarStore(builder.font, None, varDataList, regionList)

        if any(glyphInfo.variableComponents for glyphInfo in self.glyphInfos.values()):
            varcTable = self.buildVARC(axisTags)
            builder.font["VARC"] = varcTable

        builder.setupHorizontalHeader()
        builder.setupHorizontalMetrics(
            addLSB(
                getGlyphInfoAttributes(self.glyphInfos, "leftSideBearing"),
                getGlyphInfoAttributes(self.glyphInfos, "xAdvance"),
            )
        )
        hvarTable = self.buildHVAR(axisTags)
        builder.font["HVAR"] = hvarTable

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

            if self.glyphInfos[glyphName].hasContours:
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

    def buildHVAR(self, axisTags):
        return self._buildHVAR(HVAR_FIELDS, axisTags)

    def buildVVAR(self, axisTags):
        raise NotImplementedError()
        return self._buildHVAR(VVAR_FIELDS, axisTags)

    def _buildHVAR(self, tableFields, axisTags):
        tableTag = tableFields.tableTag

        VHVAR = newTable(tableTag)
        tableClass = getattr(ot, tableTag)
        vhvar = VHVAR.table = tableClass()
        vhvar.Version = 0x00010000

        # # Build list of source font advance widths for each glyph
        # metricsTag = tableFields.metricsTag
        # advMetricses = [m[metricsTag].metrics for m in master_ttfs]

        # # Build list of source font vertical origin coords for each glyph
        # if tableTag == "VVAR" and "VORG" in master_ttfs[0]:
        #     vOrigMetricses = [m["VORG"].VOriginRecords for m in master_ttfs]
        #     defaultYOrigs = [m["VORG"].defaultVertOriginY for m in master_ttfs]
        #     vOrigMetricses = list(zip(vOrigMetricses, defaultYOrigs))
        # else:
        #     vOrigMetricses = None

        metricsStore, advanceMapping, vOrigMapping = self._prepareHVVAR(
            "xAdvanceVariations", axisTags
        )

        vhvar.VarStore = metricsStore
        if advanceMapping is None:
            setattr(vhvar, tableFields.advMapping, None)
        else:
            setattr(vhvar, tableFields.advMapping, advanceMapping)

        # if vOrigMapping is not None:
        #     setattr(vhvar, tableFields.vOrigMapping, vOrigMapping)

        setattr(vhvar, tableFields.sb1, None)
        setattr(vhvar, tableFields.sb2, None)

        return VHVAR

    def _prepareHVVAR(self, advancesAttrName, axisTags, doVOrigins=False):
        # Based on fontTools.varLib._get_advance_metrics()
        glyphOrder = self.glyphOrder

        vhAdvanceDeltasAndSupports = {}
        # vOrigDeltasAndSupports = {}

        for glyphName in glyphOrder:
            glyphInfo = self.glyphInfos[glyphName]
            vhAdvances = getattr(glyphInfo, advancesAttrName)
            if glyphInfo.model is None:
                assert len(vhAdvances) == 1
                vhAdvanceDeltasAndSupports[glyphName] = [vhAdvances], [{}]
            else:
                vhAdvanceDeltasAndSupports[glyphName] = (
                    glyphInfo.model.getDeltasAndSupports(vhAdvances, round=otRound)
                )

        if doVOrigins:
            raise NotImplementedError()
            # for glyph in glyphOrder:
            #     # We need to supply a vOrigs tuple with non-None default values
            #     # for each glyph. vOrigMetricses contains values only for those
            #     # glyphs which have a non-default vOrig.
            #     vOrigs = [
            #         metrics[glyph] if glyph in metrics else defaultVOrig
            #         for metrics, defaultVOrig in vOrigMetricses
            #     ]
            #     vOrigDeltasAndSupports[glyph] = masterModel.getDeltasAndSupports(
            #         vOrigs, round=otRound
            #     )

        storeBuilder = OnlineVarStoreBuilder(axisTags)
        advMapping = {}
        for glyphName in glyphOrder:
            deltas, supports = vhAdvanceDeltasAndSupports[glyphName]
            storeBuilder.setSupports(supports)
            advMapping[glyphName] = storeBuilder.storeDeltas(deltas, round=noRound)

        # if vOrigMetricses:
        #     vOrigMap = {}
        #     for glyphName in glyphOrder:
        #         deltas, supports = vOrigDeltasAndSupports[glyphName]
        #         storeBuilder.setSupports(supports)
        #         vOrigMap[glyphName] = storeBuilder.storeDeltas(deltas, round=noRound)

        varStore = storeBuilder.finish()
        mapping2 = varStore.optimize(use_NO_VARIATION_INDEX=False)
        advMapping = [mapping2[advMapping[g]] for g in glyphOrder]
        advanceMapping = buildVarIdxMap(advMapping, glyphOrder)

        # if vOrigMetricses:
        #     vOrigMap = [mapping2[vOrigMap[g]] for g in glyphOrder]

        vOrigMapping = None

        # if vOrigMetricses:
        #     vOrigMapping = buildVarIdxMap(vOrigMap, glyphOrder)

        return varStore, advanceMapping, vOrigMapping


def prepareLocations(glyphSources, defaultLocation, axisDict):
    return [
        normalizeLocation({**defaultLocation, **source.location}, axisDict)
        for source in glyphSources
    ]


def checkInterpolationCompatibility(glyph: VariableGlyph, glyphSources):
    firstSourcePath = None

    for source in glyphSources:
        sourceGlyph = glyph.layers[source.layerName].glyph
        assert isinstance(sourceGlyph.path, PackedPath)
        if firstSourcePath is None:
            firstSourcePath = sourceGlyph.path
        else:
            if firstSourcePath.contourInfo != sourceGlyph.path.contourInfo:
                raise InterpolationError(
                    f"contours for source {source.name} of {glyph.name} are not compatible"
                )


def prepareXAdvanceVariations(glyph: VariableGlyph, glyphSources):
    return [glyph.layers[source.layerName].glyph.xAdvance for source in glyphSources]


def buildTTGlyph(glyph, glyphSources, defaultLayerGlyph, model):
    ttGlyphPen = TTGlyphPointPen(None)
    defaultLayerGlyph.path.drawPoints(ttGlyphPen)
    ttGlyph = ttGlyphPen.glyph()

    sourceCoordinates = prepareSourceCoordinates(glyph, glyphSources)
    gvarVariations = (
        prepareGvarVariations(sourceCoordinates, model) if model is not None else []
    )
    return ttGlyph, gvarVariations


def prepareSourceCoordinates(glyph: VariableGlyph, glyphSources):
    sourceCoordinates = []

    for source in glyphSources:
        sourceGlyph = glyph.layers[source.layerName].glyph

        coordinates = GlyphCoordinates()

        assert isinstance(sourceGlyph.path, PackedPath)
        coordinates.array.extend(sourceGlyph.path.coordinates)  # shortcut via ._a array

        # phantom points
        coordinates.append((0, 0))
        coordinates.append((sourceGlyph.xAdvance, 0))
        coordinates.append((0, 0))
        coordinates.append((0, 0))
        sourceCoordinates.append(coordinates)

    return sourceCoordinates


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


def buildCharString(glyph, glyphSources, defaultLayerGlyph, model):
    if model is None:
        pen = T2CharStringPen(None, None, CFF2=True)
        defaultLayerGlyph.path.drawPoints(PointToSegmentPen(pen))
        charString = pen.getCharString()
    else:
        pen = CFF2CharStringMergePen([], glyph.name, len(glyphSources), 0)
        pointPen = PointToSegmentPen(pen)
        for sourceIndex, source in enumerate(glyphSources):
            if sourceIndex:
                pen.restart(sourceIndex)
            layerGlyph = glyph.layers[source.layerName].glyph
            layerGlyph.path.drawPoints(pointPen)
        charString = pen.getCharString(var_model=model)

    return charString


def prepareCFFVarData(charStrings, charStringSupports):
    vsindexMap = {}
    for supports in charStringSupports.values():
        if supports and supports not in vsindexMap:
            vsindexMap[supports] = len(vsindexMap)

    for glyphName, charString in charStrings.items():
        supports = charStringSupports.get(glyphName)
        if supports is not None:
            assert "vsindex" not in charString.program
            vsindex = vsindexMap[supports]
            if vsindex != 0:
                charString.program[:0] = [vsindex, "vsindex"]

    assert list(vsindexMap.values()) == list(range(len(vsindexMap)))

    regionMap = {}
    for supports in vsindexMap.keys():
        for region in supports:
            if region not in regionMap:
                regionMap[region] = len(regionMap)
    assert list(regionMap.values()) == list(range(len(regionMap)))
    regionList = [dict(region) for region in regionMap.keys()]

    varDataList = []
    for supports in vsindexMap.keys():
        varTupleIndexes = [regionMap[region] for region in supports]
        varDataList.append(buildVarData(varTupleIndexes, None, False))

    return varDataList, regionList


def addLSB(
    leftSideBearings: dict[str, int], metrics: dict[str, int]
) -> dict[str, tuple[int, int]]:
    return {
        glyphName: (xAdvance, leftSideBearings.get(glyphName, 0))
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
