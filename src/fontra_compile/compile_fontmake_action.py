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


@registerActionClass("compile-fontmake")
@dataclass(kw_only=True)
class CompileFontMakeAction:
    destination: str
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

    async def process(self, outputDir: os.PathLike = pathlib.Path()) -> None:
        outputDir = pathlib.Path(outputDir)
        outputFontPath = outputDir / self.destination

        with tempfile.TemporaryDirectory() as tmpDirName:
            tmpDir = pathlib.Path(tmpDirName)

            designspacePath = tmpDir / "temp.designspace"

            dsBackend = newFileSystemBackend(designspacePath)

            async with aclosing(dsBackend):
                await copyFont(self.input, dsBackend)

            # assert 0, [p.name for p in tmpDir.iterdir()]
            command = [
                "fontmake",
                "-m",
                designspacePath,
                "-o",
                "variable",
                "--output-path",
                os.fspath(outputFontPath),
            ]
            subprocess.run(command, check=True)
