"""ensemble-orchestrator issue read: the query, the view's fields and directions."""

from ensemble.orchestrator.engine.types import GraphQLErrors, IssueError, LinearError
from ensemble.orchestrator.trackers.linear import issue_read
from tests.orchestrator.case import Case
from tests.orchestrator.unit.issue_fixtures import Gql, comment, conn, issue_node, rel


def relation(kind, field, ident):
    return {"type": kind, field: rel(ident)}


class View(Case):
    def test_blocking_directions_and_family_stay_apart(self):
        # A blocks B, B blocks C; B has parent P and child K; related links are no column
        node = issue_node(
            "TASK-2",
            parent=rel("TASK-9", state="In Progress"),
            children=conn([rel("TASK-5", state="Done")]),
            relations=conn(
                [
                    relation("blocks", "relatedIssue", "TASK-3"),
                    relation("related", "relatedIssue", "TASK-7"),
                ]
            ),
            inverseRelations=conn(
                [relation("blocks", "issue", "TASK-1"), relation("duplicate", "issue", "TASK-8")]
            ),
        )
        view = issue_read.view(Gql(node), "TASK-2")
        self.assertEqual(view["blocks"], [{**rel("TASK-3"), "state": "Todo"}])
        self.assertEqual(view["blocked_by"], [{**rel("TASK-1"), "state": "Todo"}])
        self.assertEqual(view["parent"], {**rel("TASK-9"), "state": "In Progress"})
        self.assertEqual(view["children"], [{**rel("TASK-5"), "state": "Done"}])

    def test_fields_and_empty_values(self):
        view = issue_read.view(Gql(issue_node("TASK-2")), "TASK-2")
        self.assertEqual(
            view,
            {
                "id": "id-TASK-2",
                "identifier": "TASK-2",
                "title": "title TASK-2",
                "url": "https://linear.app/t/issue/TASK-2",
                "state": {"id": "s-todo", "name": "Todo", "type": "unstarted"},
                "labels": [],
                "project": None,
                "milestone": None,
                "assignee": None,
                "description": None,
                "comments": [],
                "blocks": [],
                "blocked_by": [],
                "parent": None,
                "children": [],
                "truncated": [],
                "continue": None,
            },
        )
        full = issue_node(
            "TASK-2",
            description="Body",
            labels=conn([{"id": "l1", "name": "Bug"}]),
            project={"id": "p1", "name": "Ensemble"},
            projectMilestone={"id": "m1", "name": "M1"},
            assignee={"id": "u1", "name": "Ann", "email": "ann@x.com"},
        )
        view = issue_read.view(Gql(full), "TASK-2")
        self.assertEqual(view["labels"], [{"id": "l1", "name": "Bug"}])
        self.assertEqual(view["project"], {"id": "p1", "name": "Ensemble"})
        self.assertEqual(view["milestone"], {"id": "m1", "name": "M1"})
        self.assertEqual(view["assignee"], {"id": "u1", "name": "Ann", "email": "ann@x.com"})
        self.assertEqual(view["description"], "Body")

    def test_comments_newest_first_with_reply_quote_and_delegated_author(self):
        node = issue_node(
            comments=conn(
                [
                    comment(1),
                    comment(3, parentId="c1", quotedText="quoted"),
                    comment(2, user=None, onBehalfOf={"id": "u2", "name": "Bo"}),
                ]
            )
        )
        comments = issue_read.view(Gql(node), "TASK-2")["comments"]
        self.assertEqual([c["id"] for c in comments], ["c3", "c2", "c1"])
        self.assertEqual(
            comments[0],
            {
                "id": "c3",
                "author": {"id": "u-me", "name": "Me"},
                "created_at": "2026-10-03T00:00:00.000Z",
                "body": "comment 3",
                "parent_id": "c1",
                "quoted_text": "quoted",
                "on_behalf_of": None,
            },
        )
        self.assertIsNone(comments[1]["author"])
        self.assertEqual(comments[1]["on_behalf_of"], {"id": "u2", "name": "Bo"})

    def test_more_pages_are_marked(self):
        node = issue_node(
            comments=conn([comment(1)], more=True, cursor="x"),
            labels=conn([], more=True, cursor="x"),
            relations=conn([], more=True, cursor="x"),
            inverseRelations=conn([], more=True, cursor="x"),
            children=conn([], more=True, cursor="x"),
        )
        view = issue_read.view(Gql(node), "TASK-2")
        self.assertEqual(
            view["truncated"], ["comments", "labels", "blocks", "blocked_by", "children"]
        )

    def test_the_id_is_a_variable_and_both_forms_are_accepted(self):
        uuid = "0b0e6b52-7f43-4c1e-9d55-3c1f1a2b3c4d"
        for ident in ("TASK-2", uuid):
            gql = Gql(issue_node())
            issue_read.view(gql, ident)
            ((query, variables),) = gql.calls
            self.assertEqual(variables, {"i": ident})
            self.assertNotIn(ident, query)
            for field in ("first: 5, orderBy: createdAt", "children(first: 10)"):
                self.assertIn(field, query)

    def test_unresolved_references(self):
        for ident in ("task-2", "TASK", "TASK-2 } x", ""):
            with self.subTest(ident=ident), self.assertRaises(IssueError):
                issue_read.view(Gql(issue_node()), ident)
        with self.assertRaisesRegex(IssueError, "TASK-2: no such issue"):
            issue_read.view(Gql(None), "TASK-2")
        missing = LinearError("linear api error: [{'message': 'Entity not found: Issue'}]")
        with self.assertRaisesRegex(IssueError, "TASK-2: no such issue"):
            issue_read.view(Gql(None, fail=missing), "TASK-2")
        missing = GraphQLErrors([{"message": "Entity not found: Issue"}])
        with self.assertRaisesRegex(IssueError, "TASK-2: no such issue"):
            issue_read.view(Gql(None, fail=missing), "TASK-2")
        other = GraphQLErrors([{"message": "boom", "extensions": {"code": "Entity not found"}}])
        with self.assertRaises(GraphQLErrors):
            issue_read.view(Gql(None, fail=other), "TASK-2")
        other = LinearError("linear api error: [{'message': 'boom'}]")
        with self.assertRaises(LinearError):
            issue_read.view(Gql(None, fail=other), "TASK-2")


