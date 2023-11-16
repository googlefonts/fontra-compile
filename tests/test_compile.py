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


@pytest.mark.parametrize("sourceName", ["figArnaud.rcjk"])
def test_main(tmpdir, sourceName):
    tmpdir = pathlib.Path(tmpdir)
    sourcePath = dataDir / sourceName
    ttxPath = dataDir / (sourcePath.stem + ".ttx")
    outPath = tmpdir / (sourcePath.stem + ".ttf")
    outTTXPath = tmpdir / (sourcePath.stem + ".ttx")
    subprocess.run(["fontra-compile", sourcePath, outPath], check=True)
    subprocess.run(["ttx", outPath], check=True)
    ttxLines = cleanupTTX(outTTXPath.read_text())
    expectedLines = cleanupTTX(ttxPath.read_text())
    assert expectedLines == ttxLines
