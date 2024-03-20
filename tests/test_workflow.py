import pathlib
import subprocess

import yaml
from fontra.workflow.workflow import Workflow
from test_compile import cleanupTTX

testDir = pathlib.Path(__file__).resolve().parent
dataDir = testDir / "data"


testWorkFlow = """
steps:
- action: input
  source: "tests/data/MutatorSans.fontra"
- action: compile-varc
  destination: "output1.ttf"
"""


async def test_workflow(tmpdir):
    tmpdir = pathlib.Path(tmpdir)
    config = yaml.safe_load(testWorkFlow)

    workflow = Workflow(config=config)

    async with workflow.endPoints() as endPoints:
        assert endPoints.endPoint is not None

        for output in endPoints.outputs:
            await output.process(tmpdir)
            ttxPath = dataDir / "MutatorSans.ttx"
            outPath = tmpdir / output.destination
            outTTXPath = tmpdir / (outPath.stem + ".ttx")
            subprocess.run(["ttx", outPath], check=True)

            ttxLines = cleanupTTX(outTTXPath.read_text())
            expectedLines = cleanupTTX(ttxPath.read_text())
            assert expectedLines == ttxLines
