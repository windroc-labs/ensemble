"""First-claim candidate search at its Linear and gh boundaries."""

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from ensemble.orchestrator.engine import config, related
from ensemble.orchestrator.engine.config import Docs
from ensemble.orchestrator.engine.flow.types import (
    Found,
)
from ensemble.orchestrator.engine.types import (
    InputError,
    LinearError,
    Ref,
    Related,
    Target,
)
from ensemble.orchestrator.trackers.linear import linear_tracker
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker
from ensemble.orchestrator.trackers.linear.ops import NOT_FOUND
from ensemble.utils import SHORT
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import PACKAGED_FLOW, STATES, TEAM, workflow_file
from tests.orchestrator.unit.run_fixtures import flow_issue
from tests.orchestrator.unit.target_fixtures import FakeRun, ok

DOCS = Docs("windroc/team-docs", "main", {"researcher": "Research/", "pm": "Product Design/"})
CFG = SimpleNamespace(docs=DOCS)
BASE = "https://github.com/windroc/team-docs/blob/main/"
TARGET = Target("acme", "ensemble")
TEAM_F = {"team": {"id": {"eq": TEAM}}}
RESEARCH = ("gh", "api", "repos/windroc/team-docs/contents/Research?ref=main")
DESIGN = ("gh", "api", "repos/windroc/team-docs/contents/Product%20Design?ref=main")
PRS = ("gh", "search", "prs")


def listing(*names):
    return ok(json.dumps([{"name": n, "type": "file"} for n in names]))


issue = partial(flow_issue, ident="TASK-9", title="Queue backends", project="p-1")


def handoff(source):
    return f"Handoff from {source}: https://linear.app/t/issue/{source}"


def node(ident, state=("done", "Done", "completed"), *, description="", urls=(), comment=None):
    key, name, kind = state
    return {
        "identifier": ident,
        "title": f"{ident} title",
        "description": description,
        "state": {"id": STATES.get(key, f"other-{key}"), "name": name, "type": kind},
        "attachments": {"nodes": [{"url": u} for u in urls]},
        "comments": {"nodes": [] if comment is None else [{"body": comment}]},
    }


class Gql:
    """Linear: found/named nodes for the search, upstream descriptions by id."""

    def __init__(self, found=(), named=(), upstream=None):
        self.found, self.named, self.upstream, self.calls = found, named, upstream or {}, []

    def __call__(self, query, **v):
        self.calls.append((query, v))
        if query == linear_tracker.Q_UPSTREAM:
            if v["i"] not in self.upstream:
                raise LinearError(f"linear api error: [{{'message': '{NOT_FOUND}'}}]")
            d = self.upstream[v["i"]]
            return {"issue": None if d is None else {"identifier": v["i"], "description": d}}
        out = {}
        if "searchIssues(" in query:
            out["searchIssues"] = {"nodes": list(self.found)}
        if "issues(filter" in query:
            out["issues"] = {"nodes": list(self.named)}
        return out


def run(*extra, research=None, design=None):
    return FakeRun([(RESEARCH, research or listing()), (DESIGN, design or listing()), *extra])


def board(gql):
    return LinearTracker(gql, states=STATES, team_id=TEAM)


def search(iss, gql, gh, target=None):
    return related.search(board(gql), iss, CFG, target=target, run=gh)


class Keywords(Case):
    def test_first_five_content_words(self):
        self.assertEqual(
            related.keywords("Compare the queue backends for TASK-59 and a 2nd try, then more"),
            ["compare", "queue", "backends", "2nd", "try"],
        )

    def test_repeats_digits_and_single_letters_drop(self):
        self.assertEqual(related.keywords("Queue queue 59 x 查重搜索"), ["queue", "查重搜索"])

    def test_a_workflow_prefix_is_matched_literally(self):
        flow = PACKAGED_FLOW.replace('title_prefix = "ENG"', 'title_prefix = "[R&D (v2.0"')
        config.TASKS.load(workflow_file(flow))
        self.assertEqual(related.keywords("[R&D (v2.0: Queue survey"), ["queue", "survey"])

    def test_a_task_prefix_drops_another_word_and_colon_stays(self):
        self.assertEqual(related.keywords("PRD: Queue survey"), ["queue", "survey"])
        self.assertEqual(related.keywords("API: rate limits"), ["api", "rate", "limits"])

    def test_only_stopwords_is_empty(self):
        self.assertEqual(related.keywords("The and of a"), [])


