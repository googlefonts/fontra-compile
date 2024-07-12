import pathlib
import re
import subprocess

import pytest

ignorePatterns = [
    (r"<(checkSumAdjustment|created|modified) value=\"([^\"]+)\"/>", "--------"),
    (r"ttLibVersion=\"([^\"]+)\"", 'ttLibVersion="---"'),
]


def cleanupTTX(ttx):
    for ignorePattern, replaceString in ignorePatterns:
        ttx = re.sub(ignorePattern, replaceString, ttx)
    return ttx


testDir = pathlib.Path(__file__).resolve().parent
dataDir = testDir / "data"


@pytest.mark.parametrize("sourceName", ["figArnaud.rcjk", "MutatorSans.fontra"])
@pytest.mark.parametrize("outSuffix", [".ttf"])
def test_main(tmpdir, sourceName, outSuffix):
    tmpdir = pathlib.Path(tmpdir)
    sourcePath = dataDir / sourceName
    outFileName = sourcePath.stem + outSuffix
    ttxFileName = outFileName + ".ttx"
    ttxPath = dataDir / ttxFileName
    outPath = tmpdir / outFileName
    outTTXPath = tmpdir / ttxFileName
    subprocess.run(["fontra-compile", sourcePath, outPath], check=True)
    subprocess.run(["ttx", outPath, "-o", outTTXPath], check=True)

    # # Write expected
    # ttxPath.write_text(outTTXPath.read_text())

    ttxLines = cleanupTTX(outTTXPath.read_text())
    expectedLines = cleanupTTX(ttxPath.read_text())
    assert expectedLines == ttxLines
