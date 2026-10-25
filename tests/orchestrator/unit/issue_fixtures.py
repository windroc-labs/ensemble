"""Linear issue nodes, as `ensemble-orchestrator issue read`, `move`, `comment`, `archive`, `relation`,
`label swap` and `edit` query them, and recording fake gqls; the lookup and list entities by edge with Routes, a fake gql answering each
by its root field; IssueCase runs issue.main against a test configuration, names_cfg is its
abstract names' part; list_query, new_issue and issue_change build the backends' requests.
"""

import io
import math
import os
import re
from types import SimpleNamespace

from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueEdit,
    LinearError,
    ListQuery,
    NewIssue,
)
from ensemble.orchestrator.issue import command as issue
from ensemble.orchestrator.trackers.linear import (
    client,
    issue_edit,
    issue_label,
    issue_names,
    issue_read,
    issue_refs,
    issue_relation,
    linear_tracker,
    ops,
)
from tests.orchestrator.case import Case
from tests.orchestrator.unit import promote_fixtures as tp
from tests.orchestrator.unit.board_ids import (
    ACCOUNTS,
    HEADER,
    STATES,
    TASK_GROUP,
    TEAM,
    role_app,
    role_id,
)
from tests.orchestrator.unit.run_fixtures import CORE_LOCAL

OTHER_TEAM = "00000000-0000-4000-8000-0000000000f2"

MANAGER_KEY = "linear-api-key-manager"


def uid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


TASK_LABELS = {"build": uid(0xB1), "light-build": uid(0xB2)}
CLIENT_GROUP = uid(0xC0)
CLIENT_LABELS = {"codex": uid(0xC1), "claude": uid(0xC2)}
# each role's account's Linear user id
ROLE_USERS = {a: role_id(r) for r, a in ACCOUNTS.items()}
CONFIG = f'linear.manager_key = "{MANAGER_KEY}"\n' + tp.CONFIG.replace(
    HEADER,
    HEADER
    + "linear.task_labels = { "
    + ", ".join(f'{k} = "{v}"' for k, v in TASK_LABELS.items())
    + f' }}\nlinear.agent_label_group = "{CLIENT_GROUP}"\nlinear.agent_labels = {{ '
    + ", ".join(f'{k} = "{v}"' for k, v in CLIENT_LABELS.items())
    + " }\n",
)


def names_cfg(**fields):
    """The LinearSettings fields issue_names reads, as CONFIG loads them."""
    cfg = {
        "team": TEAM,
        "states": dict(STATES),
        "roles": {r: role_app(r) for r in ACCOUNTS},
        "task_label_group": TASK_GROUP,
        "task_labels": dict(TASK_LABELS),
        "agent_label_group": CLIENT_GROUP,
        "agent_labels": dict(CLIENT_LABELS),
    }
    return SimpleNamespace(**{**cfg, **fields})


def list_query(**given):
    """A ListQuery of only the given filters."""
    none = ("query", "state", "role", "task", "client", "priority", "parent", "label", "project")
    return ListQuery(**{**dict.fromkeys(none), "include_archived": False, **given})


def new_issue(**given):
    """A NewIssue titled t, body b, with only the given fields and relations."""
    none = ("state", "role", "task", "client", "priority", "parent", "project", "milestone")
    empty = {"labels": (), "blocks": (), "blocked_by": ()}
    return NewIssue(**{"title": "t", "body": "b", **dict.fromkeys(none), **empty, **given})


def issue_change(ident, **given):
    """An IssueEdit of ident changing only the given fields."""
    none = ("title", "body", "role", "project", "task", "client", "priority")
    return IssueEdit(**{"id": ident, **dict.fromkeys(none), **given})


def role_users(variables):
    """A Routes users answer to ops.Q_USER: the user of a role account, else none."""
    found = ROLE_USERS.get(variables["e"])
    return {"nodes": [{"id": found}] if found else []}


