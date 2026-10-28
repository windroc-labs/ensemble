"""The issue command's backend on a local board: Linear's output shapes with local values,
writing as a person (other)."""

import re
from collections.abc import Mapping
from datetime import datetime

from ensemble.orchestrator.engine.flow.types import (
    BOTH_DIRECTIONS,
    IDENT_ERROR,
    ISSUE_ID,
    PRIORITY_NUMBERS,
)
from ensemble.orchestrator.engine.types import (
    UUID_RE,
    Blocking,
    IssueError,
    Receipt,
    RelationsFailed,
    Role,
    TrackerFileError,
)
from ensemble.orchestrator.trackers.local import issue_file
from ensemble.orchestrator.trackers.local.local_tracker import (
    ARCHIVED,
    UNASSIGNED,
    LocalTracker,
    archived_from,
    stamp_time,
    url,
)
from ensemble.orchestrator.trackers.local.types import LocalState, Source

ACTOR = "other"
TYPES = {
    LocalState.BACKLOG: "backlog",
    LocalState.READY: "unstarted",
    LocalState.RUNNING: "started",
    LocalState.REVIEW: "started",
    LocalState.APPROVED: "started",
    LocalState.DONE: "completed",
    LocalState.CANCELED: "canceled",
}
_BAD_CLIENT = re.compile(r'^"|[\s\x00-\x1f\x7f-\x9f]')


