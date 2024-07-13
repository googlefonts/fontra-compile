from __future__ import annotations

import pathlib
import random
import subprocess
from dataclasses import dataclass, replace

import pytest
import yaml
from fontra.core.classes import VariableGlyph
from fontra.workflow.actions import registerFilterAction
from fontra.workflow.actions.base import BaseFilter
from fontra.workflow.workflow import Workflow
from test_compile import cleanupTTX

testDir = pathlib.Path(__file__).resolve().parent
dataDir = testDir / "data"


@registerFilterAction("randomize-glyph-source-order")
@dataclass(kw_only=True)
class RandomizeGlyphSourceOrder(BaseFilter):
    async def processGlyph(self, glyph: VariableGlyph) -> VariableGlyph:
        newSources = list(glyph.sources)
        random.shuffle(newSources)
        return replace(glyph, sources=newSources)


testData = [
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- output: compile-varc
  destination: "output1.ttf"
""",
        "MutatorSans.ttf.ttx",
    ),
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- output: compile-varc
  destination: "output1.otf"
""",
        "MutatorSans.otf.ttx",
    ),
    (
        """
# This test confirms that the order of sources in glyphs is irrelevant.
# In other words, check that we produce the same output regardless of glyph
# source order.
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- filter: randomize-glyph-source-order
- output: compile-varc
  destination: "output-random-source-order.otf"
""",
        "MutatorSans.otf.ttx",
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
- output: compile-fontmake
  options:
    flatten-components:  # no value
  destination: "output-fontmake.otf"
""",
        "MutatorSans-fontmake-cff2.ttx",
    ),
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- filter: subset-axes
  axisNames: []
- output: compile-fontmake
  options:
    flatten-components:  # no value
  destination: "output-fontmake-static.ttf"
""",
        "MutatorSans-fontmake-static.ttx",
    ),
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- filter: subset-axes
  axisNames: []
- output: compile-fontmake
  options:
    flatten-components:  # no value
  destination: "output-fontmake-static.otf"
""",
        "MutatorSans-fontmake-static-cff.ttx",
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
    (
        """
steps:
- input: fontra-read
  source: "tests/data/MutatorSans.fontra"
- filter: subset-axes
  axisNames: []
- output: compile-fontc
  destination: "output-fontc-static.ttf"
""",
        "MutatorSans-fontc-static.ttx",
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
