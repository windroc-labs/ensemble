"""delegate/event/outcome.py: SCHEMA beside it; main's drive.validate cases with the same results
(FR-20) and read with deliverable_file (FR-19); main's save cases: unit/utils/files_test.py.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock

from ensemble.delegate.event import outcome
from ensemble.delegate.event.outcome import (
    PR_PATH,
    PULL,
    SCHEMA,
    STATUSES,
    InvalidOutcome,
    Outcome,
    OutcomeStatus,
    read,
    validate,
)
from ensemble.delegate.event.types import Updraft, UpdraftStatus
from tests.delegate.case import Case
from tests.delegate.unit.composer.shipped import (
    SCHEMA_SHA256,
)

DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
FILE = {"status": "done", "title": "T", "summary": "S", "deliverable_file": "out.md"}


LOCAL = {"type": "local"}
GH = frozenset({"github.com"})


def schema():
    with open(SCHEMA) as f:
        return json.load(f)


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")

    def check(self, data, output=None, hosts=GH):
        return validate(data, output or LOCAL, self.work, hosts)

    def fails(self, data, msg, output=None, hosts=GH):
        with self.assertRaises(InvalidOutcome) as cm:
            self.check(data, output, hosts)
        self.assertIn(msg, str(cm.exception))

    def touch(self, *parts, text="x"):
        path = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def outside(self, name="secret.md", text="key"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path


class Schema(Case):
    def test_schema_and_validate_agree(self):
        s = schema()
        self.assertEqual(tuple(s["properties"]["status"]["enum"]), STATUSES)
        self.assertEqual(
            set(s["properties"]), {f.name for f in dataclasses.fields(Outcome)} | {"updraft"}
        )
        updraft = s["properties"]["updraft"]
        self.assertEqual(tuple(updraft["properties"]["status"]["enum"]), tuple(UpdraftStatus))
        self.assertEqual(set(updraft["properties"]), {f.name for f in dataclasses.fields(Updraft)})
        self.assertEqual(s["properties"]["questions"]["maxItems"], 4)


class Validate(Base):
    def test_valid(self):
        self.assertEqual(
            self.check({**DONE, "title": "  T  "}), Outcome("done", "T", "S", deliverable="# Doc\n")
        )

    def test_the_shape_schema_and_title_hold(self):
        for data, msg in (
            ([DONE], "not an object"),
            ({**DONE, "status": "maybe"}, "status 'maybe'"),
            ({**DONE, "summary": 3}, "must be text"),
            ({**DONE, "deliverable": ["x"]}, "must be text"),
            ({**DONE, "files": "x.md"}, "must be lists"),
            ({**DONE, "extra": 1}, "unknown field 'extra'"),
            ({**DONE, "title": "x" * 201}, "title is longer than 200"),
            ({**DONE, "summary": "x" * 301}, "summary is longer than 300"),
            ({**DONE, "status": "needs_input", "questions": [3]}, "questions[0] must be text"),
            ({**DONE, "files": None}, "files must be a list"),
            ({**DONE, "url": None}, "url must be text"),
            *(
                ({**DONE, "title": title}, "printable")
                for title in ("", "  ", "a\nb", "a\x1b[2Jb", 5)
            ),
        ):
            with self.subTest(msg=msg, data=data):
                self.fails(data, msg)

    def test_a_summary_of_up_to_300_characters_holds(self):
        self.assertEqual(self.check({**DONE, "summary": "x" * 300}).summary, "x" * 300)

    def test_the_status_is_one_of_the_outcome_statuses(self):
        for status in STATUSES:
            data = {**DONE, "status": status, "questions": ["q?"]}
            with self.subTest(status=status):
                self.assertIs(self.check(data).status, OutcomeStatus(status))
        for bad in ("maybe", "DONE", None, ["done"]):
            with self.subTest(bad=bad):
                self.fails(
                    {**DONE, "status": bad},
                    f"status {bad!r} is not one of done, needs_input, failed",
                )

    def test_questions(self):
        ask = {**DONE, "status": "needs_input"}
        self.fails(ask, "1–4 questions")
        self.fails({**ask, "questions": ["q"] * 5}, "1–4 questions")
        self.assertEqual(self.check({**ask, "questions": ["q?"]}).questions, ["q?"])
        self.assertEqual(self.check({**DONE, "questions": ["q"]}).questions, [])

    def test_done_needs_the_deliverable_when_it_comes_back(self):
        self.fails({**DONE, "deliverable": " "}, "must carry the deliverable", {"type": "local"})
        self.assertEqual(
            self.check({**DONE, "deliverable": ""}, {"type": "pull-request"}).deliverable, ""
        )
        self.assertEqual(
            self.check({**DONE, "status": "failed", "deliverable": ""}).status, "failed"
        )

    def test_url_must_fit_the_destination(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        ok = "https://github.com/o/docs/blob/main/R/x.md"
        self.assertEqual(self.check({**DONE, "url": ok}, gh).url, ok)
        for bad in (
            "https://evil.example/o/docs/blob/main/x.md",
            "https://github.com/o/docs/blob/dev/x.md",
        ):
            self.fails({**DONE, "url": bad}, "url must start with", gh)
        for bad in (
            ok + "/../../../../x/y",
            "https://github.com/o/docs/blob/main/%2e%2e/%2e%2e/x",
            ok + "\n)[x](y",
            ok + " x",
            "http://github.com/o/docs/blob/main/x.md",
            5,
        ):
            self.fails({**DONE, "url": bad}, "plain https link", gh)
        pr = {"type": "pull-request"}
        for good in (
            "https://github.com/o/n/pull/7",
            "https://ghe.io/o/n/compare/main...b?expand=1",
            "https://github.com/o/n/tree/b",
        ):
            self.assertEqual(self.check({**DONE, "url": good}, pr, GH | {"ghe.io"}).url, good)
        self.fails(
            {**DONE, "url": "https://github.com/o/n/issues/1"}, "pull request, compare or tree", pr
        )
        self.assertEqual(self.check({**DONE, "url": "https://x"}).url, "")

    def test_pull_request_urls_must_name_a_gh_host(self):
        pr, ghe = {"type": "pull-request"}, frozenset({"ghe.example.com"})
        for status in ("done", "failed"):
            for url, hosts, shown in (
                ("https://attacker.example/o/n/pull/1", GH, "attacker.example"),
                ("https://attacker.example/o/n/tree/b", GH, "attacker.example"),
                ("https://github.com/o/n/pull/7", ghe, "github.com"),
                ("https://user@github.com/o/n/pull/7", GH, "user@github.com"),
                ("https://github.com:8443/o/n/pull/7", GH, "github.com:8443"),
                ("https://github.com/o/n/pull/7", frozenset(), "github.com"),
            ):
                with self.subTest(status=status, url=url, hosts=hosts):
                    self.fails(
                        {**DONE, "status": status, "url": url},
                        f"url host {shown} is not one of {', '.join(sorted(hosts)) or '(none)'}",
                        pr,
                        hosts,
                    )
        url = "https://ghe.example.com/o/n/pull/3"
        self.assertEqual(self.check({**DONE, "url": url}, pr, ghe).url, url)

    def test_files_are_md_files_under_the_workdir(self):
        plan = self.touch("docs", "plan.md")
        self.assertEqual(
            self.check({**DONE, "files": [plan, "docs/plan.md"]}).files,
            [os.path.realpath(plan)] * 2,
        )
        os.makedirs(self.work + "2")
        sibling = os.path.join(self.work + "2", "x.md")
        with open(sibling, "w") as f:
            f.write("x")
        outside = self.outside()
        os.symlink(outside, os.path.join(self.work, "link.md"))
        for bad in (
            self.touch("notes.txt"),
            os.path.join(self.work, "missing.md"),
            sibling,
            "link.md",
            os.path.join(self.work, "..", "secret.md"),
            os.path.expanduser("~/.ssh/id_rsa"),
            7,
            "a\0b.md",
        ):
            self.fails({**DONE, "files": [bad]}, "is not a .md file under the workdir")


class Reading(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.work)

    def read(self, data, output=None):
        return self.pair(data, output)[0]

    def pair(self, data, output=None):
        body = data if isinstance(data, str) else json.dumps(data)
        return read(body, output or LOCAL, self.work, GH)

    def refused(self, data, msg):
        with self.assertRaises(InvalidOutcome) as cm:
            self.read(data)
        self.assertIn(msg, str(cm.exception))
        return str(cm.exception)


class Read(Reading):
    def test_not_json_or_not_an_object_is_refused(self):
        for body, msg in (
            *(
                (body, "not JSON: ")
                for body in ("", "{", "{'status': 'done'}", "[" * 10**6 + "]" * 10**6)
            ),
            *((body, "not an object") for body in ("[1]", '"x"', "3", "null")),
        ):
            with self.subTest(body=body[:20]):
                self.refused(body, msg)

    def test_without_deliverable_file_it_is_validate(self):
        self.assertEqual(self.read(DONE), self.check(DONE))
        self.refused({**DONE, "status": "maybe"}, "status 'maybe'")

    def test_deliverable_file_becomes_the_deliverable(self):
        text = "# Report\n\nü and more\n"
        path = self.touch("out.md", text=text)
        for name in ("out.md", path):
            with self.subTest(name=name):
                self.assertEqual(
                    self.read({**FILE, "deliverable_file": name}),
                    Outcome("done", "T", "S", deliverable=text),
                )

    def test_deliverable_file_must_be_an_md_file_under_the_workdir(self):
        self.touch("notes.txt")
        outside = self.outside()
        os.symlink(outside, os.path.join(self.work, "link.md"))
        for name in (
            "missing.md",
            "notes.txt",
            "../secret.md",
            outside,
            "link.md",
            7,
            None,
            ["out.md"],
            "a\0b.md",
        ):
            with self.subTest(name=name):
                self.refused(
                    {**FILE, "deliverable_file": name}, "is not a .md file under the workdir"
                )

    def test_a_fifo_is_refused_at_once(self):
        os.mkfifo(os.path.join(self.work, "out.md"))
        failed = []
        worker = threading.Thread(
            target=lambda: failed.append(self.refused(FILE, "is not a .md file")), daemon=True
        )
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "read blocked on a FIFO")
        self.assertEqual(len(failed), 1)

    def test_a_blank_too_large_or_not_utf8_deliverable_file_is_refused(self):
        cap = 4 * schema()["properties"]["deliverable"]["maxLength"]
        for data, msg in (
            (b" \n", "must carry the deliverable"),
            (b"\0" * (cap + 1), "too large"),
            (b"# Doc \xff\n", "not UTF-8 text"),
        ):
            with self.subTest(msg=msg):
                with open(os.path.join(self.work, "out.md"), "wb") as f:
                    f.write(data)
                self.refused(FILE, msg)

    def test_a_name_with_control_characters_is_shown_as_its_repr(self):
        name = "a\x1b[2J\nb.md"
        error = self.refused({**FILE, "deliverable_file": name}, repr(name))
        self.assertTrue(error.isprintable(), error)

    def test_deliverable_and_deliverable_file_together(self):
        self.touch("out.md")
        for deliverable in ("# Doc\n", ""):
            with self.subTest(deliverable=deliverable):
                self.refused(
                    {**FILE, "deliverable": deliverable},
                    "give deliverable or deliverable_file, not both",
                )


class ReadRace(Base):
    """
    A file swapped between _file's check and the open, faked by patching os.open to swap first.
    """

    def swapped(self, swap, reason=""):
        real_open = os.open
        done = []

        def open_(path, *args, **kw):
            if not done:
                done.append(swap())
            return real_open(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=open_),
            self.assertRaises(InvalidOutcome) as cm,
        ):
            read(json.dumps({**FILE, "deliverable_file": "d/out.md"}), LOCAL, self.work, GH)
        self.assertEqual(len(done), 1)
        self.assertIn("deliverable_file ", str(cm.exception))
        self.assertIn(reason, str(cm.exception))

    def setUp(self):
        super().setUp()
        self.target = self.touch("d", "out.md", text="# Doc\n")
        self.secret = self.outside("secret.md")

    def test_a_symlink_to_an_outside_md_file(self):
        def swap():
            os.unlink(self.target)
            os.symlink(self.secret, self.target)

        self.swapped(swap)

    def test_a_fifo(self):
        def swap():
            os.unlink(self.target)
            os.mkfifo(self.target)

        self.swapped(swap, "not a regular file")

    def test_a_parent_dir_swapped_for_a_symlink_out(self):
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        shutil.copy(self.secret, os.path.join(out, "out.md"))

        def swap():
            os.rename(os.path.dirname(self.target), os.path.join(self.work, "d-old"))
            os.symlink(out, os.path.dirname(self.target))

        self.swapped(swap, "no longer under the workdir")


HEAD = "0123456789abcdef" * 2 + "01234567"


class UpdraftField(Reading):
    def test_read_returns_the_updraft_result_or_none(self):
        self.assertEqual(self.pair(DONE), (self.check(DONE), None))
        for status in UpdraftStatus:
            with self.subTest(status=status):
                data = {**DONE, "updraft": {"status": str(status), "head": HEAD}}
                self.assertEqual(self.pair(data), (self.check(DONE), Updraft(status, HEAD)))

    def test_an_invalid_updraft_is_refused(self):
        good = {"status": "converged", "head": HEAD}
        for bad, msg in (
            ("converged", "updraft must be an object"),
            ({"status": "converged"}, "updraft lacks 'updraft.head'"),
            ({"head": HEAD}, "updraft lacks 'updraft.status'"),
            ({**good, "pr": ""}, "unknown field 'updraft.pr'"),
            (
                {**good, "status": "done"},
                "updraft.status 'done' is not one of converged, capped-without-pass, stopped",
            ),
            ({**good, "head": HEAD[:-1]}, "head must match"),
            ({**good, "head": HEAD + "0"}, "head must match"),
            ({**good, "head": HEAD.upper()}, "head must match"),
            ({**good, "head": 7}, "head must be text"),
            ({**good, "head": HEAD + "\n"}, "head must match"),
        ):
            with self.subTest(bad=bad):
                self.refused({**DONE, "updraft": bad}, msg)


class FieldPaths(Base):
    def test_every_schema_error_keeps_its_path(self):
        for value, rule, message in (
            (7, {"type": "object"}, "{path} must be an object"),
            ({}, {"type": "object", "required": ["child"]}, "{path} lacks '{path}.child'"),
            (
                {"extra": 1},
                {"type": "object", "additionalProperties": False},
                "unknown field '{path}.extra'",
            ),
            (7, {"type": "string"}, "{path} must be text"),
            ("", {"type": "string", "minLength": 1}, "{path} is shorter than 1 characters"),
            ("xx", {"type": "string", "maxLength": 1}, "{path} is longer than 1 characters"),
            ("x", {"type": "string", "pattern": "[a-z]{2}"}, "{path} must match [a-z]{{2}}"),
            ("x", {"type": "string", "enum": ["y", "z"]}, "{path} 'x' is not one of y, z"),
            (7, {"type": "array"}, "{path} must be a list"),
            ([1, 2], {"type": "array", "maxItems": 1}, "{path} has more than 1 items"),
            (
                [7],
                {"type": "array", "items": {"type": "string"}},
                "{path}[0] must be text",
            ),
        ):
            for path, wrapped, properties in (
                ("detail", value, rule),
                (
                    "detail.inner",
                    {"inner": value},
                    {"type": "object", "properties": {"inner": rule}},
                ),
                (
                    "detail[0].inner",
                    [{"inner": value}],
                    {
                        "type": "array",
                        "items": {"type": "object", "properties": {"inner": rule}},
                    },
                ),
            ):
                with self.subTest(path=path, rule=rule):
                    s = {"type": "object", "properties": {"detail": properties}}
                    with self.assertRaises(InvalidOutcome) as cm:
                        outcome._conform({"detail": wrapped}, s, "")
                    self.assertEqual(str(cm.exception), message.format(path=path))

    def test_root_errors_keep_their_original_text(self):
        for data, s, message in (
            (DONE, {"type": "object", "required": ["detail"]}, "outcome lacks 'detail'"),
            (
                {**DONE, "extra": 1},
                schema(),
                "unknown field 'extra'",
            ),
            (
                {**DONE, "status": "converged"},
                schema(),
                "status 'converged' is not one of done, needs_input, failed",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaises(InvalidOutcome) as cm:
                    outcome._conform(data, s, "")
                self.assertEqual(str(cm.exception), message)


class Paths(Case):
    def test_pull_is_the_pull_request_part_of_pr_path(self):
        for path, pull, pr in (
            ("/o/n/pull/7", True, True),
            ("/o/n/pull/7/files", False, False),
            ("/o/n/pull/x", False, False),
            ("/o/n/compare/main...b", False, True),
            ("/o/n/tree/b", False, True),
            ("/o/pull/7", False, False),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    (bool(PULL.fullmatch(path)), bool(PR_PATH.fullmatch(path))), (pull, pr)
                )


class SchemaFile(Case):
    def test_is_beside_outcome_py(self):
        self.assertEqual(os.path.dirname(SCHEMA), os.path.dirname(outcome.__file__))


class Acceptance(Case):
    def test_the_outcome_schema_is_mains(self):
        with open(SCHEMA, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), SCHEMA_SHA256)


if __name__ == "__main__":
    unittest.main()
