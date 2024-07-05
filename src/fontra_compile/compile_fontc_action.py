import os
import subprocess
from dataclasses import dataclass

from fontra.workflow.actions import registerOutputAction

from .compile_fontmake_action import CompileFontMakeAction


@registerOutputAction("compile-fontc")
@dataclass(kw_only=True)
class CompileFontCAction(CompileFontMakeAction):

    def compileFromDesignspace(
        self, designspacePath, outputFontPath, isVariable, extraArguments
    ):
        arguments = [
            "fontc",
            "--output-file",
            os.fspath(outputFontPath),
            os.fspath(designspacePath),
        ]

        subprocess.run(arguments + extraArguments, check=True)
