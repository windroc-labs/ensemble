"""session/engine/records.py: RunLog, run.jsonl writes and reads."""

import errno
import io
import json
import os
import threading
import unittest
import unittest.mock
from contextlib import redirect_stderr
from datetime import datetime

from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import SessionInfo
from tests.session.case import Case
from tests.session.threads import capture_errors
from tests.utils.sandbox import scratch

SID = "4f1c2b9e-0d6a-4c1e-9b7a-2e5f8a3d6c10"


class Base(Case):
    def setUp(self):
        self.dir = scratch(self, real=False)
        self.path = os.path.join(self.dir, "run.jsonl")
        self.log = RunLog(self.dir)

    def content(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def write(self, *lines):
        with open(self.path, "wb") as f:
            f.writelines(
                (line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n"
                for line in lines
            )

    def promptly(self, call):
        """call()'s result or exception; fails instead of hanging when it blocks."""
        out = {}

        def run():
            out["value"] = call()

        thread = threading.Thread(target=run, daemon=True)
        with capture_errors(thread, lambda error: out.update(error=error)):
            thread.start()
            thread.join(5)
        if thread.is_alive():
            self.fail("blocked")
        if "error" in out:
            raise out["error"]
        return out["value"]


class Stamp(Case):
    def test_is_local_iso_8601_with_offset_to_the_millisecond(self):
        text = RunLog.stamp()
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")
        self.assertIsNotNone(datetime.fromisoformat(text).utcoffset())


class RecordPath(Case):
    def test_is_run_jsonl_in_the_absolute_workdir(self):
        self.assertEqual(RunLog("w").path, os.path.join(os.path.abspath("w"), "run.jsonl"))


class Note(Base):
    def test_writes_one_json_line_with_a_millisecond_ts_kind_and_fields(self):
        self.log.note("session", sid=SID, cwd="/wörk", project=False)
        line = self.content().decode()
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1)
        self.assertIn("/wörk", line)
        record = json.loads(line)
        self.assertRegex(record.pop("ts"), r"T\d\d:\d\d:\d\d\.\d{3}[+-]")
        self.assertEqual(record, {"kind": "session", "sid": SID, "cwd": "/wörk", "project": False})

    def test_durable_syncs_the_file_and_its_directory(self):
        with unittest.mock.patch.object(os, "fsync", wraps=os.fsync) as fsync:
            self.log.note("client_session", sid=SID, client_id="n")
            self.assertEqual(fsync.call_count, 0)
            self.log.note("client_session", durable=True, sid=SID, client_id="n")
            self.assertEqual(fsync.call_count, 2)
        self.assertEqual(len(self.log.read()), 2)

    def test_an_os_error_is_raised_or_with_best_effort_printed(self):
        missing = RunLog(os.path.join(self.dir, "gone"))
        with self.assertRaises(OSError):
            missing.note("stderr", text="x")
        err = io.StringIO()
        with redirect_stderr(err):
            missing.note("stderr", best_effort=True, text="x")
        self.assertRegex(err.getvalue(), r"^records: .*gone")


class Read(Base):
    def test_the_json_object_lines_in_order_invalid_ones_skipped(self):
        first, second = {"kind": "session", "n": 1}, {"kind": "result", "n": "é"}
        self.write(
            first,
            b"",
            b"not json",
            b"[]",
            b"3",
            b"\xff\xfe",
            b'{"kind": "session"',
            b"[" * 100_000,
            second,
        )
        self.assertEqual(self.log.read(), [first, second])

    def test_missing_or_empty_is_empty(self):
        self.assertEqual(self.log.read(), [])
        self.write()
        self.assertEqual(self.log.read(), [])

    def test_a_fifo_or_directory_is_never_read(self):
        os.mkfifo(self.path)
        self.assertEqual(self.promptly(self.log.read), [])
        os.remove(self.path)
        os.mkdir(self.path)
        self.assertEqual(self.log.read(), [])

    def test_an_os_error_while_reading_is_empty(self):
        self.write({"kind": "session"})
        with unittest.mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertEqual(self.log.read(), [])


def info(**changes) -> SessionInfo:
    fields = {"sid": SID, "cwd": "/a", "project": False}
    return SessionInfo(**{**fields, "transcript": "", "resume": "", **changes})


class Session(Base):
    def test_the_latest_valid_session_line_wins_and_client_session_lines_change_nothing(self):
        first = {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": False}
        latest = {**first, "cwd": "/b", "project": True, "transcript": "/t", "resume": "r"}
        plain = {"kind": "session", "sid": SID, "cwd": "/a", "project": False}
        for case, lines, expected in (
            (
                "latest valid line wins",
                (
                    first,
                    latest,
                    b"not json",
                    b"[]",
                    b"\xff\xfe",
                    b'{"kind": "session"',
                    {**first, "kind": "input"},
                    {**first, "sid": "other"},
                    {**first, "cwd": "rel"},
                    {**first, "cwd": "/x\x1b[2J"},
                    {**first, "cwd": 3},
                    {**first, "project": 1},
                    {**first, "transcript": "rel.jsonl"},
                    {**first, "transcript": 3},
                    {**first, "resume": None},
                    {k: v for k, v in first.items() if k != "project"},
                ),
                info(cwd="/b", project=True, transcript="/t", resume="r"),
            ),
            ("no transcript or resume", (plain,), info()),
            (
                "client session lines",
                (
                    {"kind": "client_session", "sid": SID, "client_id": "early"},
                    plain,
                    {"kind": "client_session", "sid": SID, "client_id": "n1"},
                ),
                info(),
            ),
        ):
            with self.subTest(case=case):
                self.write(*lines)
                self.assertEqual(self.log.session(SID), expected)

    def test_metadata_updates_only_the_matching_latest_session(self):
        first = {"kind": "session", "sid": SID, "cwd": "/a", "project": False}
        self.write(
            first,
            {"kind": "session_metadata", "sid": "other", "resume": "wrong"},
            {"kind": "session_metadata", "sid": SID, "transcript": "/t", "resume": "resume"},
            {"kind": "session_metadata", "sid": SID, "transcript": "rel", "resume": "forged"},
            {"kind": "session_metadata", "sid": SID, "resume": 3},
        )
        self.assertEqual(self.log.session(SID), info(transcript="/t", resume="resume"))
        self.write(first, {"kind": "session_metadata", "sid": SID, "transcript": "/t"}, first)
        self.assertEqual(self.log.session(SID), info())

    def test_none_without_a_valid_session_line(self):
        self.assertIsNone(self.log.session(SID))
        self.write(b"", {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": "yes"})
        self.assertIsNone(self.log.session(SID))


if __name__ == "__main__":
    unittest.main()
