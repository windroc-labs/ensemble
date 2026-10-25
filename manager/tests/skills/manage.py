"""manager/skills/manage/SKILL.md's rules, by the `- ` bullet that holds a phrase."""

import re

from manager.tests.skills.commands import text


def rule(phrase: str) -> str:
    """The one bullet of /ensemble:manage that contains phrase."""
    [found] = [b for b in re.findall(r"^- .*$", text("manage"), re.M) if phrase in b]
    return found
