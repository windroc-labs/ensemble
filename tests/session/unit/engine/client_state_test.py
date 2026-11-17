"""session/engine/client_state.py: locate's lexical mapping, and read, stat, entries and snapshot
refusing links, names and paths outside a client home, absent paths and size limits.
"""

import errno
import os
import sqlite3
import stat as stat_module
from unittest import mock

from ensemble.session.engine import client_state
from ensemble.session.engine.types import StateEntry, StateFile, StateKind, StateStat
from tests.session.case import Case

HOME_DIR = "app"
OPS = ("read", "stat", "entries", "snapshot")


class StateCase(Case):
    def setUp(self):
        self.state = os.path.join(self.ensemble, "docker-state")
        self.client = os.path.join(self.state, "s1", HOME_DIR)
        os.makedirs(os.path.join(self.client, "a"))

    def put(self, rel: str, data: bytes = b"") -> str:
        path = os.path.join(self.client, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_op(self, op: str, file: StateFile):
        if op == "snapshot":
            with client_state.snapshot(file, limit=1 << 20) as path:
                return path
        return getattr(client_state, op)(file)

    def refused(self, op: str, file: StateFile) -> OSError:
        with self.assertRaises(OSError) as caught:
            self.run_op(op, file)
        self.assertNotIsInstance(caught.exception, FileNotFoundError)
        return caught.exception


class ReadTest(StateCase):
    def test_reads_whole_offset_limit_and_tail(self):
        self.put("a/t.jsonl", b"0123456789")
        file = StateFile("s1", HOME_DIR, "a/t.jsonl")
        self.assertEqual(client_state.read(file), b"0123456789")
        self.assertEqual(client_state.read(file, offset=4), b"456789")
        self.assertEqual(client_state.read(file, limit=3), b"012")
        self.assertEqual(client_state.read(file, offset=2, limit=3), b"234")
        self.assertEqual(client_state.read(file, limit=3, tail=True), b"789")
        self.assertEqual(client_state.read(file, offset=8, limit=5, tail=True), b"89")
        self.assertEqual(client_state.read(file, limit=0), b"")

    def test_caps_a_read_at_read_max(self):
        path = self.put("big")
        os.truncate(path, client_state.READ_MAX + 1)
        file = StateFile("s1", HOME_DIR, "big")
        self.assertEqual(len(client_state.read(file) or b""), client_state.READ_MAX)
        self.assertEqual(
            len(client_state.read(file, limit=client_state.READ_MAX + 5) or b""),
            client_state.READ_MAX,
        )

    def test_absent_file_dir_client_or_session_is_none(self):
        for file in (
            StateFile("s1", HOME_DIR, "missing"),
            StateFile("s1", HOME_DIR, "nodir/t"),
            StateFile("s1", "other", "t"),
            StateFile("s9", HOME_DIR, "t"),
        ):
            with self.subTest(file=file):
                self.assertIsNone(client_state.read(file))

    def test_absent_state_root_is_none(self):
        os.rename(self.state, self.state + ".gone")
        self.assertIsNone(client_state.read(StateFile("s1", HOME_DIR, "a/t")))

    def test_refuses_a_directory_or_fifo(self):
        os.mkfifo(os.path.join(self.client, "fifo"))
        for rel in ("a", "fifo"):
            with self.subTest(rel=rel):
                self.refused("read", StateFile("s1", HOME_DIR, rel))


class LinkTest(StateCase):
    def setUp(self):
        super().setUp()
        self.secret = os.path.join(self.home, "secret")
        os.makedirs(os.path.join(self.secret, HOME_DIR, "a"))
        for rel in ("t", f"{HOME_DIR}/t", f"{HOME_DIR}/a/t"):
            with open(os.path.join(self.secret, rel), "wb") as f:
                f.write(b"SECRET")
        os.mkdir(os.path.join(self.secret, "d"))

    def test_refuses_a_linked_final_file(self):
        os.symlink(os.path.join(self.secret, "t"), os.path.join(self.client, "t"))
        for op in ("read", "stat", "snapshot"):
            with self.subTest(op=op):
                self.refused(op, StateFile("s1", HOME_DIR, "t"))
        os.symlink(os.path.join(self.secret, "d"), os.path.join(self.client, "d"))
        self.refused("entries", StateFile("s1", HOME_DIR, "d"))

    def test_refuses_a_linked_middle_dir(self):
        os.symlink(os.path.join(self.secret, HOME_DIR), os.path.join(self.client, "m"))
        for op in OPS:
            rel = "m/a" if op == "entries" else "m/a/t"
            with self.subTest(op=op):
                self.refused(op, StateFile("s1", HOME_DIR, rel))

    def test_refuses_a_linked_client_home_or_session_dir(self):
        os.symlink(self.secret, os.path.join(self.state, "s2"))
        os.symlink(os.path.join(self.secret, HOME_DIR), os.path.join(self.state, "s1", "linked"))
        for file in (StateFile("s2", HOME_DIR, "a/t"), StateFile("s1", "linked", "a/t")):
            for op in OPS:
                target = file._replace(rel="a") if op == "entries" else file
                with self.subTest(file=file, op=op):
                    self.refused(op, target)

    def test_a_linked_state_root_is_followed(self):
        os.rename(self.state, os.path.join(self.home, "elsewhere"))
        os.symlink(os.path.join(self.home, "elsewhere"), self.state)
        self.put("a/t", b"ok")
        self.assertEqual(client_state.read(StateFile("s1", HOME_DIR, "a/t")), b"ok")


class NameTest(StateCase):
    def test_refuses_invalid_parts_before_opening(self):
        bad = [
            StateFile("", HOME_DIR, "t"),
            StateFile(".", HOME_DIR, "t"),
            StateFile("..", HOME_DIR, "t"),
            StateFile("s/1", HOME_DIR, "t"),
            StateFile("s\0", HOME_DIR, "t"),
            StateFile("s1", "", "t"),
            StateFile("s1", "..", "t"),
            StateFile("s1", "a/b", "t"),
            StateFile("s1", HOME_DIR, ""),
            StateFile("s1", HOME_DIR, "/etc/hosts"),
            StateFile("s1", HOME_DIR, "a/../t"),
            StateFile("s1", HOME_DIR, "./t"),
            StateFile("s1", HOME_DIR, "a//t"),
            StateFile("s1", HOME_DIR, "a/"),
            StateFile("s1", HOME_DIR, "t\0x"),
        ]
        with mock.patch.object(os, "open", side_effect=AssertionError("opened")):
            for file in bad:
                for op in OPS:
                    with self.subTest(file=file, op=op):
                        self.assertEqual(self.refused(op, file).errno, errno.EINVAL)


class LocateTest(StateCase):
    def test_maps_a_path_below_a_client_home(self):
        self.assertEqual(
            client_state.locate(os.path.join(self.client, "a", "b.jsonl")),
            StateFile("s1", HOME_DIR, "a/b.jsonl"),
        )
        self.assertEqual(
            client_state.locate(os.path.join(self.client, "x", "..", "a", "b")),
            StateFile("s1", HOME_DIR, "a/b"),
        )

    def test_outside_the_state_root_is_none(self):
        for path in (
            os.path.join(self.home, "t.jsonl"),
            os.path.join(self.ensemble, "docker-statex", "s1", HOME_DIR, "t"),
            os.path.join(self.client, "..", "..", "..", "t"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(client_state.locate(path))

    def test_below_the_state_root_but_not_a_client_home_is_refused(self):
        for path in (self.state, os.path.join(self.state, "s1"), self.client):
            with self.subTest(path=path), self.assertRaises(OSError) as caught:
                client_state.locate(path)
            self.assertEqual(caught.exception.errno, errno.EXDEV)

    def test_a_realpath_under_a_linked_home_is_located(self):
        real = os.path.join(self.home, "real")
        os.mkdir(real)
        linked = os.path.join(self.home, "linked")
        os.symlink(real, linked)
        os.makedirs(os.path.join(real, ".ensemble", "docker-state", "s1", HOME_DIR))
        with mock.patch.dict(os.environ, {"HOME": linked}):
            for base in (linked, real):
                path = os.path.join(base, ".ensemble", "docker-state", "s1", HOME_DIR, "t")
                with self.subTest(base=base):
                    self.assertEqual(client_state.locate(path), StateFile("s1", HOME_DIR, "t"))
            with open(
                os.path.join(real, ".ensemble", "docker-state", "s1", HOME_DIR, "t"), "wb"
            ) as f:
                f.write(b"via link")
            self.assertEqual(client_state.read(StateFile("s1", HOME_DIR, "t")), b"via link")


class StatTest(StateCase):
    def test_a_regular_file_has_size_and_mtime(self):
        path = self.put("a/t", b"12345")
        os.utime(path, (1000, 2000))
        self.assertEqual(client_state.stat(StateFile("s1", HOME_DIR, "a/t")), StateStat(5, 2000))

    def test_absent_is_none_and_others_are_refused(self):
        os.mkfifo(os.path.join(self.client, "fifo"))
        self.assertIsNone(client_state.stat(StateFile("s1", HOME_DIR, "missing")))
        for rel in ("a", "fifo"):
            with self.subTest(rel=rel):
                self.refused("stat", StateFile("s1", HOME_DIR, rel))


class EntriesTest(StateCase):
    def test_lists_kinds_sizes_and_mtimes_sorted(self):
        path = self.put("a/f", b"xyz")
        os.utime(path, (1, 50))
        os.mkdir(os.path.join(self.client, "a", "d"))
        os.mkfifo(os.path.join(self.client, "a", "p"))
        os.symlink(os.path.join(self.client, "a", "d"), os.path.join(self.client, "a", "l"))
        found = client_state.entries(StateFile("s1", HOME_DIR, "a"))
        assert found is not None
        self.assertEqual([e.name for e in found], ["d", "f", "l", "p"])
        kinds = {e.name: e.kind for e in found}
        self.assertEqual(
            kinds,
            {"d": StateKind.DIR, "f": StateKind.FILE, "l": StateKind.OTHER, "p": StateKind.OTHER},
        )
        self.assertEqual(found[1], StateEntry("f", StateKind.FILE, 3, 50))

    def test_absent_is_none_a_file_is_refused(self):
        self.put("a/f")
        self.assertIsNone(client_state.entries(StateFile("s1", HOME_DIR, "missing")))
        self.refused("entries", StateFile("s1", HOME_DIR, "a/f"))

    def test_refuses_more_entries_than_the_limit(self):
        for n in range(3):
            self.put(f"a/{n}")
        file = StateFile("s1", HOME_DIR, "a")
        self.assertEqual(len(client_state.entries(file, limit=3) or ()), 3)
        with self.assertRaises(OSError) as caught:
            client_state.entries(file, limit=2)
        self.assertEqual(caught.exception.errno, errno.EFBIG)


class SnapshotTest(StateCase):
    def test_copies_main_and_present_siblings_into_a_private_dir(self):
        self.put("a/x.db", b"main")
        self.put("a/x.db-wal", b"wal")
        file = StateFile("s1", HOME_DIR, "a/x.db")
        with client_state.snapshot(file, limit=100, siblings=("-wal", "-shm")) as path:
            assert path is not None
            folder = os.path.dirname(path)
            self.assertEqual(os.path.basename(path), "x.db")
            self.assertEqual(sorted(os.listdir(folder)), ["x.db", "x.db-wal"])
            self.assertEqual(stat_module.S_IMODE(os.stat(folder).st_mode), 0o700)
            with open(path + "-wal", "rb") as f:
                self.assertEqual(f.read(), b"wal")
            self.assertFalse(folder.startswith(self.state))
        self.assertFalse(os.path.exists(folder))

    def test_absent_main_is_none(self):
        with client_state.snapshot(StateFile("s1", HOME_DIR, "a/x.db"), limit=10) as path:
            self.assertIsNone(path)

    def test_removes_the_copy_after_an_exception(self):
        self.put("a/x.db", b"main")
        file = StateFile("s1", HOME_DIR, "a/x.db")
        with self.assertRaises(RuntimeError), client_state.snapshot(file, limit=10) as path:
            assert path is not None
            folder = os.path.dirname(path)
            raise RuntimeError
        self.assertFalse(os.path.exists(folder))

    def test_refuses_a_total_over_the_limit(self):
        self.put("a/x.db", b"12345")
        self.put("a/x.db-wal", b"678")
        file = StateFile("s1", HOME_DIR, "a/x.db")
        with client_state.snapshot(file, limit=8, siblings=("-wal",)) as path:
            self.assertIsNotNone(path)
        with (
            self.assertRaises(OSError) as caught,
            client_state.snapshot(file, limit=7, siblings=("-wal",)),
        ):
            pass
        self.assertEqual(caught.exception.errno, errno.EFBIG)

    def test_refuses_a_linked_sibling(self):
        self.put("a/x.db", b"main")
        os.symlink(
            os.path.join(self.client, "a", "x.db"), os.path.join(self.client, "a", "x.db-wal")
        )
        with (
            self.assertRaises(OSError),
            client_state.snapshot(
                StateFile("s1", HOME_DIR, "a/x.db"), limit=100, siblings=("-wal",)
            ),
        ):
            pass

    def test_an_unstable_source_is_retried_then_refused(self):
        self.put("a/x.db", b"main")
        real = os.fstat
        calls = []

        def drifting(fd):
            found = real(fd)
            calls.append(fd)
            if not stat_module.S_ISREG(found.st_mode):
                return found
            values = list(found)
            values[stat_module.ST_SIZE] = found.st_size + len(calls) % 2
            return os.stat_result(values)

        file = StateFile("s1", HOME_DIR, "a/x.db")
        with (
            mock.patch.object(os, "fstat", drifting),
            self.assertRaises(OSError) as caught,
            client_state.snapshot(file, limit=100),
        ):
            pass
        self.assertEqual(caught.exception.errno, errno.EAGAIN)

    def test_a_wal_database_copy_opens_with_its_uncheckpointed_rows(self):
        db = os.path.join(self.client, "a", "x.db")
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (v)")
        writer.execute("INSERT INTO t VALUES (1), (2)")
        writer.commit()
        self.assertGreater(os.path.getsize(db + "-wal"), 0)
        file = StateFile("s1", HOME_DIR, "a/x.db")
        with client_state.snapshot(file, limit=1 << 20, siblings=("-wal", "-shm")) as path:
            assert path is not None
            reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertEqual(reader.execute("SELECT count(*) FROM t").fetchone(), (2,))
            finally:
                reader.close()
