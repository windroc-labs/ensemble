"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import io
import json
import os
import re
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from ensemble.delegate import Spec as DelegateSpec
from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import (
    config,
    inputs,
    router,
    runs,
)
from ensemble.orchestrator.engine import (
    gate as quota,
)
from ensemble.orchestrator.engine.types import LinearError, Spec, TrackerKind
from ensemble.orchestrator.trackers.linear import (
    linear_tracker,
    ops,
)
from ensemble.session import SessionStatus
from ensemble.utils import EVENTS_ENV, SHORT, SID_ENV
from tests.orchestrator.unit import run_fixtures
from tests.orchestrator.unit.board_ids import (
    HARNESS_USER,
    HEADER,
    ROLES,
    STATE_NAMES,
    TEAM,
    role_id,
    team_node,
)
from tests.orchestrator.unit.board_ids import STATES as IDS_BY_KEY
from tests.orchestrator.unit.board_ids import role as role_table
from tests.orchestrator.unit.clock import logs_at
from tests.orchestrator.unit.delegate_fixture import provider
from tests.orchestrator.unit.fake_api import FakeApi
from tests.orchestrator.unit.run_fixtures import (
    ID,
    PROJECT,
    SID,
    USER_NOTE,
    UUID,
    Gql,
    logged,
    node,
    show,
)
from tests.orchestrator.unit.streams import Stderr
from tests.orchestrator.unit.target_fixtures import FakeRun, ok
from tests.orchestrator.unit.target_fixtures import api as repo_info
from tests.utils.sandbox import scratch

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


WEST = timezone(timedelta(hours=-4))


AGENT, USER, MANAGER, BOT = "agent", "user", "manager", "bot"


STATES = {
    "Todo": IDS_BY_KEY["todo"],
    "In Progress": IDS_BY_KEY["in_progress"],
    "In Review": IDS_BY_KEY["in_review"],
}


NAMES = {i: n for n, i in STATES.items()}


DR, PD = "Deep Research", "Product Design"


IDS = {DR: "p-dr", PD: "p-pd"}


# history actor ids as users: the harness app, the user, the manager and the role apps;
# None: Linear automation; BOT: an integration's move, credited to the user
ACTORS = {
    AGENT: {"id": HARNESS_USER},
    USER: {"id": USER},
    MANAGER: {"id": MANAGER},
    BOT: {"id": USER},
    **{role_id(r): {"id": role_id(r)} for r in ROLES},
}


def with_actor(node):
    """A stored history node as Q_HISTORY reads it."""
    bot = {"type": "github"} if node["actorId"] == BOT else None
    return node | {"actor": ACTORS.get(node["actorId"]), "botActor": bot}


CLIENTS = {"researcher": "a", "pm": "b", "engineer": "a"}


OVERLAY = '[core]\nclient = "a"\n\n[core.roles.pm]\nclient = "b"\n'


CONFIG = HEADER + role_table("researcher") + role_table("pm") + role_table("engineer") + OVERLAY


LIGHT, DEEP, STRAY, ORPHAN_LABEL = (
    f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24)
)


RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } delegate { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } archivedAt } }"
)


NO = SessionStatus("no", None, "")


RESUME = ["ts", "kind", "issue", "sid", "role", "client", "delegate_kind"]


START = [*RESUME, "uuid", "project", "target", "task"]


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def who(role):
    """A role's app user, no email."""
    return None if role is None else {"id": role_id(role), "email": None}


def op(query):
    return re.search(r"\{ (\w+)\(", query).group(1)


def issue(
    ident,
    state,
    role=None,
    *,
    priority=0,
    created="2026-09-01T00:00:00Z",
    project=DR,
    inverse=(),
    labels=(),
    archived=False,
):
    return {
        "archived": archived,
        "id": ident,
        "identifier": ident,
        "url": f"https://linear.app/x/{ident}",
        "state": state,
        "project": project and {"id": IDS[project], "name": project},
        "delegate": who(role),
        "assignee": None,
        "priority": priority,
        "createdAt": created,
        "inverseRelations": {"nodes": list(inverse)},
        "labels": list(labels),
    }


LS_REMOTE = (
    "git",
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
    "ls-remote",
    "--heads",
    "https://github.com/Acme/Ensemble.git",
    "TASK-7-*",
)


