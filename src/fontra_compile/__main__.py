import argparse
import asyncio
import pathlib

from fontra.backends import getFileSystemBackend

from .builder import Builder


async def main_async() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_font")
    parser.add_argument("output_font")
    parser.add_argument(
        "--glyph-names",
        help="Comma- or space-delimited list of glyph names to include. "
        "When not given, include all glyphs.",
    )

    args = parser.parse_args()
    sourceFontPath = pathlib.Path(args.source_font).resolve()
    outputFontPath = pathlib.Path(args.output_font).resolve()
    glyphNames = (
        args.glyph_names.replace(",", " ").split() if args.glyph_names else None
    )

    reader = getFileSystemBackend(sourceFontPath)
    builder = Builder(
        reader=reader,
        requestedGlyphNames=glyphNames,
        buildCFF2=outputFontPath.suffix.lower() == ".otf",
    )
    await builder.setup()
    ttFont = await builder.build()
    ttFont.save(outputFontPath)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
