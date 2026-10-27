"""utils/files.py: read_regular, read_beneath, open_beneath, append_line, holds, file_lock, save, drop_stale,
remove_tree and the events file checks (validate_events, events_file).
"""

import errno
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from ensemble.utils import ConfinedFileError, ConfinedReason, files, holds, read_confined
from tests.utils.case import Case

DATA = b"0123456789"


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "f")

    def content(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def promptly(self, call):
        """call()'s result or exception; fails instead of hanging when it blocks."""
        out = {}

        def run():
            try:
                out["value"] = call()
            except (OSError, TypeError, ValueError) as e:
                out["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            self.fail("blocked")
        if "error" in out:
            raise out["error"]
        return out["value"]


class ReadRegular(Base):
    def write(self, data=DATA, path=None):
        with open(path or self.path, "wb") as f:
            f.write(data)

    def test_a_regular_file_whole(self):
        self.write()
        self.assertEqual(files.read_regular(self.path), DATA)
        self.assertEqual(files.read_regular(Path(self.path)), DATA)
        self.write(b"")
        self.assertEqual(files.read_regular(self.path), b"")

    def test_reads_a_bounded_window(self):
        self.write()
        for options, want in (
            ({"limit": 4, "tail": False}, b"0123"),
            ({"limit": 4, "tail": True}, b"6789"),
            ({"limit": 0, "tail": False}, b""),
            ({"limit": 0, "tail": True}, b""),
            ({"limit": 20, "tail": False}, DATA),
            ({"limit": 20, "tail": True}, DATA),
            ({"limit": None, "tail": True}, DATA),
            ({"offset": 3, "limit": None, "tail": False}, b"3456789"),
            ({"offset": 3, "limit": 4, "tail": False}, b"3456"),
            ({"offset": 3, "limit": 4, "tail": True}, b"6789"),
            ({"offset": 8, "limit": 4, "tail": True}, b"89"),
            ({"offset": 10, "limit": None, "tail": False}, b""),
            ({"offset": 12, "limit": 4, "tail": False}, b""),
        ):
            with self.subTest(**options):
                self.assertEqual(files.read_regular(self.path, **options), want)

    def test_a_fifo_or_directory_is_none_without_blocking(self):
        os.mkfifo(self.path)
        self.assertIsNone(self.promptly(lambda: files.read_regular(self.path)))
        self.assertIsNone(files.read_regular(self.dir))

    def test_no_fd_is_left_open(self):
        os.mkdir(self.path)
        before = len(os.listdir("/dev/fd"))
        for _ in range(3):
            self.assertIsNone(files.read_regular(self.path))
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_missing_is_none(self):
        self.assertIsNone(files.read_regular(self.path))
        self.assertIsNone(files.read_regular(os.path.join(self.path, "under-a-missing-dir")))

    def test_an_os_error_while_reading_is_none(self):
        self.write()
        with unittest.mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertIsNone(files.read_regular(self.path))


class Beneath(Base):
    def setUp(self):
        super().setUp()
        self.sub = os.path.join(self.dir, "a", "b")
        os.makedirs(self.sub)
        self.file = os.path.join(self.sub, "f")
        with open(self.file, "wb") as f:
            f.write(DATA)


class ReadBeneath(Beneath):
    def test_reads_a_file_below_root_as_read_regular(self):
        self.assertEqual(files.read_beneath(self.dir, self.file), DATA)
        self.assertEqual(files.read_beneath(self.dir, self.file, offset=3, limit=4), b"3456")
        self.assertEqual(files.read_beneath(self.dir, self.file, limit=4, tail=True), b"6789")

    def test_a_link_anywhere_below_root_is_none(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "f"), "wb") as f:
            f.write(b"secret")
        root = os.path.join(self.dir, "a")
        os.symlink(os.path.join(outside, "f"), os.path.join(self.sub, "file-link"))
        os.symlink(outside, os.path.join(root, "dir-link"))
        for path in (os.path.join(self.sub, "file-link"), os.path.join(root, "dir-link", "f")):
            with self.subTest(path=path):
                self.assertIsNone(files.read_beneath(root, path))

    def test_outside_root_missing_fifo_or_directory_is_none_without_blocking(self):
        root = os.path.join(self.dir, "a")
        fifo = os.path.join(self.sub, "fifo")
        os.mkfifo(fifo)
        self.assertIsNone(self.promptly(lambda: files.read_beneath(root, fifo)))
        for path in (self.path, root, self.sub, os.path.join(self.sub, "missing"), f"{root}/../f"):
            with self.subTest(path=path):
                self.assertIsNone(files.read_beneath(root, path))
        self.assertIsNone(files.read_beneath(os.path.join(self.dir, "missing"), self.file))

    def test_no_fd_is_left_open(self):
        before = len(os.listdir("/dev/fd"))
        for path in (self.file, os.path.join(self.sub, "missing")):
            files.read_beneath(self.dir, path)
        self.assertEqual(len(os.listdir("/dev/fd")), before)


class OpenBeneath(Beneath):
    def test_opens_a_file_or_a_directory_below_root(self):
        fd = files.open_beneath(self.dir, self.file)
        try:
            self.assertEqual(os.read(fd, 16), DATA)
        finally:
            os.close(fd)
        fd = files.open_beneath(self.dir, self.sub, directory=True)
        try:
            self.assertEqual(os.listdir(fd), ["f"])
        finally:
            os.close(fd)

    def test_a_link_a_file_as_directory_or_a_path_outside_root_raises(self):
        root = os.path.join(self.dir, "a")
        os.symlink(self.file, os.path.join(self.sub, "file-link"))
        os.symlink(self.sub, os.path.join(root, "dir-link"))
        for path, directory in (
            (os.path.join(self.sub, "file-link"), False),
            (os.path.join(root, "dir-link", "f"), False),
            (os.path.join(root, "dir-link"), True),
            (self.file, True),
        ):
            with self.subTest(path=path, directory=directory), self.assertRaises(OSError):
                os.close(files.open_beneath(root, path, directory=directory))
        for path in (root, self.dir, f"{root}/../f"):
            with self.subTest(path=path), self.assertRaises(OSError) as raised:
                os.close(files.open_beneath(root, path))
            self.assertEqual(raised.exception.errno, errno.EXDEV)

    def test_no_fd_is_left_open_on_failure(self):
        before = len(os.listdir("/dev/fd"))
        for path in (os.path.join(self.sub, "missing"), os.path.join(self.dir, "x", "f")):
            with self.assertRaises(OSError):
                files.open_beneath(self.dir, path)
        self.assertEqual(len(os.listdir("/dev/fd")), before)


class AppendLine(Base):
    def test_default_append_creates_no_sidecar_lock(self):
        files.append_line(self.path, "one\n")
        self.assertFalse(Path(self.path + ".lock").exists())
        self.assertEqual(self.content(), b"one\n")

    def test_locked_append_opens_current_path_after_stable_lock_is_released(self):
        files.append_line(self.path, "old\n")
        waiting = threading.Event()
        finished = threading.Event()
        real_open = os.open

        def opened(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if (
                str(path) == self.path + ".lock"
                and threading.current_thread() is not threading.main_thread()
            ):
                waiting.set()
            return fd

        def append():
            files.append_line(self.path, "appended\n", locked=True)
            finished.set()

        with files.file_lock(self.path), mock.patch("os.open", side_effect=opened):
            thread = threading.Thread(target=append)
            thread.start()
            self.assertTrue(waiting.wait(5))
            self.assertFalse(finished.wait(0.05))
            files.save(self.path, "replacement\n")
            self.assertEqual(self.content(), b"replacement\n")
        thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.content(), b"replacement\nappended\n")

    def test_appends_after_what_is_there_and_an_empty_text_creates_the_file_owner_only(self):
        files.append_line(self.path, "")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        files.append_line(self.path, "one\n")
        files.append_line(self.path, "two\n")
        self.assertEqual(self.content(), b"one\ntwo\n")

    def test_a_line_is_written_without_failing_to_encode(self):
        for name, text, want in (
            ("bytes", b"raw\xff\n", b"raw\xff\n"),
            (
                "surrogate",
                json.dumps({"text": "a\ud800b"}, ensure_ascii=False) + "\n",
                b'{"text": "a?b"}\n',
            ),
        ):
            with self.subTest(text=text):
                path = os.path.join(self.dir, name)
                files.append_line(path, text)
                self.assertEqual(Path(path).read_bytes(), want)

    def test_a_short_write_continues_with_the_rest(self):
        real = os.write
        with unittest.mock.patch.object(
            os, "write", side_effect=lambda fd, data: real(fd, bytes(data[:3]))
        ) as write:
            files.append_line(self.path, "héllo wörld\n")
        self.assertEqual((write.call_count, self.content()), (5, "héllo wörld\n".encode()))

    def test_a_fifo_is_refused_without_blocking_with_or_without_a_reader(self):
        os.mkfifo(self.path)
        with self.assertRaises(OSError) as e:
            self.promptly(lambda: files.append_line(self.path, "x\n"))
        self.assertEqual(e.exception.errno, errno.ENXIO)
        reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        with self.assertRaises(OSError) as e:
            self.promptly(lambda: files.append_line(self.path, "x\n"))
        self.assertEqual(e.exception.errno, errno.EINVAL)


class Holds(Base):
    def test_only_a_regular_file_holding_exactly_the_text(self):
        self.assertFalse(holds(self.path, "x"))
        with open(self.path, "w") as f:
            f.write("héllo")
        self.assertTrue(holds(self.path, "héllo"))
        self.assertTrue(holds(Path(self.path), "héllo"))
        for text in ("héll", "héllo!", "", "other"):
            self.assertFalse(holds(self.path, text), text)

    def test_a_fifo_is_no_without_blocking(self):
        os.mkfifo(self.path)
        self.assertFalse(self.promptly(lambda: holds(self.path, "")))


class FileLock(Base):
    def test_lock_is_private_regular_and_non_inheritable(self):
        descriptors = []
        real_open = os.open

        def opened(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if str(path) == self.path + ".lock":
                descriptors.append(fd)
            return fd

        with mock.patch("os.open", side_effect=opened), files.file_lock(self.path):
            self.assertEqual(len(descriptors), 1)
            self.assertFalse(os.get_inheritable(descriptors[0]))
            self.assertEqual(stat.S_IMODE(os.stat(self.path + ".lock").st_mode), 0o600)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(Path(self.path).exists())

    def test_a_fifo_lock_is_refused(self):
        os.mkfifo(self.path + ".lock")
        with self.assertRaises(OSError), files.file_lock(self.path):
            self.fail("accepted FIFO lock")

    def test_no_wait_raises_while_held_and_locks_once_free(self):
        with (
            files.file_lock(self.path),
            self.assertRaises(BlockingIOError),
            files.file_lock(self.path, wait=False),
        ):
            self.fail("locked a held lock")
        with files.file_lock(self.path, wait=False):
            pass

    def test_shared_holders_share_it_and_hold_off_an_exclusive_one(self):
        with (
            files.file_lock(self.path, shared=True),
            files.file_lock(self.path, shared=True),
            self.assertRaises(BlockingIOError),
            files.file_lock(self.path, wait=False),
        ):
            self.fail("locked a shared lock exclusively")
        with files.file_lock(self.path, wait=False):
            pass


class DropStale(Base):
    def entry(self, name: str, age_days: float) -> str:
        path = os.path.join(self.dir, name)
        if not os.path.lexists(path):
            Path(path).touch()
        when = time.time() - age_days * 86400
        os.utime(path, (when, when), follow_symlinks=False)
        return path

    def test_only_regular_files_past_the_age_go(self):
        self.entry("old.json", 31)
        self.entry("young.json", 29)
        self.entry(".tmp-old", 31)
        self.assertEqual(files.drop_stale(self.dir), [])
        self.assertEqual(os.listdir(self.dir), ["young.json"])

    def test_kept_names_stay(self):
        self.entry("kept.json", 31)
        self.entry("old.json", 31)
        self.assertEqual(files.drop_stale(self.dir, keep={"kept.json"}), [])
        self.assertEqual(os.listdir(self.dir), ["kept.json"])

    def test_directories_and_symlinks_stay(self):
        os.mkdir(os.path.join(self.dir, "x.json"))
        self.entry("x.json", 31)
        target = self.entry("target", 1)
        os.symlink(target, os.path.join(self.dir, "link.json"))
        self.entry("link.json", 31)
        self.assertEqual(files.drop_stale(self.dir), [])
        self.assertEqual(sorted(os.listdir(self.dir)), ["link.json", "target", "x.json"])

    def test_an_unlink_error_is_returned_and_the_rest_still_go(self):
        self.entry("a.json", 31)
        self.entry("b.json", 31)
        real = os.unlink
        refused = OSError(errno.EPERM, "refused", os.path.join(self.dir, "a.json"))

        def unlink(path, *args, **kwargs):
            if os.path.basename(path) == "a.json":
                raise refused
            real(path, *args, **kwargs)

        with mock.patch("os.unlink", side_effect=unlink):
            self.assertEqual(files.drop_stale(self.dir), [refused])
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_an_entry_gone_before_its_unlink_is_no_error(self):
        self.entry("a.json", 31)
        gone = FileNotFoundError(errno.ENOENT, "gone", os.path.join(self.dir, "a.json"))
        with mock.patch("os.unlink", side_effect=gone):
            self.assertEqual(files.drop_stale(self.dir), [])

    def test_a_directory_it_cannot_list_is_one_error(self):
        (error,) = files.drop_stale(os.path.join(self.dir, "missing"))
        self.assertIsInstance(error, FileNotFoundError)


class InodeLock(Base):
    def held(self) -> bool:
        with files.inode_lock(self.path, wait=False) as got:
            return not got

    def test_a_lock_unlinked_while_waited_on_is_retaken_on_the_new_file(self):
        real_flock, calls = files.fcntl.flock, []

        def flock(fd, operation):
            if not calls:
                os.unlink(self.path)
                Path(self.path).touch()
            calls.append(fd)
            return real_flock(fd, operation)

        with mock.patch("fcntl.flock", side_effect=flock), files.inode_lock(self.path) as got:
            self.assertTrue(got)
            self.assertEqual(len(calls), 2)
            self.assertTrue(os.path.samestat(os.fstat(calls[1]), os.stat(self.path)))
        with files.inode_lock(self.path) as got:
            self.assertTrue(got)
            self.assertTrue(self.promptly(self.held))
        self.assertFalse(self.held())

    def test_a_holder_may_unlink_and_a_missing_path_is_retried(self):
        with files.inode_lock(self.path):
            os.unlink(self.path)
        real_flock, calls = files.fcntl.flock, []

        def flock(fd, operation):
            if not calls:
                os.unlink(self.path)
            calls.append(fd)
            return real_flock(fd, operation)

        with mock.patch("fcntl.flock", side_effect=flock), files.inode_lock(self.path):
            self.assertEqual(len(calls), 2)
            self.assertTrue(os.path.samestat(os.fstat(calls[1]), os.stat(self.path)))

    def test_without_waiting_a_held_lock_yields_false(self):
        with files.inode_lock(self.path, wait=False) as first:
            self.assertTrue(first)
            self.assertTrue(self.promptly(self.held))
        self.assertFalse(self.held())

    def test_a_non_regular_lock_path_is_refused(self):
        os.mkfifo(self.path)
        with self.assertRaises(OSError) as raised, files.inode_lock(self.path):
            self.fail("accepted FIFO lock")
        self.assertEqual(raised.exception.errno, errno.EINVAL)


class Idle(Base):
    def test_every_existing_path_older_than_the_age_and_missing_ones_ignored(self):
        old, new = os.path.join(self.dir, "old"), os.path.join(self.dir, "new")
        for path in (old, new):
            Path(path).touch()
        os.utime(old, (0, 0))
        os.symlink(old, self.path)
        os.utime(self.path, (0, 0), follow_symlinks=False)
        self.assertTrue(files.idle([old, self.path, os.path.join(self.dir, "gone")], 60))
        self.assertFalse(files.idle([old, new], 60))
        self.assertTrue(files.idle([], 60))


class Save(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "sub", "f.txt")

    def test_writes_replaces_and_makes_the_directory(self):
        files.save(self.path, "one\n")
        files.save(self.path, "two\n")
        with open(self.path) as f:
            self.assertEqual(f.read(), "two\n")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["f.txt"])

    def test_binary_data_is_preserved_during_atomic_durable_save(self):
        files.save(self.path, b"raw\xff\x00\n", durable=True)
        self.assertEqual(Path(self.path).read_bytes(), b"raw\xff\x00\n")

    def test_a_planted_symlink_is_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.makedirs(os.path.dirname(self.path))
        os.symlink(outside, self.path)
        files.save(self.path, "new\n")
        with open(self.path) as f, open(outside) as g:
            self.assertEqual(
                (os.path.islink(self.path), f.read(), g.read()), (False, "new\n", "keep\n")
            )

    def test_durable_syncs_temp_before_replace_and_directory_after(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace

        def synced(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_sync(fd)

        def replaced(source, destination):
            events.append("replace")
            real_replace(source, destination)

        with (
            mock.patch.object(os, "fsync", side_effect=synced),
            mock.patch.object(os, "replace", side_effect=replaced),
        ):
            files.save(self.path, "durable", durable=True)
        self.assertEqual(events, ["file", "replace", "directory"])
        self.assertEqual(Path(self.path).read_text(), "durable")

    def test_no_temp_is_left_when_the_replace_fails(self):
        files.save(self.path, "old\n")
        with (
            unittest.mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            files.save(self.path, "new\n")
        with open(self.path) as f:
            self.assertEqual(
                (f.read(), os.listdir(os.path.dirname(self.path))), ("old\n", ["f.txt"])
            )


class RemoveTree(Base):
    """A Go module cache's shape: read-only directories holding read-only files."""

    def setUp(self):
        super().setUp()
        self.tree = os.path.join(self.dir, "tree")
        self.outside = os.path.join(self.dir, "outside")

    def read_only(self, *paths):
        for path in paths:
            os.chmod(path, 0o555 if os.path.isdir(path) else 0o444)

    def test_a_read_only_file_in_a_read_only_directory_is_removed(self):
        os.makedirs(self.tree)
        Path(self.tree, "go.mod").write_text("module x\n")
        self.read_only(os.path.join(self.tree, "go.mod"), self.tree)
        files.remove_tree(self.tree)
        self.assertFalse(os.path.lexists(self.tree))

    def test_nested_read_only_directories_are_removed(self):
        module = os.path.join(self.tree, "mod", "github.com", "x@v1.0.12")
        os.makedirs(os.path.join(module, "sub"))
        for name in ("go.mod", "sub/a.go"):
            Path(module, name).write_text("x")
        self.read_only(
            os.path.join(module, "go.mod"),
            os.path.join(module, "sub", "a.go"),
            os.path.join(module, "sub"),
            module,
            os.path.dirname(module),
        )
        with mock.patch.object(files.shutil, "rmtree", wraps=shutil.rmtree) as rmtree:
            files.remove_tree(self.tree)
        self.assertEqual((os.path.lexists(self.tree), rmtree.call_count), (False, 2))

    def test_links_inside_are_removed_never_followed(self):
        os.makedirs(os.path.join(self.outside, "d"))
        for name in ("f", "d/f"):
            Path(self.outside, name).write_text("kept")
        targets = (os.path.join(self.outside, "d", "f"), os.path.join(self.outside, "f"))
        self.read_only(*targets, os.path.join(self.outside, "d"))
        inner = os.path.join(self.tree, "inner")
        os.makedirs(inner)
        os.symlink(os.path.join(self.outside, "d"), os.path.join(inner, "dir-link"))
        os.symlink(targets[1], os.path.join(inner, "file-link"))
        self.read_only(inner)
        outside = (*targets, os.path.join(self.outside, "d"), self.outside)
        modes = [os.stat(path).st_mode for path in outside]
        files.remove_tree(self.tree)
        self.assertFalse(os.path.lexists(self.tree))
        self.assertEqual([os.stat(path).st_mode for path in outside], modes)
        self.assertEqual([Path(path).read_text() for path in targets], ["kept", "kept"])

    def test_a_link_as_the_tree_is_refused_and_its_target_kept(self):
        os.makedirs(self.outside)
        os.symlink(self.outside, self.tree)
        with self.assertRaises(OSError):
            files.remove_tree(self.tree)
        self.assertTrue(os.path.isdir(self.outside))

    def test_a_tree_relative_to_dir_fd_is_removed(self):
        os.makedirs(os.path.join(self.tree, "ro"))
        Path(self.tree, "ro", "f").write_text("x")
        self.read_only(os.path.join(self.tree, "ro"), self.tree)
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        files.remove_tree("tree", dir_fd=fd)
        self.assertEqual(os.listdir(self.dir), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_an_unreadable_directory_is_skipped_and_the_retry_raises(self):
        unreadable = os.path.join(self.tree, "unreadable")
        os.makedirs(unreadable)
        Path(unreadable, "f").write_text("x")
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, stat.S_IRWXU)
        with self.assertRaises(PermissionError):
            files.remove_tree(self.tree)
        self.assertEqual(stat.S_IMODE(os.stat(unreadable).st_mode), 0)

    def test_a_refusal_after_the_retry_is_raised(self):
        os.makedirs(self.tree)
        with (
            mock.patch.object(
                files.shutil, "rmtree", side_effect=PermissionError("denied")
            ) as rmtree,
            self.assertRaisesRegex(PermissionError, "denied"),
        ):
            files.remove_tree(self.tree)
        self.assertEqual(rmtree.call_count, 2)

    def test_other_errors_are_raised_with_no_retry(self):
        with (
            mock.patch.object(files.shutil, "rmtree", wraps=shutil.rmtree) as rmtree,
            self.assertRaises(FileNotFoundError),
        ):
            files.remove_tree(self.tree)
        self.assertEqual(rmtree.call_count, 1)


class ConfinedRead(Case):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "result.md"
        self.path.write_bytes(b"complete")

    def refused(self, reason, path=None):
        with self.assertRaises(ConfinedFileError) as error:
            read_confined(str(path or self.path), str(self.root), limit=8)
        self.assertIs(error.exception.reason, reason)
        return error.exception

    def test_reasons_are_a_str_enum_of_their_values(self):
        values = ["regular", "suffix", "outside", "too_large"]
        self.assertEqual([r.value for r in ConfinedReason], values)
        self.assertEqual(list(ConfinedReason), values)

    def test_exact_limit_and_empty_file(self):
        self.assertEqual(read_confined(str(self.path), str(self.root), limit=8), b"complete")
        self.path.write_bytes(b"")
        self.assertEqual(read_confined(str(self.path), str(self.root), limit=0), b"")

    def test_over_limit_reports_the_cap(self):
        self.path.write_bytes(b"too large")
        self.assertEqual(self.refused(ConfinedReason.TOO_LARGE).limit, 8)

    def test_a_missing_file_reports_a_filesystem_failure(self):
        with self.assertRaises(OSError):
            read_confined(str(self.root / "absent.md"), str(self.root), limit=8)

    def test_directory_and_fifo_are_refused(self):
        fifo = self.root / "fifo.md"
        os.mkfifo(fifo)
        for path in (self.root, fifo):
            with self.subTest(path=path):
                self.refused(ConfinedReason.REGULAR, path)

    def test_descriptor_suffix_is_checked(self):
        other = self.root / "result.txt"
        self.path.rename(other)
        self.refused(ConfinedReason.SUFFIX, other)
        self.assertEqual(
            read_confined(str(other), str(self.root), limit=8, suffix=".txt"), b"complete"
        )

    def test_outside_root_is_refused(self):
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(ConfinedFileError) as error:
            read_confined(str(self.path), str(child), limit=8)
        self.assertIs(error.exception.reason, ConfinedReason.OUTSIDE)

    def test_ubuntu_descriptor_path_uses_proc(self):
        with (
            mock.patch.object(os, "readlink", return_value=str(self.path)) as readlink,
            mock.patch("sys.platform", "linux"),
        ):
            self.assertEqual(read_confined(str(self.path), str(self.root), limit=8), b"complete")
        self.assertEqual(readlink.call_count, 1)
        self.assertTrue(
            all(call.args[0].startswith("/proc/self/fd/") for call in readlink.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()


class EventsFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_created_0600_and_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        path = os.path.join(self.root, "e")
        self.assertEqual(files.events_file("e"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "a") as f:
            f.write("x\n")
        self.assertEqual(files.events_file(path), path)
        with open(path) as f:
            self.assertEqual(f.read(), "x\n")

    def test_refused(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        for path in (self.root, os.path.join(self.root, "missing", "e")):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, f"^events file {re.escape(path)}: "),
            ):
                files.events_file(path)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600)
        with (
            unittest.mock.patch("os.fstat", return_value=fifo),
            self.assertRaisesRegex(ValueError, "not a regular file$"),
        ):
            files.events_file(target)

    def test_a_fifo_without_a_reader_is_refused_at_once(self):
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo, 0o600)
        caught = []

        def run():
            for check in (files.validate_events, files.events_file):
                try:
                    check(fifo)
                except ValueError as e:
                    caught.append(str(e))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # lets the blocked open return
            thread.join()
            os.close(reader)
            self.fail("blocked on a FIFO without a reader")
        self.assertEqual(caught, [f"events file {fifo}: not a regular file"] * 2)

    def test_a_parent_that_is_a_file_is_refused_with_nothing_made(self):
        parent = os.path.join(self.root, "file")
        open(parent, "w").close()
        path = os.path.join(parent, "e")
        for check in (files.validate_events, files.events_file):
            with self.subTest(check=check.__name__):
                with self.assertRaises(ValueError) as caught:
                    check(path)
                self.assertEqual(str(caught.exception), f"events file {path}: Not a directory")
        self.assertEqual(os.listdir(self.root), ["file"])
        self.assertEqual(os.path.getsize(parent), 0)

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    ValueError, f"^events file {re.escape(path)}: tmux would misread"
                ),
            ):
                files.events_file(path)
            self.assertFalse(os.path.exists(path))