LISTING = ("gh", "api", "repos/windroc/team-docs/contents/Research?ref=main")


DESIGN_LISTING = ("gh", "api", "repos/windroc/team-docs/contents/Product%20Design?ref=main")


PRS = ("gh", "search", "prs")


# a first claim's related search: no docs, no PRs
SEARCHED = [(LISTING, ok("[]")), (DESIGN_LISTING, ok("[]")), (PRS, ok("[]"))]


DOCS_SEARCH = [(LISTING, SHORT), (DESIGN_LISTING, SHORT)]


def pr_search(*words, repo="acme/ensemble"):
    argv = (*PRS, "--merged", "--repo", repo, "--limit", "5", "--json", "number,title,url", "--")
    return ((*argv, *[t for w in words for t in ("OR", w)][1:]), SHORT)


ENG_RUN = [
    (("gh", "api", "repos/acme/ensemble"), repo_info("Acme/Ensemble")),
    (LS_REMOTE[:3], ok()),
    *SEARCHED,
]


OUTER = (
    HEADER
    + 'linear.human_members = ["Me@X.com"]\n'
    + CONFIG.removeprefix(HEADER)
    + f'[linear.project_repos]\n"{PROJECT}" = "acme/ensemble"\n'
)


INPUT = f"""Reference: TASK-7
Title: TASK-7: Session registry
Repo: Acme/Ensemble
Checkout: TASK-7
Branch: TASK-7-session-registry
Links: {run_fixtures.URL}

{inputs.PRECEDENCE["build"]}

## The user's requirements since the last build

Me, 2026-09-02T00:00:00.000Z:
Use SQLite.

## The user's instructions

ENG: Session registry

Add a session registry.

## PRD

None linked."""


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations, self.queries, self.services = [], [], []
        self.comments = {}
        self.todo_error = False
        self.unreadable = set()
        self.refuse = set()
        self.ids = {}  # an issue's uuid → its identifier, for write-back's calls
        self.group = {
            "isGroup": True,
            "children": {"nodes": [{"id": i} for i in (LIGHT, DEEP, ORPHAN_LABEL)]},
        }

    def __call__(self, query, *, identity, **v):
        if query == linear_tracker.Q_STATE_NAME:
            return {"workflowState": {"name": STATE_NAMES[v["i"]]}}
        self.queries.append((query, v))
        self.services.append(identity.service)
        if (value := self._identity(query, v)) is not None:
            return value
        if "history" in query:
            return self._issue_read(query, v)
        if "mutation" in query:
            return self._mutate(query, v)
        if "issues(filter" in query:
            return self._queue(query, v)
        if (value := self._issue_read(query, v)) is not None:
            return value
        raise AssertionError(query)

    def _identity(self, query, v):
        if query == ops.Q_COMMENT:
            return {"comment": self.comments.get(v["c"])}
        if query in (linear_tracker.Q_SEARCH, linear_tracker.Q_SEARCH_IDS, linear_tracker.Q_IDS):
            return {"searchIssues": {"nodes": []}, "issues": {"nodes": []}}
        if query == ops.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == ops.Q_TASK_GROUP:
            return {"issueLabel": self.group}
        return None

    def _issue_read(self, query, v):
        if "attachments" in query:
            return {"issue": {"attachments": {"nodes": []}}}
        if "history" in query:
            found = sorted(self.history.get(v["i"], []), key=lambda n: n["createdAt"], reverse=True)
            start = int(v.get("after") or 0)
            return {
                "issue": {
                    "history": {
                        "nodes": [with_actor(n) for n in found[start : start + 250]],
                        "pageInfo": {
                            "hasNextPage": start + 250 < len(found),
                            "endCursor": str(start + 250),
                        },
                    }
                }
            }
        if query == ops.Q_ISSUE_STATE:
            issue = self.issues[self.ids.get(v["i"], v["i"])]
            return {"issue": {"state": {"id": STATES[issue["state"]]}}}
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise LinearError(f"linear api error: {[{'message': 'Entity not found'}]}")
            return {"issue": {"inverseRelations": self.issues[v["i"]]["inverseRelations"]}}
        if query == RECHECK:
            if v["i"] not in self.issues:
                raise LinearError("linear api error: Entity not found: Issue")
            i = self.issues[v["i"]]
            return {
                "issue": {
                    "state": {"id": STATES[i["state"]]},
                    "assignee": i["assignee"],
                    "delegate": i.get("delegate"),
                    "project": i["project"],
                    "labels": {"nodes": i["labels"]},
                    "archivedAt": "2026-10-26T04:11:00.000Z" if i["archived"] else None,
                }
            }
        return None

    def _mutate(self, query, v):
        self.mutations.append((query, v))
        issue = self.issues[self.ids.get(v["i"], v["i"])]
        if "issueSubscribe" in query:
            issue.setdefault("subscribers", []).append(v["e"])
        elif "commentCreate" in query:
            issue.setdefault("comments", []).append(v["b"])
            if "c" in v:
                self.comments[v["c"]] = {
                    "body": v["b"],
                    "issue": {"id": v["i"]},
                    "user": {"isMe": True},
                }
        elif v["i"] in self.refuse:
            return {"issueUpdate": {"success": False}}
        else:
            issue["state"] = NAMES[v["s"]]
        return {op(query): {"success": True}}

    def _queue(self, query, v):
        f = v["f"]
        if self.todo_error and "inverseRelations" in query:
            raise LinearError(f"linear api error: {[{'message': 'Entity not found'}]}")
        drop = (
            {"state", "labels", "archived"}
            if "inverseRelations" in query
            else {"state", "labels", "archived", "inverseRelations"}
        )
        nodes = [
            {k: x for k, x in i.items() if k not in drop}
            for i in self.issues.values()
            if f["team"]["id"]["eq"] == TEAM
            and not i["archived"]  # Linear lists none without includeArchived
            and f["project"] == {"null": False}
            and i["project"]
            and i["state"] == NAMES[f["state"]["id"]["eq"]]
        ]
        start = int(v.get("c") or 0)
        return {
            "issues": {
                "nodes": nodes[start : start + 100],
                "pageInfo": {
                    "hasNextPage": start + 100 < len(nodes),
                    "endCursor": str(start + 100),
                },
            }
        }

    def reads(self, state="Todo"):
        return [
            q
            for q, v in self.queries
            if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}
        ]


