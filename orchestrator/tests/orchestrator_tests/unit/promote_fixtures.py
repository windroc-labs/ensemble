"""Promotion fixtures with explicit role accounts and logs."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import promote
from ensemble.orchestrator.engine.types import LinearError
from ensemble.orchestrator.trackers.linear import linear_tracker, ops
from orchestrator_tests.unit import run_fixtures
from orchestrator_tests.unit.board_ids import (
    ACCOUNTS,
    HEADER,
    PACKAGED_FLOW,
    STATE_NAMES,
    team_node,
    workflow_line,
)
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.run_fixtures import logged, show

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)

STATES = {
    "Todo": IDS_BY_KEY["todo"],
    "In Progress": IDS_BY_KEY["in_progress"],
    "In Review": IDS_BY_KEY["in_review"],
    "Handoff": IDS_BY_KEY["handoff"],
    "Done": IDS_BY_KEY["done"],
}

PROJECTS = {"Deep Research": "p-dr", "Product Design": "p-pd"}

ROLE = ACCOUNTS

HUMAN = {"email": "me@x.com", "name": "Me"}

# a role account's: self, as the harness's
AGENT = {"email": "Researcher@agents.test", "name": "researcher@agents.test"}

# not in human_members, not a role or the harness: other, as the user's
MANAGER = {"email": "manager@x.com", "name": "Manager"}

OTHER = {"email": "other@x.com", "name": "Other"}

# pm is the last stage: only the researcher's handoffs promote
PM_LAST = workflow_line(PACKAGED_FLOW.replace('next = "engineer"\n', ""))

CONFIG = (
    HEADER
    + PM_LAST
    + 'linear.human_members = ["me@x.com"]\n'
    + role_table("researcher")
    + role_table("pm")
    + role_table("engineer")
)

# the packaged workflow: pm hands off to engineer, needing no instructions
PM_NEXT = CONFIG.replace(PM_LAST, "")


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def instructions(description):
    # `## Comments` repeats the comments, so never search the whole description
    start = description.index("\n## Instructions\n") + 1
    return description[start:].split("\n\n## ", 1)[0]


class FakeLinear:
    def __init__(self):
        self.issues, self.children, self.mutations, self.fail, self.calls = {}, {}, [], {}, []
        self.state_ids, self.services = None, []

    def add(self, ident, project="Deep Research", state="Handoff", role="researcher", **kw):
        self.issues[ident] = dict(
            id=ident,
            identifier=ident,
            url=f"https://l/{ident}",
            title=f"Title {ident}",
            priority=2,
            createdAt=ago(10000),
            state=state,
            history=[],
            comments=[],
            relations=[],
            inverse=[],
            attachments=[],
            assignee=role and {"id": f"u-{role}"},
            project=project and {"id": PROJECTS[project], "name": project},
        )
        self.issues[ident].update(kw)
        return self.issues[ident]

    def moved(self, ident, minutes, to, frm=None):
        self.issues[ident]["history"].append(
            {
                "createdAt": ago(minutes),
                "fromStateId": frm or STATES["In Progress"],
                "toStateId": STATES[to],
            }
        )

    def said(self, ident, minutes, user=HUMAN, body="build X"):
        self.issues[ident]["comments"].append(
            {"body": body, "createdAt": ago(minutes), "user": user}
        )

    def writes(self):
        return [q for q, _ in self.calls if q == ops.Q_ISSUE_STATE or q.startswith("mutation")]

    def __call__(self, query, *, identity, **v):
        if query == linear_tracker.Q_STATE_NAME:
            return {"workflowState": {"name": STATE_NAMES[v["i"]]}}
        self.calls.append((query, v))
        self.services.append(identity.service)
        if error := (self.fail.get(query) or self.fail.get((query, v.get("i")))):
            raise LinearError(f"linear api error: {error}")
        if (value := self._identity(query, v)) is not None:
            return value
        if (value := self._detail(query, v)) is not None:
            return value
        self.mutations.append((query, v))
        return self._mutate(query, v)

    def _identity(self, query, v):
        if query == ops.Q_TEAM:
            return {"teams": {"nodes": [team_node(self.state_ids)]}}
        if query == ops.Q_SETUP:
            emails = {f["email"]["eqIgnoreCase"].lower() for f in v["u"]["or"]}
            nodes = [{"id": f"u-{r}", "email": a} for r, a in ROLE.items() if a.lower() in emails]
            return {"teams": {"nodes": [team_node(self.state_ids)]}, "users": {"nodes": nodes}}
        if query == ops.Q_USER:
            users = {a.lower(): f"u-{r}" for r, a in ROLE.items()}
            return {
                "users": {
                    "nodes": [{"id": users[v["e"].lower()]}] if v["e"].lower() in users else []
                }
            }
        if query == linear_tracker.Q_HANDOFF:
            self.handoff_vars = v
            return {
                "issues": {
                    "nodes": [
                        dict(i, attachments={"nodes": i["attachments"]})
                        for i in self.issues.values()
                        if i["state"] == "Handoff"
                        and i["project"]
                        and i["assignee"]
                        and (i["assignee"]["id"] in v["a"])
                    ]
                }
            }
        return None

    def _detail(self, query, v):
        if query == linear_tracker.Q_DETAIL:
            i = self.issues[v["i"]]
            return {
                "issue": {
                    "state": {"id": STATES[i["state"]]},
                    "history": {"nodes": list(reversed(i["history"]))},
                    "comments": {"nodes": list(reversed(i["comments"]))},
                    "relations": {"nodes": [{"relatedIssue": {"id": r}} for r in i["relations"]]},
                    "inverseRelations": {"nodes": [{"issue": {"id": r}} for r in i["inverse"]]},
                }
            }
        if query == linear_tracker.Q_CHILD:
            return {"issues": {"nodes": [self.children[v["c"]]] if v["c"] in self.children else []}}
        if query == ops.Q_ISSUE_STATE:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        return None

    def _mutate(self, query, v):
        if query == linear_tracker.M_CREATE:
            inp = v["in"]
            child = dict(inp, identifier=f"C-{len(self.children) + 1}")
            self.children[inp["id"]] = child
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {"id": inp["id"], "identifier": child["identifier"]},
                }
            }
        if query == linear_tracker.M_RELATE:
            self.issues[v["in"]["issueId"]]["relations"].append(v["in"]["relatedIssueId"])
            return {"issueRelationCreate": {"success": True}}
        if query == ops.M_COMMENT:
            self.issues[v["i"]].setdefault("posted", []).append(v["b"])
            return {"commentCreate": {"success": True}}
        if query == ops.M_SUBSCRIBE:
            self.issues[v["i"]].setdefault("subscribers", []).append(v["e"])
            return {"issueSubscribe": {"success": True}}
        if query == ops.M_STATE:
            self.issues[v["i"]]["state"] = next((n for n, i in STATES.items() if i == v["s"]))
            return {"issueUpdate": {"success": True}}
        raise AssertionError(query)


class FakePruner:
    calls = []

    def __init__(self, board, gql, now, *, dry, work, cfg):
        self.cfg = cfg
        FakePruner.calls.append((board, gql, now, dry, work))

    def run(self):
        return 0


class Base(run_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.write_local(f'work_dir = "{self.work}"\n')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_path = os.path.join(tmp.name, "config.toml")
        self.write_config(CONFIG)
        self.fake = FakeLinear()

    def write_config(self, text):
        self.write(self.config_path, text)

    def run_main(self, *argv, gql=None, pruner=FakePruner):
        FakePruner.calls.clear()
        before, err, out = len(logged(self.logs)), io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = promote.main(
                list(argv),
                trackers=cli.TRACKERS,
                gql=gql or self.fake,
                now=NOW,
                config_path=self.config_path,
                pruner=pruner,
            )
        self.assertEqual(out.getvalue(), "")
        self.err = err.getvalue()
        copies = [json.loads(line) for line in self.err.splitlines() if line.startswith("{")]
        self.out = "\n".join(show(e) for e in logged(self.logs)[before:] + copies)
        return rc

    def ready(self, ident="DR-1", handoff=30, **kw):
        issue = self.fake.add(ident, **kw)
        self.fake.moved(ident, handoff + 30, "In Review")
        self.fake.said(ident, handoff + 15)
        self.fake.moved(ident, handoff, "Handoff", frm=STATES["In Review"])
        return issue
