"""A local issue file's text read into an IssueFile: front matter, then the Description, Comments and
History sections; and the text edits a board write makes, each keeping every other line."""

import json
import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import quote

from ensemble.orchestrator.engine.flow.types import ISSUE_ID
from ensemble.orchestrator.engine.types import TrackerFileError
from ensemble.orchestrator.trackers.local.types import (
    Entry,
    FileLink,
    IssueFile,
    LocalState,
    Note,
    Source,
)


class _Key(StrEnum):
    ID = "id"
    TITLE = "title"
    TASK = "task"
    CLIENT = "client"
    PRIORITY = "priority"
    CREATED = "created"
    BLOCKED_BY = "blocked_by"
    RELATED = "related"
    PARENT = "parent"
    LINKS = "links"


class _Section(StrEnum):
    DESCRIPTION = "Description"
    COMMENTS = "Comments"
    HISTORY = "History"


_ORDER = list(_Section)


_FRONT = re.compile(r"([a-z_]+):(.*)")
_LINK_TITLE = re.compile(r"  - title:(.*)")
_LINK_URL = re.compile(r"    url:(.*)")
_SECTION = re.compile(r"#{3} (Description|Comments|History)")
_TIME = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
_SOURCE = "(" + "|".join(Source) + ")"
_HEADER = re.compile(rf"#{{4}} {_SOURCE} · {_TIME}(?: <!-- id=\S+ key=(\S+) -->)?")
_HEADED = re.compile(rf"#{{4}} {_SOURCE} · ")  # header-shaped; other #### lines are body text
_BARE = "###"
_HISTORY = re.compile(rf"- {_TIME} (\w+) {_SOURCE}")


class _Bad(Exception):
    """A reason this file cannot be read."""


def parse(
    path: str, text: str, *, assignee: str | None, state: LocalState | None, mtime: datetime
) -> IssueFile:
    """assignee None: archived, its located state its last History entry's; naive times are local."""
    try:
        return _parse(path, text, assignee, state, mtime)
    except _Bad as bad:
        raise TrackerFileError(path, str(bad)) from None


def _parse(path, text, assignee, state, mtime):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _Bad("no front matter")
    if "---" not in lines[1:]:
        raise _Bad("front matter has no closing '---'")
    end = lines.index("---", 1)
    front = _front(lines[1:end])
    sections = _sections(lines[end + 1 :])
    history = tuple(
        sorted(map(_entry, _filled(sections.get(_Section.HISTORY, []))), key=lambda e: e.at)
    )
    if assignee is None:
        state = history[-1].state if history else None
    return IssueFile(
        path=path,
        assignee=assignee,
        state=state,
        mtime=mtime,
        id=_id(_Key.ID, _required(front, _Key.ID)),
        title=_required(front, _Key.TITLE),
        task=_scalar(front.get(_Key.TASK, "")),
        client=_scalar(front.get(_Key.CLIENT, "")),
        priority=_priority(front.get(_Key.PRIORITY, "")),
        created=_created(_required(front, _Key.CREATED)),
        blocked_by=_ids(_Key.BLOCKED_BY, front.get(_Key.BLOCKED_BY, "")),
        related=_ids(_Key.RELATED, front.get(_Key.RELATED, "")),
        parent=_optional_id(front.get(_Key.PARENT, "")),
        links=front.get(_Key.LINKS, ()),
        description="\n".join(sections.get(_Section.DESCRIPTION, [])).strip(),
        comments=_comments(sections.get(_Section.COMMENTS, []), mtime),
        history=history,
    )


def _front(lines):
    front, i = {}, 0
    while i < len(lines):
        line, i = lines[i], i + 1
        if not line.strip():
            continue
        match = _FRONT.fullmatch(line)
        if not match:
            raise _Bad(f"bad front-matter line {line!r}")
        key, value = match[1], match[2].strip()
        if key not in set(_Key):
            raise _Bad(f"unknown key {key!r}")
        if key in front:
            raise _Bad(f"duplicate key {key!r}")
        front[key] = value
        if key == _Key.LINKS:
            front[key], i = _links(value, lines, i)
    return front


