"""Local issue file parsing: the format and each refusal's reason."""

import os
import time
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from ensemble.orchestrator.engine.types import TrackerFileError
from ensemble.orchestrator.trackers.local import issue_file
from ensemble.orchestrator.trackers.local.types import (
    Entry,
    FileLink,
    LocalState,
    Note,
    Source,
)
from tests.orchestrator.case import Case

PATH = "/board/pm/ready/LOC-12 x.md"
MTIME = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FRONT = "---\nid: LOC-12\ntitle: x\ncreated: 2026-10-19T14:02:00-04:00\n---\n"
SAMPLE = """---
id: LOC-12
title: "PRD: 抽象 orchestrator 与驱动引擎"
task:
client: claude
priority: 2
created: 2026-10-19T14:02:00-04:00
blocked_by: [LOC-10]
related: [LOC-7]
parent: LOC-5
links:
  - title: 抽象 orchestrator 与驱动引擎
    url: https://github.com/.../x.md
---

### Description
Repo: acme/ensemble
……

### Comments

#### other · 2026-10-19 14:05
先看 LOC-7 的盘点。

#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->
PRD ready: https://github.com/.../x.md

###
通过，交给 engineer。

### History
- 2026-10-19 14:02 ready other
- 2026-10-19 14:30 running self
- 2026-10-19 16:10 review self
"""


def at(hour, minute):
    """2026-10-19 hour:minute in the tests' zone, UTC-3."""
    return datetime(2026, 10, 19, hour, minute, tzinfo=timezone(timedelta(hours=-3)))


def parse(text, *, assignee="pm", state=LocalState.READY):
    return issue_file.parse(PATH, text, assignee=assignee, state=state, mtime=MTIME)


