import os
import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import OutputActionProtocol, registerActionClass

from .builder import Builder


@registerActionClass("fontra-compile")
@dataclass(kw_only=True)
class FontraCompileAction:
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
        builder = Builder(self.input)
        await builder.setup()
        ttFont = await builder.build()
        ttFont.save(outputFontPath)