class Base(run_fixtures.Base):
    wiring = cli.TRACKERS[TrackerKind.LINEAR]

    def setUp(self):
        super().setUp()
        self.keychain_calls = []
        self.write_local(f'work_dir = "{self.work}"\n')
        self.tmp = scratch(self, real=False)
        self.config_path = os.path.join(self.tmp, "config.toml")
        self.write_config(CONFIG)
        self.lines, self.hist, self.names = [], {}, {}
        self.logs, self.mark, self.raw, self.taken = None, 0, "", None
        self.api = FakeApi()

    def write_config(self, text):
        self.write(self.config_path, text)

    def sid(self, name):
        s = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
        self.names[s] = name
        return s

    def unmap(self, text):
        for s, name in self.names.items():
            text = text.replace(s, name)
        return text

    def fake(self, *issues):
        return FakeLinear(issues, self.hist)

    def moved(self, ident, minutes_ago, state=STATES["In Progress"], actor=AGENT):
        self.hist.setdefault(ident, []).insert(
            0, {"createdAt": ago(minutes=minutes_ago), "actorId": actor, "toStateId": state}
        )

    def resumable(self, ident, sid, minutes_ago, role="researcher"):
        self.moved(ident, minutes_ago + 1)
        self.add(kind="start", ident=ident, sid=sid, minutes_ago=minutes_ago, role=role)
        self.touch(sid, 40)

    def row(self, kind, ident, sid, role="researcher", client=None, *, task=None, **ago):
        return json.dumps(
            {
                "ts": (NOW - timedelta(**ago)).astimezone(WEST).isoformat(timespec="seconds"),
                "kind": kind,
                "issue": ident,
                "sid": self.sid(sid),
                "role": role,
                "client": client or CLIENTS.get(role, "a"),
                "delegate_kind": "harness",
                **({"task": task} if task else {}),
            }
        )

    def add(self, *, kind, ident, sid, minutes_ago, role="researcher", client=None, task=None):
        self.lines.append(
            self.row(
                kind,
                ident,
                sid,
                role,
                client,
                task=task,
                minutes=minutes_ago,
            )
        )

    def touch(self, sid, minutes_ago):
        # the session's transcript, last written minutes_ago
        at = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        entry = next(
            (
                json.loads(line)
                for line in reversed(self.lines)
                if json.loads(line).get("sid") == self.sid(sid)
            ),
            None,
        )
        if entry is not None:
            self.api.records[self.sid(sid)] = {
                "cwd": os.path.join(self.work, "work", entry["issue"])
            }
        self.api.statuses[self.sid(sid)] = SessionStatus("yes", at, f"resume {sid}")

    def write_runs(self):
        self.write(self.runs, "".join(f"{line}\n" for line in self.lines))
        for e in runs.parse_log(self.runs):
            self.api.statuses.setdefault(e.sid, NO)

    def log_dir(self, *, again):
        if not again or self.logs is None:
            self.logs = os.path.join(self.work, "logs")
            path = os.path.join(self.logs, "orchestrator.jsonl")
            if os.path.exists(path):
                os.unlink(path)
        self.mark = len(logged(self.logs))
        return logs_at(self.logs)

    def keychain(self, identity):
        self.keychain_calls.append(identity.service)
        return True

    def cfg(self):
        cfg = config.load(path=self.config_path)
        return replace(cfg, delegates={**cfg.delegates, cfg.delegate_kind: provider(self.api, cfg)})

    def board(self, fake, use, *, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = self.cfg()
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=again):
            out = use(
                router.Board(
                    fake,
                    runs.parse_log(self.runs),
                    cfg,
                    now=NOW,
                    dry=dry,
                    keychain=self.keychain,
                    wiring=self.wiring,
                    gate=quota.Gate(dry=dry, cfg=cfg) if gate else None,
                )
            )
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, *, dry=False, again=False):
        def use(b):
            if cands := b.recover():
                return ("resume", *cands[0])
            return ("new",) if b.queue() else None

        run = self.board(fake, use, dry=dry, again=again)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(
                f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}"
            )
        return "new" if run else ""

    def claim(self, fake, *, full=(), dry=False, recover=False, gate=False, again=False):

        def use(b):
            recover and b.recover()
            return b.take(full=full)

        self.taken = taken = self.board(fake, use, dry=dry, gate=gate, again=again)
        return (
            taken
            and f"{taken[0]['identifier']} {taken[0]['url']} {taken[0]['project']['name']}"
            or ""
        )

    def said(self):
        events = logged(self.logs)[self.mark :] + [
            json.loads(x) for x in self.raw.splitlines() if x.startswith("{")
        ]
        self.assertEqual({e["src"] for e in events} - {"router"}, set())
        return [self.unmap(show(e)) for e in events]


