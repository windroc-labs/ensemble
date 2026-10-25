"""issue_page: a connection's page by edge, and every page."""

from ensemble.orchestrator.engine.types import LinearError
from ensemble.orchestrator.trackers.linear import issue_page, issue_refs
from tests.orchestrator.case import Case
from tests.orchestrator.unit.issue_fixtures import Routes, edges, team_ref


def keep_even(node):
    return None if int(node["id"][-1], 16) % 2 else ({"id": node["id"]}, [])


class Fetch(Case):
    def test_rows_carry_item_cuts_and_cursor_and_the_page_info(self):
        gql = Routes(teams=edges([team_ref(1, "A"), team_ref(2, "B")], more=True, cursor="end"))
        rows, more, end = issue_page.fetch(gql, issue_refs.teams(None), 2, "after-me")
        self.assertEqual(
            [r[2] for r in rows], [f"cur-{team_ref(1, 'A')['id']}", f"cur-{team_ref(2, 'B')['id']}"]
        )
        self.assertEqual((more, end), (True, "end"))
        self.assertEqual(gql.sent("teams"), [{"first": 2, "after": "after-me", "f": None}])

    def test_a_node_shape_skips_is_left_out(self):
        gql = Routes(teams=edges([team_ref(1, "A"), team_ref(2, "B")]))
        source = issue_refs.teams(None, keep_even)
        rows, _, _ = issue_page.fetch(gql, source, 20)
        self.assertEqual([r[0]["id"] for r in rows], [team_ref(2, "B")["id"]])

    def test_a_missing_connection_is_a_linear_failure(self):
        for answer in (None, {"edges": None, "pageInfo": {}}, {"nodes": []}):
            with self.subTest(answer=answer), self.assertRaises(LinearError):
                issue_page.fetch(Routes(team=answer), issue_refs.projects("t", None), 20)


class Walk(Case):
    def test_follows_end_cursors_to_the_last_page(self):
        gql = Routes(
            teams=[
                edges([team_ref(1, "A")], more=True, cursor="c1"),
                edges([team_ref(2, "B")], more=True, cursor="c2"),
                edges([team_ref(3, "C")]),
            ]
        )
        rows = issue_page.walk(gql, issue_refs.teams(None))
        self.assertEqual([r[0]["key"] for r in rows], ["A", "B", "C"])
        sent = gql.sent("teams")
        self.assertEqual(
            [(v["first"], v["after"]) for v in sent], [(250, None), (250, "c1"), (250, "c2")]
        )

    def test_a_next_page_without_a_new_cursor_is_a_linear_failure(self):
        # hasNextPage with no endCursor, or one already read, would loop forever
        for pages in (
            [edges([team_ref(1, "A")], more=True)],
            [edges([team_ref(1, "A")], more=True, cursor="c1")] * 2,
        ):
            gql = Routes(teams=pages)
            with self.subTest(pages=pages), self.assertRaisesRegex(LinearError, "endCursor"):
                issue_page.walk(gql, issue_refs.teams(None))