def _links(value, lines, i):
    if value:
        raise _Bad(f"bad links value {value!r}")
    links = []
    while i < len(lines) and lines[i].startswith(" "):
        title = _LINK_TITLE.fullmatch(lines[i])
        if not title:
            raise _Bad(f"bad links line {lines[i]!r}")
        line = lines[i + 1] if i + 1 < len(lines) else ""
        found = _LINK_URL.fullmatch(line)
        url = found and _scalar(found[1].strip())
        if not url:
            raise _Bad(f"bad links line {line or lines[i]!r}")
        links.append(FileLink(_scalar(title[1].strip()) or "", url))
        i += 2
    return tuple(links), i


def _scalar(value):
    if not value:
        return None
    if not value.startswith('"'):
        return value
    try:
        loaded = json.loads(value)
    except ValueError:
        loaded = None
    if not isinstance(loaded, str):
        raise _Bad(f"bad string {value!r}")
    return loaded or None


def _required(front, key):
    value = _scalar(front.get(key, ""))
    if not value:
        raise _Bad(f"missing {key}")
    return value


def _id(key, value):
    if not re.fullmatch(ISSUE_ID, value):
        raise _Bad(f"bad {key} {value!r}")
    return value


def _optional_id(value):
    value = _scalar(value)
    return value and _id(_Key.PARENT, value)


def _ids(key, value):
    if not value:
        return ()
    if not (value.startswith("[") and value.endswith("]")):
        raise _Bad(f"bad list {key}: {value!r}")
    items = [item.strip() for item in value[1:-1].split(",")]
    if items == [""]:
        return ()
    return tuple(_id(f"{key} id", item) for item in items)


def _priority(value):
    value = _scalar(value)
    if value is None:
        return 0
    if not re.fullmatch(r"[0-4]", value):
        raise _Bad(f"bad priority {value!r}")
    return int(value)


def _created(value):
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        created = None
    if created is None or created.tzinfo is None:
        raise _Bad(f"bad created {value!r}: want ISO 8601 with an offset or Z")
    return created


def _sections(lines):
    sections, current = {}, None
    for line in lines:
        match = _SECTION.fullmatch(line)
        if not match:
            if current is None and line.strip():
                raise _Bad(f"text before the first section: {line!r}")
            if current is not None:
                sections[current].append(line)
            continue
        section = _Section(match[1])
        if section in sections:
            raise _Bad(f"duplicate section {section.value!r}")
        later = [s for s in sections if _ORDER.index(s) > _ORDER.index(section)]
        if later:
            raise _Bad(f"section {section.value!r} after {later[0].value!r}")
        sections[section], current = [], section
    return sections


