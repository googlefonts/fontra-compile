import itertools
import os
import pathlib
import tempfile
from contextlib import aclosing, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import AsyncGenerator, ContextManager

from fontmake.__main__ import main as fontmake_main
from fontra.backends import getFileSystemBackend, newFileSystemBackend
from fontra.backends.copy import copyFont
from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import OutputActionProtocol, registerOutputAction
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.ufoLib import UFOReaderWriter


@registerOutputAction("compile-fontmake")
@dataclass(kw_only=True)
class CompileFontMakeAction:
    destination: str
    options: dict[str, str] = field(default_factory=dict)
    setOverlapSimpleFlag: bool = False
    ufoTempDir: str | None = None
    input: ReadableFontBackend | None = field(init=False, default=None)

    @asynccontextmanager
    async def connect(
        self, input: ReadableFontBackend
    ) -> AsyncGenerator[OutputActionProtocol, None]:
        self.input = input
        try:
            yield self
        finally:
            self.input = None

    async def process(
        self, outputDir: os.PathLike = pathlib.Path(), *, continueOnError=False
    ) -> None:
        assert self.input is not None
        outputDir = pathlib.Path(outputDir)
        outputFontPath = outputDir / self.destination

        axes = await self.input.getAxes()
        isVariable = bool(axes.axes)

        tempDirContext: ContextManager

        if self.ufoTempDir:
            tempDirContext = nullcontext(enter_result=self.ufoTempDir)
        else:
            tempDirContext = tempfile.TemporaryDirectory()

        with tempDirContext as tmpDirName:
            tmpDir = pathlib.Path(tmpDirName)

            fileName = "temp." + ("designspace" if isVariable else "ufo")
            sourcePath = tmpDir / fileName

            dsBackend = newFileSystemBackend(sourcePath)

            if self.setOverlapSimpleFlag:
                assert hasattr(dsBackend, "setOverlapSimpleFlag")
                dsBackend.setOverlapSimpleFlag = True

            async with aclosing(dsBackend):
                await copyFont(self.input, dsBackend, continueOnError=continueOnError)

            if isVariable:
                addInstances(sourcePath)
            addGlyphOrder(sourcePath)

            extraArguments = []
            for option, value in self.options.items():
                extraArguments.append(f"--{option}")
                if value:
                    extraArguments.append(value)

            self.compileFromDesignspace(sourcePath, outputFontPath, extraArguments)

    def compileFromDesignspace(self, sourcePath, outputFontPath, extraArguments):
        isVariable = sourcePath.suffix == ".designspace"
        outputType = (
            ("variable-cff2" if isVariable else "otf")
            if outputFontPath.suffix.lower() != ".ttf"
            else ("variable" if isVariable else "ttf")
        )
        arguments = [
            "-u" if sourcePath.suffix == ".ufo" else "-m",
            os.fspath(sourcePath),
            "-o",
            outputType,
            "--output-path",
            os.fspath(outputFontPath),
        ]

        fontmake_main(arguments + extraArguments)


def addInstances(designspacePath):
    dsDoc = DesignSpaceDocument.fromfile(designspacePath)
    if dsDoc.instances:
        # There are instances
        return

    # We will make up instances based on the axis value labels

    sortOrder = {
        "wght": 0,
        "wdth": 1,
        "ital": 2,
        "slnt": 3,
    }
    axes = sorted(dsDoc.axes, key=lambda axis: sortOrder.get(axis.tag, 100))

    elidedFallbackName = dsDoc.elidedFallbackName or "Regular"
    dsDoc.elidedFallbackName = elidedFallbackName

    axisLabels = [
        [
            (axis.name, label.name if not label.elidable else None, label.userValue)
            for label in axis.axisLabels
        ]
        for axis in axes
    ]

    axesByName = {axis.name: axis for axis in dsDoc.axes}

    for items in itertools.product(*axisLabels):
        location = {name: value for (name, valueLabel, value) in items}
        nameParts = [valueLabel for (name, valueLabel, value) in items if valueLabel]
        if not nameParts:
            nameParts = [elidedFallbackName]
        styleName = " ".join(nameParts)

        # TODO: styleName seems to be ignored, and the instance names are derived
        # from axis labels elsewhere. Figure out where this happens.
        location = mapLocationForward(location, axesByName)
        dsDoc.addInstanceDescriptor(
            familyName="Testing", styleName=styleName, location=location
        )

    dsDoc.write(designspacePath)


def mapLocationForward(location, axes):
    return {name: axes[name].map_forward(value) for name, value in location.items()}


def addGlyphOrder(designspacePath):
    backend = getFileSystemBackend(designspacePath)
    dsDoc = backend.dsDoc
    defaultSource = dsDoc.findDefault()
    ufo = UFOReaderWriter(defaultSource.path)
    lib = ufo.readLib()
    if "public.glyphOrder" not in lib:
        glyphSet = ufo.getGlyphSet()
        lib["public.glyphOrder"] = sorted(glyphSet.keys())
        ufo.writeLib(lib)
