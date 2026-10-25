"""The hidden store: one {sid, hidden} file per session name under the test's HOME."""

import io
import json
import os
import time
from contextlib import redirect_stderr

from ensemble.session.viewer import hidden
from ensemble.utils import STALE_AGE

from session.tests.case import Case

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


class HiddenTest(Case):
    def path(self, name: str = "w1") -> str:
        return os.path.join(self.ensemble, "hidden", f"{name}.json")

    def put(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.path()), exist_ok=True)
        with open(self.path(), "w", encoding="utf-8") as f:
            f.write(text)

    def test_write_then_read_answers_for_its_sid(self):
        hidden.write("w1", SID, hidden=True, live=None)
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"sid": SID, "hidden": True})
        self.assertTrue(hidden.read("w1", SID))
        hidden.write("w1", SID, hidden=False, live=None)
        self.assertFalse(hidden.read("w1", SID))

    def test_read_is_false_for_a_missing_bad_or_other_sid_entry(self):
        self.assertFalse(hidden.read("w1", SID))
        for text in (
            "not json",
            "[]",
            json.dumps({"sid": SID}),
            json.dumps({"sid": SID, "hidden": "yes"}),
            json.dumps({"sid": OTHER, "hidden": True}),
            json.dumps({"sid": SID, "hidden": True}) + " " * 5000,
        ):
            with self.subTest(text=text[:40]):
                self.put(text)
                self.assertFalse(hidden.read("w1", SID))
        os.unlink(self.path())
        os.mkdir(self.path())
        self.assertFalse(hidden.read("w1", SID))

    def test_start_writes_only_on_a_new_sid(self):
        hidden.start("w1", SID, live=None)
        self.assertTrue(hidden.read("w1", SID))
        hidden.write("w1", SID, hidden=False, live=None)
        hidden.start("w1", SID, live=None)
        self.assertFalse(hidden.read("w1", SID))
        hidden.start("w1", OTHER, live=None)
        self.assertTrue(hidden.read("w1", OTHER))
        self.put("not json")
        hidden.start("w1", SID, live=None)
        self.assertTrue(hidden.read("w1", SID))

    def test_names_have_their_own_entries(self):
        hidden.write("w1", SID, hidden=True, live=None)
        self.assertFalse(hidden.read("w2", SID))

    def test_a_failed_write_raises_its_oserror(self):
        os.makedirs(os.path.join(self.ensemble, "hidden"))
        os.chmod(os.path.join(self.ensemble, "hidden"), 0o500)
        self.addCleanup(os.chmod, os.path.join(self.ensemble, "hidden"), 0o700)
        with self.assertRaises(OSError):
            hidden.write("w1", SID, hidden=True, live=None)

    def test_a_write_drops_entries_unwritten_for_stale_age_but_live_sessions(self):
        for live, want in (
            (None, ["live.json", "old.json", "recent.json", "w1.json"]),
            (["live", "w1"], ["live.json", "recent.json", "w1.json"]),
        ):
            with self.subTest(live=live):
                if os.path.exists(self.path()):
                    os.unlink(self.path())
                then = time.time() - STALE_AGE
                for name, when in (("old", then - 60), ("live", then - 60), ("recent", then + 60)):
                    hidden.write(name, SID, hidden=True, live=None)
                    os.utime(self.path(name), (when, when))
                err = io.StringIO()
                with redirect_stderr(err):
                    hidden.start("w1", OTHER, live=live)
                self.assertEqual(sorted(os.listdir(os.path.dirname(self.path()))), want)
                self.assertEqual(err.getvalue(), "")