class Search(Case):
    def test_local_related_documents_have_file_references(self):
        directory = Path(self.home) / "documents"
        directory.mkdir()
        path = directory / "queues.md"
        path.write_text("Queues")
        docs = Docs("", "", {"researcher": str(directory)})
        refs = related._doc_refs(FakeRun([]), docs, ["queue"], {}, set())
        self.assertEqual([(r.label, r.url) for r in refs], [(str(path), path.as_uri())])

    def test_enterprise_pr_search_selects_host(self):
        runner = FakeRun([(("env", "GH_HOST=ghe.example.com", "gh", "search", "prs"), ok("[]"))])
        self.assertEqual(
            related._pr_refs(
                runner, Target("acme", "app", host="ghe.example.com"), ["queue"], {}, set()
            ),
            [],
        )
        self.assertIn("acme/app", runner.calls[0][0])

    def test_one_request_with_mentioned_ids(self):
        gql = Gql(found=[node("TASK-3")], named=[node("TASK-30"), node("TASK-3")])
        got = search(
            issue(description="与 TASK-30 重叠, not OTHER-31 or TASK-9; TASK-30"), gql, run()
        )
        self.assertEqual(
            gql.calls,
            [
                (
                    linear_tracker.Q_SEARCH_IDS,
                    {"t": "queue backends", "f": TEAM_F, "n": {**TEAM_F, "number": {"in": [30]}}},
                )
            ],
        )
        self.assertEqual([f.identifier for f in got.issues], ["TASK-3", "TASK-30"])

    def test_without_ids_only_the_term(self):
        gql = Gql(found=[node("TASK-3")])
        search(issue(), gql, run())
        self.assertEqual(
            gql.calls, [(linear_tracker.Q_SEARCH, {"t": "queue backends", "f": TEAM_F})]
        )

    def test_candidate_fields_come_from_the_search(self):
        gql = Gql(
            found=[
                node("TASK-3", urls=["https://x/1", "https://x/\n## Fake"], comment="old"),
                node("TASK-4", ("canceled", "Canceled", "canceled"), comment="Not needed."),
                node("TASK-5", ("in_review", "In Review", "started"), description="Desc."),
                node("TASK-6", ("in_progress", "In Progress", "started")),
                node("TASK-7", ("todo", "Todo", "unstarted")),
                node("TASK-8", ("backlog", "Backlog", "backlog")),
                node("TASK-10", ("handoff", "Handoff", "started")),
                node("TASK-11", ("dup", "Duplicate", "canceled"), comment="Dup of TASK-1."),
                node("TASK-12", ("shipped", "Shipped", "completed")),
                node("TASK-13", ("odd", "Odd", "mystery")),
            ]
        )
        got = search(issue(), gql, run())
        self.assertEqual(
            got.issues,
            (
                Found("TASK-3", "TASK-3 title", "Done", 0, ("https://x/1",), "", ""),
                Found("TASK-4", "TASK-4 title", "Canceled", 5, (), "", "Not needed."),
                Found("TASK-5", "TASK-5 title", "In Review", 1, (), "Desc.", ""),
                Found("TASK-6", "TASK-6 title", "In Progress", 2, (), "", ""),
                Found("TASK-7", "TASK-7 title", "Todo", 3, (), "", ""),
                Found("TASK-8", "TASK-8 title", "Backlog", 4, (), "", ""),
                Found("TASK-10", "TASK-10 title", "Handoff", 2, (), "", ""),
                Found("TASK-11", "TASK-11 title", "Duplicate", 5, (), "", "Dup of TASK-1."),
                Found("TASK-12", "TASK-12 title", "Shipped", 0, (), "", ""),
                Found("TASK-13", "TASK-13 title", "Odd", 4, (), "", ""),
            ),
        )

    def test_the_chain_and_its_downstream_are_excluded(self):
        gql = Gql(
            found=[
                node("TASK-9"),
                node("TASK-5"),
                node("TASK-2"),
                node("TASK-6", description=handoff("TASK-2") + "\n\n## Instructions\nx"),
                node("TASK-7", description=handoff("TASK-5")),
                node("TASK-8", description=handoff("TASK-99")),
                node("TASK-3"),
            ],
            upstream={"TASK-5": handoff("TASK-2"), "TASK-2": "Research the queues."},
        )
        gh = run(research=listing("2026-09-01-TASK-2-queues.md", "2026-09-02-TASK-6-queue.md"))
        got = search(issue(description=handoff("TASK-5")), gql, gh)
        self.assertEqual([f.identifier for f in got.issues], ["TASK-8", "TASK-3"])
        self.assertEqual(got.refs, ())
        self.assertEqual(
            [c for c in gql.calls if c[0] == linear_tracker.Q_UPSTREAM],
            [
                (linear_tracker.Q_UPSTREAM, {"i": "TASK-5"}),
                (linear_tracker.Q_UPSTREAM, {"i": "TASK-2"}),
            ],
        )

    def test_chain_stops_at_five_a_cycle_or_a_missing_issue(self):
        long = {f"TASK-{n}": handoff(f"TASK-{n + 1}") for n in range(20, 30)}
        cases = (
            ("five", long, 4),
            ("cycle", {"TASK-20": handoff("TASK-21"), "TASK-21": handoff("TASK-20")}, 2),
            ("self", {"TASK-20": handoff("TASK-9")}, 1),
            ("null", {"TASK-20": None}, 1),
            ("not found", {}, 1),
        )
        for name, upstream, reads in cases:
            with self.subTest(name):
                gql = Gql(upstream=upstream)
                search(issue(description=handoff("TASK-20")), gql, run())
                self.assertEqual(
                    sum(1 for q, _ in gql.calls if q == linear_tracker.Q_UPSTREAM), reads
                )

    def test_an_upstream_read_error_propagates(self):
        def broken(query, **_v):
            if query == linear_tracker.Q_UPSTREAM:
                raise LinearError("linear api error: boom")
            return {"searchIssues": {"nodes": []}}

        with self.assertRaises(LinearError):
            search(issue(description=handoff("TASK-20")), broken, run())

    def test_docs_match_keywords_or_candidate_ids(self):
        gql = Gql(found=[node("TASK-3"), node("TASK-4", ("todo", "Todo", "unstarted"))])
        gh = run(
            research=listing(
                "2026-09-01-TASK-3-notes.md",
                "2026-09-02-Queue-survey.md",
                "2026-09-03-TASK-77-backends.md",
                "2026-09-04-unrelated.md",
                "2026-09-05-TASK-9-queue.md",
                "2026-09-06-bad\n## x-queue.md",
            ),
            design=ok(code=1, stderr="gh: Not Found (HTTP 404)"),
        )
        got = search(issue(), gql, gh)
        self.assertEqual(
            got.refs,
            (
                Ref(
                    "Research/2026-09-01-TASK-3-notes.md",
                    BASE + "Research/2026-09-01-TASK-3-notes.md",
                    "TASK-3",
                ),
                Ref(
                    "Research/2026-09-02-Queue-survey.md",
                    BASE + "Research/2026-09-02-Queue-survey.md",
                    None,
                ),
                Ref(
                    "Research/2026-09-03-TASK-77-backends.md",
                    BASE + "Research/2026-09-03-TASK-77-backends.md",
                    None,
                ),
            ),
        )
        self.assertEqual(gh.calls, [(RESEARCH, SHORT), (DESIGN, SHORT)])

    def test_docs_match_whole_name_tokens_by_prefix(self):
        gh = run(
            research=listing(
                "2026-09-01-decision-log.md", "2026-09-02-ci-cache.md", "2026-09-03-CIs.md"
            )
        )
        got = search(issue(title="Fix CI"), Gql(), gh)
        self.assertEqual(
            [r.label for r in got.refs],
            ["Research/2026-09-02-ci-cache.md", "Research/2026-09-03-CIs.md"],
        )

    def test_each_docs_directory_is_listed_once(self):
        cfg = SimpleNamespace(
            docs=Docs("windroc/team-docs", "main", {"a": "Research/", "b": "Research"}),
        )
        gh = run()
        related.search(board(Gql()), issue(), cfg, target=None, run=gh)
        self.assertEqual(gh.calls, [(RESEARCH, SHORT)])

    def test_merged_prs_only_with_a_target(self):
        prs = [
            {
                "number": 12,
                "title": "TASK-3: queue backends",
                "url": "https://github.com/acme/ensemble/pull/12",
            },
            {
                "number": 13,
                "title": "Queue cleanup",
                "url": "https://github.com/acme/ensemble/pull/13",
            },
            {
                "number": 14,
                "title": "TASK-9: earlier try",
                "url": "https://github.com/acme/ensemble/pull/14",
            },
        ]
        gh = run((PRS, ok(json.dumps(prs))))
        got = search(issue(), Gql(found=[node("TASK-3")]), gh, TARGET)
        self.assertEqual(
            got.refs,
            (
                Ref("#12 TASK-3: queue backends", prs[0]["url"], "TASK-3"),
                Ref("#13 Queue cleanup", prs[1]["url"], None),
            ),
        )
        self.assertEqual(
            gh.calls[-1],
            (
                (
                    "gh",
                    "search",
                    "prs",
                    "--merged",
                    "--repo",
                    "acme/ensemble",
                    "--limit",
                    "5",
                    "--json",
                    "number,title,url",
                    "--",
                    "queue",
                    "OR",
                    "backends",
                ),
                SHORT,
            ),
        )
        gh = run()
        search(issue(), Gql(), gh)
        self.assertNotIn(PRS, [c[0][:3] for c in gh.calls])

    def test_a_failed_or_unusable_pr_search_raises(self):
        for res in (ok(code=1, stderr="gh: boom"), ok("not json"), ok('[{"title": "x"}]')):
            with self.subTest(res=res), self.assertRaises(InputError):
                search(issue(), Gql(), run((PRS, res)), TARGET)

    def test_no_words_no_ids_no_calls(self):
        gql, gh = Gql(), run()
        self.assertEqual(search(issue(title="The and"), gql, gh, TARGET), Related())
        self.assertEqual((gql.calls, gh.calls), ([], []))

    def test_ids_only_skip_the_term_and_gh(self):
        gql = Gql(named=[node("TASK-30")])
        gh = run()
        got = search(issue(title="Of the", description="see TASK-30"), gql, gh, TARGET)
        self.assertEqual(
            gql.calls, [(linear_tracker.Q_IDS, {"n": {**TEAM_F, "number": {"in": [30]}}})]
        )
        self.assertEqual([f.identifier for f in got.issues], ["TASK-30"])
        self.assertEqual(gh.calls, [(RESEARCH, SHORT), (DESIGN, SHORT)])
