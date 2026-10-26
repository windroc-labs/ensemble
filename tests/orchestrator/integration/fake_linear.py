"""Linear's GraphQL API as the orchestrator's queries use it, in memory, served over HTTP through the
integration transport: each exact query text is an op (OPS; BUILT: the router's issue lists, built
by router.q_issues; module.NAME[key]: a dict constant's text), its answer projected to the query's
selection. Issues list most recently written first; the issue command's connections are cut to the
query's first, its cursors node ids."""

import functools
import hashlib
import io
import itertools
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ensemble.orchestrator.engine import (
    types,
)
from ensemble.orchestrator.engine.types import LinearError
from ensemble.orchestrator.trackers.linear import (
    client,
    issue_archive,
    issue_create,
    issue_edit,
    issue_label,
    issue_list,
    issue_names,
    issue_read,
    issue_refs,
    issue_relation,
    issue_write,
    linear_tracker,
    ops,
)
from tests.orchestrator.integration.fixture_errors import Problem
from tests.orchestrator.unit.board_ids import STATES, TEAM


@dataclass
class Request:
    op: str | None
    account: str | None
    variables: dict
    result: str


@dataclass
class _Failure:
    op: str
    kind: str
    account: str | None
    times: int
    seconds: float


KEY = "TASK"

BACKLOG = STATES["backlog"]

IDS = STATES

TYPES = {
    "todo": "unstarted",
    "in_progress": "started",
    "in_review": "started",
    "handoff": "started",
    "done": "completed",
    "canceled": "canceled",
    "backlog": "backlog",
}

NAMES = {i: k for k, i in IDS.items()}

TITLES = {
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "handoff": "Handoff",
    "done": "Done",
    "canceled": "Canceled",
    "backlog": "Backlog",
}

KINDS = ("error", "unsuccessful", "503", "hang")

MODULES = {
    "linear": ops,
    "sessions": linear_tracker,
    "writeback": linear_tracker,
    "promote": linear_tracker,
    "prune": linear_tracker,
    "router": linear_tracker,
    "related": linear_tracker,
    "issue_read": issue_read,
    "issue_list": issue_list,
    "issue_create": issue_create,
    "issue_names": issue_names,
    "issue_refs": issue_refs,
    "issue_edit": issue_edit,
    "issue_label": issue_label,
    "issue_write": issue_write,
    "issue_archive": issue_archive,
    "issue_relation": issue_relation,
}

BUILT = {
    "issues.Q_ISSUE": linear_tracker.Q_READ,
    "router.issues": linear_tracker.q_issues(),
    "router.issues+relations": linear_tracker.q_issues(linear_tracker.RELATIONS),
}

CREATE = {"id", "teamId", "projectId", "assigneeId", "stateId", "priority", "title", "description"}

ISSUE_CREATE = CREATE | {"labelIds", "parentId"}

UPDATE = {
    "title",
    "description",
    "priority",
    "assigneeId",
    "addedLabelIds",
    "removedLabelIds",
    "stateId",
}

ONE_PAGE = {"hasNextPage": False, "endCursor": None}

COMPARATORS = {
    "eq": lambda x, a: x == a,
    "in": lambda x, a: x in a,
    "null": lambda x, a: (x is None) == a,
    "containsIgnoreCase": lambda x, a: x is not None and a.lower() in x.lower(),
}


