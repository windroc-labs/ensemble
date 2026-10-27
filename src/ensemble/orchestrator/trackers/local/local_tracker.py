"""The engine Tracker on a local board (docs/architecture.md › Local board): its reads, and its writes, each
one locked rewrite of one issue file."""

import os
import re
import stat
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from ensemble.orchestrator.engine.flow.handoff import parse_handoff
from ensemble.orchestrator.engine.flow.tracker import Tracker
from ensemble.orchestrator.engine.flow.types import (
    NO_ISSUE,
    Approved,
    ArchivePlan,
    ArchiveRefused,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    Issue,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Origin,
    Pick,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)
from ensemble.orchestrator.engine.types import UUID_RE, IssueError, TrackerFileError
from ensemble.orchestrator.trackers.local import issue_file, store
from ensemble.orchestrator.trackers.local.board import ARCHIVE, read_one, read_text, scan
from ensemble.orchestrator.trackers.local.types import Entry, LocalState, Source

ISSUE_STATES = {
    LocalState.BACKLOG: IssueState.BACKLOG,
    LocalState.READY: IssueState.READY,
    LocalState.RUNNING: IssueState.RUNNING,
    LocalState.REVIEW: IssueState.REVIEW,
    LocalState.APPROVED: IssueState.APPROVED,
    LocalState.DONE: IssueState.DONE,
    LocalState.CANCELED: IssueState.CANCELED,
}
LOCAL_STATES = {issue: local for local, issue in ISSUE_STATES.items()}
ORIGINS: dict[Source, Origin] = {Source.SELF: "self", Source.OTHER: "other"}
ENDED = frozenset({LocalState.DONE, LocalState.CANCELED})
LISTED = frozenset({IssueState.READY, IssueState.RUNNING})
GROUPS = {
    LocalState.DONE: Group.DONE,
    LocalState.REVIEW: Group.IN_REVIEW,
    LocalState.RUNNING: Group.IN_PROGRESS,
    LocalState.APPROVED: Group.IN_PROGRESS,
    LocalState.READY: Group.TODO,
    LocalState.CANCELED: Group.CANCELED,
}
RUN = re.compile(f"Run {UUID_RE.pattern} · ")
ARCHIVED = "archived"
SEARCHED = 10
TRIES = 3
PROMOTED = "Created by promote."
NAME_BYTES = 200
UNASSIGNED = "unassigned"
UNNAMED = re.compile(r"[/\x00-\x1f\x7f]")


