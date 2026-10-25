"""A local board's issue files, found by walking its layout (docs/board.md › Local board). Reads take
no lock: only writers do."""

import os
from datetime import datetime

from ensemble.orchestrator.engine.types import TrackerFileError
from ensemble.orchestrator.trackers.local import issue_file
from ensemble.orchestrator.trackers.local.types import IssueFile, LocalState

ARCHIVE = "archive"


def scan(board: str) -> tuple[IssueFile, ...]:
    """Every issue file on the board, archived ones included, in name order; ids are unique."""
    if not os.path.isdir(board):
        raise TrackerFileError(board, "no such board directory")
    found, assignees = [], False
    for top in _entries(board):
        if not _is_dir(top):
            _outside(top)
        elif top.name == ARCHIVE:
            found += _archive(top.path)
        else:
            assignees = True
            found += _assignee(top)
    if not assignees:
        raise TrackerFileError(board, "empty board: no assignee directory")
    seen = {}
    for each in found:
        if each.id in seen:
            raise TrackerFileError(each.path, f"duplicate id {each.id}, also in {seen[each.id]}")
        seen[each.id] = each.path
    return tuple(found)


def read_one(board: str, path: str, ident: str) -> IssueFile | None:
    """Issue `ident`, reread at `path` (where a scan last saw it) if it is still there, else found by
    a scan; None: not on the board."""
    place = _place(board, path)
    if place and os.path.isfile(path) and not os.path.islink(path):
        found = _read(path, *place)
        if found.id == ident:
            return found
    return next((each for each in scan(board) if each.id == ident), None)


def _place(board, path):
    """(assignee, state) of a path a scan returned; None if it has no such shape."""
    parts = os.path.relpath(path, board).split(os.sep)
    if len(parts) == 2 and parts[0] == ARCHIVE:
        return None, None
    if len(parts) == 3 and not parts[0].startswith(".") and parts[1] in set(LocalState):
        return parts[0], LocalState(parts[1])
    return None


def _entries(path):
    try:
        with os.scandir(path) as found:
            return sorted((e for e in found if not e.name.startswith(".")), key=lambda e: e.name)
    except OSError as error:
        raise TrackerFileError(path, error.strerror or str(error)) from None


def _is_dir(entry):
    """A directory; a symlink to one is an error."""
    if entry.is_dir(follow_symlinks=False):
        return True
    if entry.is_dir():
        raise TrackerFileError(entry.path, "not a directory")
    return False


def _outside(entry):
    if entry.name.endswith(".md"):
        raise TrackerFileError(entry.path, "file outside a state dir")


def _issue(entry):
    """Whether an entry of a state dir or the archive is an issue file; it must be a regular file."""
    if not entry.name.endswith(".md"):
        return False
    if not entry.is_file(follow_symlinks=False):
        raise TrackerFileError(entry.path, "not a regular file")
    return True


def _assignee(top):
    found = []
    for sub in _entries(top.path):
        if not _is_dir(sub):
            _outside(sub)
            continue
        if sub.name not in set(LocalState):
            raise TrackerFileError(sub.path, f"unknown state dir {sub.name!r}")
        state = LocalState(sub.name)
        found += [_read(e.path, top.name, state) for e in _entries(sub.path) if _issue(e)]
    return found


def _archive(path):
    found = []
    for entry in _entries(path):
        if entry.is_dir():
            raise TrackerFileError(entry.path, "sub-directory in archive")
        if _issue(entry):
            found.append(_read(entry.path, None, None))
    return found


def read_text(path: str) -> tuple[str, datetime]:
    """An issue file's text and modification time."""
    try:
        with open(path, encoding="utf-8") as file:
            return file.read(), datetime.fromtimestamp(
                os.fstat(file.fileno()).st_mtime
            ).astimezone()
    except UnicodeDecodeError:
        raise TrackerFileError(path, "not UTF-8 text") from None
    except OSError as error:
        raise TrackerFileError(path, error.strerror or str(error)) from None


def _read(path, assignee, state):
    text, mtime = read_text(path)
    return issue_file.parse(path, text, assignee=assignee, state=state, mtime=mtime)
