"""utils/beneath.py: write_beneath."""

import errno
import os
import stat
from unittest import mock

from ensemble.utils import write_beneath
from tests.utils.case import Case
from tests.utils.sandbox import scratch


class WriteBeneath(Case):
    def setUp(self):
        self.dir = scratch(self, real=False)
        self.sub = os.path.join(self.dir, "a", "b")
        os.makedirs(self.sub)
        self.file = os.path.join(self.sub, "f")
        self.path = os.path.join(self.dir, "f")
        with open(self.file, "wb") as f:
            f.write(b"0123456789")

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_creates_missing_parents_private_and_the_file_owner_only(self):
        path = os.path.join(self.dir, "a", "new", "deeper", "doc.md")
        write_beneath(self.dir, path, "text ✓")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "text ✓")
        self.assertEqual(self.mode(path), 0o600)
        for made in (os.path.join(self.dir, "a", "new"), os.path.dirname(path)):
            self.assertEqual(self.mode(made), 0o700)

    def test_replaces_an_existing_file_whole(self):
        write_beneath(self.dir, self.file, "new")
        write_beneath(self.dir, self.file, "x")
        with open(self.file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "x")
        self.assertEqual(os.listdir(self.sub), ["f"])

    def test_a_hard_link_at_the_leaf_is_replaced_not_written_through(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "wb") as f:
            f.write(b"secret")
        os.unlink(self.file)
        os.link(outside, self.file)
        write_beneath(os.path.join(self.dir, "a"), self.file, "doc")
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"secret")
        with open(self.file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "doc")

    def test_a_symlink_at_the_leaf_is_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "wb") as f:
            f.write(b"secret")
        leaf = os.path.join(self.sub, "link")
        os.symlink(outside, leaf)
        write_beneath(os.path.join(self.dir, "a"), leaf, "doc")
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"secret")
        self.assertFalse(os.path.islink(leaf))

    def test_a_linked_directory_below_root_raises_and_writes_nothing(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        root = os.path.join(self.dir, "a")
        os.symlink(outside, os.path.join(root, "dir-link"))
        with self.assertRaises(OSError):
            write_beneath(root, os.path.join(root, "dir-link", "doc.md"), "doc")
        self.assertEqual(os.listdir(outside), [])

    def test_a_path_outside_root_raises(self):
        root = os.path.join(self.dir, "a")
        for path in (root, self.path, f"{root}/../f"):
            with self.subTest(path=path), self.assertRaises(OSError) as raised:
                write_beneath(root, path, "doc")
            self.assertEqual(raised.exception.errno, errno.EXDEV)

    def test_a_failed_write_leaves_no_temp_file_or_fd(self):
        before = len(os.listdir("/dev/fd"))
        with (
            mock.patch.object(os, "replace", side_effect=OSError(errno.EIO, "I/O error")),
            self.assertRaises(OSError),
        ):
            write_beneath(self.dir, self.file, "doc")
        self.assertEqual(os.listdir(self.sub), ["f"])
        self.assertEqual(len(os.listdir("/dev/fd")), before)
