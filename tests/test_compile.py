import pathlib
import re
import subprocess

ignorePatterns = [
    (r"<(checkSumAdjustment|created|modified) value=\"([^\"]+)\"/>", "--------"),
    (r"ttLibVersion=\"([^\"]+)\"", 'ttLibVersion="---"'),
]


def cleanupTTX(ttx):
    for ignorePattern, replaceString in ignorePatterns:
        ttx = re.sub(ignorePattern, replaceString, ttx)
    return ttx


testDir = pathlib.Path(__file__).resolve().parent
rcjkPath = testDir / "data" / "figArnaud.rcjk"
ttxPath = testDir / "data" / "figArnaud.ttx"


def test_main(tmpdir):
    tmpdir = pathlib.Path(tmpdir)
    outPath = tmpdir / "test.ttf"
    outTTXPath = tmpdir / "test.ttx"
    subprocess.run(["fontra-compile", rcjkPath, outPath], check=True)
    subprocess.run(["ttx", outPath], check=True)
    ttxLines = cleanupTTX(outTTXPath.read_text())
    expectedLines = cleanupTTX(ttxPath.read_text())
    assert expectedLines == ttxLines