class LocalTracker(Tracker):
    """board: its realpath'd dir, scanned once on first use; roles: the role dirs listed; now: the
    time writes record. Keys and identifiers are front-matter ids; an issue's home is its parent
    ("" none)."""

    def __init__(
        self,
        board: str,
        roles: Collection[str],
        *,
        prefix: str,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.board, self.roles, self._cache, self.now = board, frozenset(roles), None, now
        self.prefix = prefix

    def state_name(self, state: IssueState) -> str:
        return f"{LOCAL_STATES[state].value}/"

    def files(self):
        if self._cache is None:
            self._cache = {each.id: each for each in scan(self.board)}
        return self._cache

    def _need(self, ident):
        found = self.files().get(ident)
        if found is None:
            raise IssueError(f"{ident}: issue not found")
        return found

    def _in(self, *states, owners=None):
        """Role dirs' (owners' when given) issues located in states."""
        return [
            each
            for each in self.files().values()
            if each.assignee in self.roles
            and (owners is None or each.assignee in owners)
            and each.state in states
        ]

    def _related(self, issue):
        """Own related and blocked_by ids, then the issues naming it in theirs."""
        naming = [
            each.id
            for each in self.files().values()
            if issue.id in each.related or issue.id in each.blocked_by
        ]
        return list(dict.fromkeys([*issue.related, *issue.blocked_by, *naming]))

    def role(self, issue):
        return issue["assignee"]["id"]

    def assigned(self, state):
        if state not in LISTED:
            raise ValueError(f"no list of {state} issues")
        return [_record(each) for each in self._in(LOCAL_STATES[state])]

    def ready(self):
        """An id with no issue file blocks."""
        files = self.files()
        return [
            (
                _record(each),
                tuple(b for b in each.blocked_by if b not in files or files[b].state not in ENDED),
            )
            for each in self._in(LocalState.READY)
        ]

    def dependency_cycles(self):
        files = {key: each for key, each in self.files().items() if each.state not in ENDED}
        graph = {
            key: sorted(b for b in each.blocked_by if b in files) for key, each in files.items()
        }
        return _cycles(graph)

    def recheck(self, key):
        known = self.files().get(key)
        found = read_one(self.board, known.path, key) if known else None
        if found is None:
            return NO_ISSUE
        self.files()[key] = found
        role = found.assignee if found.assignee in self.roles else None
        return Recheck(
            ready=role is not None and found.state == LocalState.READY,
            role=role,
            refused=None,
            assignee=None if found.assignee is None else {"id": found.assignee},
            home=None,
            task=Pick(found.task),
            client=Pick(found.client),
        )

    def moved_at(self, key, state, *, by=None):
        found = self.files().get(key)
        return None if found is None else _last(found, state, by)

    def read(self, ident):
        found = self.files().get(ident)
        return None if found is None else Snapshot(_state(found.state), _last(found, found.state))

    def issue(self, ident):
        found = self._need(ident)
        files = self.files()
        notes = (
            Note(n.body, stamp_time(n.at), None, n.source.value, ORIGINS[n.source])
            for n in found.comments
            if not _is_run(n)
        )
        return Issue(
            id=found.id,
            identifier=found.id,
            url=url(found),
            title=found.title,
            description=found.description,
            created_at=stamp_time(found.created),
            home=None,
            notes=tuple(sorted(notes, key=lambda n: n.at)),
            links=tuple(Link(each.url) for each in found.links),
            linked=tuple(
                Linked(i, files[i].title, _shown(files[i]))
                for i in self._related(found)
                if i in files
            ),
            handoff=parse_handoff(found.description),
        )

    def search(self, words, idents):
        files = self.files().values()
        lower = [w.lower() for w in words]
        matched = [
            each
            for each in files
            if lower
            and all(w in each.title.lower() or w in each.description.lower() for w in lower)
        ]
        named = [each for each in files if each.id in idents]
        return tuple(map(_found, named + matched[:SEARCHED]))

    def description(self, ident):
        found = self.files().get(ident)
        return None if found is None else found.description

    def finished(self, owners):
        return [
            Finished(each.id, each.id, _last(each, each.state))
            for each in self._in(LocalState.DONE, LocalState.CANCELED)
            if each.state == LocalState.CANCELED or each.assignee in owners
        ]

    def archive_plan(self, key):
        """Unless archived, its unarchived descendants level by level."""
        found = self._need(key)
        archived = found.assignee is None
        below = () if archived else self._descendants(key)
        mine = (_sub_issue(found), *below)
        return ArchivePlan(
            found.id,
            found.id,
            url(found),
            archived,
            below,
            () if archived else tuple(s for s in mine if not s.finished),
        )

    def _descendants(self, key):
        children, found, level, seen = self._children(), [], [key], {key}
        while level:
            below = [s for k in level for s in children.get(k, ()) if s.key not in seen]
            seen.update(s.key for s in below)
            found += below
            level = [s.key for s in below]
        return tuple(found)

    def _children(self):
        """Unarchived issues by parent id."""
        found = {}
        for each in self.files().values():
            if each.assignee is not None and each.parent:
                found[each.parent] = (*found.get(each.parent, ()), _sub_issue(each))
        return found

    def approved(self, owners):
        return [
            Approved(
                each.id,
                each.id,
                "",
                each.title,
                each.priority,
                each.created.isoformat(),
                each.parent or "",
                each.parent or "",
                each.assignee or "",
                tuple((link.title, link.url) for link in each.links),
            )
            for each in self._in(LocalState.APPROVED, owners=owners)
        ]

    def detail(self, key):
        found = self._need(key)
        moves, frm = [], None
        for move in _moves(found):
            moves.append(Move(stamp_time(move.at), _state(move.state), frm))
            frm = moves[-1].to
        return Detail(
            _state(found.state),
            tuple(moves),
            tuple(
                Comment(
                    stamp_time(n.at),
                    n.body,
                    n.source.value,
                    ORIGINS[n.source],
                    run=_is_run(n),
                )
                for n in found.comments
            ),
            frozenset(self._related(found)),
        )

    def links(self, key):
        return {each.url for each in self._need(key).links}

    def on_board(self, ident):
        return ident if ident in self.files() else None

    def find_comment(self, key, ref):
        wanted = issue_file.ref_key(ref)
        return next(
            (
                n.body
                for n in self._need(key).comments
                if n.source == Source.SELF and n.key == wanted
            ),
            None,
        )

    def created(self, once):
        wanted = issue_file.ref_key(once)
        found = next(
            (
                each
                for each in self.files().values()
                if any(n.source == Source.SELF and n.key == wanted for n in each.comments)
            ),
            None,
        )
        return None if found is None else Ref(found.id, found.id)

    def create(self, once, *, home, owner, priority, title, description):
        """once's issue if one exists by now; a number whose id or path is taken is skipped."""

        def render(ident):
            now = self.now()
            return issue_file.new(
                ident=ident,
                title=title,
                priority=priority,
                created=now,
                parent=home or None,
                description=description,
                note_at=now,
                note=PROMOTED,
                note_key=issue_file.ref_key(once),
                state=LocalState.READY,
            )

        with store.locked(self.board):
            self._cache = None
            if made := self.created(once):
                return made
            return self._add(owner, LocalState.READY, title, render)

    def add(self, owner, state, title, render):
        """A new issue in <owner>/<state>/: render(its id)."""
        with store.locked(self.board):
            self._cache = None
            return self._add(owner, state, title, render)

    def _add(self, owner, state, title, render):
        """add's, the lock held."""
        files = self.files()
        prefix = _prefix(files) if files else self.prefix
        folder = self._folder(owner, state.value)
        while True:
            ident = store.next_id(self.board, prefix)
            path = os.path.join(folder, f"{ident} {_name(title)}.md")
            if ident not in files and not os.path.lexists(path):
                break
        store.replace(path, render(ident))
        text, mtime = read_text(path)
        files[ident] = issue_file.parse(path, text, assignee=owner, state=state, mtime=mtime)
        return Ref(ident, ident)

    def relate(self, key, other):

        def change(text, found):
            if other in self._related(found):
                return None
            return issue_file.add_related(text, other), None

        self.write(key, change)

    def _write_archive(self, key):
        """Rechecked under the lock; descendants deepest first, each with its archive note."""

        def change(text, found):
            if found.assignee is None:
                return None
            ref = issue_file.ref_key((ARCHIVED, found.assignee))
            body = f"Archived from {found.assignee}/{found.state}/."
            text = issue_file.drop_comment(text, ref) or text
            text = issue_file.add_comment(text, at=self.now(), body=body, ref_key=ref)
            return text, (None, None)

        with store.locked(self.board):
            self._cache = None
            plan = self.archive_plan(key)
            if plan.outcome == "refused":
                raise ArchiveRefused(plan)
            if plan.outcome == "unchanged":
                return False
            for each in (*reversed(plan.descendants), plan):
                self._write_locked(each.key, change)
        return True

    def write(self, key, change):
        """Under the board lock, issue key's file read fresh and Stamped, then change(text, found):
        None, or (text, None or the (assignee, state) dirs it moves to, (None, None) archive/);
        written when either changes and the file still holds what was read. A file a person moved
        or deleted meanwhile is read again. Returns the file as the deciding change saw it."""
        with store.locked(self.board):
            return self._write_locked(key, change)

    def _write_locked(self, key, change):
        """write's, the lock held."""
        for _ in range(TRIES):
            found = read_one(self.board, self._need(key).path, key)
            if found is None:
                raise IssueError(f"{key}: issue not found")
            read = _read_if_there(found.path)
            if read is None:
                continue
            text, mtime = read
            found = issue_file.parse(
                found.path, text, assignee=found.assignee, state=found.state, mtime=mtime
            )
            stamped = issue_file.stamp(
                text, mtime=mtime, state=found.state if found.assignee else None
            )
            changed = change(stamped, found)
            if changed is None or changed == (stamped, None):
                self.files()[key] = found
                return found
            again = _read_if_there(found.path)
            if again is None or again[0] != text:
                continue
            assignee, state = changed[1] or (found.assignee, found.state)
            path = found.path if changed[1] is None else self._rename(found, assignee, state)
            if path is None:
                continue
            store.replace(path, changed[0])
            text, mtime = read_text(path)
            self.files()[key] = issue_file.parse(
                path, text, assignee=assignee, state=state, mtime=mtime
            )
            return found
        raise TrackerFileError(found.path, "changed while writing")

    def _folder(self, *parts):
        """<board>/<parts…>, made if absent; a level that is no directory (or a symlink) refused."""
        folder = self.board
        for part in parts:
            folder = os.path.join(folder, part)
            try:
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    raise TrackerFileError(folder, "not a directory")
            except FileNotFoundError:
                os.mkdir(folder)
        return folder

    def _rename(self, issue, assignee, state):
        """issue's file moved, its name kept, into <assignee>/<state>/ (no assignee: archive/), made
        if absent; None: it was no longer there."""
        folder = self._folder(assignee, state.value) if assignee else self._folder(ARCHIVE)
        target = os.path.join(folder, os.path.basename(issue.path))
        if os.path.lexists(target):
            raise TrackerFileError(target, "file exists")
        try:
            os.rename(issue.path, target)
        except FileNotFoundError:
            if os.path.lexists(issue.path):
                raise
            return None
        return target

    def _move(self, key, to, frm, body=None):
        """None: moved (with body, a comment), or already in to with no frm; else its state."""

        def change(text, found):
            if found.assignee is None:
                raise IssueError(f"{key}: archived")
            if frm is not None and found.state != LOCAL_STATES[frm]:
                return None
            if found.state == LOCAL_STATES[to]:
                return None
            if body is not None:
                text = issue_file.add_comment(text, at=self.now(), body=body)
            return issue_file.add_history(text, at=self.now(), state=LOCAL_STATES[to]), (
                found.assignee,
                LOCAL_STATES[to],
            )

        state = self.write(key, change).state  # None only archived, which change refuses
        if state is None or frm is None or state == LOCAL_STATES[frm]:
            return None
        return state.value

    def move(self, key, to, *, frm=None):
        return self._move(key, to, frm)

    def claim(self, key):
        self._move(key, IssueState.RUNNING, None)

    def comment_and_move(self, key, body, *, to, frm, notify):
        """No one to notify on a local board."""
        return self._move(key, to, frm, body)

    def comment(self, key, body, ref=None):
        """A ref already posted: nothing."""
        wanted = None if ref is None else issue_file.ref_key(ref)

        def change(text, found):
            if wanted and any(n.source == Source.SELF and n.key == wanted for n in found.comments):
                return None
            return issue_file.add_comment(text, at=self.now(), body=body, ref_key=wanted), None

        self.write(key, change)

    def put_comment(self, key, body, ref):
        wanted = issue_file.ref_key(ref)

        def change(text, _):
            edited = issue_file.edit_comment(text, wanted, body)
            if edited is None:
                edited = issue_file.add_comment(text, at=self.now(), body=body, ref_key=wanted)
            return edited, None

        self.write(key, change)
        return True

    def notify(self, key, people):
        return ""

    def link(self, key, url, title):
        def change(text, found):
            if any(each.url == url for each in found.links):
                return None
            return issue_file.add_link(text, title=title, url=url), None

        self.write(key, change)

    def retitle(self, key, title):
        self.write(key, lambda text, _: (issue_file.set_title(text, title), None))

    def unarchive(self, key):
        """Back to its archive note's assignee (none valid: unassigned) in its last state."""

        def change(text, found):
            if found.assignee is not None:
                return None
            return text, (archived_from(found), found.state or LocalState.BACKLOG)

        self.write(key, change)


def _read_if_there(path):
    """read_text's, or None when nothing is at path any more."""
    try:
        return read_text(path)
    except TrackerFileError:
        if os.path.lexists(path):
            raise
        return None


def archived_from(issue):
    """The last archive note's assignee if it names one directory on the board."""
    notes = [n.key for n in issue.comments if n.source == Source.SELF and n.key]
    keys = [k for k in notes if k.startswith(f"{ARCHIVED}:")]
    name = unquote(keys[-1].split(":", 1)[1]) if keys else ""
    if not name or name.startswith(".") or name == ARCHIVE or "/" in name or "\0" in name:
        return UNASSIGNED
    return name


def _prefix(files):
    return max(files, key=lambda i: int(i.rsplit("-", 1)[1])).rsplit("-", 1)[0]


def _cycles(graph):
    """Iterative DFS: each back edge supplies a closed path, rotated to its smallest id."""
    done, cycles = set(), set()
    for root in sorted(graph):
        if root in done:
            continue
        path, active = [root], {root: 0}
        stack = [iter(graph[root])]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                done.add(path[-1])
                active.pop(path.pop())
                stack.pop()
            elif child in active:
                cycle = path[active[child] :]
                pivot = cycle.index(min(cycle))
                ordered = cycle[pivot:] + cycle[:pivot]
                cycles.add((*ordered, ordered[0]))
            elif child not in done:
                active[child] = len(path)
                path.append(child)
                stack.append(iter(graph[child]))
    return tuple(sorted(cycles))


def _name(title):
    return UNNAMED.sub("-", title).encode()[:NAME_BYTES].decode("utf-8", "ignore")


def _sub_issue(issue):
    return SubIssue(issue.id, issue.id, _shown(issue), issue.state in ENDED)


def url(issue):
    return Path(issue.path).as_uri()


def stamp_time(at):
    """As Linear's times: UTC, milliseconds, Z."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state(local):
    return None if local is None else ISSUE_STATES[local]


def _shown(issue):
    return issue.state.value if issue.state else ARCHIVED


def _record(issue):
    return {
        "id": issue.id,
        "identifier": issue.id,
        "url": url(issue),
        "title": issue.title,
        "priority": issue.priority,
        "createdAt": stamp_time(issue.created),
        "assignee": {"id": issue.assignee},
        "project": None,
    }


def _moves(issue):
    """History, then the derived move: a person's move to where the file sits, unrecorded."""
    moves = list(issue.history)
    if issue.state is not None and (not moves or moves[-1].state != issue.state):
        moves.append(Entry(issue_file.minute(issue.mtime), issue.state, Source.OTHER))
    return moves


def _last(issue, state, by=None):
    """The latest move into state (by that origin when given); None if none."""
    times = [m.at for m in _moves(issue) if m.state == state and by in (None, m.source)]
    return times[-1] if times else None


def _is_run(note):
    return note.source == Source.SELF and RUN.match(note.body) is not None


def _found(issue):
    last = max(issue.comments, key=lambda n: n.at).body if issue.comments else ""
    return Found(
        issue.id,
        issue.title,
        _shown(issue),
        GROUPS.get(issue.state, Group.BACKLOG),
        tuple(each.url for each in issue.links),
        issue.description,
        last if issue.state == LocalState.CANCELED else "",
    )
