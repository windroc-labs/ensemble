"""The read-only opencode.db reader over fake dbs in OpenCode 1.19.3's layout."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from ensemble.session.clients.opencode import transcript
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import SES, make_db, text

OTHER = "ses_otherOtherOtherOther0001"


class TranscriptTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(os.path.realpath(tmp.name), "opencode", "opencode.db")

    def build(self, messages=(), sessions=None):
        return make_db(
            self.db, {SES: 1_760_000_000_500} if sessions is None else sessions, messages
        )

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = transcript.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_delivered_message_is_listed(self):
        self.build(
            [
                (SES, "user", 1_760_000_000_000, [text("Fix the build.")]),
                (SES, "assistant", 1_760_000_000_100, [text("Done: tests pass.")]),
            ]
        )
        found = transcript.entries(self.db, SES, 100)
        self.assertEqual(
            found,
            [
                {"time": "2025-10-09T08:53:20.000Z", "role": "user", "text": "Fix the build."},
                {
                    "time": "2025-10-09T08:53:20.100Z",
                    "role": "assistant",
                    "text": "Done: tests pass.",
                },
            ],
        )

    def test_an_undelivered_text_is_not(self):
        self.build([(SES, "user", 1, [text("first")]), (OTHER, "user", 2, [text("elsewhere")])])
        texts = [entry["text"] for entry in transcript.entries(self.db, SES, 100)]
        self.assertEqual(texts, ["first"])
        self.assertNotIn("not sent yet", texts)

    def test_synthetic_and_ignored_parts_are_left_out(self):
        self.build(
            [
                (
                    SES,
                    "user",
                    1,
                    [
                        text("kept"),
                        text("context", synthetic=True),
                        text("hidden", ignored=True),
                        text("   "),
                        {"type": "tool", "tool": "bash"},
                    ],
                ),
                (SES, "system", 2, [text("not a role it lists")]),
            ]
        )
        self.assertEqual([e["text"] for e in transcript.entries(self.db, SES, 100)], ["kept"])

    def test_limit_keeps_the_newest(self):
        self.build([(SES, "user", n, [text(f"t{n}")]) for n in range(1, 6)])
        found = transcript.entries(self.db, SES, 2)
        self.assertEqual([e["text"] for e in found], ["t4", "t5"])

    def test_bad_rows_are_skipped(self):
        self.build(
            [
                (SES, "user", 1, ["{not json", json.dumps(["a list"]), text("good")]),
                (SES, "assistant", 10**17, [text("bad time")]),
            ]
        )
        self.assertEqual([e["text"] for e in transcript.entries(self.db, SES, 100)], ["good"])

    def test_time_format(self):
        self.build([(SES, "user", 1_760_000_000_007, [text("x")])])
        (entry,) = transcript.entries(self.db, SES, 100)
        self.assertEqual(entry["time"], "2025-10-09T08:53:20.007Z")

    def test_updated(self):
        self.build()
        self.assertEqual(transcript.updated(self.db, SES), 1_760_000_000.5)
        self.assertIsNone(transcript.updated(self.db, OTHER))

    def test_a_non_number_time_updated_is_refused(self):
        self.build(sessions={SES: "soon"})
        with self.assertRaises(transcript.Refused):
            transcript.updated(self.db, SES)

    def test_a_missing_db_is_refused(self):
        with self.assertRaisesRegex(transcript.Refused, "no database"):
            transcript.updated(self.db, SES)
        with self.assertRaises(transcript.Refused):
            transcript.entries(self.db, SES, 100)

    def test_a_non_sqlite_or_tableless_db_is_refused(self):
        os.makedirs(os.path.dirname(self.db))
        Path(self.db).write_text("not a database at all, just text" * 100)
        with self.assertRaises(transcript.Refused):
            transcript.updated(self.db, SES)
        os.remove(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE other (x)")
        with self.assertRaises(transcript.Refused):
            transcript.updated(self.db, SES)
        with self.assertRaises(transcript.Refused):
            transcript.entries(self.db, SES, 100)

    def test_the_db_is_opened_read_only(self):
        self.build([(SES, "user", 1, [text("x")])])
        before = Path(self.db).read_bytes()
        db = transcript.connect(self.db)
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM part")
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_an_unknown_session_is_refused(self):
        self.build()
        with self.assertRaisesRegex(transcript.Refused, "no session"):
            transcript.entries(self.db, OTHER, 100)

    def test_database_follows_xdg_data_home(self):
        self.assertEqual(
            transcript.database({"XDG_DATA_HOME": "/data"}), "/data/opencode/opencode.db"
        )
        home = os.path.expanduser("~/.local/share/opencode/opencode.db")
        self.assertEqual(transcript.database({"XDG_DATA_HOME": "relative"}), home)
        self.assertEqual(transcript.database({}), home)

    def test_main_prints_json_lines(self):
        self.build([(SES, "user", 1, [text("héllo")]), (SES, "assistant", 2, [text("two")])])
        code, out, err = self.main(SES, "--db", self.db)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([json.loads(line)["text"] for line in lines], ["héllo", "two"])
        self.assertIn("héllo", lines[0])

    def test_main_defaults_to_xdg_data_home(self):
        self.build([(SES, "user", 1, [text("x")])])
        data = os.path.dirname(os.path.dirname(self.db))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}):
            code, out, _ = self.main(SES)
        self.assertEqual((code, json.loads(out)["text"]), (0, "x"))

    def test_main_missing_db(self):
        code, out, err = self.main(SES, "--db", self.db)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("opencode transcript: "))

    def test_main_unknown_session(self):
        self.build()
        code, out, err = self.main(OTHER, "--db", self.db)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no session", err)

    def test_main_bad_usage(self):
        self.build()
        for argv in (("not-a-session",), (SES, "--limit", "0"), (SES, "--limit", "10001"), ()):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    transcript.main(list(argv))
                self.assertEqual(raised.exception.code, 2)