class IssueCase(Case):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.home, "root")
        os.makedirs(self.root)
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL)
        self.config = os.path.join(self.root, "config.toml")
        with open(self.config, "w") as f:
            f.write(CONFIG)

    def run_main(self, argv, gql, stdin=b""):
        out, err = io.BytesIO(), io.StringIO()
        code = issue.main(
            argv, gql=gql, config_path=self.config, out=out, err=err, stdin=io.BytesIO(stdin)
        )
        return code, out.getvalue(), err.getvalue()


def rel(ident, *, title=None, state="Todo"):
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": title or f"title {ident}",
        "state": {"name": state},
    }


def conn(nodes, *, more=False, cursor=None):
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def comment(n, **fields):
    return {
        "id": f"c{n}",
        "body": f"comment {n}",
        "createdAt": f"2026-10-{n:02d}T00:00:00.000Z",
        "parentId": None,
        "quotedText": None,
        "user": {"id": "u-me", "name": "Me"},
        "onBehalfOf": None,
        **fields,
    }


def issue_node(ident="TASK-2", **fields):
    node = {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": f"title {ident}",
        "url": f"https://linear.app/t/issue/{ident}",
        "description": None,
        "state": {"id": "s-todo", "name": "Todo", "type": "unstarted"},
        "project": None,
        "projectMilestone": None,
        "assignee": None,
        "parent": None,
        **{name: conn([]) for name in issue_read.CONNECTIONS},
    }
    node.update(fields)
    return node


class Gql:
    """Answers the read query with `node`, each later page of a connection from `pages[name]` (in
    order; an exception there is raised, None or a dict without nodes answers as the whole issue), and records every (query, variables) call; an exception
    in `fail` is raised instead.
    """

    def __init__(self, node, pages=None, fail=None):
        self.node, self.fail, self.calls = node, fail, []
        self.pages = {name: list(found) for name, found in (pages or {}).items()}

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if self.fail is not None:
            raise self.fail
        for name, more in issue_read.MORE.items():
            if query == more:
                page = self.pages[name].pop(0)
                if isinstance(page, BaseException):
                    raise page
                return (
                    {"issue": page}
                    if page is None or "nodes" not in page
                    else {"issue": {name: page}}
                )
        return {"issue": self.node}