class Start:
    """The injected start: records each launch Spec; per issue an exit code (`exits`) or an
    exception to raise.
    """

    def __init__(self, exits=None, raises=None):
        self.launches, self.exits, self.raises = [], exits or {}, raises or {}

    def __call__(self, a):
        self.launches.append(a)
        if a.issue in self.raises:
            raise self.raises[a.issue]
        return self.exits.get(a.issue, 0)


class TickBase(Base):
    def tick(
        self,
        fake,
        *argv,
        live=(),
        start=None,
        again=False,
        now=NOW,
        tty=False,
        outer=None,
        clock=None,
        env=None,
        trackers=cli.TRACKERS,
    ):
        # outer: main's run and keychain; main then starts through its own outer
        # env: the caller's variables over PATH; a launcher's events file and sid only from env
        self.api.live = None if live is None else list(live)
        recorded = {json.loads(line)["issue"] for line in self.lines}
        for name in live or ():
            found = re.fullmatch(r"ensemble-([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)", name)
            if found and found[2] not in recorded:
                self.add(
                    kind="start", ident=found[2], sid=f"live:{name}", minutes_ago=10, role=found[1]
                )
                recorded.add(found[2])
        if live is None and not self.lines:
            self.add(kind="start", ident="TASK-0", sid="unknown", minutes_ago=10)
        try:
            delegate_cls = self.cfg().delegate_cls
        except config.ConfigError:
            delegate_cls = None

        self.api.order, self.api.usage_calls, self.api.info = [], [], []
        self.start = None if outer else start or Start()
        self.write_runs()
        err, out = Stderr(tty=tty), io.StringIO()
        with (
            redirect_stderr(err),
            redirect_stdout(out),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.log_dir(again=again),
        ):
            for key in (EVENTS_ENV, SID_ENV):
                os.environ.pop(key, None)
            os.environ.update(env or {})
            rc = router.main(
                list(argv),
                now=clock or (lambda: now),
                config_path=self.config_path,
                gql=fake,
                delegate_cls=delegate_cls,
                start=self.start,
                trackers=trackers,
                **{"keychain": self.keychain, **(outer or {})},
            )
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        self.out = out.getvalue()
        with open(self.runs) as f:
            self.state = self.unmap(f.read())
        return rc

    def before(self):
        return self.unmap("".join(f"{line}\n" for line in self.lines))

    def last_run(self):
        """runs.jsonl's last row, a start or resume row."""
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), START if entry["kind"] == "start" else RESUME)
        return entry["kind"], entry["issue"], entry["sid"], entry["role"], entry["client"]

    def raw_runs(self):
        with open(self.runs, encoding="utf-8") as f:
            return f.read()

    def launched_run(self):
        """The run the tick handed to its start, as last_run's row would show it."""
        a = self.start.launches[-1]
        return (
            "start" if a.mode == "new" else "resume",
            a.issue,
            self.unmap(a.sid),
            a.role,
            a.client,
        )

    def launched(self):
        (launch,) = self.start.launches
        return launch.issue

    def launched_runs(self):
        return [(a.issue, a.mode) for a in self.start.launches]


