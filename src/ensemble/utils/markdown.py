"""Markdown code fences: 3+ backticks or tildes, closed by a line of only that char, as many or
more; an unclosed one runs to the end.
"""

import re

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _scan(text: str) -> tuple[list[tuple[int, str]], list[tuple[str, str]]]:
    outside, blocks, fence, info, body = [], [], None, "", []
    for i, line in enumerate(text.split("\n")):
        shut = line.strip()
        if fence:
            if len(shut) >= fence[1] and shut == fence[0] * len(shut):
                blocks.append((info, "\n".join(body)))
                fence = None
            else:
                body.append(line)
        elif m := FENCE.match(line):
            fence, info, body = (m[1][0], len(m[1])), line[m.end() :].strip(), []
        else:
            outside.append((i, line))
    if fence:
        blocks.append((info, "\n".join(body)))
    return outside, blocks


def unfenced(text: str) -> list[tuple[int, str]]:
    """(index, line) of each line outside fences."""
    return _scan(text)[0]


def fenced(text: str) -> list[tuple[str, str]]:
    """(info string, body) of each fenced block."""
    return _scan(text)[1]