def edges(nodes, *, more=False, cursor=None):
    found = [{"cursor": f"cur-{n['id']}", "node": n} for n in nodes]
    return {"edges": found, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def team_ref(n, key, name=None):
    return {"id": uid(n), "name": name or f"team {key}", "key": key}


def state(n, name, kind="unstarted", team_id=TEAM):
    return {"id": uid(n), "name": name, "type": kind, "team": {"id": team_id}}


def label(n, name, team_id=TEAM, *, group=None, is_group=False, retired=None):
    return {
        "id": uid(n),
        "name": name,
        "isGroup": is_group,
        "retiredAt": retired,
        "team": {"id": team_id} if team_id else None,
        "parent": {"id": group} if group else None,
    }


def project(n, name, team_ids=(TEAM,), *, more=False):
    teams = {"nodes": [{"id": t} for t in team_ids], "pageInfo": {"hasNextPage": more}}
    return {"id": uid(n), "name": name, "teams": teams}


def milestone(n, name, project_id):
    return {"id": uid(n), "name": name, "project": {"id": project_id}}


def user(n, name, email=None, *, active=True):
    return {"id": uid(n), "name": name, "email": email or f"u{n}@example.com", "active": active}


LIMIT = 10_000  # Linear's per-query complexity limit


def complexity(query, variables):
    """Linear's score: a field 0.1, an object 1, a connection its fields times first (or 50)."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\$?\w+|\S', query)
    tenths, _, _ = _selection(tokens, tokens.index("{") + 1, variables)
    return math.ceil(tenths / 10)


def _selection(tokens, at, variables):
    tenths, names = 0, set()
    while tokens[at] != "}":
        name, at, args = tokens[at], at + 1, []
        names.add(name)
        if tokens[at] == "(":
            depth = 0
            while depth or not args:
                depth += {"(": 1, ")": -1}.get(tokens[at], 0)
                args.append(tokens[at])
                at += 1
        if tokens[at] != "{":
            tenths += 1
            continue
        inner, fields, at = _selection(tokens, at + 1, variables)
        if fields & {"nodes", "edges"}:
            first = args[args.index("first") + 2] if "first" in args else "50"
            tenths += inner * int(variables[first[1:]] if first.startswith("$") else first)
        else:
            tenths += 10 + inner
    return tenths, names, at + 1


class Routes:
    """A fake gql answering a query by its root field (teams, team, issueLabels, …): routes[field]
    is a list of answers taken in order, else one answer for every call; an answer is the field's
    value, a callable of the variables giving it, or an exception it raises. strict: a query over
    LIMIT fails as Linear refuses it, an HTTP 400. Records every (query, variables) call.
    """

    def __init__(self, *, strict=False, **routes):
        self.routes = {k: list(v) if isinstance(v, list) else v for k, v in routes.items()}
        self.strict, self.calls = strict, []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if self.strict and complexity(query, variables) > LIMIT:
            raise LinearError(f"linear api error: {client.operation(query)}: HTTP 400")
        field = re.match(r"query[^{]*\{\s*(\w+)", query).group(1)
        answer = self.routes[field]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        answer = answer(variables) if callable(answer) else answer
        if isinstance(answer, BaseException):
            raise answer
        return {field: answer}

    def sent(self, field):
        """The variables of every call to field."""
        return [v for q, v in self.calls if re.match(r"query[^{]*\{\s*" + field + r"\b", q)]


def issue_row(ident, team_id=TEAM, **fields):
    """An issue node as ensemble-orchestrator issue list queries it."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": f"title {ident}",
        "url": f"https://linear.app/t/issue/{ident}",
        "state": {"id": uid(3), "name": "Todo"},
        "assignee": None,
        "project": None,
        "parent": None,
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "team": {"id": team_id},
        **fields,
    }


VIEWER = {"id": "u-me", "name": "Me"}


def write_issue(ident="TASK-2", *, state=STATES["todo"], team_id=TEAM):
    """An issue as `ensemble-orchestrator issue move` and `comment` read it."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "url": f"https://linear.app/t/issue/{ident}",
        "state": {"id": state},
        "team": {"id": team_id, "key": ident.split("-")[0]},
    }


class WriteGql:
    """Answers a write's read with `issue` (None: no such issue) and VIEWER, and each mutation with
    `result`, raised when an exception; an exception in `fail` is raised by the read instead.
    Records every (query, variables) call.
    """

    DEFAULTS = {
        "issueUpdate": {"success": True},
        "commentCreate": {"success": True, "comment": {"id": "c-new"}},
    }

    def __init__(self, issue, *, result=None, fail=None, viewer=VIEWER):
        self.issue, self.result, self.fail, self.viewer = issue, result, fail, viewer
        self.calls = []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            if isinstance(self.result, BaseException):
                raise self.result
            field = client.operation(query)
            return {field: self.DEFAULTS[field] if self.result is None else self.result}
        if self.fail is not None:
            raise self.fail
        return {"viewer": self.viewer, "issue": self.issue}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


def tree(ident="TASK-1", kind="completed", *children, archived=None):
    """An issue as `ensemble-orchestrator issue archive` reads it, its unarchived sub-issues children."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "url": f"https://linear.app/t/issue/{ident}",
        "archivedAt": archived,
        "updatedAt": "2026-10-01T00:00:00+00:00",
        "state": {"name": kind.title(), "type": kind},
        "kids": list(children),
    }