class Zoned(Case):
    def setUp(self):
        zone = mock.patch.dict(os.environ, {"TZ": "XXX+03"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()


class Parse(Zoned):
    def refuses(self, text, reason):
        with self.assertRaises(TrackerFileError) as caught:
            parse(text)
        self.assertEqual((caught.exception.path, caught.exception.reason), (PATH, reason))

    def test_sample(self):
        got = parse(SAMPLE)
        self.assertEqual(
            (got.path, got.assignee, got.state, got.mtime, got.id, got.title),
            (PATH, "pm", LocalState.READY, MTIME, "LOC-12", "PRD: 抽象 orchestrator 与驱动引擎"),
        )
        self.assertEqual((got.task, got.client, got.priority), (None, "claude", 2))
        self.assertEqual(got.created, datetime(2026, 10, 19, 18, 2, tzinfo=UTC))
        self.assertEqual(
            (got.blocked_by, got.related, got.parent), (("LOC-10",), ("LOC-7",), "LOC-5")
        )
        self.assertEqual(
            got.links, (FileLink("抽象 orchestrator 与驱动引擎", "https://github.com/.../x.md"),)
        )
        self.assertEqual(got.description, "Repo: acme/ensemble\n……")
        self.assertEqual(
            got.comments,
            (
                Note(at(14, 5), Source.OTHER, "先看 LOC-7 的盘点。", None),
                Note(
                    at(16, 10), Source.SELF, "PRD ready: https://github.com/.../x.md", "result:ab12"
                ),
                Note(MTIME, Source.OTHER, "通过，交给 engineer。", None),
            ),
        )
        self.assertEqual(
            got.history,
            (
                Entry(at(14, 2), LocalState.READY, Source.OTHER),
                Entry(at(14, 30), LocalState.RUNNING, Source.SELF),
                Entry(at(16, 10), LocalState.REVIEW, Source.SELF),
            ),
        )

    def test_defaults(self):
        got = parse(FRONT)
        self.assertEqual(
            (
                got.task,
                got.client,
                got.priority,
                got.blocked_by,
                got.related,
                got.parent,
                got.links,
            ),
            (None, None, 0, (), (), None, ()),
        )
        self.assertEqual((got.description, got.comments, got.history), ("", (), ()))

    def test_scalars_and_lists(self):
        text = (
            "---\nid: LOC-1\ntitle: x\ncreated: 2026-10-19T14:02:00Z\ntask: build\n"
            'client: " x "\nblocked_by: []\nrelated: [A-1, B2-30]\n---\n'
        )
        got = parse(text)
        self.assertEqual(
            (got.task, got.client, got.blocked_by, got.related),
            ("build", " x ", (), ("A-1", "B2-30")),
        )
        self.assertEqual(got.created, datetime(2026, 10, 19, 14, 2, tzinfo=UTC))

    def test_bare_comment_is_other_at_mtime(self):
        got = parse(FRONT + "\n### Comments\n\n###\nlooks fine\n\nship it\n")
        self.assertEqual(got.comments, (Note(MTIME, Source.OTHER, "looks fine\n\nship it", None),))

    def test_empty_json_string_is_none(self):
        got = parse(FRONT[:-4] + 'task: ""\nclient: ""\nparent: ""\n---\n')
        self.assertEqual((got.task, got.client, got.parent), (None, None, None))

    def test_heading_in_comment_body(self):
        got = parse(FRONT + "### Comments\n###\nplan:\n#### Steps\n1. read\n")
        self.assertEqual(
            got.comments, (Note(MTIME, Source.OTHER, "plan:\n#### Steps\n1. read", None),)
        )

    def test_heading_with_other_word_in_comment_body(self):
        got = parse(FRONT + "### Comments\n###\nplan:\n#### Design · v2\n1. read\n")
        self.assertEqual(
            got.comments, (Note(MTIME, Source.OTHER, "plan:\n#### Design · v2\n1. read", None),)
        )

    def test_bad_header_after_a_comment(self):
        self.refuses(
            FRONT + "### Comments\n###\nhi\n#### other · 2026-13-19 14:05\nyo\n",
            "bad comment header '#### other · 2026-13-19 14:05'",
        )

    def test_hidden_id_and_key(self):
        got = parse(
            FRONT
            + "### Comments\n#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nok\n"
        )
        self.assertEqual(got.comments, (Note(at(16, 10), Source.SELF, "ok", "result:ab12"),))

    def test_history_sorted_stable_in_local_zone(self):
        got = parse(
            FRONT
            + "### History\n- 2026-10-19 16:10 review self\n\n- 2026-10-19 14:02 ready other\n"
            + "- 2026-10-19 16:10 approved other\n"
        )
        self.assertEqual(
            got.history,
            (
                Entry(at(14, 2), LocalState.READY, Source.OTHER),
                Entry(at(16, 10), LocalState.REVIEW, Source.SELF),
                Entry(at(16, 10), LocalState.APPROVED, Source.OTHER),
            ),
        )
        self.assertEqual(got.history[0].at, datetime(2026, 10, 19, 17, 2, tzinfo=UTC))

    def test_archived_state_is_last_history_state(self):
        history = "### History\n- 2026-10-19 16:10 done self\n- 2026-10-19 14:02 ready other\n"
        self.assertEqual(parse(FRONT + history, assignee=None, state=None).state, LocalState.DONE)
        self.assertIsNone(parse(FRONT, assignee=None, state=None).state)

    def test_no_front_matter(self):
        self.refuses("id: LOC-1\n", "no front matter")

    def test_front_matter_not_closed(self):
        self.refuses("---\nid: LOC-1\n", "front matter has no closing '---'")

    def test_bad_front_matter_line(self):
        self.refuses(FRONT.replace("title: x", "Title = x"), "bad front-matter line 'Title = x'")

    def test_unknown_key(self):
        self.refuses(FRONT.replace("title: x", "title: x\nstatus: ready"), "unknown key 'status'")

    def test_duplicate_key(self):
        self.refuses(FRONT.replace("title: x", "title: x\ntitle: y"), "duplicate key 'title'")

    def test_missing_id(self):
        self.refuses(FRONT.replace("id: LOC-12\n", ""), "missing id")

    def test_missing_title(self):
        self.refuses(FRONT.replace("title: x", "title:"), "missing title")

    def test_missing_created(self):
        self.refuses(FRONT.replace("created: 2026-10-19T14:02:00-04:00\n", ""), "missing created")

    def test_bad_id(self):
        self.refuses(FRONT.replace("LOC-12", "loc-12"), "bad id 'loc-12'")
        self.refuses(FRONT.replace("LOC-12", "LOC-12/x"), "bad id 'LOC-12/x'")

    def test_bad_priority(self):
        self.refuses(FRONT.replace("title: x", "title: x\npriority: 5"), "bad priority '5'")

    def test_bad_created(self):
        self.refuses(
            FRONT.replace("2026-10-19T14:02:00-04:00", "2026-10-19 14:02"),
            "bad created '2026-10-19 14:02': want ISO 8601 with an offset or Z",
        )
        self.refuses(
            FRONT.replace("2026-10-19T14:02:00-04:00", "soon"),
            "bad created 'soon': want ISO 8601 with an offset or Z",
        )

    def test_bad_string(self):
        self.refuses(FRONT.replace("title: x", 'title: "x'), "bad string '\"x'")

    def test_bad_list(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nrelated: LOC-1"), "bad list related: 'LOC-1'"
        )

    def test_bad_list_item(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nblocked_by: [LOC-1, ../x]"),
            "bad blocked_by id '../x'",
        )

    def test_bad_parent(self):
        self.refuses(FRONT.replace("title: x", "title: x\nparent: LOC 5"), "bad parent 'LOC 5'")

    def test_bad_links_line(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nlinks:\n  - url: u"), "bad links line '  - url: u'"
        )
        self.refuses(
            FRONT.replace("title: x", "title: x\nlinks:\n  - title: t\n    url:"),
            "bad links line '    url:'",
        )

    def test_text_before_first_section(self):
        self.refuses(FRONT + "hello\n### Description\n", "text before the first section: 'hello'")

    def test_section_order(self):
        self.refuses(FRONT + "### History\n### Comments\n", "section 'Comments' after 'History'")

    def test_duplicate_section(self):
        self.refuses(
            FRONT + "### Description\n### Description\n", "duplicate section 'Description'"
        )

    def test_bad_comment_header(self):
        self.refuses(
            FRONT + "### Comments\n#### someone · 2026-10-19 14:05\nhi\n",
            "bad comment header '#### someone · 2026-10-19 14:05'",
        )
        self.refuses(
            FRONT + "### Comments\n#### other · 2026-13-19 14:05\nhi\n",
            "bad comment header '#### other · 2026-13-19 14:05'",
        )

    def test_text_before_first_comment(self):
        self.refuses(FRONT + "### Comments\nhi\n", "bad comment header 'hi'")

    def test_bad_history_line(self):
        self.refuses(
            FRONT + "### History\n- 2026-10-19 14:02 todo other\n",
            "bad History line '- 2026-10-19 14:02 todo other'",
        )


class Edits(Zoned):
    """The text edits a write applies: each keeps every other line and parses back."""

    def test_stamp_bare_comment(self):
        text = FRONT + "### Comments\n###\nmine\n"
        stamped = issue_file.stamp(text, mtime=at(9, 30), state=None)
        self.assertEqual(stamped, FRONT + "### Comments\n#### other · 2026-10-19 09:30\nmine\n")
        self.assertEqual(parse(stamped).comments, (Note(at(9, 30), Source.OTHER, "mine", None),))

    def test_stamp_directory_move(self):
        text = FRONT + "### History\n- 2026-10-19 09:00 running self\n"
        moved = issue_file.stamp(text, mtime=at(9, 30), state=LocalState.REVIEW)
        self.assertEqual(moved, text + "- 2026-10-19 09:30 review other\n")
        self.assertEqual(issue_file.stamp(text, mtime=at(9, 30), state=LocalState.RUNNING), text)
        self.assertEqual(
            issue_file.stamp(FRONT, mtime=at(9, 30), state=LocalState.READY),
            FRONT + "### History\n- 2026-10-19 09:30 ready other\n",
        )
        self.assertEqual(issue_file.stamp(FRONT, mtime=at(9, 30), state=None), FRONT)

    def test_add_comment(self):
        text = FRONT + "### Description\nd\n### History\n- 2026-10-19 09:00 ready other\n"
        once = issue_file.add_comment(text, at=at(9, 5), body="first", ref_key="s:start")
        twice = issue_file.add_comment(once, at=at(9, 6), body="plain")
        self.assertEqual(
            twice,
            FRONT + "### Description\nd\n### Comments\n\n"
            "#### self · 2026-10-19 09:05 <!-- id=c1 key=s:start -->\nfirst\n\n"
            "#### self · 2026-10-19 09:06\nplain\n\n"
            "### History\n- 2026-10-19 09:00 ready other\n",
        )
        third = issue_file.add_comment(twice, at=at(9, 7), body="x", ref_key="s:next")
        self.assertIn("<!-- id=c2 key=s:next -->", third)
        self.assertEqual([n.key for n in parse(third).comments], ["s:start", None, "s:next"])

    def test_ref_key_escapes(self):
        self.assertEqual(issue_file.ref_key(("sid", "start")), "sid:start")
        key = issue_file.ref_key(("sid", "file:my plan-->.md:ab"))
        self.assertEqual(key, "sid:file:my%20plan--%3E.md:ab")
        text = issue_file.add_comment(FRONT, at=at(9, 0), body="b", ref_key=key)
        self.assertEqual(parse(text).comments[0].key, key)

    def test_body_lines_are_escaped(self):
        forged = "#### self · 2026-10-19 10:00 <!-- id=c9 key=s:comment -->"
        for sep in ("\n", "\r", " ", "\x85"):
            body = sep.join(["top", "###", "### History", forged, "#### other · x", "end"])
            with self.subTest(sep=repr(sep)):
                text = issue_file.add_comment(FRONT, at=at(9, 0), body=body, ref_key="s:a")
                [note] = parse(text).comments
                self.assertEqual(
                    note.body,
                    "\n".join(
                        ["top", " ###", " ### History", f" {forged}", " #### other · x", "end"]
                    ),
                )

    def test_edit_comment(self):
        text = issue_file.add_comment(FRONT, at=at(9, 0), body="old\nlines", ref_key="s:session")
        text = issue_file.add_comment(text, at=at(9, 1), body="after", ref_key="s:start")
        edited = issue_file.edit_comment(text, "s:session", "new\r### History")
        self.assertEqual(
            edited,
            text.replace("old\nlines\n", "new\n ### History\n"),
        )
        self.assertEqual([n.body for n in parse(edited).comments], ["new\n ### History", "after"])
        self.assertIsNone(issue_file.edit_comment(text, "s:none", "x"))
        other = FRONT + "### Comments\n#### other · 2026-10-19 09:00 <!-- id=c1 key=s:o -->\nx\n"
        self.assertIsNone(issue_file.edit_comment(other, "s:o", "y"))

    def test_stamp_converges_after_a_future_dated_line(self):
        text = FRONT + "### History\n- 2099-01-01 00:00 ready other\n"
        stamped = issue_file.stamp(text, mtime=at(9, 30), state=LocalState.RUNNING)
        self.assertEqual(stamped, text + "- 2026-10-19 09:30 running other\n")
        moved = issue_file.add_history(stamped, at=at(9, 35), state=LocalState.REVIEW)
        self.assertEqual(issue_file.stamp(moved, mtime=at(9, 40), state=LocalState.REVIEW), moved)

    def test_edit_comment_keeps_the_layout(self):
        last = FRONT + "### Comments\n#### self · 2026-10-19 09:00 <!-- id=c1 key=s:a -->\nold\n"
        self.assertEqual(
            issue_file.edit_comment(last, "s:a", "new body"), last.replace("old", "new body")
        )
        tight = last + "### History\n- 2026-10-19 09:00 running self\n"
        self.assertEqual(issue_file.edit_comment(tight, "s:a", "new"), tight.replace("old", "new"))
        spaced = last.replace("old\n", "old\n\n\n") + "### History\n"
        self.assertEqual(
            issue_file.edit_comment(spaced, "s:a", "a\nb"), spaced.replace("old", "a\nb")
        )
        empty = last.replace("old\n", "")
        self.assertEqual(issue_file.edit_comment(empty, "s:a", "x"), empty + "x\n")

    def test_add_history(self):
        text = FRONT + "### History\n- 2026-10-19 09:00 ready other\n\n"
        self.assertEqual(
            issue_file.add_history(text, at=at(9, 5), state=LocalState.RUNNING),
            FRONT
            + "### History\n- 2026-10-19 09:00 ready other\n- 2026-10-19 09:05 running self\n\n",
        )

    def test_set_title(self):
        text = issue_file.set_title(FRONT, "ENG: 新 x")
        self.assertEqual(text, FRONT.replace("title: x", 'title: "ENG: \\u65b0\\u2028x"'))
        self.assertEqual(parse(text).title, "ENG: 新 x")

    def test_add_link_creates_links(self):
        text = issue_file.add_link(FRONT, title="PR one", url="https://x/1")
        text = issue_file.add_link(text, title="", url="https://x/2")
        self.assertIn('links:\n  - title: "PR\\u2028one"\n    url: "https://x/1"\n', text)
        self.assertEqual(
            parse(text).links, (FileLink("PR one", "https://x/1"), FileLink("", "https://x/2"))
        )

    def test_front_matter_strings_are_ascii(self):
        text = issue_file.add_link(issue_file.set_title(FRONT, "a\x85b"), title=" ", url="u\x85")
        self.assertTrue(text.isascii())
        self.assertEqual(len(text.splitlines()), len(FRONT.splitlines()) + 3)

    def test_drop_comment(self):
        text = issue_file.add_comment(FRONT, at=at(9, 0), body="a\nlines", ref_key="s:a")
        text = issue_file.add_comment(text, at=at(9, 1), body="b", ref_key="s:b")
        dropped = issue_file.drop_comment(text, "s:a")
        self.assertEqual([(n.key, n.body) for n in parse(dropped).comments], [("s:b", "b")])
        self.assertEqual(issue_file.drop_comment(dropped, "s:b"), FRONT + "### Comments\n\n")
        self.assertIsNone(issue_file.drop_comment(text, "s:none"))
        other = FRONT + "### Comments\n#### other · 2026-10-19 09:00 <!-- id=c1 key=s:o -->\nx\n"
        self.assertIsNone(issue_file.drop_comment(other, "s:o"))

    def test_add_related(self):
        text = issue_file.add_related(FRONT, "LOC-3")
        self.assertEqual(text, FRONT.replace("\n---\n", "\nrelated: [LOC-3]\n---\n"))
        self.assertEqual(parse(text).related, ("LOC-3",))
        listed = FRONT.replace("title: x\n", "title: x\nrelated: [LOC-1]\n")
        self.assertEqual(
            issue_file.add_related(listed, "LOC-3"),
            listed.replace("[LOC-1]", "[LOC-1, LOC-3]"),
        )
        empty = FRONT.replace("title: x\n", "title: x\nrelated: []\n")
        self.assertEqual(parse(issue_file.add_related(empty, "LOC-3")).related, ("LOC-3",))

    def test_add_comment_as_other(self):
        text = issue_file.add_comment(FRONT, at=at(9, 5), body="mine", source=Source.OTHER)
        self.assertEqual(text, FRONT + "### Comments\n\n#### other · 2026-10-19 09:05\nmine\n")
        self.assertEqual(parse(text).comments, (Note(at(9, 5), Source.OTHER, "mine", None),))

    def test_set_description_replaces_the_body(self):
        text = (
            FRONT
            + "\n### Description\n\nold\n\nlines\n\n### History\n- 2026-10-19 09:00 ready other\n"
        )
        edited = issue_file.set_description(text, "new\n### History\nend")
        self.assertEqual(edited, text.replace("old\n\nlines", "new\n ### History\nend"))
        self.assertEqual(parse(edited).description, "new\n ### History\nend")
        self.assertEqual(len(parse(edited).history), 1)
        cleared = issue_file.set_description(text, "")
        self.assertEqual(cleared, text.replace("old\n\nlines\n", ""))
        self.assertEqual(parse(cleared).description, "")

    def test_set_description_fills_an_empty_section(self):
        text = FRONT + "### Description\n\n### History\n"
        self.assertEqual(
            issue_file.set_description(text, "d"), FRONT + "### Description\nd\n\n### History\n"
        )

    def test_set_description_makes_the_section(self):
        history = "### History\n- 2026-10-19 09:00 ready other\n"
        made = issue_file.set_description(FRONT + history, "d\ne")
        self.assertEqual(made, FRONT + "### Description\nd\ne\n" + history)
        self.assertEqual(parse(made).description, "d\ne")
        self.assertEqual(issue_file.set_description(FRONT, "d"), FRONT + "### Description\nd\n")
        self.assertEqual(
            issue_file.set_description(FRONT + history, ""),
            FRONT + "### Description\n" + history,
        )

    def test_set_front_adds_each_key(self):
        text = issue_file.set_front(FRONT, "task", "light-build")
        text = issue_file.set_front(text, "client", "claude")
        text = issue_file.set_front(text, "priority", 3)
        text = issue_file.set_front(text, "blocked_by", ["LOC-1", "LOC-2"])
        self.assertEqual(
            text,
            FRONT.replace(
                "\n---\n",
                '\ntask: "light-build"\nclient: "claude"\npriority: 3\n'
                "blocked_by: [LOC-1, LOC-2]\n---\n",
            ),
        )
        found = parse(text)
        self.assertEqual(
            (found.task, found.client, found.priority, found.blocked_by),
            ("light-build", "claude", 3, ("LOC-1", "LOC-2")),
        )

    def test_set_front_replaces_in_place(self):
        text = FRONT.replace(
            "title: x\n", "title: x\ntask: build\nclient: x\npriority: 1\nblocked_by: [LOC-1]\n"
        )
        edited = issue_file.set_front(text, "task", "review")
        edited = issue_file.set_front(edited, "client", "codex")
        edited = issue_file.set_front(edited, "priority", 0)
        edited = issue_file.set_front(edited, "blocked_by", [])
        self.assertEqual(
            edited,
            FRONT.replace(
                "title: x\n",
                'title: x\ntask: "review"\nclient: "codex"\npriority: 0\nblocked_by: []\n',
            ),
        )
        found = parse(edited)
        self.assertEqual(
            (found.task, found.client, found.priority, found.blocked_by),
            ("review", "codex", 0, ()),
        )

    def test_set_front_removes_task_and_client(self):
        text = FRONT.replace("title: x\n", "title: x\ntask: build\nclient: x\n")
        cleared = issue_file.set_front(issue_file.set_front(text, "task", None), "client", "")
        self.assertEqual(cleared, FRONT)
        self.assertEqual((parse(cleared).task, parse(cleared).client), (None, None))
        self.assertEqual(issue_file.set_front(FRONT, "task", None), FRONT)

    def test_set_front_values_stay_on_one_line(self):
        for client in ("acme\nparent: LOC-1", '"acme', "a\x85b c\r"):
            with self.subTest(client=client):
                text = issue_file.set_front(FRONT, "client", client)
                self.assertEqual(len(text.splitlines()), len(FRONT.splitlines()) + 1)
                found = parse(text)
                self.assertEqual((found.client, found.parent), (client, None))


class New(Zoned):
    """A new issue's text: front matter, Description, its first comment and History."""

    def new(self, **given):
        fields = {
            "ident": "LOC-12",
            "title": "ENG: 新 x",
            "priority": 2,
            "created": at(9, 30),
            "parent": "LOC-5",
            "description": "Handoff from LOC-1: file:///b\n\nBuild it.",
            "note_at": at(9, 30),
            "note": "Created by promote.",
            "note_key": "LOC-1:engineer:2026-10-19T12:00:00.000Z",
            "state": LocalState.READY,
        }
        return issue_file.new(**(fields | given))

    def test_round_trips(self):
        text = self.new()
        self.assertTrue(text.split("---")[1].isascii())
        found = parse(text, assignee="engineer")
        self.assertEqual(
            (found.id, found.title, found.priority, found.created, found.parent, found.related),
            ("LOC-12", "ENG: 新 x", 2, at(9, 30), "LOC-5", ()),
        )
        self.assertEqual(found.description, "Handoff from LOC-1: file:///b\n\nBuild it.")
        self.assertEqual(
            found.comments,
            (
                Note(
                    at(9, 30),
                    Source.SELF,
                    "Created by promote.",
                    "LOC-1:engineer:2026-10-19T12:00:00.000Z",
                ),
            ),
        )
        self.assertEqual(found.history, (Entry(at(9, 30), LocalState.READY, Source.SELF),))

    def test_no_parent(self):
        text = self.new(parent=None)
        self.assertNotIn("parent:", text)
        self.assertIsNone(parse(text).parent)

    def test_description_structure_is_escaped(self):
        text = self.new(description="top\n### Comments\n#### self · x\n###\nend")
        found = parse(text)
        self.assertEqual(found.description, "top\n ### Comments\n #### self · x\n ###\nend")
        self.assertEqual(len(found.comments), 1)

    def test_text(self):
        self.assertEqual(
            self.new(),
            '---\nid: LOC-12\ntitle: "ENG: \\u65b0 x"\npriority: 2\n'
            "created: 2026-10-19T09:30:00-03:00\nparent: LOC-5\n---\n\n"
            "### Description\nHandoff from LOC-1: file:///b\n\nBuild it.\n\n"
            "### Comments\n\n"
            "#### self · 2026-10-19 09:30 <!-- id=c1 key=LOC-1:engineer:2026-10-19T12:00:00.000Z -->\n"
            "Created by promote.\n\n"
            "### History\n- 2026-10-19 09:30 ready self\n",
        )

    def test_without_a_note(self):
        text = issue_file.new(
            ident="LOC-12",
            title="x",
            priority=0,
            created=at(9, 30),
            parent=None,
            description="d",
            state=LocalState.BACKLOG,
            source=Source.OTHER,
        )
        self.assertEqual(
            text,
            '---\nid: LOC-12\ntitle: "x"\npriority: 0\ncreated: 2026-10-19T09:30:00-03:00\n---\n\n'
            "### Description\nd\n\n### History\n- 2026-10-19 09:30 backlog other\n",
        )
        found = parse(text)
        self.assertEqual((found.description, found.comments), ("d", ()))
        self.assertEqual(found.history, (Entry(at(9, 30), LocalState.BACKLOG, Source.OTHER),))

    def test_task_client_and_blockers(self):
        text = self.new(task="light-build", client="claude", blocked_by=("LOC-1", "LOC-3"))
        self.assertIn(
            '\ntitle: "ENG: \\u65b0 x"\ntask: "light-build"\nclient: "claude"\npriority: 2\n'
            "created: 2026-10-19T09:30:00-03:00\nblocked_by: [LOC-1, LOC-3]\nparent: LOC-5\n---\n",
            text,
        )
        found = parse(text)
        self.assertEqual(
            (found.task, found.client, found.blocked_by, found.parent),
            ("light-build", "claude", ("LOC-1", "LOC-3"), "LOC-5"),
        )

    def test_client_stays_on_one_line(self):
        for client in ("acme\nparent: LOC-1", '"acme'):
            with self.subTest(client=client):
                text = self.new(parent=None, client=client)
                self.assertEqual(text.split("\n---\n")[0].count("\n"), 5)
                found = parse(text)
                self.assertEqual((found.client, found.parent), (client, None))

    def test_new_comment_ids_follow(self):
        text = issue_file.add_comment(self.new(), at=at(9, 31), body="b", ref_key="s:a")
        self.assertIn("<!-- id=c2 key=s:a -->", text)
