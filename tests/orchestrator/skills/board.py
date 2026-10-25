"""skills/board/SKILL.md as a commander reads it."""

import os
import re
import shlex

from tests.orchestrator.case import REPO

SKILL = os.path.join(REPO, "skills", "board", "SKILL.md")
RULES = re.compile(r"^  - Bash\((.+)\)$", re.M)
EXAMPLE = re.compile(r"ensemble-orchestrator issue [^`\n]*")
SHELL = ("<", ">", "|", "&&", ";")


def text() -> str:
    with open(SKILL, encoding="utf-8") as f:
        return f.read()


def front() -> str:
    return text().split("\n---\n", 1)[0]


def body() -> str:
    return text().split("\n---\n", 1)[1]


def rules() -> list[str]:
    return RULES.findall(front())


def examples() -> list[list[str]]:
    """Each ensemble-orchestrator issue command the body shows, as its argv after `issue`, up to any shell
    redirection or pipe."""
    found = []
    for line in EXAMPLE.findall(body()):
        argv = shlex.split(line.split("  #", 1)[0])[2:]
        cut = [i for i, word in enumerate(argv) if word in SHELL]
        found.append(argv[: cut[0]] if cut else argv)
    return found