class ArchiveGql:
    """Answers issue archive's lookup with `root` (None: no such issue) and VIEWER, LinearTracker's
    archive read with root and its first children page, each later children page in pages of
    CHILDREN (cursor: the next offset), and the mutation with `result`, raised when an exception.
    `broken` maps (issue id, offset) to an exception that page raises, or None for a page missing.
    Records every (query, variables) call.
    """

    CHILDREN = 50

    def __init__(self, root, *, broken=None, result=None, viewer=VIEWER):
        self.root, self.broken, self.result, self.viewer = root, dict(broken or {}), result, viewer
        self.nodes, self.calls, stack = {}, [], [root] if root else []
        while stack:
            node = stack.pop()
            self.nodes[node["id"]] = node
            stack += node["kids"]

    def _page(self, node, offset):
        if (node["id"], offset) in self.broken:
            if (error := self.broken[node["id"], offset]) is not None:
                raise error
            return None
        kids = node["kids"][offset : offset + self.CHILDREN]
        more = offset + self.CHILDREN < len(node["kids"])
        nodes = [{k: kid[k] for k in ("id", "identifier", "updatedAt", "state")} for kid in kids]
        return conn(nodes, more=more, cursor=str(offset + self.CHILDREN) if more else None)

    def _plan(self, key):
        node = {k: v for k, v in self.nodes[key].items() if k != "kids"}
        return {"issue": node | {"children": self._page(self.nodes[key], 0)}}

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            if isinstance(self.result, BaseException):
                raise self.result
            return {"issueArchive": {"success": True} if self.result is None else self.result}
        if query == linear_tracker.Q_CHILDREN:
            page = self._page(self.nodes[variables["i"]], int(variables["c"] or 0))
            return {"issue": None if page is None else {"children": page}}
        if query == linear_tracker.Q_ARCHIVE:
            return self._plan(variables["i"])
        found = self.root and {k: self.root[k] for k in ("id", "identifier", "url")}
        return {"viewer": self.viewer, "issue": found}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


NEW = {"id": "id-TASK-9", "identifier": "TASK-9", "url": "https://linear.app/t/issue/TASK-9"}


def by_name(*nodes):
    """A Routes answer: the edges of nodes whose name the query's filter names exactly."""

    def find(found):
        if isinstance(found, dict):
            if isinstance(found.get("name"), dict) and "eq" in found["name"]:
                return found["name"]["eq"]
            found = list(found.values())
        if isinstance(found, list):
            return next((n for n in map(find, found) if n is not None), None)
        return None

    return lambda variables: edges([n for n in nodes if n["name"] == find(variables)])


class CreateGql(Routes):
    """Routes for create's reads; issueCreate answers `created` (default: NEW made), each
    issueRelationCreate the next of `relations` (default: success); an exception there is raised.
    Records every (query, variables) call.
    """

    def __init__(self, *, created=None, relations=(), **routes):
        super().__init__(**routes)
        self.created, self.relations = created, list(relations)

    def __call__(self, query, **variables):
        if not query.startswith("mutation"):
            return super().__call__(query, **variables)
        self.calls.append((query, variables))
        field = client.operation(query)
        if field == "issueCreate":
            answer = {"success": True, "issue": NEW} if self.created is None else self.created
        else:
            answer = self.relations.pop(0) if self.relations else {"success": True}
        if isinstance(answer, BaseException):
            raise answer
        return {field: answer}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


