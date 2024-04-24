import itertools
import os
import pathlib
import subprocess
import tempfile
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fontra.backends import newFileSystemBackend
from fontra.backends.copy import copyFont
from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import OutputActionProtocol, registerActionClass
from fontTools.designspaceLib import DesignSpaceDocument


@registerActionClass("compile-fontmake")
@dataclass(kw_only=True)
class CompileFontMakeAction:
    destination: str
    options: dict[str, str] = field(default_factory=dict)
    input: ReadableFontBackend | None = field(init=False, default=None)

    @asynccontextmanager
    async def connect(
        self, input: ReadableFontBackend
    ) -> AsyncGenerator[ReadableFontBackend | OutputActionProtocol, None]:
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

        with tempfile.TemporaryDirectory() as tmpDirName:
            tmpDir = pathlib.Path(tmpDirName)

            designspacePath = tmpDir / "temp.designspace"

            dsBackend = newFileSystemBackend(designspacePath)

            async with aclosing(dsBackend):
                await copyFont(self.input, dsBackend, continueOnError=continueOnError)

            addInstances(designspacePath)

            command = [
                "fontmake",
                "-m",
                os.fspath(designspacePath),
                "-o",
                "variable",
                "--output-path",
                os.fspath(outputFontPath),
            ]

            for option, value in self.options.items():
                command.append(f"--{option}")
                if value:
                    command.append(value)

            subprocess.run(command, check=True)


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

    for items in itertools.product(*axisLabels):
        location = {name: value for (name, valueLabel, value) in items}
        nameParts = [valueLabel for (name, valueLabel, value) in items if valueLabel]
        if not nameParts:
            nameParts = [elidedFallbackName]
        styleName = " ".join(nameParts)
        dsDoc.addInstanceDescriptor(styleName=styleName, userLocation=location)

    dsDoc.write(designspacePath)
