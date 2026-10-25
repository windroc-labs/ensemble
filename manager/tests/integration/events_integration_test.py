"""Event rotation transaction recovery, including native process death and later appends."""

import subprocess
from pathlib import Path

from ensemble.utils import append_line

from manager.tests.case import PY, child_env
from manager.tests.events_case import EventsCase

CRASH = (
    "import os,sys\n"
    "from unittest import mock\n"
    "from ensemble.manager.engine.events import Events\n"
    "from ensemble.utils import file_lock\n"
    "directory,source,destination,when,verb=sys.argv[1:]\n"
    "events=Events(directory)\n"
    "original=os.replace\n"
    "def replace(src,dst):\n"
    " match=str(src)==source and str(dst)==destination\n"
    " if match and when=='before': os._exit(91)\n"
    " original(src,dst)\n"
    " if match and when=='after': os._exit(91)\n"
    "with file_lock(events.path),mock.patch('os.replace',side_effect=replace):\n"
    " events.recover(0) if verb=='recover' else events.rotate(1,0)"
)
COMMIT = (
    "import os,sys\n"
    "from ensemble.manager.engine.events import Events\n"
    "from ensemble.utils import file_lock,save\n"
    "directory,ledger=sys.argv[1:]\n"
    "events=Events(directory)\n"
    "with file_lock(events.path):\n"
    " events.rotate(1,0)\n"
    ' save(ledger,\'{"gen":1,"cursor":0}\\n\',durable=True)\n'
    " os._exit(91)"
)


class EventsTest(EventsCase):
    def crashed(self, argv):
        result = subprocess.run(
            argv,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 91, result.stderr)

    def crash_replace(self, source, destination, *, after=False, recovering=False):
        self.crashed(
            (
                *PY,
                "-c",
                CRASH,
                self.directory,
                str(source),
                str(destination),
                "after" if after else "before",
                "recover" if recovering else "rotate",
            )
        )

    def test_crash_before_archive_rename_preserves_original_and_later_appends(self):
        self.crash_replace(self.events.path, self.events.archive)
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")
        self.assertFalse(Path(self.events.archive).exists())
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_after_archive_rename_preserves_new_file_arrivals(self):
        self.crash_replace(self.events.path, self.events.archive, after=True)
        self.assertFalse(Path(self.events.path).exists())
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_before_candidate_publish_preserves_unprefixed_new_file_arrivals(self):
        self.crash_replace(self.events.candidate, self.events.path)
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")
        self.assertFalse(Path(self.events.candidate).exists())
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_after_candidate_publish_strips_only_copied_tail(self):
        self.crash_replace(self.events.candidate, self.events.path, after=True)
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        append_line(self.events.path, b"later\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\nlater\n")
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_before_rollback_publish_rebuilds_with_later_appends(self):
        self.crash_replace(self.events.candidate, self.events.path, after=True)
        append_line(self.events.path, b"one\n", locked=True)
        self.crash_replace(self.events.rollback, self.events.path, recovering=True)
        append_line(self.events.path, b"two\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"one\ntwo\n")
        self.assertFalse(Path(self.events.rollback).exists())
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_after_rollback_publish_does_not_duplicate_old_or_new_lines(self):
        self.crash_replace(self.events.candidate, self.events.path, after=True)
        append_line(self.events.path, b"one\n", locked=True)
        self.crash_replace(self.events.rollback, self.events.path, after=True, recovering=True)
        append_line(self.events.path, b"two\n", locked=True)
        self.recover()
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"one\ntwo\n")
        self.assertFalse(Path(self.events.journal).exists())

    def test_crash_after_durable_ledger_commit_finishes_without_rollback(self):
        self.crashed((*PY, "-c", COMMIT, self.directory, str(self.ledger)))
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        self.assertEqual(self.generation(), 1)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nnew\n")
        self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
        self.assertFalse(Path(self.events.journal).exists())
