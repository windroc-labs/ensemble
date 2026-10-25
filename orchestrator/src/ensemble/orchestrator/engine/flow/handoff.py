"""The one Handoff grammar."""

import re

from ensemble.orchestrator.engine.flow.types import ISSUE_ID, Handoff
from ensemble.utils import one_line

HANDOFF = re.compile(rf"Handoff from ({ISSUE_ID})(?:: \S+)?")


SECTIONS = ("## Source", "## Instructions", "## Comments")


COMMENTS = re.compile(rf"^{re.escape(SECTIONS[2])}\r?$", re.M)


def _lines(description):
    return re.split(r"\r?\n", description or "")


def _starts(lines):
    # Unquoted user text only under Instructions: Source = first header if before Instructions'
    # first, Instructions = first after it, Comments = last after Instructions.
    source, instructions, comments = SECTIONS

    def find(header, after, *, last=False):
        found = [i for i, line in enumerate(lines) if line == header and i > after]
        return (found[-1] if last else found[0]) if found else None

    src, ins = find(source, 0), find(instructions, 0)
    if src is not None and ins is not None and src > ins:
        src = None
    if src is not None:
        ins = find(instructions, src)
    starts = {
        source: src,
        instructions: ins,
        comments: find(
            comments, max((s for s in (src, ins) if s is not None), default=0), last=True
        ),
    }
    return {h: i for h, i in starts.items() if i is not None}


def parse_handoff(description) -> Handoff | None:
    lines = _lines(description)
    m = HANDOFF.fullmatch(lines[0])
    if not m:
        return None
    starts = _starts(lines)

    def body(header):
        if header not in starts:
            return ""
        end = min((s for s in starts.values() if s > starts[header]), default=len(lines))
        return "\n".join(lines[starts[header] + 1 : end]).strip()

    return Handoff(m.group(1), body(SECTIONS[1]), body(SECTIONS[2]))


def head(description) -> str:
    text = description or ""
    lines = _lines(text)
    if not HANDOFF.fullmatch(lines[0]):
        m = COMMENTS.search(text)
        return text[: m.start()] if m else text
    i = _starts(lines).get(SECTIONS[2])
    return text if i is None else text[: list(re.finditer(r"\r?\n", text))[i - 1].end()]


def handoff(identifier, url, *, sources, instructions, comments) -> str:
    """sources: (title, url); instructions, comments: (name, at, body)."""
    parts = [f"Handoff from {identifier}" + (f": {url}" if url else "")]
    source, said, quoted = SECTIONS
    if sources:
        parts.append(
            f"{source}\n" + "\n".join(f"- {one_line(t)}: {one_line(u)}" for t, u in sources)
        )
    if instructions:
        parts.append(
            f"{said}\n" + "\n\n".join(f"{n}, {at}:\n{body}" for n, at, body in instructions)
        )
    if comments:
        # Quoted: agent text cannot pose as a section.
        parts.append(
            f"{quoted}\n"
            + "\n".join(
                f"- {n}, {at}:\n" + "\n".join(f"  > {line}" for line in body.splitlines())
                for n, at, body in comments
            )
        )
    return "\n\n".join(parts)
