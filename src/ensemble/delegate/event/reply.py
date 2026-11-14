"""The reply protocol's progress mark tag (PROGRESS); a reply's progress mark lines outside code
fences (marks) and the reply as shown (outcome_only).
"""

import re

from ensemble.utils import OUTCOME, outcome_block, unfenced

PROGRESS = "ensemble-progress"
MARK = re.compile(rf"^\s*(?:[-*] )?\[{PROGRESS}:(?P<name>[^\]\s]*)\](?P<text>.*)$")
NAME = re.compile(r"[\w-]+")
TICKS = re.compile(r"`+")


def marks(text: str) -> list[tuple[str, str]]:
    """(name, text) of each mark line, whitespace collapsed; one with a bad name or no text is
    skipped.
    """
    out = []
    for _, line in unfenced(text):
        if (
            (m := MARK.match(line))
            and NAME.fullmatch(m["name"])
            and (said := " ".join(m["text"].split()))
        ):
            out.append((m["name"], said))
    return out


def outcome_only(text: str) -> str:
    """The last OUTCOME block alone, fenced so outcome_block reads it back; text without one as is."""
    if (body := outcome_block(text)) is None:
        return text
    fence = "`" * max([3, *(len(run) + 1 for run in TICKS.findall(body))])
    return f"{fence}{OUTCOME}\n{body}\n{fence}"