def _local(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone()


def _comments(lines, mtime):
    notes = []
    for line in lines:
        if line == _BARE:
            notes.append([minute(mtime), Source.OTHER, [], None])
        elif _HEADED.match(line) or (not notes and line.strip()):
            header = _HEADER.fullmatch(line)
            try:
                at = _local(header[2]) if header else None
            except ValueError:
                at = None
            if not header or not at:
                raise _Bad(f"bad comment header {line!r}")
            notes.append([at, Source(header[1]), [], header[3]])
        elif notes:
            notes[-1][2].append(line)
    return tuple(Note(at, source, "\n".join(body).strip(), key) for at, source, body, key in notes)


def _filled(lines):
    return [line for line in lines if line.strip()]


def _entry(line):
    match = _HISTORY.fullmatch(line)
    try:
        if match:
            return Entry(_local(match[1]), LocalState(match[2]), Source(match[3]))
    except ValueError:
        pass
    raise _Bad(f"bad History line {line!r}")


def ref_key(ref: tuple[str, ...]) -> str:
    """A ref as a comment header's key: no whitespace, never `-->`."""
    return quote(":".join(ref), safe=":/")


def stamp(text: str, *, mtime: datetime, state: LocalState | None) -> str:
    """A person's edits fixed at mtime: bare `###` comments get their header, and a directory state
    (None: archived) other than the last History line's its `other` move."""
    lines, end = _split(text)
    spans = _spans(lines, end)
    for i in range(*spans.get(_Section.COMMENTS, (0, 0))):
        if lines[i] == _BARE:
            lines[i] = f"#### {Source.OTHER} · {_minute(mtime)}"
    text = _join(lines)
    if state is None:
        return text
    # the line written last, not the latest by time: a future-dated one would never be passed
    entries = [_entry(line) for line in _filled(lines[slice(*spans.get(_Section.HISTORY, (0, 0)))])]
    if entries and entries[-1].state == state:
        return text
    return add_history(text, at=mtime, state=state, source=Source.OTHER)


def add_comment(
    text: str, *, at: datetime, body: str, ref_key: str | None = None, source: Source = Source.SELF
) -> str:
    """A comment, last; with a ref key its hidden id (c<n>, the next) and key."""
    lines, end = _split(text)
    ids = [int(m[1]) for line in lines[end:] if (m := _ID.search(line))]
    header = _header(source, at, ref_key, max(ids, default=0) + 1)
    return _join(_append(lines, end, _Section.COMMENTS, ["", header, *_body(body)]))


def _header(source, at, ref_key, n):
    header = f"#### {source} · {_minute(at)}"
    return header if ref_key is None else f"{header} <!-- id=c{n} key={ref_key} -->"


def edit_comment(text: str, ref_key: str, body: str) -> str | None:
    """The engine's comment with ref_key holding body, the blank lines after it kept; None: there
    is none."""
    lines, end = _split(text)
    if found := _comment(lines, end, ref_key):
        i, after = found
        filled = max((j for j in range(i + 1, after) if lines[j].strip()), default=i)
        return _join([*lines[: i + 1], *_body(body), *lines[filled + 1 :]])
    return None


def drop_comment(text: str, ref_key: str) -> str | None:
    """Without the engine's comment with ref_key; None: there is none."""
    lines, end = _split(text)
    if found := _comment(lines, end, ref_key):
        return _join([*lines[: found[0]], *lines[found[1] :]])
    return None


def _comment(lines, end, ref_key):
    """The engine's comment ref_key: its header index, then the next comment's or section's."""
    first, last = _spans(lines, end).get(_Section.COMMENTS, (0, 0))
    for i in range(first, last):
        header = _HEADER.fullmatch(lines[i])
        if header and header[1] == Source.SELF and header[3] == ref_key:
            after = next(
                (j for j in range(i + 1, last) if lines[j] == _BARE or _HEADED.match(lines[j])),
                last,
            )
            return i, after
    return None


def add_history(text: str, *, at: datetime, state: LocalState, source: Source = Source.SELF) -> str:
    lines, end = _split(text)
    return _join(_append(lines, end, _Section.HISTORY, [f"- {_minute(at)} {state} {source}"]))


def set_title(text: str, title: str) -> str:
    lines, end = _split(text)
    i = next(i for i in range(1, end) if lines[i].startswith(f"{_Key.TITLE}:"))
    lines[i] = f"{_Key.TITLE}: {json.dumps(title)}"
    return _join(lines)


def set_description(text: str, body: str) -> str:
    """Description holding body, the blank lines around it kept; the section made if absent."""
    lines, end = _split(text)
    if _Section.DESCRIPTION not in (spans := _spans(lines, end)):
        return _join(_append(lines, end, _Section.DESCRIPTION, _body(body)))
    first, after = spans[_Section.DESCRIPTION]
    filled = [i for i in range(first, after) if lines[i].strip()]
    start, stop = (filled[0], filled[-1] + 1) if filled else (first, first)
    return _join([*lines[:start], *_body(body), *lines[stop:]])


def set_front(
    text: str,
    key: Literal["task", "client", "priority", "blocked_by"],
    value: str | int | Sequence[str] | None,
) -> str:
    """key's line replaced in place or added last, a str as JSON (one line); a task or client None
    or empty drops it."""
    lines, end = _split(text)
    line = [] if value is None or value == "" else [f"{key}: {_written(value)}"]
    i = next((i for i in range(1, end) if lines[i].startswith(f"{key}:")), None)
    if i is None:
        return _join([*lines[:end], *line, *lines[end:]])
    return _join([*lines[:i], *line, *lines[i + 1 :]])


def _written(value):
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    return f"[{', '.join(value)}]"


def add_related(text: str, ident: str) -> str:
    lines, end = _split(text)
    key = next((i for i in range(1, end) if lines[i].startswith(f"{_Key.RELATED}:")), None)
    if key is None:
        return _join([*lines[:end], f"{_Key.RELATED}: [{ident}]", *lines[end:]])
    listed = _ids(_Key.RELATED, lines[key][len(f"{_Key.RELATED}:") :].strip())
    lines[key] = f"{_Key.RELATED}: [{', '.join([*listed, ident])}]"
    return _join(lines)


def new(
    *,
    ident: str,
    title: str,
    priority: int,
    created: datetime,
    parent: str | None,
    description: str,
    state: LocalState,
    note: str | None = None,
    note_at: datetime | None = None,
    note_key: str | None = None,
    task: str | None = None,
    client: str | None = None,
    blocked_by: Sequence[str] = (),
    source: Source = Source.SELF,
) -> str:
    """With a note, its first comment the engine's, keyed note_key; one History line by source;
    both at note_at, else created."""
    at = note_at or created
    front = [
        f"{_Key.ID}: {ident}",
        f"{_Key.TITLE}: {json.dumps(title)}",
        *([f"{_Key.TASK}: {json.dumps(task)}"] if task else []),
        *([f"{_Key.CLIENT}: {json.dumps(client)}"] if client else []),
        f"{_Key.PRIORITY}: {priority}",
        f"{_Key.CREATED}: {created.isoformat()}",
        *([f"{_Key.BLOCKED_BY}: {_written(blocked_by)}"] if blocked_by else []),
        *([f"{_Key.PARENT}: {parent}"] if parent else []),
    ]
    comments = (
        []
        if note is None
        else ["", f"### {_Section.COMMENTS.value}", "", _header(Source.SELF, at, note_key, 1)]
        + _body(note)
    )
    return _join(
        [
            "---",
            *front,
            "---",
            "",
            f"### {_Section.DESCRIPTION.value}",
            *_body(description),
            *comments,
            "",
            f"### {_Section.HISTORY.value}",
            f"- {_minute(at)} {state} {source}",
        ]
    )


def add_link(text: str, *, title: str, url: str) -> str:
    lines, end = _split(text)
    pair = [f"  - title: {json.dumps(title)}", f"    url: {json.dumps(url)}"]
    key = next((i for i in range(1, end) if lines[i].startswith(f"{_Key.LINKS}:")), None)
    if key is None:
        return _join([*lines[:end], f"{_Key.LINKS}:", *pair, *lines[end:]])
    after = next((i for i in range(key + 1, end) if not lines[i].startswith(" ")), end)
    return _join([*lines[:after], *pair, *lines[after:]])


_ID = re.compile(r"^#{4} .* <!-- id=c(\d+) key=\S+ -->$")


def minute(at: datetime) -> datetime:
    """at as a stamp records it: through its History text."""
    return _local(_minute(at))


def _minute(at):
    return at.astimezone().strftime("%Y-%m-%d %H:%M")


def _split(text):
    lines = text.splitlines()
    return lines, lines.index("---", 1)


def _join(lines):
    return "\n".join(lines) + "\n"


def _spans(lines, end):
    starts = [
        (i, _Section(m[1]))
        for i in range(end + 1, len(lines))
        if (m := _SECTION.fullmatch(lines[i]))
    ]
    ends = [i for i, _ in starts[1:]] + [len(lines)] * bool(starts)
    return {section: (i + 1, after) for (i, section), after in zip(starts, ends, strict=True)}


def _append(lines, end, section, block):
    """block after the section's last filled line, the section made (in _ORDER) if absent; a block
    led by a blank line is one apart from the next section too."""
    spans = _spans(lines, end)
    if section not in spans:
        later = [spans[s][0] - 1 for s in _ORDER[_ORDER.index(section) + 1 :] if s in spans]
        at = later[0] if later else len(lines)
        gap = [""] if at < len(lines) and block[:1] == [""] else []
        return [*lines[:at], f"### {section.value}", *block, *gap, *lines[at:]]
    first, after = spans[section]
    last = max((i for i in range(first, after) if lines[i].strip()), default=first - 1)
    return [*lines[: last + 1], *block, *lines[last + 1 :]]


def _body(body):
    """body's lines as the parser splits them, those it would read as structure indented a space."""
    return [
        f" {line}" if line == _BARE or _SECTION.fullmatch(line) or _HEADED.match(line) else line
        for line in body.strip().splitlines()
    ]
