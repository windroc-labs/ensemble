"""utils/replies.py: save_reply and read_reply, the reply store beside an events file."""

import os
import time
from unittest import mock

from ensemble.utils import REPLY_MAX, read_reply, replies, save_reply
from tests.utils.case import Case
from tests.utils.sandbox import scratch

ID = "0123456789abcdef0123456789abcdef"


class RepliesTest(Case):
    def setUp(self):
        self.events = os.path.join(scratch(self, real=False), "events")
        self.store = self.events + ".replies"

    def test_save_then_read(self):
        self.assertTrue(save_reply(self.events, ID, "the answer\nline two"))
        self.assertEqual(read_reply(self.events, ID), "the answer\nline two")
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.store, ID)).st_mode & 0o777, 0o600)

    def test_second_save_of_an_id_claims_nothing(self):
        self.assertTrue(save_reply(self.events, ID, "first"))
        self.assertFalse(save_reply(self.events, ID, "second"))
        self.assertEqual(read_reply(self.events, ID), "first")
        self.assertEqual(os.listdir(self.store), [ID])

    def test_bad_id_refused(self):
        for bad in ("x", ID[:-1], ID.upper(), ID + "/../x", "../" + ID, ID + "\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    save_reply(self.events, bad, "text")
                with self.assertRaises(ValueError):
                    read_reply(self.events, bad)
        self.assertFalse(os.path.lexists(self.store))

    def test_text_cut_at_utf8_boundary(self):
        text = "a" + "é" * REPLY_MAX
        self.assertTrue(save_reply(self.events, ID, text))
        stored = read_reply(self.events, ID) or ""
        self.assertLessEqual(len(stored.encode()), REPLY_MAX)
        self.assertTrue(text.startswith(stored))
        self.assertEqual(len(stored.encode()), REPLY_MAX - 1)

    def test_read_of_missing_is_none(self):
        self.assertIsNone(read_reply(self.events, ID))
        os.mkdir(self.store, 0o700)
        self.assertIsNone(read_reply(self.events, ID))

    def test_symlinked_entry_is_not_read(self):
        os.mkdir(self.store, 0o700)
        target = os.path.join(os.path.dirname(self.events), "secret")
        with open(target, "w", encoding="utf-8") as f:
            f.write("secret")
        os.symlink(target, os.path.join(self.store, ID))
        self.assertIsNone(read_reply(self.events, ID))

    def test_symlinked_store_refused(self):
        elsewhere = os.path.join(os.path.dirname(self.events), "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, self.store)
        with self.assertRaises(OSError):
            save_reply(self.events, ID, "text")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_store_that_is_a_file_refused(self):
        with open(self.store, "w", encoding="utf-8") as f:
            f.write("")
        with self.assertRaises(OSError):
            save_reply(self.events, ID, "text")

    def test_store_of_another_owner_refused(self):
        os.mkdir(self.store, 0o700)
        with (
            mock.patch.object(replies.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(OSError),
        ):
            save_reply(self.events, ID, "text")
        self.assertEqual(os.listdir(self.store), [])

    def test_stale_entries_pruned(self):
        os.mkdir(self.store, 0o700)
        old = os.path.join(self.store, "f" * 32)
        with open(old, "w", encoding="utf-8") as f:
            f.write("old")
        then = time.time() - 31 * 24 * 60 * 60
        os.utime(old, (then, then))
        self.assertTrue(save_reply(self.events, ID, "new"))
        self.assertEqual(os.listdir(self.store), [ID])
