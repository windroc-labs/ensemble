"""The local board's directory walk: which entries are issue files, and each layout error."""

import os
import shutil
from datetime import datetime
from unittest import mock

from ensemble.orchestrator.engine.types import TrackerFileError
from ensemble.orchestrator.trackers.local import board
from ensemble.orchestrator.trackers.local.types import LocalState
from tests.orchestrator.case import Case


def text(ident, history=""):
    return f"---\nid: {ident}\ntitle: t\ncreated: 2026-10-19T14:02:00Z\n---\n{history}"


class Board(Case):
    def setUp(self):
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.mkdir(self.board)

    def path(self, rel):
        return os.path.join(self.board, rel)

    def write(self, rel, content):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
        return path

    def refuses(self, path, reason):
        with self.assertRaises(TrackerFileError) as caught:
            board.scan(self.board)
        self.assertEqual((caught.exception.path, caught.exception.reason), (path, reason))


class Scan(Board):
    def test_layout(self):
        self.write("pm/ready/LOC-1 a.md", text("LOC-1"))
        self.write("engineer/running/LOC-2.md", text("LOC-2"))
        self.write("me/backlog/LOC-3.md", text("LOC-3"))
        self.write("unassigned/ready/LOC-4.md", text("LOC-4"))
        done = "### History\n- 2026-10-19 14:02 done self\n"
        self.write("archive/LOC-5 x.md", text("LOC-5", done))
        self.write("archive/LOC-6.md", text("LOC-6"))
        got = board.scan(self.board)
        self.assertEqual(
            [(f.id, f.path, f.assignee, f.state) for f in got],
            [
                ("LOC-5", self.path("archive/LOC-5 x.md"), None, LocalState.DONE),
                ("LOC-6", self.path("archive/LOC-6.md"), None, None),
                ("LOC-2", self.path("engineer/running/LOC-2.md"), "engineer", LocalState.RUNNING),
                ("LOC-3", self.path("me/backlog/LOC-3.md"), "me", LocalState.BACKLOG),
                ("LOC-1", self.path("pm/ready/LOC-1 a.md"), "pm", LocalState.READY),
                ("LOC-4", self.path("unassigned/ready/LOC-4.md"), "unassigned", LocalState.READY),
            ],
        )
        os.utime(self.path("pm/ready/LOC-1 a.md"), (1_800_000_000, 1_800_000_000))
        mtime = next(f.mtime for f in board.scan(self.board) if f.id == "LOC-1")
        self.assertEqual(mtime, datetime.fromtimestamp(1_800_000_000).astimezone())
        self.assertIsNotNone(mtime.tzinfo)

    def test_ignores_dot_entries_and_other_files(self):
        self.write("pm/ready/LOC-1.md", text("LOC-1"))
        for rel in (
            ".state/logs/runs.jsonl",
            ".state/x/ready/bad.md",
            ".next-id",
            ".lock",
            "README.txt",
            ".hidden/ready/bad.md",
            "pm/.DS_Store",
            "pm/.drafts/bad.md",
            "pm/ready/.LOC-9.md",
            "pm/ready/.x.tmp",
            "pm/ready/notes.txt",
            "archive/.bad.md",
            "archive/notes.txt",
        ):
            self.write(rel, "not an issue")
        os.mkdir(self.path("pm/ready/drafts"))
        self.assertEqual([f.id for f in board.scan(self.board)], ["LOC-1"])

    def test_not_a_regular_file(self):
        target = self.write("elsewhere.txt", text("LOC-1"))
        link = self.path("pm/ready/LOC-1.md")
        os.makedirs(os.path.dirname(link))
        os.symlink(target, link)
        self.refuses(link, "not a regular file")
        os.unlink(link)
        os.mkfifo(link)
        self.refuses(link, "not a regular file")
        os.unlink(link)
        os.mkdir(link)
        self.refuses(link, "not a regular file")
        os.rmdir(link)
        os.mkdir(self.path("archive"))
        os.symlink(target, self.path("archive/LOC-1.md"))
        self.refuses(self.path("archive/LOC-1.md"), "not a regular file")

    def test_symlinked_dirs(self):
        self.write("pm/ready/LOC-1.md", text("LOC-1"))
        os.mkdir(self.path("../elsewhere"))
        for rel in ("qa", "pm/review", "archive"):
            with self.subTest(rel=rel):
                os.symlink(self.path("../elsewhere"), self.path(rel))
                self.refuses(self.path(rel), "not a directory")
                os.unlink(self.path(rel))

    def test_unknown_state_dir(self):
        self.write("pm/todo/LOC-1.md", text("LOC-1"))
        self.refuses(self.path("pm/todo"), "unknown state dir 'todo'")

    def test_file_outside_state_dir(self):
        path = self.write("pm/LOC-1.md", text("LOC-1"))
        self.refuses(path, "file outside a state dir")
        os.unlink(path)
        self.refuses(self.write("LOC-2.md", text("LOC-2")), "file outside a state dir")

    def test_archive_subdir(self):
        self.write("pm/ready/LOC-1.md", text("LOC-1"))
        self.write("archive/pm/done/LOC-2.md", text("LOC-2"))
        self.refuses(self.path("archive/pm"), "sub-directory in archive")

    def test_duplicate_id(self):
        self.write("pm/ready/LOC-1 a.md", text("LOC-1"))
        second = self.write("pm/ready/LOC-1 b.md", text("LOC-1"))
        self.refuses(second, f"duplicate id LOC-1, also in {self.path('pm/ready/LOC-1 a.md')}")

    def test_parse_error_names_the_file(self):
        path = self.write("pm/ready/LOC-1.md", "id: LOC-1\n")
        self.refuses(path, "no front matter")

    def test_not_utf8(self):
        path = self.write("pm/ready/LOC-1.md", "")
        with open(path, "wb") as file:
            file.write(b"---\nid: LOC-1\xff\n")
        self.refuses(path, "not UTF-8 text")

    def test_missing_board(self):
        shutil.rmtree(self.board)
        self.refuses(self.board, "no such board directory")

    def test_unreadable_dir(self):
        self.write("pm/ready/LOC-1.md", text("LOC-1"))
        scandir, ready = os.scandir, os.path.join(self.board, "pm", "ready")

        def denied(path):
            if path == ready:
                raise PermissionError(13, "Permission denied", path)
            return scandir(path)

        with mock.patch("os.scandir", denied):
            self.refuses(ready, "Permission denied")

    def test_empty_board(self):
        self.write(".state/logs/orchestrator.jsonl", "")
        self.write("archive/LOC-1.md", text("LOC-1"))
        self.refuses(self.board, "empty board: no assignee directory")

    def test_empty_assignee_dir_is_a_board(self):
        os.mkdir(self.path("pm"))
        self.assertEqual(board.scan(self.board), ())