class LocalIssues:
    def __init__(
        self,
        board,
        roles: Mapping[str, Role],
        *,
        prefix,
        now=lambda: datetime.now().astimezone(),
    ):
        self.tracker = LocalTracker(board, roles, prefix=prefix, now=now)
        self.roles = list(roles)
        self.tasks = list(dict.fromkeys(task for role in roles.values() for task in role.tasks))
        self.tracker.files()

    def _issue(self, ident, option=None):
        where = "" if option is None else f"{option} "
        if not (re.fullmatch(ISSUE_ID, ident) or UUID_RE.fullmatch(ident)):
            raise IssueError(where + IDENT_ERROR.format(ident=ident))
        found = self.tracker.files().get(ident)
        if found is None:
            raise IssueError(f"{where}{ident}: no such issue")
        return found

    def _folder(self, spec):
        if spec in self.roles or spec in ("me", UNASSIGNED):
            return spec
        raise IssueError(f"--role {spec}: not a role: {', '.join([*self.roles, 'me', UNASSIGNED])}")

    def _names(self, task, client):
        if task is not None and task not in self.tasks:
            raise IssueError(f"--task {task}: not a configured task: {', '.join(self.tasks)}")
        if client is not None and (not client or _BAD_CLIENT.search(client)):
            raise IssueError(
                f"--client {client!r}: want a client name: no whitespace, control character "
                "or leading quote"
            )

    def view(self, ident, *, full):
        """Always whole: full changes nothing."""
        found, files = self._issue(ident), self.tracker.files()
        state, person = found.state, _person(found)
        notes = _numbered(found)
        notes.sort(key=lambda p: (p[1].at, p[0]), reverse=True)
        return {
            "id": found.id,
            "identifier": found.id,
            "title": found.title,
            "url": url(found),
            "state": None
            if state is None
            else {"id": state.value, "name": state.value, "type": TYPES[state]},
            "labels": _labels(found),
            "project": None,
            "milestone": None,
            "assignee": None if person is None else {**person, "email": None},
            "description": found.description,
            "comments": [_comment(found.id, n, note) for n, note in notes],
            "blocks": self._column(i for i, each in files.items() if found.id in each.blocked_by),
            "blocked_by": self._column(found.blocked_by),
            "parent": None if found.parent is None else self._column([found.parent])[0],
            "children": self._column(
                i for i, each in files.items() if each.assignee and each.parent == found.id
            ),
            "truncated": [],
            "continue": None,
        }

    def _column(self, idents):
        files, column = self.tracker.files(), []
        for ident in sorted(idents, key=_number):
            found = files.get(ident)
            title = None if found is None else found.title
            state = None if found is None or found.state is None else found.state.value
            column.append({"id": ident, "identifier": ident, "title": title, "state": state})
        return column

    def page(self, query, first, cursor):
        rows = self.walk(query)
        if cursor is not None:
            at = next((n for n, row in enumerate(rows) if row[2] == cursor), None)
            if at is None:
                raise IssueError(f"--cursor {cursor}: not a cursor of this list")
            rows = rows[at + 1 :]
        page = rows[:first]
        return page, len(rows) > first, page[-1][2] if page else None

    def walk(self, query):
        keep = self._filters(query)
        found = [each for each in self.tracker.files().values() if all(k(each) for k in keep)]
        found.sort(key=lambda each: (each.mtime, _number(each.id)), reverse=True)
        return [(_item(each), [], each.id) for each in found]

    def _filters(self, query):
        self._names(query.task, query.client)
        keep = [] if query.include_archived else [lambda each: each.assignee is not None]
        if query.query:
            text = query.query.lower()
            keep.append(lambda each: text in each.title.lower() or text in each.description.lower())
        if query.state is not None:
            keep.append(lambda each: each.state == query.state)
        if query.role is not None:
            folder = self._folder(query.role)
            keep.append(lambda each: _dir(each) == folder)
        if query.task is not None:
            keep.append(lambda each: each.task == query.task)
        if query.client is not None:
            keep.append(lambda each: each.client == query.client)
        if query.priority is not None:
            keep.append(lambda each: each.priority == PRIORITY_NUMBERS[query.priority])
        if query.parent is not None:
            parent = self._issue(query.parent, "--parent").id
            keep.append(lambda each: each.parent == parent)
        return keep

    def create(self, new):
        owner = self._folder(UNASSIGNED if new.role is None else new.role)
        self._names(new.task, new.client)
        priority = 0 if new.priority is None else PRIORITY_NUMBERS[new.priority]
        parent = None if new.parent is None else self._issue(new.parent, "--parent").id
        picked = {}
        for option, values in (("--blocks", new.blocks), ("--blocked-by", new.blocked_by)):
            for value in dict.fromkeys(values):
                found = self._issue(value, option)
                if picked.setdefault(found.id, option) != option:
                    raise IssueError(BOTH_DIRECTIONS.format(identifier=found.id))
        state = LocalState(new.state or LocalState.BACKLOG)

        def render(ident):
            return issue_file.new(
                ident=ident,
                title=new.title,
                priority=priority,
                created=self.tracker.now(),
                parent=parent,
                description=new.body,
                state=state,
                task=new.task,
                client=new.client,
                blocked_by=[i for i, option in picked.items() if option == "--blocked-by"],
                source=Source.OTHER,
            )

        key = self.tracker.add(owner, state, new.title, render).key
        done, failed = [], []
        for other, option in picked.items():
            if option == "--blocked-by":
                done.append(Blocking("blocked_by", other))
                continue
            try:
                self._write(other, _blocked(key, add=True))
            except (IssueError, TrackerFileError, OSError) as error:
                failed.append((Blocking("blocks", other), str(error) or type(error).__name__))
            else:
                done.append(Blocking("blocks", other))
        receipt = self._receipt(key, "create", "changed")
        if failed:
            raise RelationsFailed(receipt, tuple(done), tuple(failed))
        return receipt

    def edit(self, change):
        owner = None if change.role is None else self._folder(change.role)
        self._names(change.task, change.client)
        key = self._issue(change.id).id

        def rewrite(text, found):
            if owner is not None and found.assignee is None:
                raise IssueError(f"{key}: archived")
            edited = text
            if change.title is not None and change.title != found.title:
                edited = issue_file.set_title(edited, change.title)
            if change.body is not None and change.body.strip() != found.description:
                edited = issue_file.set_description(edited, change.body)
            if change.task is not None and change.task != found.task:
                edited = issue_file.set_front(edited, "task", change.task)
            if change.client is not None and change.client != found.client:
                edited = issue_file.set_front(edited, "client", change.client)
            priority = None if change.priority is None else PRIORITY_NUMBERS[change.priority]
            if priority is not None and priority != found.priority:
                edited = issue_file.set_front(edited, "priority", priority)
            if owner is not None and owner != found.assignee:
                return edited, (owner, found.state)
            return None if edited == text else (edited, None)

        return self._receipt(key, "edit", self._write(key, rewrite))

    def move(self, ident, state):
        key, target = self._issue(ident).id, LocalState(state)

        def change(text, found):
            if found.assignee is None:
                raise IssueError(f"{key}: archived")
            if found.state == target:
                return None
            now = self.tracker.now()
            moved = issue_file.add_history(text, at=now, state=target, source=Source.OTHER)
            return moved, (found.assignee, target)

        return self._receipt(key, "move", self._write(key, change))

    def comment(self, ident, body):
        key = self._issue(ident).id

        def change(text, _):
            now = self.tracker.now()
            return issue_file.add_comment(text, at=now, body=body, source=Source.OTHER), None

        self.tracker.write(key, change)
        count = len(_numbered(self.tracker.files()[key]))
        return self._receipt(key, "comment", "changed", comment_id=f"{key}#{count}")

    def relation(self, op, ident, direction, other):
        option = "--" + direction.replace("_", "-")
        node = self._issue(ident).id
        target = self._issue(other, option).id
        if target == node:
            raise IssueError(f"{option} {other}: {ident} cannot block itself")
        blocker, blocked = (node, target) if direction == "blocks" else (target, node)
        return self._receipt(
            node, f"relation {op}", self._write(blocked, _blocked(blocker, add=op == "add"))
        )

    def archive_plan(self, ident):
        return self.tracker.archive_plan(self._issue(ident).id)

    def archive(self, ident):
        key = self._issue(ident).id
        changed = self.tracker.archive(key, self.tracker.archive_plan(key))
        return self._receipt(key, "archive", "changed" if changed else "unchanged")

    def _write(self, key, change):
        """tracker.write(key, change); changed when change's last call wrote."""
        wrote = []

        def step(text, found):
            changed = change(text, found)
            wrote.append(changed is not None)
            return changed

        self.tracker.write(key, step)
        return "changed" if wrote[-1] else "unchanged"

    def _receipt(self, key, op, result, comment_id=None):
        found = self.tracker.files()[key]
        return Receipt(key, key, url(found), op, result, ACTOR, ACTOR, comment_id)