class RelationGql:
    """A board of issues by identifier, each one's id id-<identifier> unless `ids` names it, and its
    relations, records [relation id, type, from identifier, to identifier]: answers
    `ensemble-orchestrator issue relation`'s read with VIEWER and the issue's side in pages of
    issue_read.PAGE (cursor: the next offset), an issue reference read, and each mutation by
    applying it to relations, or with `result`, raised when an exception. Records every (query,
    variables) call.
    """

    def __init__(self, idents, relations=(), *, ids=None, result=None, viewer=VIEWER):
        self.ids = {ident: f"id-{ident}" for ident in idents} | dict(ids or {})
        self.result, self.viewer = result, viewer
        self.relations, self.calls = [list(r) for r in relations], []

    def _ident(self, value):
        found = [i for i, issue_id in self.ids.items() if value in (i, issue_id)]
        return found[0] if found else None

    def _side(self, ident, direction, offset):
        name, end = issue_relation.SIDES[direction]
        mine, other = (2, 3) if direction == "blocks" else (3, 2)
        nodes = [
            {"id": r[0], "type": r[1], end: {"id": self.ids[r[other]]}}
            for r in self.relations
            if r[mine] == ident
        ]
        page = nodes[offset : offset + issue_read.PAGE]
        more = offset + issue_read.PAGE < len(nodes)
        return name, conn(page, more=more, cursor=str(offset + issue_read.PAGE) if more else None)

    def _mutate(self, query, variables):
        if isinstance(self.result, BaseException):
            raise self.result
        field = client.operation(query)
        if self.result is not None:
            return {field: self.result}
        if field == "issueRelationCreate":
            pair = [self._ident(variables["i"]), self._ident(variables["r"])]
            self.relations.append([f"r-new-{len(self.calls)}", "blocks", *pair])
        else:
            self.relations = [r for r in self.relations if r[0] != variables["i"]]
        return {field: {"success": True}}

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            return self._mutate(query, variables)
        if query == issue_refs.Q_ISSUE:
            ident = self._ident(variables["id"])
            return {"issue": {"id": self.ids[ident], "identifier": ident} if ident else None}
        ident = self._ident(variables["i"])
        if ident is None:
            return {"viewer": self.viewer, "issue": None}
        for direction in issue_relation.SIDES:
            if query in (issue_relation.Q_READ[direction], issue_relation.Q_MORE[direction]):
                name, page = self._side(ident, direction, int(variables.get("c") or 0))
        node = {
            "id": self.ids[ident],
            "identifier": ident,
            "url": f"https://linear.app/t/issue/{ident}",
            name: page,
        }
        return {"viewer": self.viewer, "issue": node}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


class SecondFails(RelationGql):
    """RelationGql whose second mutation answers with `later`, raised when an exception."""

    def __init__(self, *args, later, **options):
        super().__init__(*args, **options)
        self.later = later

    def _mutate(self, query, variables):
        if len(self.writes) == 2:
            self.result = self.later
        return super()._mutate(query, variables)


def _issue_ref(variables):
    ident = variables["id"]
    if ident.startswith("NOPE"):
        return GraphQLErrors([{"message": "Entity not found: Issue"}])
    return {"id": f"id-{ident}", "identifier": ident}


def create_gql(**overrides):
    """A strict CreateGql knowing team TEAM's labels bug, ops (workspace) and old (retired), project
    Ensemble with milestone M1, the role accounts' users, and every issue but NOPE-*.
    """
    routes = {
        "issueLabels": by_name(
            label(4, "bug"), label(5, "ops", None), label(6, "old", retired="2026-01-01")
        ),
        "team": lambda v: {"projects": by_name(project(7, "Ensemble"))(v)},
        "project": lambda v: {"projectMilestones": by_name(milestone(8, "M1", uid(7)))(v)},
        "users": role_users,
        "issue": _issue_ref,
        "viewer": VIEWER,
    }
    return CreateGql(strict=True, **{**routes, **overrides})


def _holds(node, match):
    # the IssueLabelFilter subset issue_refs sends
    if "and" in match:
        return all(_holds(node, m) for m in match["and"])
    if "or" in match:
        return any(_holds(node, m) for m in match["or"])
    ((field, test),) = match.items()
    if field == "team":
        team_id = (node["team"] or {}).get("id")
        return team_id is None if "null" in test else team_id == test["id"]["eq"]
    ((kind, want),) = test.items()
    if kind == "containsIgnoreCase":
        return want.lower() in node[field].lower()
    return node[field] == want