def stamp(t=None):
    if t is None:
        t = datetime.now(UTC)
    elif isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    t = (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _key(service):
    return "lin_api_" + hashlib.sha256(service.encode()).hexdigest()[:32]


def _errors(message):
    return {"data": None, "errors": [{"message": message}]}


def _newest(nodes):
    return sorted(reversed(nodes), key=lambda n: n["createdAt"], reverse=True)


@functools.cache
def selection(query):
    tokens = re.findall(r"\w+|[{}()]", query)

    def skip(i):
        depth = 0
        while True:
            depth += {"(": 1, ")": -1}.get(tokens[i], 0)
            i += 1
            if depth == 0:
                return i

    def block(i):
        fields, i, last = {}, i + 1, None
        while tokens[i] != "}":
            if tokens[i] == "(":
                i = skip(i)
            elif tokens[i] == "{":
                fields[last], i = block(i)
            else:
                last, i = tokens[i], i + 1
                fields[last] = None
        return fields, i + 1

    i = 0
    while tokens[i] != "{":
        i = skip(i) if tokens[i] == "(" else i + 1
    return block(i)[0]


def project(value, sel, op):
    if sel is None or value is None:
        return value
    if isinstance(value, list):
        return [project(v, sel, op) for v in value]
    if missing := [f for f in sel if f not in value]:
        raise Problem(f"fake: {op} has no field {missing[0]}")
    return {f: project(value[f], s, op) for f, s in sel.items()}


def _text(op):
    if op in BUILT:
        return BUILT[op]
    module, name = op.split(".")
    name, _, key = name.partition("[")
    value = getattr(MODULES[module], name)
    return value[key.removesuffix("]")] if key else value


def texts():
    return {op: _text(op) for op in OPS}


def _page(nodes, first, after=None):
    ids = [n["id"] for n in nodes]
    if after is not None and after not in ids:
        raise Problem(f"fake: no cursor {after}")
    start = 0 if after is None else ids.index(after) + 1
    page = nodes[start : start + first]
    return {
        "nodes": page,
        "pageInfo": {
            "hasNextPage": start + first < len(nodes),
            "endCursor": page[-1]["id"] if page else None,
        },
    }


@functools.cache
def _firsts(query):
    """Each connection's literal first, and whether it reads after $c."""
    return re.findall(r"(\w+)\(first: (\d+)(, after: \$c)?", query)


def _relation_id(type, issue, related):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fake-linear/{type}/{issue}/{related}"))


def _paged_op(query, *, viewer=False):
    return lambda fake, caller, v: fake._paged(caller, v, query, viewer=viewer)


def index(named):
    out = {}
    for name, text in named.items():
        if text in out:
            raise ValueError(f"one query text, two ops: {out[text]}, {name}")
        out[text] = name
    return out


class FakeLinear:
    def __init__(self, harness=None):
        self.harness, self.lock = harness, threading.RLock()
        self.ops = dict(TABLE)
        self.requests, self.failures = [], []
        self.users, self.issues, self.labels, self.projects, self.relations = {}, {}, {}, {}, []
        self.clock = itertools.count()

    def user(self, email, name, service=None):
        with self.lock:
            if any(
                u["email"].lower() == email.lower() or (service and u["service"] == service)
                for u in self.users.values()
            ):
                raise ValueError(f"user {email} or service {service} seeded twice")
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "email": email, "name": name, "service": service}
            return uid

    def keys(self):
        with self.lock:
            return {u["service"]: _key(u["service"]) for u in self.users.values() if u["service"]}

    def label_group(self, id, name, labels):
        with self.lock:
            self.labels[id] = {"id": id, "name": name, "isGroup": True, "parent": None}
            for lid, lname in labels.items():
                self.labels[lid] = {"id": lid, "name": lname, "isGroup": False, "parent": id}

    def issue(
        self,
        title="Issue",
        *,
        identifier=None,
        id=None,
        state="todo",
        assignee=None,
        project=None,
        description="",
        priority=0,
        labels=(),
        created=None,
        parent=None,
    ):
        with self.lock:
            if (
                assignee is not None
                and assignee not in self.users
                or any(i not in self.labels for i in labels)
                or parent is not None
                and self.find(parent) is None
            ):
                raise ValueError("assignee, a label or the parent not seeded")
            if project:
                self.projects[project["id"]] = project["name"]
            try:
                return self._add(
                    id or str(uuid.uuid4()),
                    identifier,
                    title=title,
                    description=description,
                    priority=priority,
                    state=IDS[state],
                    assignee=assignee,
                    project=project and dict(project),
                    labels=list(labels),
                    createdAt=stamp(created),
                    parent=parent and self.find(parent)["id"],
                )
            except Problem as e:
                raise ValueError(str(e)) from None

    def comment(self, issue, user, body, *, at=None):
        with self.lock:
            if user is not None and user not in self.users:
                raise ValueError(f"user {user} not seeded")
            return self._comment(self._get(issue), user, body, stamp(at))

    def move(self, issue, to, *, actor, at=None, bot=False):
        """actor None: Linear automation's; bot: an integration's, credited to actor."""
        with self.lock:
            self._move(self._get(issue), IDS[to], actor, stamp(at), bot=bot)

    def relate(self, type, issue, related):
        with self.lock:
            a, b = self.find(issue), self.find(related)
            if a is None or b is None:
                raise ValueError(f"issue {(issue if a is None else related)} not seeded")
            self.relations.append((type, a["id"], b["id"]))

    def find(self, ref):
        with self.lock:
            return self.issues.get(ref) or next(
                (i for i in self.issues.values() if i["identifier"] == ref), None
            )

    def fail(self, op, kind, *, account=None, times=1, seconds=3):
        with self.lock:
            if (
                op not in OPS
                or kind not in KINDS
                or (kind == "unsuccessful" and not texts()[op].startswith("mutation"))
                or times < 1
                or (
                    account is not None
                    and not any(
                        u["service"] and u["email"].lower() == account.lower()
                        for u in self.users.values()
                    )
                )
            ):
                raise ValueError(
                    f"fail({op!r}, {kind!r}, account={account!r}, times={times!r}): a misuse"
                )
            self.failures.append(_Failure(op, kind, account, times, seconds))

    def answer(self, query, variables, key):
        with self.lock:
            caller = next(
                (u for u in self.users.values() if u["service"] and _key(u["service"]) == key),
                None,
            )
            account = caller and caller["email"]
            if not isinstance(query, str) or not isinstance(variables, (dict, type(None))):
                return self._log(
                    op=None,
                    account=account,
                    variables={},
                    result="error",
                    status=400,
                    body=_errors("fake: bad request: not a query string and a variables object"),
                )
            variables, op = variables or {}, self.ops.get(query)
            if caller is None:
                return self._log(
                    op=op or query,
                    account=None,
                    variables=variables,
                    result="unauthorized",
                    status=401,
                    body=_errors("fake: unauthorized"),
                )
            if op is None:
                return self._log(
                    op=query,
                    account=account,
                    variables=variables,
                    result="unknown",
                    status=200,
                    body=_errors("fake: unknown query"),
                )
            failure = self._failure(op, account)
            kind = failure.kind if failure else "ok"
            if kind in ("error", "503"):
                return self._log(
                    op=op,
                    account=account,
                    variables=variables,
                    result=kind,
                    status=int(kind) if kind == "503" else 200,
                    body=_errors(f"fake: {op}: injected {kind}"),
                )
            try:
                sel = selection(query)
                if kind == "unsuccessful":
                    top = next(iter(sel))
                    data = {top: {f: False if f == "success" else None for f in sel[top]}}
                else:
                    data = project(OPS[op](self, caller, variables), sel, op)
            except (Problem, KeyError, TypeError, ValueError, LookupError, AttributeError) as e:
                message = (
                    str(e) if isinstance(e, Problem) else f"fake: {op}: {type(e).__name__}: {e}"
                )
                return self._log(
                    op=op,
                    account=account,
                    variables=variables,
                    result="error",
                    status=200,
                    body=_errors(message),
                )
            return self._log(
                op=op,
                account=account,
                variables=variables,
                result=kind,
                status=200,
                body={"data": data},
                delay=failure.seconds if kind == "hang" else 0,
            )

    def __call__(self, query, *, timeout=30, service=None, **variables):
        service = service or self.harness
        keys = self.keys()
        if service not in keys:
            raise LinearError(f"fake transport: no key for {service}")
        status, body, delay = self.answer(query, variables, keys[service])
        if delay:
            time.sleep(min(delay, timeout))
            if delay >= timeout:
                raise types.Unavailable(client.operation(query), reason="TimeoutError: timed out")
        if status >= 500:
            raise types.Unavailable(client.operation(query), status=status)
        if status != 200:
            raise urllib.error.HTTPError(
                "fake_linear",
                status,
                HTTPStatus(status).phrase,
                None,
                io.BytesIO(json.dumps(body).encode()),
            )
        if body.get("errors"):
            raise LinearError(f"linear api error: {body['errors']}")
        return body["data"]

    def _log(self, *, op, account, variables, result, status, body, delay=0):
        self.requests.append(Request(op, account, variables, result))
        return status, body, delay

    def _failure(self, op, account):
        for f in self.failures:
            if f.op == op and (f.account is None or f.account.lower() == account.lower()):
                f.times -= 1
                if f.times <= 0:
                    self.failures.remove(f)
                return f
        return None

    def _get(self, ref):
        if (rec := self.find(ref)) is None:
            raise Problem(f"Entity not found: {'Issue'}")
        return rec

    def _add(self, id, identifier, **fields):
        numbers = [int(i["identifier"].split("-")[1]) for i in self.issues.values()]
        identifier = identifier or f"{KEY}-{max(numbers, default=0) + 1}"
        if self.find(id) or self.find(identifier) or not re.fullmatch(rf"{KEY}-\d+", identifier):
            raise Problem(
                f"fake: issue id {id} or identifier {identifier} in use, or not {KEY}-<n>"
            )
        self.issues[id] = {
            "id": id,
            "identifier": identifier,
            "url": f"https://linear.app/fake/issue/{identifier}",
            "parent": None,
            "touched": next(self.clock),
            **fields,
            "comments": [],
            "attachments": [],
            "subscribers": [],
            "history": [],
            "archived": False,
        }
        return self.issues[id]

    def _comment(self, rec, user, body, at, cid=None):
        if cid is None:
            cid = str(uuid.uuid4())
        elif self._comment_of(cid) is not None:
            raise Problem(f"fake: comment id {cid} in use")
        rec["comments"].append({"id": cid, "body": body, "createdAt": at, "user": user})
        self._touch(rec)
        return cid

    def _touch(self, *recs):
        for rec in recs:
            rec["touched"] = next(self.clock)

    def _children(self, rec):
        return [i for i in self.issues.values() if i["parent"] == rec["id"] and not i["archived"]]

    def _labelled(self, lids):
        """lids, each a seeded label, no label group and no two of one group."""
        if any(i not in self.labels or self.labels[i]["isGroup"] for i in lids):
            raise Problem(f"Entity not found: {'IssueLabel'}")
        groups = [self.labels[i]["parent"] for i in lids if self.labels[i]["parent"]]
        if dup := next((g for g in groups if groups.count(g) > 1), None):
            raise Problem(f"fake: two labels of group {dup}")
        return list(lids)

    def _move(self, rec, state, actor, at, *, bot=False):
        if state != rec["state"]:
            move = {
                "createdAt": at,
                "actorId": actor,
                "fromStateId": rec["state"],
                "toStateId": state,
            }
            rec["history"].append(move | ({"bot": True} if bot else {}))
            rec["state"] = state
            self._touch(rec)

    def _entered(self, rec, kind):
        """When rec last moved into its state, if that state is of type kind (Linear clears it on
        leaving); else None."""
        if TYPES[NAMES[rec["state"]]] != kind:
            return None
        return max(
            (h["createdAt"] for h in rec["history"] if h["toStateId"] == rec["state"]), default=None
        )

    def _state(self, sid):
        return {"id": sid, "name": TITLES[NAMES[sid]], "type": TYPES[NAMES[sid]]}

    def _team(self):
        return {
            "id": TEAM,
            "name": "Team",
            "key": KEY,
            "states": {"nodes": [self._state(i) for i in IDS.values()]},
        }

    def _person(self, uid, caller):
        u = self.users.get(uid) if uid else None
        return u and {
            "id": u["id"],
            "email": u["email"],
            "name": u["name"],
            "isMe": u["id"] == caller["id"],
        }

    def _label(self, lid):
        parent = self.labels[lid]["parent"]
        return {
            **{k: self.labels[lid][k] for k in ("id", "name", "isGroup")},
            "parent": parent and {"id": parent},
        }

    def _brief(self, iid):
        rec = self.issues[iid]
        return {
            **{k: rec[k] for k in ("id", "identifier", "title", "url")},
            "state": self._state(rec["state"]),
        }

    def _view(self, rec, caller):
        out = {
            k: rec[k]
            for k in (
                "id",
                "identifier",
                "url",
                "title",
                "description",
                "priority",
                "createdAt",
                "project",
            )
        }
        comments = [
            {
                **c,
                "user": self._person(c["user"], caller),
                **dict.fromkeys(("parentId", "quotedText", "onBehalfOf")),
            }
            for c in rec["comments"]
        ]
        return {
            **out,
            "state": self._state(rec["state"]),
            "team": self._team(),
            "assignee": self._person(rec["assignee"], caller),
            "parent": rec["parent"] and self._brief(rec["parent"]),
            "projectMilestone": None,
            "labels": {"nodes": [self._label(i) for i in rec["labels"]], "pageInfo": ONE_PAGE},
            "comments": {"nodes": _newest(comments), "pageInfo": ONE_PAGE},
            "attachments": {"nodes": rec["attachments"]},
            "children": {
                "nodes": [self._view(c, caller) for c in self._children(rec)],
                "pageInfo": ONE_PAGE,
            },
            "archivedAt": stamp() if rec["archived"] else None,
            "updatedAt": max([rec["createdAt"], *(h["createdAt"] for h in rec["history"])]),
            "history": {
                "nodes": _newest(rec["history"]),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "canceledAt": self._entered(rec, "canceled"),
            "completedAt": self._entered(rec, "completed"),
            "relations": self._relations(rec, 1),
            "inverseRelations": self._relations(rec, 2),
        }

    def _relations(self, rec, end):
        """The relations whose end (1: issue, 2: relatedIssue) rec is."""
        nodes = [
            {
                "id": _relation_id(*r),
                "type": r[0],
                "issue": self._brief(r[1]),
                "relatedIssue": self._brief(r[2]),
            }
            for r in self.relations
            if r[end] == rec["id"]
        ]
        return {"nodes": nodes, "pageInfo": ONE_PAGE}

    def _list(self, caller, flt, first, after=None, *, archived=False):
        found = [
            i
            for i in self.issues.values()
            if (archived or not i["archived"]) and self._match(i, flt)
        ]
        start = int(after or 0)
        page = found[start : start + first]
        return {
            "issues": {
                "nodes": [self._view(i, caller) for i in page],
                "pageInfo": {
                    "hasNextPage": start + first < len(found),
                    "endCursor": str(start + len(page)) if page else None,
                },
            }
        }

    def _match(self, rec, flt):
        values = {
            "id": rec["id"],
            "number": int(rec["identifier"].split("-")[1]),
            "team": TEAM,
            "state": rec["state"],
            "assignee": rec["assignee"],
            "project": rec["project"] and rec["project"]["id"],
            "parent": rec["parent"],
            "priority": rec["priority"],
            "title": rec["title"],
            "description": rec["description"],
        }
        for field, cond in flt.items():
            if field in ("or", "and"):
                if not (any if field == "or" else all)(self._match(rec, f) for f in cond):
                    return False
                continue
            if field == "labels":
                if list(cond) != ["some"] or list(cond["some"]) != ["id"]:
                    raise Problem(f"fake: no label filter {cond}")
                some = cond["some"]["id"].items()
                if not any(all(COMPARATORS[c](i, a) for c, a in some) for i in rec["labels"]):
                    return False
                continue
            if field not in values:
                raise Problem(f"fake: no issue filter {field}")
            for c, arg in (cond["id"] if field != "id" and "id" in cond else cond).items():
                if c not in COMPARATORS:
                    raise Problem(f"fake: no comparator {c}")
                if not COMPARATORS[c](values[field], arg):
                    return False
        return True

    def _comment_of(self, cid):
        return next(
            ((i, c) for i in self.issues.values() for c in i["comments"] if c["id"] == cid), None
        )

    def _issue(self, caller, v):
        return {"issue": self._view(self._get(v["i"]), caller)}

    def _paged(self, caller, v, query, *, viewer):
        """v["i"]'s view, each connection query names cut to its first, after $c if it says so."""
        view = self._view(self._get(v["i"]), caller)
        for name, first, after in _firsts(query):
            view[name] = _page(view[name]["nodes"], int(first), v.get("c") if after else None)
        return {"issue": view} | ({"viewer": self._viewer(caller, v)["viewer"]} if viewer else {})

    def _viewer(self, caller, v):
        return {"viewer": self._person(caller["id"], caller)}

    def _issue_ref(self, caller, v):
        return {"issue": self._brief(self._get(v["id"])["id"])}

    def _issue_list(self, caller, v):
        found = sorted(
            (
                i
                for i in self.issues.values()
                if (v.get("a") or not i["archived"]) and self._match(i, v.get("f") or {})
            ),
            key=lambda i: i["touched"],
            reverse=True,
        )
        page = _page(found, v["first"], v.get("after"))
        edges = []
        for rec in page["nodes"]:
            node = self._view(rec, caller)
            for name, first, _ in _firsts(issue_list.Q_LIST):
                node[name] = _page(node[name]["nodes"], int(first))
            edges.append({"cursor": rec["id"], "node": node})
        return {"issues": {"edges": edges, "pageInfo": page["pageInfo"]}}

    def _users(self, caller, v):
        return {
            "users": {
                "nodes": [
                    self._person(i, caller)
                    for i, u in self.users.items()
                    if u["email"].lower() == v["e"].lower()
                ]
            }
        }

    def _history(self, caller, v):
        found = _newest(self._get(v["i"])["history"])
        start = int(v.get("after") or 0)
        page = [
            h
            | {
                "actor": self._person(h["actorId"], caller),
                "botActor": {"type": "github"} if h.get("bot") else None,
            }
            for h in found[start : start + 250]
        ]
        return {
            "issue": {
                "history": {
                    "nodes": page,
                    "pageInfo": {
                        "hasNextPage": start + 250 < len(found),
                        "endCursor": str(start + len(page)) if page else None,
                    },
                }
            }
        }

    def _state_name(self, caller, v):
        return {"workflowState": {"name": TITLES[NAMES[v["i"]]]}}

    def _teams(self, caller, v):
        return {"teams": {"nodes": [self._team()] if v.get("t") == TEAM else []}}

    def _setup(self, caller, v):
        """Q_SETUP's users filter: an or of email eqIgnoreCase."""
        emails = {f["email"]["eqIgnoreCase"].lower() for f in v["u"]["or"]}
        found = [
            self._person(i, caller) for i, u in self.users.items() if u["email"].lower() in emails
        ]
        return {**self._teams(caller, v), "users": {"nodes": found}}

    def _label_group(self, caller, v):
        if v["i"] not in self.labels:
            raise Problem(f"Entity not found: {'IssueLabel'}")
        children = [self._label(i) for i, label in self.labels.items() if label["parent"] == v["i"]]
        return {"issueLabel": {**self._label(v["i"]), "children": {"nodes": children}}}

    def _related(self, caller, v):
        return {"searchIssues": {"nodes": []}, "issues": {"nodes": []}}

    def _board(self, caller, v):
        return self._list(caller, v.get("f") or {}, 100)

    def _handoff(self, caller, v):
        return self._list(
            caller,
            {
                "team": {"id": {"eq": v.get("t")}},
                "state": {"id": {"eq": v.get("s")}},
                "project": {"null": False},
                "assignee": {"id": {"in": v.get("a")}},
            },
            100,
        )

    def _child(self, caller, v):
        return self._list(caller, {"id": {"eq": v["c"]}}, 50, archived=True)

    def _finished(self, caller, v):
        return self._list(
            caller,
            {
                "team": {"id": {"eq": v.get("t")}},
                "or": [
                    {"state": {"id": {"in": v.get("d")}}, "assignee": {"id": {"in": v.get("a")}}},
                    {"state": {"id": {"in": v.get("s")}}},
                ],
            },
            50,
            v.get("c"),
        )

    def _snapshots(self, caller, v):
        return self._list(caller, v["f"], 50, archived=True)

    def _changed(self, caller, v):
        """Every issue: the fake keeps no update time."""
        flt = {k: c for k, c in v["f"].items() if k != "updatedAt"}
        return self._list(caller, flt, 100, v.get("c"), archived=True)

    def _comment_create(self, caller, v, cid=None):
        self._comment(self._get(v["i"]), caller["id"], v["b"], stamp(), cid)
        return {"commentCreate": {"success": True}}

    def _comment_create_id(self, caller, v):
        return self._comment_create(caller, v, v["c"])

    def _comment_by_id(self, caller, v):
        if (found := self._comment_of(v["c"])) is None:
            raise Problem(f"Entity not found: {'Comment'}")
        rec, c = found
        return {
            "comment": {**c, "issue": {"id": rec["id"]}, "user": self._person(c["user"], caller)}
        }

    def _comment_update(self, caller, v):
        if (found := self._comment_of(v["c"])) is None:
            raise Problem(f"Entity not found: {'Comment'}")
        _, c = found
        if c["user"] != caller["id"]:
            raise Problem("fake: commentUpdate: only its author edits a comment")
        c["body"] = v["b"]
        return {"commentUpdate": {"success": True}}

    def _set_state(self, caller, v):
        if v["s"] not in NAMES:
            raise Problem(f"Entity not found: {'WorkflowState'}")
        self._move(self._get(v["i"]), v["s"], caller["id"], stamp())
        return {"issueUpdate": {"success": True}}

    def _set_title(self, caller, v):
        rec = self._get(v["i"])
        rec["title"] = v["t"]
        self._touch(rec)
        return {"issueUpdate": {"success": True}}

    def _update(self, caller, v):
        rec, new = self._get(v["i"]), v["in"]
        if extra := sorted(set(new) - UPDATE):
            raise Problem(f"fake: issueUpdate has no input {extra[0]}")
        if "stateId" in new and new["stateId"] not in NAMES:
            raise Problem(f"Entity not found: {'WorkflowState'}")
        if new.get("assigneeId") is not None and new["assigneeId"] not in self.users:
            raise Problem(f"Entity not found: {'User'}")
        removed = set(new.get("removedLabelIds", ()))
        labels = [i for i in rec["labels"] if i not in removed]
        labels = self._labelled(
            labels + [i for i in new.get("addedLabelIds", ()) if i not in labels]
        )
        for key in ("title", "description", "priority"):
            if key in new:
                rec[key] = new[key]
        if "assigneeId" in new:
            rec["assignee"] = new["assigneeId"]
        rec["labels"] = labels
        if "stateId" in new:
            self._move(rec, new["stateId"], caller["id"], stamp())
        self._touch(rec)
        return {"issueUpdate": {"success": True}}

    def _comment_with_id(self, caller, v):
        cid = self._comment(self._get(v["i"]), caller["id"], v["b"], stamp())
        return {"commentCreate": {"success": True, "comment": {"id": cid}}}

    def _subscribe(self, caller, v):
        rec = self._get(v["i"])
        user = next((u for u in self.users.values() if u["email"].lower() == v["e"].lower()), None)
        if user is None:
            raise Problem(f"Entity not found: {'User'}")
        if user["email"] not in rec["subscribers"]:
            rec["subscribers"].append(user["email"])
        return {"issueSubscribe": {"success": True}}

    def _attach(self, caller, v):
        rec = self._get(v["i"])
        if all(a["url"] != v["u"] for a in rec["attachments"]):
            rec["attachments"].append({"url": v["u"], "title": v.get("t")})
        return {"attachmentLinkURL": {"success": True}}

    def _archive(self, caller, v):
        """With its sub-issues, as Linear archives them."""
        level = [self._get(v["i"])]
        while level:
            for rec in level:
                rec["archived"] = True
                self._touch(rec)
            level = [c for rec in level for c in self._children(rec)]
        return {"issueArchive": {"success": True}}

    def _unarchive(self, caller, v):
        rec = self._get(v["i"])
        rec["archived"] = False
        self._touch(rec)
        return {"issueUnarchive": {"success": True}}

    def _issue_create(self, caller, v):
        return self._create(caller, v, ISSUE_CREATE)

    def _create(self, caller, v, inputs=CREATE):
        new, now = v["in"], stamp()
        pid, uid, sid = (
            new.get("projectId"),
            new.get("assigneeId"),
            new.get("stateId", STATES["todo"]),
        )
        if extra := sorted(set(new) - inputs):
            raise Problem(f"fake: issueCreate has no input {extra[0]}")
        if (
            new.get("teamId") != TEAM
            or (pid and pid not in self.projects)
            or (uid and uid not in self.users)
            or sid not in NAMES
        ):
            raise Problem(
                f"Entity not found: {', '.join(('Team', 'Project', 'User', 'WorkflowState')[:-1])} or {('Team', 'Project', 'User', 'WorkflowState')[-1]}"
            )
        labels = self._labelled(new.get("labelIds", []))
        parent = new.get("parentId") and self._get(new["parentId"])["id"]
        rec = self._add(
            new.get("id") or str(uuid.uuid4()),
            None,
            title=new.get("title", ""),
            description=new.get("description"),
            priority=new.get("priority", 0),
            state=sid,
            assignee=uid,
            project=pid and {"id": pid, "name": self.projects[pid]},
            labels=labels,
            createdAt=now,
            parent=parent,
        )
        return {"issueCreate": {"success": True, "issue": self._view(rec, caller)}}

    def _relate(self, caller, v):
        new = v["in"]
        self._add_relation(new["type"], new["issueId"], new["relatedIssueId"])
        return {"issueRelationCreate": {"success": True}}

    def _relate_blocks(self, caller, v):
        self._add_relation("blocks", v["i"], v["r"])
        return {"issueRelationCreate": {"success": True}}

    def _add_relation(self, type, issue, related):
        a, b = self._get(issue), self._get(related)
        self.relations.append((type, a["id"], b["id"]))
        self._touch(a, b)

    def _unrelate(self, caller, v):
        found = next((r for r in self.relations if _relation_id(*r) == v["i"]), None)
        if found is None:
            raise Problem(f"Entity not found: {'IssueRelation'}")
        self.relations.remove(found)
        self._touch(self.issues[found[1]], self.issues[found[2]])
        return {"issueRelationDelete": {"success": True}}


OPS = {
    "linear.M_COMMENT": FakeLinear._comment_create,
    "linear.M_COMMENT_ID": FakeLinear._comment_create_id,
    "linear.Q_COMMENT": FakeLinear._comment_by_id,
    "linear.M_STATE": FakeLinear._set_state,
    "linear.M_SUBSCRIBE": FakeLinear._subscribe,
    "linear.Q_ISSUE_STATE": FakeLinear._issue,
    "linear.Q_USER": FakeLinear._users,
    "linear.Q_TEAM": FakeLinear._teams,
    "linear.Q_SETUP": FakeLinear._setup,
    "linear.Q_TASK_GROUP": FakeLinear._label_group,
    "router.Q_RELATIONS": FakeLinear._issue,
    "router.Q_RECHECK": FakeLinear._issue,
    "router.Q_HISTORY": FakeLinear._history,
    "router.issues": FakeLinear._board,
    "router.issues+relations": FakeLinear._board,
    "issues.Q_ISSUE": FakeLinear._issue,
    "sessions.Q_STATE_NAME": FakeLinear._state_name,
    "sessions.M_UPDATE": FakeLinear._comment_update,
    "writeback.M_TITLE": FakeLinear._set_title,
    "writeback.M_ATTACH": FakeLinear._attach,
    "writeback.M_UNARCHIVE": FakeLinear._unarchive,
    "writeback.Q_ATTACHED": FakeLinear._issue,
    "writeback.Q_ID": FakeLinear._issue,
    "promote.Q_HANDOFF": FakeLinear._handoff,
    "promote.Q_DETAIL": FakeLinear._issue,
    "promote.Q_CHILD": FakeLinear._child,
    "promote.M_CREATE": FakeLinear._create,
    "promote.M_RELATE": FakeLinear._relate,
    "prune.Q_ISSUE": FakeLinear._issue,
    "prune.Q_FINISHED": FakeLinear._finished,
    "prune.Q_SNAPSHOTS": FakeLinear._snapshots,
    "prune.Q_CHANGED": FakeLinear._changed,
    "prune.Q_ARCHIVE": FakeLinear._issue,
    "prune.Q_CHILDREN": FakeLinear._issue,
    "prune.M_ARCHIVE": FakeLinear._archive,
    "related.Q_SEARCH": FakeLinear._related,
    "related.Q_SEARCH_IDS": FakeLinear._related,
    "related.Q_IDS": FakeLinear._related,
    "related.Q_UPSTREAM": FakeLinear._issue,
    "issue_read.Q_READ": _paged_op(issue_read.Q_READ),
    "issue_read.Q_FULL": _paged_op(issue_read.Q_FULL),
    **{f"issue_read.MORE[{n}]": _paged_op(q) for n, q in issue_read.MORE.items()},
    "issue_list.Q_LIST": FakeLinear._issue_list,
    "issue_create.M_CREATE": FakeLinear._issue_create,
    "issue_create.Q_VIEWER": FakeLinear._viewer,
    "issue_names.Q_VIEWER": FakeLinear._viewer,
    "issue_refs.Q_ISSUE": FakeLinear._issue_ref,
    "issue_edit.Q_READ": _paged_op(issue_edit.Q_READ, viewer=True),
    "issue_edit.M_EDIT": FakeLinear._update,
    "issue_label.Q_MORE": _paged_op(issue_label.Q_MORE),
    "issue_write.Q_MOVE": _paged_op(issue_write.Q_MOVE, viewer=True),
    "issue_write.Q_COMMENT": _paged_op(issue_write.Q_COMMENT, viewer=True),
    "issue_archive.Q_ISSUE": _paged_op(issue_archive.Q_ISSUE, viewer=True),
    "issue_write.M_COMMENT": FakeLinear._comment_with_id,
    **{
        f"issue_relation.Q_READ[{d}]": _paged_op(q, viewer=True)
        for d, q in issue_relation.Q_READ.items()
    },
    **{f"issue_relation.Q_MORE[{d}]": _paged_op(q) for d, q in issue_relation.Q_MORE.items()},
    "issue_relation.M_ADD": FakeLinear._relate_blocks,
    "issue_relation.M_REMOVE": FakeLinear._unrelate,
}

TABLE = index(texts())


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins the handlers: none outlives the case


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/graphql":
            return self._send(404, _errors("fake: not found"))
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            req = None
        req = req if isinstance(req, dict) else {}
        status, body, delay = self.server.fake.answer(
            req.get("query"), req.get("variables"), self.headers.get("Authorization")
        )
        if delay and self.server.closing.wait(delay):
            return
        self._send(status, body)

    def do_GET(self):
        self._send(405, _errors("fake: POST only"))

    do_PUT = do_PATCH = do_DELETE = do_GET

    def _send(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def serve(case, fake):
    server = _Server(("127.0.0.1", 0), _Handler)
    server.fake, server.closing = fake, threading.Event()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()

    def stop():
        server.closing.set()
        server.shutdown()
        server.server_close()
        thread.join()

    case.addCleanup(stop)
    return f"http://127.0.0.1:{server.server_address[1]}/graphql"


def seam(case, fake, url):
    fd, path = tempfile.mkstemp(prefix="fake-linear-", suffix=".json")
    case.addCleanup(os.remove, path)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "keys": fake.keys()}, f)
    return path