def _number(ident):
    return int(ident.rsplit("-", 1)[1]), ident


def _dir(found):
    return archived_from(found) if found.assignee is None else found.assignee


def _person(found):
    folder = _dir(found)
    return None if folder == UNASSIGNED else {"id": folder, "name": folder}


def _labels(found):
    return [{"id": name, "name": name} for name in (found.task, found.client) if name]


def _archive_note(note):
    """The engine's bookkeeping, which Linear has no comment for."""
    return note.source == Source.SELF and (note.key or "").startswith(f"{ARCHIVED}:")


def _numbered(found):
    """(n, comment) of each comment but archive notes, which re-archiving moves, so ids never shift."""
    return list(enumerate((n for n in found.comments if not _archive_note(n)), 1))


def _comment(ident, n, note):
    return {
        "id": f"{ident}#{n}",
        "author": {"id": note.source.value, "name": note.source.value},
        "created_at": stamp_time(note.at),
        "body": note.body,
        "parent_id": None,
        "quoted_text": None,
        "on_behalf_of": None,
    }


def _item(found):
    state = found.state
    return {
        "id": found.id,
        "identifier": found.id,
        "title": found.title,
        "url": url(found),
        "state": None if state is None else {"id": state.value, "name": state.value},
        "labels": _labels(found),
        "assignee": _person(found),
        "project": None,
        "parent_id": found.parent,
    }


def _blocked(blocker, *, add):
    """A write's change adding blocker to an issue's blocked_by, or removing it."""

    def change(text, found):
        if (blocker in found.blocked_by) == add:
            return None
        ids = [*found.blocked_by, blocker] if add else [i for i in found.blocked_by if i != blocker]
        return issue_file.set_front(text, "blocked_by", ids), None

    return change