class LabelGql:
    """The workspace's `catalog` of labels (label() nodes) and one issue, TASK-2 of team TEAM,
    carrying the labels whose ids are `carried`: answers `ensemble-orchestrator issue label swap`'s read
    with VIEWER and the issue's labels in pages of issue_read.PAGE (cursor: the next offset), each
    label reference read from the catalog, and the mutation by applying it to carried, or with
    `result`, raised when an exception. Records every (query, variables) call.
    """

    def __init__(self, catalog, carried, *, result=None, viewer=VIEWER):
        self.catalog, self.carried = {n["id"]: n for n in catalog}, list(carried)
        self.result, self.viewer, self.calls = result, viewer, []

    def _labels(self, offset):
        nodes = [
            {"id": i, "name": self.catalog[i]["name"], "parent": self.catalog[i]["parent"]}
            for i in self.carried
        ]
        page = nodes[offset : offset + issue_read.PAGE]
        more = offset + issue_read.PAGE < len(nodes)
        return conn(page, more=more, cursor=str(offset + issue_read.PAGE) if more else None)

    def _mutate(self, variables):
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result is not None:
            return {"issueUpdate": self.result}
        kept = [i for i in self.carried if i not in variables["r"]]
        self.carried = kept + [i for i in variables["a"] if i not in kept]
        return {"issueUpdate": {"success": True}}

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            return self._mutate(variables)
        if query == issue_refs.Q_LABEL:
            if variables["id"] not in self.catalog:
                raise GraphQLErrors([{"message": "Entity not found: IssueLabel"}])
            return {"issueLabel": self.catalog[variables["id"]]}
        if query.startswith("query($f: IssueLabelFilter"):
            found = [n for n in self.catalog.values() if _holds(n, variables["f"])]
            return {"issueLabels": edges(found[: variables["first"]])}
        if variables["i"] not in ("TASK-2", "id-TASK-2"):
            return {"viewer": self.viewer, "issue": None}
        labels = self._labels(int(variables.get("c") or 0))
        if query == issue_label.Q_MORE:
            return {"issue": {"labels": labels}}
        node = {
            "id": "id-TASK-2",
            "identifier": "TASK-2",
            "url": "https://linear.app/t/issue/TASK-2",
            "team": {"id": TEAM, "key": "TASK"},
            "labels": labels,
        }
        return {"viewer": self.viewer, "issue": node}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


class EditGql(LabelGql):
    """LabelGql whose issue TASK-2 also holds `fields` (title, description, priority, assignee,
    project; team to move it), answering `ensemble-orchestrator issue edit`'s read, its --role
    lookups (the role accounts' users, the viewer) and its --project ones from `projects`
    (project() nodes), strictly as Routes; the mutation applies its label changes.
    """

    def __init__(self, catalog, carried, *, fields=None, projects=(), **options):
        super().__init__(catalog, carried, **options)
        self.projects = {n["id"]: n for n in projects}
        self.fields = {
            "title": "Old",
            "description": "Old body",
            "priority": 0,
            "assignee": None,
            "project": None,
            **(fields or {}),
        }

    def _project(self, query, variables):
        if query == issue_refs.Q_PROJECT:
            if (node := self.projects.get(variables["id"])) is None:
                raise GraphQLErrors([{"message": "Entity not found: Project"}])
            teams = [t for t in node["teams"]["nodes"] if t["id"] == variables["t"]]
            return {"project": {"id": node["id"], "name": node["name"], "teams": conn(teams)}}
        if complexity(query, variables) > LIMIT:
            raise LinearError(f"linear api error: {client.operation(query)}: HTTP 400")
        found = [
            n
            for n in self.projects.values()
            if {"id": variables["t"]} in n["teams"]["nodes"]
            and (variables["f"] is None or _holds(n, variables["f"]))
        ]
        return {"team": {"projects": edges(found[: variables["first"]])}}

    def __call__(self, query, **variables):
        if query in (ops.Q_USER, issue_names.Q_VIEWER):
            self.calls.append((query, variables))
            return (
                {"users": role_users(variables)} if query == ops.Q_USER else {"viewer": self.viewer}
            )
        if "project(" in query or "ProjectFilter" in query:
            self.calls.append((query, variables))
            return self._project(query, variables)
        found = super().__call__(query, **variables)
        if query == issue_edit.Q_READ and found["issue"] is not None:
            found["issue"].update(self.fields)
        return found

    def _mutate(self, variables):
        change = variables["in"]
        labels = {"a": change.get("addedLabelIds", []), "r": change.get("removedLabelIds", [])}
        return super()._mutate(labels)
