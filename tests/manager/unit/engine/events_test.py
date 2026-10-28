"""Event rotation transactions, filesystem validation and append ordering."""

import errno
import os
import stat
from pathlib import Path
from unittest import mock

from ensemble.utils import append_line, file_lock, save
from tests.manager.events_case import EventsCase


class EventsTest(EventsCase):
    def test_identity_returns_native_owned_file_identity_and_none_when_missing(self):
        opened = os.stat(self.events.path, follow_symlinks=False)
        self.assertEqual(self.events.identity(), (opened.st_dev, opened.st_ino))
        Path(self.events.path).unlink()
        self.assertIsNone(self.events.identity())

    def test_identity_refuses_fifo_and_directory(self):
        Path(self.events.path).unlink()
        for kind in ("fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(self.events.path)
                else:
                    os.mkdir(self.events.path)
                with self.assertRaises(OSError):
                    self.events.identity()
                if kind == "directory":
                    os.rmdir(self.events.path)
                else:
                    Path(self.events.path).unlink()

    def test_committed_rotation_keeps_unread_order_and_cleans_intent(self):
        with file_lock(self.events.path):
            self.assertTrue(self.events.rotate(1, 0))
            self.assertTrue(Path(self.events.journal).is_file())
            self.assertEqual(Path(self.events.path).read_bytes(), b"old\n")
            save(self.ledger, '{"gen":1,"cursor":0}\n', durable=True)
            self.events.recover(1)
        self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
        self.assertFalse(Path(self.events.journal).exists())
        self.assertFalse(Path(self.events.candidate).exists())

    def test_uncommitted_rotation_rolls_back_before_another_rotation(self):
        with file_lock(self.events.path):
            self.assertTrue(self.events.rotate(1, 0))
        append_line(self.events.path, b"new\n", locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")
        self.assertEqual(self.generation(), 0)
        self.assertFalse(Path(self.events.journal).exists())
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")

    def test_refill_failure_propagates_and_old_generation_recovers_all_lines(self):
        real_replace = os.replace

        def replace(source, destination):
            if str(destination) == self.events.candidate:
                raise OSError(errno.ENOSPC, "disk full")
            return real_replace(source, destination)

        with file_lock(self.events.path):
            with mock.patch("os.replace", side_effect=replace), self.assertRaises(OSError):
                self.events.rotate(1, 0)
            self.assertEqual(self.generation(), 0)
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            self.events.recover(0)
        self.assertEqual(Path(self.events.path).read_bytes(), self.original)
        self.assertFalse(Path(self.events.journal).exists())

    def test_rotation_reprivatizes_fresh_0644_events_and_preserves_both_tails(self):
        settled = []

        def create_fresh(_seconds):
            if settled:  # only the settle before the candidate
                return
            settled.append(_seconds)
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            Path(self.events.path).unlink()
            Path(self.events.path).write_bytes(b"fresh\n")
            Path(self.events.path).chmod(0o644)
            self.assertEqual(stat.S_IMODE(os.stat(self.events.path).st_mode), 0o644)

        with file_lock(self.events.path), mock.patch("time.sleep", create_fresh):
            self.assertTrue(self.events.rotate(1, 0))
            save(self.ledger, '{"gen":1,"cursor":0}\n', durable=True)
            self.events.recover(1)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nfresh\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.events.path).st_mode), 0o600)
        self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
        self.assertEqual(self.generation(), 1)
        self.assertFalse(Path(self.events.journal).exists())

    def test_a_fifo_events_during_settlement_refuses_without_losing_unread_archive(self):
        ledger = self.ledger.read_bytes()

        def plant(_seconds):
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            Path(self.events.path).unlink()
            os.mkfifo(self.events.path)

        with file_lock(self.events.path):
            with mock.patch("time.sleep", plant), self.assertRaises(OSError):
                self.events.rotate(1, 0)
            self.assertEqual(self.ledger.read_bytes(), ledger)
            self.assertEqual(self.generation(), 0)
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            self.assertTrue(stat.S_ISFIFO(os.stat(self.events.path).st_mode))
            self.assertTrue(Path(self.events.journal).exists())
            self.assertFalse(Path(self.events.candidate).exists())
            Path(self.events.path).unlink()
            self.events.recover(0)
        self.assertEqual(Path(self.events.path).read_bytes(), self.original)

    def test_legacy_settlement_prepends_old_tail_before_fresh_arrivals(self):
        fd = os.open(self.events.path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        settled = []

        def append(_seconds):
            if settled:  # only the settle before the candidate
                return
            settled.append(_seconds)
            os.write(fd, b"inflight\n")
            append_line(self.events.path, b"fresh\n")

        with file_lock(self.events.path), mock.patch("time.sleep", append):
            self.assertTrue(self.events.rotate(1, 0))
            self.events.recover(1)
        self.assertTrue(settled)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\ninflight\nfresh\n")

    def test_a_line_appended_while_the_candidate_publishes_is_kept(self):
        real_replace = os.replace

        def replace(source, destination):
            if str(source) == self.events.candidate and str(destination) == self.events.path:
                append_line(self.events.path, b"late\n")
            real_replace(source, destination)

        with file_lock(self.events.path), mock.patch("os.replace", side_effect=replace):
            self.assertTrue(self.events.rotate(1, 0))
            self.events.recover(1)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nlate\n")

    def publishing(self, after):
        """os.replace that, publishing the candidate, first opens a writer on the fresh file it
        replaces, then runs after(writer).
        """
        real_replace, writers = os.replace, []

        def replace(source, destination):
            if str(source) == self.events.candidate and str(destination) == self.events.path:
                writers.append(os.open(self.events.path, os.O_WRONLY | os.O_APPEND))
                self.addCleanup(os.close, writers[0])
                real_replace(source, destination)
                after(writers[0])
                return
            real_replace(source, destination)

        return mock.patch("os.replace", side_effect=replace), writers

    def test_a_writer_opened_before_the_publish_writing_while_it_settles_is_kept(self):
        sleeps = []
        patched, writers = self.publishing(lambda _fd: None)

        def settle(_seconds):
            sleeps.append(_seconds)
            if writers and len(sleeps) == 2:
                os.write(writers[0], b"settled\n")

        with file_lock(self.events.path), patched, mock.patch("time.sleep", settle):
            self.assertTrue(self.events.rotate(1, 0))
            self.events.recover(1)
        if len(sleeps) < 2:
            os.write(writers[0], b"settled\n")
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nsettled\n")

    def test_a_failed_late_append_keeps_its_lines_in_the_archive_and_says_so(self):
        real_open = os.open
        patched, writers = self.publishing(lambda fd: os.write(fd, b"late\n"))

        def refuse(path, flags, *args):
            if writers and str(path) == self.events.path and flags & os.O_APPEND:
                raise OSError(errno.EIO, "append failed")
            return real_open(path, flags, *args)

        with (
            file_lock(self.events.path),
            patched,
            mock.patch("time.sleep"),
            mock.patch("os.open", side_effect=refuse),
            self.assertRaises(OSError) as caught,
        ):
            self.events.rotate(1, 0)
        self.assertIn(f"retained in {self.events.archive}", str(caught.exception))
        self.assertTrue(Path(self.events.archive).read_bytes().endswith(b"\nlate\n"))

    def test_late_lines_neither_append_takes_are_reported_lost(self):
        real_open = os.open
        patched, writers = self.publishing(lambda fd: os.write(fd, b"late\n"))
        refused = {self.events.path, self.events.archive}

        def refuse(path, flags, *args):
            if writers and str(path) in refused and flags & os.O_APPEND:
                raise OSError(errno.EIO, "append failed")
            return real_open(path, flags, *args)

        with (
            file_lock(self.events.path),
            patched,
            mock.patch("time.sleep"),
            mock.patch("os.open", side_effect=refuse),
            self.assertRaises(OSError) as caught,
        ):
            self.events.rotate(1, 0)
        self.assertIn("5 bytes written while rotating lost", str(caught.exception))
        self.assertNotIn("retained in", str(caught.exception))

    def test_rollback_keeps_fresh_bytes_already_in_prepared_candidate(self):
        real_replace = os.replace
        fd = os.open(self.events.path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        appended = []

        def replace(source, destination):
            real_replace(source, destination)
            if str(destination) == self.events.archive:
                os.write(fd, b"inflight\n")
                append_line(self.events.path, b"captured\n")
                appended.append(destination)
            elif str(source) == self.events.candidate and str(destination) == self.events.path:
                raise OSError(errno.EIO, "publish failed after rename")

        with (
            file_lock(self.events.path),
            mock.patch("os.replace", side_effect=replace),
            self.assertRaises(OSError),
        ):
            self.events.rotate(1, 0)
        self.assertTrue(appended)
        append_line(self.events.path, b"after\n", locked=True)
        self.recover()
        self.assertEqual(
            Path(self.events.path).read_bytes(),
            self.original + b"inflight\ncaptured\nafter\n",
        )

    def test_invalid_generation_and_changed_candidate_prefix_refuse_recovery(self):
        with file_lock(self.events.path):
            self.assertTrue(self.events.rotate(1, 0))
            with self.assertRaises(OSError):
                self.events.recover(2)
            with open(self.events.path, "r+b") as current:
                current.write(b"bad")
            with self.assertRaises(OSError):
                self.events.recover(1)
        self.assertTrue(Path(self.events.journal).exists())

    def test_a_fifo_journal_is_refused_without_waiting(self):
        os.mkfifo(self.events.journal)
        with file_lock(self.events.path), self.assertRaises(OSError):
            self.events.recover(0)
