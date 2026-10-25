"""The board lock, the atomic issue-file replace and .next-id numbering."""

import fcntl
import os
import stat
from unittest import mock

from ensemble.orchestrator.engine.types import TrackerFileError
from ensemble.orchestrator.trackers.local import store
from tests.orchestrator.case import Case


class Store(Case):
    def setUp(self):
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.mkdir(self.board)
        self.lock = os.path.join(self.board, ".lock")
        self.next = os.path.join(self.board, ".next-id")

    def read(self, path):
        with open(path, encoding="utf-8") as file:
            return file.read()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as file:
            file.write(text)

    def test_locked_excludes(self):
        with store.locked(self.board):
            self.assertEqual(stat.S_IMODE(os.stat(self.lock).st_mode), 0o600)
            other = os.open(self.lock, os.O_RDWR)
            self.addCleanup(os.close, other)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)

    def test_locked_released_on_error(self):
        with self.assertRaises(RuntimeError), store.locked(self.board):
            raise RuntimeError
        other = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_locked_keeps_content_and_refuses_symlink(self):
        self.write(self.lock, "kept")
        with store.locked(self.board):
            pass
        self.assertEqual(self.read(self.lock), "kept")
        os.unlink(self.lock)
        target = os.path.join(self.home, "target")
        os.symlink(target, self.lock)
        with self.assertRaises(OSError), store.locked(self.board):
            pass
        self.assertFalse(os.path.exists(target))

    def test_replace(self):
        path = os.path.join(self.board, "LOC-1.md")
        self.write(path, "old")
        store.replace(path, "new ✓\n")
        self.assertEqual(self.read(path), "new ✓\n")
        self.assertEqual(os.listdir(self.board), ["LOC-1.md"])

    def test_replace_keeps_old_on_error(self):
        path = os.path.join(self.board, "LOC-1.md")
        self.write(path, "old")
        temps = []

        def fail(source, _target):
            temps.append(source)
            self.assertEqual(self.read(source), "new")
            raise OSError("disk full")

        with mock.patch("os.replace", fail), self.assertRaises(OSError):
            store.replace(path, "new")
        self.assertEqual(self.read(path), "old")
        self.assertEqual(os.listdir(self.board), ["LOC-1.md"])
        name = os.path.basename(temps[0])
        self.assertEqual(os.path.dirname(temps[0]), self.board)
        self.assertTrue(name.startswith(".") and name.endswith(".tmp"), name)

    def test_next_id(self):
        with store.locked(self.board):
            self.assertEqual(store.next_id(self.board, "LOC"), "LOC-1")
            self.assertEqual(self.read(self.next), "2\n")
            self.assertEqual(store.next_id(self.board, "LOC"), "LOC-2")
            self.write(self.next, "41")
            self.assertEqual(store.next_id(self.board, "AB2"), "AB2-41")
            self.assertEqual(self.read(self.next), "42\n")

    def test_next_id_refuses_bad_text_or_id(self):
        for text in ("", "x\n", "1\n\n", " 3", "-1", "١"):
            with self.subTest(text=text):
                self.write(self.next, text)
                with self.assertRaises(TrackerFileError) as caught, store.locked(self.board):
                    store.next_id(self.board, "LOC")
                self.assertEqual(
                    (caught.exception.path, caught.exception.reason),
                    (self.next, f"bad .next-id text {text!r}"),
                )
                self.assertEqual(self.read(self.next), text)
        self.write(self.next, "3\n")
        with self.assertRaises(TrackerFileError) as caught, store.locked(self.board):
            store.next_id(self.board, "loc")
        self.assertEqual(
            (caught.exception.path, caught.exception.reason), (self.next, "bad issue id 'loc-3'")
        )
        self.assertEqual(self.read(self.next), "3\n")