class ReadOne(Board):
    def test_reads_the_path(self):
        path = self.write("pm/ready/LOC-1.md", text("LOC-1"))
        self.write("pm/ready/LOC-2.md", "broken")
        got = board.read_one(self.board, path, "LOC-1")
        self.assertEqual((got.id, got.path, got.state), ("LOC-1", path, LocalState.READY))

    def test_moved_file_found_by_scan(self):
        old = self.path("pm/ready/LOC-1.md")
        new = self.write("pm/review/LOC-1.md", text("LOC-1"))
        self.write("pm/ready/LOC-2.md", text("LOC-2"))
        got = board.read_one(self.board, old, "LOC-1")
        self.assertEqual((got.path, got.assignee, got.state), (new, "pm", LocalState.REVIEW))
        other = board.read_one(self.board, self.path("pm/ready/LOC-2.md"), "LOC-1")
        self.assertEqual(other.path, new)

    def test_archived(self):
        path = self.write("archive/LOC-1.md", text("LOC-1"))
        os.mkdir(self.path("pm"))
        got = board.read_one(self.board, path, "LOC-1")
        self.assertEqual((got.path, got.assignee, got.state), (path, None, None))

    def test_absent(self):
        self.write("pm/ready/LOC-2.md", text("LOC-2"))
        self.assertIsNone(board.read_one(self.board, self.path("pm/ready/LOC-1.md"), "LOC-1"))