class Full(Case):
    def node_and_pages(self):
        node = issue_node(
            "TASK-2",
            description="述" * 700,
            comments=conn([comment(n, body="评" * 400) for n in (9, 8)], more=True, cursor="c-1"),
            labels=conn([{"id": "l1", "name": "Bug"}], more=True, cursor="l-1"),
            relations=conn(
                [relation("related", "relatedIssue", "TASK-7")], more=True, cursor="r-1"
            ),
            inverseRelations=conn([], more=True, cursor="i-1"),
            children=conn([rel("TASK-10")], more=True, cursor="k-1"),
        )
        pages = {
            "comments": [conn([comment(1)])],
            "labels": [conn([{"id": "l2", "name": "Ops"}])],
            "relations": [conn([relation("blocks", "relatedIssue", "TASK-3")])],
            "inverseRelations": [
                conn([relation("blocks", "issue", "TASK-1")], more=True, cursor="i-2"),
                conn([relation("blocks", "issue", "TASK-4")]),
            ],
            "children": [conn([rel("TASK-11")])],
        }
        return node, pages

    def test_every_page_of_every_connection(self):
        node, pages = self.node_and_pages()
        gql = Gql(node, pages)
        view = issue_read.view(gql, "TASK-2", full=True)
        self.assertEqual([c["id"] for c in view["comments"]], ["c9", "c8", "c1"])
        self.assertEqual(len(view["comments"][0]["body"]), 400)
        self.assertEqual(len(view["description"]), 700)
        self.assertEqual([lb["id"] for lb in view["labels"]], ["l1", "l2"])
        self.assertEqual([r["identifier"] for r in view["blocks"]], ["TASK-3"])
        self.assertEqual([r["identifier"] for r in view["blocked_by"]], ["TASK-1", "TASK-4"])
        self.assertEqual([r["identifier"] for r in view["children"]], ["TASK-10", "TASK-11"])
        self.assertEqual((view["truncated"], view["continue"]), ([], None))
        first, *later = gql.calls
        self.assertIn("comments(first: 250, orderBy: createdAt)", first[0])
        self.assertEqual(first[1], {"i": "TASK-2"})
        self.assertEqual(
            sorted(v["c"] for _, v in later), ["c-1", "i-1", "i-2", "k-1", "l-1", "r-1"]
        )
        self.assertEqual({v["i"] for _, v in later}, {"id-TASK-2"})

    def test_a_failed_later_page_fails_the_read(self):
        node, pages = self.node_and_pages()
        pages["children"] = [LinearError("linear api error: boom")]
        with self.assertRaises(LinearError):
            issue_read.view(Gql(node, pages), "TASK-2", full=True)

    def test_a_vanished_later_page_is_a_linear_failure(self):
        for answer in (None, {"children": None}, {"children": {"nodes": []}}, {}):
            node, pages = self.node_and_pages()
            pages["children"] = [answer]
            with (
                self.subTest(answer=answer),
                self.assertRaisesRegex(LinearError, "TASK-2: children page missing"),
            ):
                issue_read.view(Gql(node, pages), "TASK-2", full=True)
