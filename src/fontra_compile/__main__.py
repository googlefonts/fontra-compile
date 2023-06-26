import argparse
import asyncio
import pathlib
from importlib.metadata import entry_points

from .builder import Builder


async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_font")
    parser.add_argument("output_font")
    parser.add_argument("--glyph-names")

    args = parser.parse_args()
    sourceFontPath = pathlib.Path(args.source_font).resolve()
    outputFontPath = pathlib.Path(args.output_font).resolve()
    glyphNames = (
        args.glyph_names.replace(",", " ").split() if args.glyph_names else None
    )

    fileType = sourceFontPath.suffix.lstrip(".").lower()
    backendEntryPoints = entry_points(group="fontra.filesystem.backends")
    entryPoint = backendEntryPoints[fileType]
    backendClass = entryPoint.load()
    reader = backendClass.fromPath(sourceFontPath)
    builder = Builder(reader, glyphNames)
    await builder.setup()
    ttFont = await builder.build()
    ttFont.save(outputFontPath)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
