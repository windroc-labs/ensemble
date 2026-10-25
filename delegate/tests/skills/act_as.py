"""delegate/skills/act-as/SKILL.md's step commands, as Claude Code runs them."""

import os
import re
import shlex

from delegate.tests.case import REPO

SKILL_DIR = os.path.join(REPO, "delegate", "skills", "act-as")


def text() -> str:
    with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def command(step: int) -> str:
    line = re.search(rf"^{step}\. .*$", text(), re.M)[0]
    return re.search(r"`((?:env|ensemble-delegate) [^`]+)`", line)[1]


def head() -> str:
    """Step 3's command before its options."""
    return command(3).partition(" prompt ")[0]


def prefix() -> str:
    """
    What step 4 puts before each command of the prompt that runs a team tool.
    """
    return command(4)


def argv(command: str) -> list[str]:
    """command as Bash splits it once Claude Code substitutes ${CLAUDE_SKILL_DIR}."""
    return shlex.split(command.replace("${CLAUDE_SKILL_DIR}", SKILL_DIR))
