import pathlib
import subprocess

import pytest
import yaml
from fontra.workflow.workflow import Workflow
from test_compile import cleanupTTX

testDir = pathlib.Path(__file__).resolve().parent
dataDir = testDir / "data"


testData = [
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- output: compile-varc
  destination: "output1.ttf"
""",
        "MutatorSans.ttx",
    ),
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- output: compile-fontmake
  options:
    flatten-components:  # no value
  destination: "output-fontmake.ttf"
""",
        "MutatorSans-fontmake.ttx",
    ),
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- output: compile-fontc
  destination: "output-fontc.ttf"
""",
        "MutatorSans-fontc.ttx",
    ),
]


@pytest.mark.parametrize("workflowSource, ttxFileName", testData)
async def test_workflow(tmpdir, workflowSource, ttxFileName):
    tmpdir = pathlib.Path(tmpdir)
    config = yaml.safe_load(workflowSource)

    workflow = Workflow(config=config)

    async with workflow.endPoints() as endPoints:
        assert endPoints.endPoint is not None

        for output in endPoints.outputs:
            await output.process(tmpdir)
            ttxPath = dataDir / ttxFileName
            outPath = tmpdir / output.destination
            assert outPath.exists(), outPath
            outTTXPath = tmpdir / (outPath.stem + ".ttx")
            subprocess.run(["ttx", "-o", outTTXPath, outPath], check=True)

            # # Write expected
            # ttxPath.write_text(outTTXPath.read_text())

            ttxLines = cleanupTTX(outTTXPath.read_text())
            expectedLines = cleanupTTX(ttxPath.read_text())
            assert expectedLines == ttxLines, outTTXPath
