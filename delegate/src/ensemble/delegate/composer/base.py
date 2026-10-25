"""The Composer's paths, its error and the text helpers its other modules share."""

import os
import re
from collections.abc import Mapping

import ensemble.utils as utils
from ensemble.delegate.composer.types import ConfigError

TEAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "team")
OUTPUT = "output"
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")
METHOD = re.compile(r"<team>/methods/([\w-]+)\.md")
HEADING = re.compile(r"^(#{1,6}) (.+)$")
BOLD_ITEM = re.compile(r"^\s*(?:(?:[-*]|\d+\.) )?\*\*(.+?)\*\*")
ANCHOR_LINE = re.compile(r'<a id="([^"]+)"></a>')
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
FILE_REF = re.compile(r"`(?:<(tasks)>|<team>/(methods))/(<task>|[\w-]+)\.md`")
PAREN = re.compile(r"\(([^()]*)\)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)


def fill(text: str, values: Mapping, where: str) -> str:
    def value(m):
        if m.group(1) in values:
            return str(values[m.group(1)])
        return m.group(2) if m.group(2) is not None else m.group(0)

    out = PLACEHOLDER.sub(value, text)
    if m := PLACEHOLDER.search(out):
        raise ConfigError(f"unfilled placeholder {m.group(0)} in {where}")
    return out


def anchor(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def outline(text: str) -> list[tuple[int, tuple[str, ...], str, bool]]:
    out, stack, section, last = [], [], "", (-2, "")
    for i, line in utils.unfenced(text):
        if m := HEADING.match(line):
            level, title = len(m[1]), m[2].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            explicit = ANCHOR_LINE.fullmatch(last[1]) if last[0] == i - 1 else None
            section = explicit[1] if explicit else anchor(title)
            out.append((i, tuple(t for _, t in stack), section, True))
        elif m := BOLD_ITEM.match(line):
            out.append(
                (i, tuple(t for _, t in stack) + (re.sub(r"[:.]$", "", m[1]),), section, False)
            )
        last = (i, line)
    return out


def read_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip() + "\n"
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None


def title_of(text: str, rel: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel}: must start with a '# ' heading")
    return first[2:].strip()