class OuterBase(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(OUTER)
        self.gql = Gql(issue=node(comments=[USER_NOTE]))
        self.run_ = FakeRun(ENG_RUN)
        self.api.records[SID] = {"cwd": self.rd}

    def outer(
        self,
        *,
        role="engineer",
        task=None,
        mode="new",
        ident=ID,
        sid=SID,
        client="a",
        started_task=None,
    ):
        """router.outer on a Spec; a resume's run is recorded first, as Recover finds it, its start
        row holding started_task.
        """
        if mode == "resume" and sid not in {e.sid for e in runs.parse_log(self.runs)}:
            runs.note(
                self.runs,
                "start",
                issue=ident,
                sid=sid,
                role=role or "engineer",
                client=client,
                delegate_kind="harness",
                **({"task": started_task} if started_task else {}),
            )
        a = Spec(
            uuid=UUID,
            issue=ident,
            project=PROJECT,
            role=role,
            sid=sid,
            task=task,
            mode=mode,
            client=client,
        )
        cfg = self.cfg()
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=True):
            rc = router.outer(
                a,
                cfg,
                gql=self.gql,
                run=self.run_,
                keychain=self.keychain,
                wiring=self.wiring,
                now=lambda: NOW,
            )
        self.err = err.getvalue()
        return rc

    def detached(self, role="engineer", *, tier=None):
        """The Delegate Spec of the last detached run, after its role, workdir and options (tier:
        the role tier overlay after core).
        """
        found, spec, options = self.api.detached[-1]
        self.assertEqual((found, spec.workdir), (role, self.rd))
        self.assertEqual(
            options,
            {
                "display": None,
                "callback": [
                    "ensemble-orchestrator",
                    "writeback",
                    "--issue",
                    ID,
                    "--workdir",
                    self.rd,
                ],
                "overlays": (self.cfg().core,)
                + (() if tier is None else ({"roles": {role: {"tier": tier}}},))
                + (
                    (
                        {
                            "roles": {
                                role: {"gate": "ensemble-orchestrator router --brake researcher"}
                            }
                        },
                    )
                    if role == "researcher"
                    else ()
                ),
            },
        )
        return spec

    def expected(
        self, role="engineer", *, task=None, sid=SID, client="a", resume=False, text=INPUT
    ):
        return DelegateSpec(
            text,
            self.rd,
            task=task,
            client=client,
            sid=sid,
            resume=resume,
            name=f"{role}-{ID}-{sid[:8]}",
        )

    def input(self, **kw):
        return self.detached(**kw).input
