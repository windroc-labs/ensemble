"""session/engine/client_state.py: VolumeHome's read, stat, entries and snapshot through a session's
reader (state_reader.py over its state volume, served by tests/session/state_volumes.py): refusing
links, names and paths outside a client home, absent paths, size limits and broken replies; LocalHome,
home_path, home_store and recorded_home.
"""

import errno
import io
import json
import os
import sqlite3
import stat as stat_module
import tarfile
import tempfile
import threading
import time
from unittest import mock

from ensemble.session.engine import client_state, daemon
from ensemble.session.engine.types import (
    Daemon,
    DaemonRecord,
    HomePath,
    StateEntry,
    StateKind,
    StateStat,
)
from tests.session.case import Case
from tests.session.state_volumes import DOCKER, HOST, IMAGE, LOCAL, serve
from tests.session.unit.hosts.docker.support import ssh_dir

HOME_DIR = "app"
OPS = ("read", "stat", "entries", "snapshot")
STORE = client_state.VolumeHome("s1", HOME_DIR)


def at(rel: str) -> HomePath:
    """`rel` in a volume home, whose root no read uses."""
    return HomePath("/nowhere/home", rel)


class StateCase(Case):
    def setUp(self):
        self.state = os.path.join(self.ensemble, "docker-state")
        self.client = os.path.join(self.state, "s1", HOME_DIR)
        os.makedirs(os.path.join(self.client, "a"))
        self.volumes = self.enterContext(serve())

    def put(self, rel: str, data: bytes = b"") -> str:
        path = os.path.join(self.client, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_op(self, op: str, file: HomePath, store=STORE):
        if op == "snapshot":
            with store.snapshot(file, limit=1 << 20) as path:
                return path
        return getattr(store, op)(file)

    def refused(self, op: str, file: HomePath, store=STORE) -> OSError:
        with self.assertRaises(OSError) as caught:
            self.run_op(op, file, store)
        self.assertNotIsInstance(caught.exception, FileNotFoundError)
        return caught.exception


class ReadTest(StateCase):
    def test_reads_whole_offset_limit_and_tail(self):
        self.put("a/t.jsonl", b"0123456789")
        file = at("a/t.jsonl")
        self.assertEqual(STORE.read(file), b"0123456789")
        self.assertEqual(STORE.read(file, offset=4), b"456789")
        self.assertEqual(STORE.read(file, limit=3), b"012")
        self.assertEqual(STORE.read(file, offset=2, limit=3), b"234")
        self.assertEqual(STORE.read(file, limit=3, tail=True), b"789")
        self.assertEqual(STORE.read(file, offset=8, limit=5, tail=True), b"89")
        self.assertEqual(STORE.read(file, limit=0), b"")

    def test_caps_a_read_at_read_max(self):
        path = self.put("big")
        os.truncate(path, client_state.READ_MAX + 1)
        file = at("big")
        self.assertEqual(len(STORE.read(file) or b""), client_state.READ_MAX)
        self.assertEqual(
            len(STORE.read(file, limit=client_state.READ_MAX + 5) or b""),
            client_state.READ_MAX,
        )

    def test_absent_file_dir_client_or_session_is_none(self):
        for store, rel in (
            (STORE, "missing"),
            (STORE, "nodir/t"),
            (client_state.VolumeHome("s1", "other"), "t"),
            (client_state.VolumeHome("s9", HOME_DIR), "t"),
        ):
            with self.subTest(persisted=store.persisted(), rel=rel):
                self.assertIsNone(store.read(at(rel)))

    def test_absent_state_root_is_none(self):
        os.rename(self.state, self.state + ".gone")
        self.assertIsNone(STORE.read(at("a/t")))

    def test_refuses_a_directory_or_fifo(self):
        os.mkfifo(os.path.join(self.client, "fifo"))
        for rel in ("a", "fifo"):
            with self.subTest(rel=rel):
                self.refused("read", at(rel))


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
                self.refused(op, at("t"))
        os.symlink(os.path.join(self.secret, "d"), os.path.join(self.client, "d"))
        self.refused("entries", at("d"))

    def test_refuses_a_linked_middle_dir(self):
        os.symlink(os.path.join(self.secret, HOME_DIR), os.path.join(self.client, "m"))
        for op in OPS:
            rel = "m/a" if op == "entries" else "m/a/t"
            with self.subTest(op=op):
                self.refused(op, at(rel))

    def test_refuses_a_linked_client_home(self):
        os.symlink(os.path.join(self.secret, HOME_DIR), os.path.join(self.state, "s1", "linked"))
        linked = client_state.VolumeHome("s1", "linked")
        for op in OPS:
            with self.subTest(op=op):
                self.refused(op, at("a" if op == "entries" else "a/t"), linked)

    def test_a_session_record_linked_outside_the_volume_writes_nothing_outside_the_temp_dir(self):
        os.symlink(os.path.join(self.secret, "t"), os.path.join(self.client, "a", "s.jsonl"))
        before = self.tree()
        file = at("a/s.jsonl")
        for op in ("read", "stat", "snapshot"):
            with self.subTest(op=op):
                self.assertEqual(self.refused(op, file).errno, errno.ELOOP)
        listed = STORE.entries(at("a")) or ()
        self.assertEqual([(e.name, e.kind) for e in listed], [("s.jsonl", StateKind.OTHER)])
        self.assertEqual(self.tree(), before)

    def tree(self) -> list[str]:
        return sorted(
            os.path.join(top, name)
            for top, dirs, files in os.walk(self.home)
            for name in dirs + files
        )

    def test_a_linked_state_root_is_followed(self):
        os.rename(self.state, os.path.join(self.home, "elsewhere"))
        os.symlink(os.path.join(self.home, "elsewhere"), self.state)
        self.put("a/t", b"ok")
        self.assertEqual(STORE.read(at("a/t")), b"ok")


class NameTest(StateCase):
    def test_refuses_invalid_parts_before_opening(self):
        bad = [
            ("", HOME_DIR, "t"),
            (".", HOME_DIR, "t"),
            ("..", HOME_DIR, "t"),
            ("s/1", HOME_DIR, "t"),
            ("s\0", HOME_DIR, "t"),
            ("s1", "", "t"),
            ("s1", "..", "t"),
            ("s1", "a/b", "t"),
            ("s1", HOME_DIR, ""),
            ("s1", HOME_DIR, "/etc/hosts"),
            ("s1", HOME_DIR, "a/../t"),
            ("s1", HOME_DIR, "./t"),
            ("s1", HOME_DIR, "a//t"),
            ("s1", HOME_DIR, "a/"),
            ("s1", HOME_DIR, "t\0x"),
        ]
        with mock.patch.object(os, "open", side_effect=AssertionError("opened")):
            for session, client, rel in bad:
                store = client_state.VolumeHome(session, client)
                for op in OPS:
                    with self.subTest(session=session, client=client, rel=rel, op=op):
                        self.assertEqual(self.refused(op, at(rel), store).errno, errno.EINVAL)


class StatTest(StateCase):
    def test_a_regular_file_has_size_and_mtime(self):
        path = self.put("a/t", b"12345")
        os.utime(path, (1000, 2000))
        self.assertEqual(STORE.stat(at("a/t")), StateStat(5, 2000))

    def test_absent_is_none_and_others_are_refused(self):
        os.mkfifo(os.path.join(self.client, "fifo"))
        self.assertIsNone(STORE.stat(at("missing")))
        for rel in ("a", "fifo"):
            with self.subTest(rel=rel):
                self.refused("stat", at(rel))


class EntriesTest(StateCase):
    def test_lists_kinds_sizes_and_mtimes_sorted(self):
        path = self.put("a/f", b"xyz")
        os.utime(path, (1, 50))
        os.mkdir(os.path.join(self.client, "a", "d"))
        os.mkfifo(os.path.join(self.client, "a", "p"))
        os.symlink(os.path.join(self.client, "a", "d"), os.path.join(self.client, "a", "l"))
        found = STORE.entries(at("a"))
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
        self.assertIsNone(STORE.entries(at("missing")))
        self.refused("entries", at("a/f"))

    def test_refuses_more_entries_than_the_limit(self):
        for n in range(3):
            self.put(f"a/{n}")
        file = at("a")
        self.assertEqual(len(STORE.entries(file, limit=3) or ()), 3)
        with self.assertRaises(OSError) as caught:
            STORE.entries(file, limit=2)
        self.assertEqual(caught.exception.errno, errno.EFBIG)


class SnapshotTest(StateCase):
    def test_copies_main_and_present_siblings_into_a_private_dir(self):
        self.put("a/x.db", b"main")
        self.put("a/x.db-wal", b"wal")
        file = at("a/x.db")
        with STORE.snapshot(file, limit=100, siblings=("-wal", "-shm")) as path:
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
        with STORE.snapshot(at("a/x.db"), limit=10) as path:
            self.assertIsNone(path)

    def test_removes_the_copy_after_an_exception(self):
        self.put("a/x.db", b"main")
        file = at("a/x.db")
        with self.assertRaises(RuntimeError), STORE.snapshot(file, limit=10) as path:
            assert path is not None
            folder = os.path.dirname(path)
            raise RuntimeError
        self.assertFalse(os.path.exists(folder))

    def test_refuses_a_total_over_the_limit(self):
        self.put("a/x.db", b"12345")
        self.put("a/x.db-wal", b"678")
        file = at("a/x.db")
        with STORE.snapshot(file, limit=8, siblings=("-wal",)) as path:
            self.assertIsNotNone(path)
        with (
            self.assertRaises(OSError) as caught,
            STORE.snapshot(file, limit=7, siblings=("-wal",)),
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
            STORE.snapshot(at("a/x.db"), limit=100, siblings=("-wal",)),
        ):
            pass

    def test_a_source_that_settles_is_copied_after_three_attempts_at_most(self):
        self.put("a/x.db", b"main")
        real = os.fstat
        calls = []

        def drifting(fd):
            found = real(fd)
            if not stat_module.S_ISREG(found.st_mode):
                return found
            calls.append(fd)
            values = list(found)
            values[stat_module.ST_SIZE] = found.st_size + (1 if len(calls) == 1 else 0)
            return os.stat_result(values)

        with (
            mock.patch.object(os, "fstat", drifting),
            STORE.snapshot(at("a/x.db"), limit=100) as path,
        ):
            assert path is not None
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"main")

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

        file = at("a/x.db")
        with (
            mock.patch.object(os, "fstat", drifting),
            self.assertRaises(OSError) as caught,
            STORE.snapshot(file, limit=100),
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
        file = at("a/x.db")
        with STORE.snapshot(file, limit=1 << 20, siblings=("-wal", "-shm")) as path:
            assert path is not None
            reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertEqual(reader.execute("SELECT count(*) FROM t").fetchone(), (2,))
            finally:
                reader.close()


class ReaderTest(StateCase):
    def test_one_reader_serves_many_calls_with_no_mac_file_opened(self):
        path = self.put("a/t", b"abc")
        file = at("a/t")
        for _ in range(5):
            self.assertEqual(STORE.read(file), b"abc")
            self.assertEqual(STORE.stat(file), StateStat(3, os.stat(path).st_mtime))
        self.assertEqual(len(self.volumes.readers), 1)
        runs = [c for c in self.volumes.calls if c[3] == "run"]
        self.assertEqual(len(runs), 1)

    def test_the_reader_runs_confined_on_the_records_daemon(self):
        self.put("a/t", b"abc")
        STORE.read(at("a/t"))
        (run,) = [c for c in self.volumes.calls if c[3] == "run"]
        self.assertEqual(run[:3], [DOCKER, "--host", HOST])
        for flag in (
            ["--log-driver", "none"],
            ["--network", "none"],
            ["--cap-drop", "ALL"],
            ["--security-opt", "no-new-privileges"],
            ["--user", "1001:1002"],
            ["--label", "ensemble.state-reader=s1"],
            [
                "--mount",
                "type=volume,source=ensemble-state-s1,target=/state,readonly,volume-nocopy",
            ],
        ):
            self.assertTrue(
                any(run[i : i + 2] == flag for i in range(len(run))), f"{flag} not in {run}"
            )
        self.assertIn("--read-only", run)
        self.assertIn("--rm", run)
        self.assertEqual(run[run.index(IMAGE) + 1 : run.index(IMAGE) + 3], ["-I", "-c"])
        with open(client_state.READER, encoding="utf-8") as f:
            self.assertEqual(run[run.index(IMAGE) + 3], f.read())

    def test_two_threads_each_get_their_own_reply(self):
        self.put("a/x", b"x" * 300000)
        self.put("a/y", b"y" * 300000)
        found, errors = {}, []

        def read(name):
            try:
                for _ in range(20):
                    data = STORE.read(at(f"a/{name}"))
                    if data != name.encode() * 300000:
                        raise AssertionError(name)
                found[name] = True
            except (AssertionError, OSError) as error:
                errors.append(error)

        threads = [threading.Thread(target=read, args=(n,)) for n in ("x", "y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(found, {"x": True, "y": True})
        self.assertEqual(len(self.volumes.readers), 1)

    def test_no_volume_is_absent_and_starts_no_reader(self):
        self.volumes.volumes = set()
        self.put("a/t", b"abc")
        file = at("a/t")
        self.assertIsNone(STORE.read(file))
        self.assertIsNone(STORE.stat(file))
        self.assertIsNone(STORE.entries(at("a")))
        with STORE.snapshot(file, limit=10) as path:
            self.assertIsNone(path)
        self.assertFalse([c for c in self.volumes.calls if c[3] == "run"])

    def test_no_worker_image_is_refused(self):
        self.volumes.images = "ensemble-worker:latest\n"
        self.put("a/t")
        self.assertEqual(self.refused("read", at("a/t")).errno, errno.EIO)

    def test_any_failing_docker_call_is_refused_whatever_it_says(self):
        self.put("a/t")
        for error in ("Cannot connect to the Docker daemon", "Error: no such volume: x"):
            with self.subTest(error=error):
                self.volumes.error = error
                self.assertEqual(self.refused("read", at("a/t")).errno, errno.EIO)
                self.assertFalse(self.volumes.readers)

    def test_a_reader_that_exited_is_replaced_on_the_next_call(self):
        self.put("a/t", b"abc")
        file = at("a/t")
        self.assertEqual(STORE.read(file), b"abc")
        self.volumes.readers[0].kill()
        self.assertEqual(STORE.read(file), b"abc")
        self.assertEqual(len(self.volumes.readers), 2)

    def test_a_reader_that_leaves_mid_request_is_replaced_and_asked_again(self):
        self.put("a/t", b"abc")
        file = at("a/t")
        self.assertEqual(STORE.read(file), b"abc")
        self.volumes.readers[0].leave = True
        self.assertEqual(STORE.read(file), b"abc")
        self.assertEqual(len(self.volumes.readers), 2)

    def test_a_volume_with_no_owner_is_refused(self):
        self.volumes.owner = ""
        self.put("a/t")
        self.assertEqual(self.refused("read", at("a/t")).errno, errno.EIO)
        self.assertFalse(self.volumes.readers)

    def test_a_large_read_streams_within_its_time(self):
        path = self.put("big")
        os.truncate(path, client_state.READ_MAX)
        started = time.monotonic()
        self.assertEqual(len(STORE.read(at("big")) or b""), client_state.READ_MAX)
        self.assertLess(time.monotonic() - started, client_state.REPLY_TIMEOUT)

    def test_a_silent_reader_times_out_and_is_stopped(self):
        self.put("a/t", b"abc")
        file = at("a/t")
        self.assertEqual(STORE.read(file), b"abc")
        self.volumes.readers[0].hang = True
        clock = iter([0.0, *[client_state.REPLY_TIMEOUT + 1.0] * 4])
        with mock.patch("time.monotonic", lambda: next(clock)):
            self.assertEqual(self.refused("read", file).errno, errno.ETIMEDOUT)
        self.assertTrue(self.volumes.readers[0].killed)
        self.assertEqual(STORE.read(file), b"abc")


class DaemonRecordTest(Case):
    def setUp(self):
        self.client = os.path.join(self.ensemble, "docker-state", "s1", HOME_DIR)
        os.makedirs(self.client)
        with open(os.path.join(self.client, "t"), "wb") as f:
            f.write(b"ok")
        self.volumes = self.enterContext(serve(sessions=()))

    def remote(self, path):
        found = Daemon("remote", "ssh://pc", remote=True, path=path, id="daemon-a")
        daemon.save_record("s1", DaemonRecord(found, "/opt/docker"))

    def test_calls_use_the_sessions_recorded_docker_and_daemon(self):
        self.remote(f"{ssh_dir(self.home)}{os.pathsep}/usr/bin")
        self.assertEqual(STORE.read(at("t")), b"ok")
        self.assertEqual(
            [call[3:5] for call in self.volumes.calls],
            [["volume", "ls"], ["volume", "inspect"], ["image", "ls"], ["run", "--rm"]],
        )
        for call in self.volumes.calls:
            self.assertEqual(call[:3], ["/opt/docker", "--host", "ssh://pc"])

    def test_a_remote_record_without_its_ssh_wrapper_starts_no_process(self):
        bare = os.path.join(self.home, "bare")
        os.makedirs(bare)
        self.remote(f"{bare}{os.pathsep}{ssh_dir(self.home)}")
        with self.assertRaises(OSError) as caught:
            STORE.read(at("t"))
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertIn(f"ssh wrapper unavailable: {bare}/ssh", str(caught.exception))
        self.assertFalse(self.volumes.calls)

    def test_no_record_is_absent(self):
        self.assertIsNone(STORE.read(at("t")))
        self.assertFalse(self.volumes.calls)

    def test_a_malformed_record_is_refused_naming_it_and_never_read_locally(self):
        path = os.path.join(self.ensemble, "docker-state", "s1", daemon.RECORD_FILE)
        good = {"name": "local", "id": "daemon-a", "endpoint": HOST, "path": "/usr/bin"}
        for text in (
            "[]",
            "{",
            json.dumps({**good, "docker": "docker"}),
            json.dumps({**good, "docker": "/d", "name": "pc"}),
            json.dumps({**good, "docker": "/d", "id": "../x"}),
            json.dumps({**good, "docker": "/d", "endpoint": ""}),
            json.dumps({**good, "docker": "/d", "extra": "x"}),
            json.dumps(good),
        ):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.subTest(text=text), self.assertRaises(OSError) as caught:
                STORE.read(at("t"))
            self.assertEqual(
                (caught.exception.errno, caught.exception.filename), (errno.EINVAL, path)
            )
        self.assertFalse(self.volumes.calls)


class BrokenReplyTest(StateCase):
    def reply(self, header: dict | None = None, payload: bytes = b"", *, raw: bytes | None = None):
        base = {"errno": None, "message": "", "size": 0, "mtime": 0.0, "entries": []}
        if raw is None:
            found = {**base, **(header or {})}
            found.setdefault("length", len(payload))
            raw = json.dumps(found).encode() + b"\n" + payload
        self.volumes.reply = raw

    def assert_broken(self, op: str, file: HomePath):
        self.assertEqual(self.refused(op, file).errno, errno.EPROTO)
        self.assertTrue(self.volumes.readers[-1].killed)

    @staticmethod
    def archive(*members: tarfile.TarInfo, data: bytes = b"") -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for member in members:
                tar.addfile(member, io.BytesIO(data) if member.isreg() else None)
        return buffer.getvalue()

    def test_bad_headers_are_refused(self):
        file = at("a/t")
        for raw in (
            b"not json\n",
            b"[]\n",
            json.dumps({"errno": None}).encode() + b"\n",
            json.dumps(
                {"errno": None, "message": "", "size": -1, "mtime": 0, "entries": [], "length": 0}
            ).encode()
            + b"\n",
        ):
            with self.subTest(raw=raw):
                self.reply(raw=raw)
                self.assert_broken("read", file)

    def test_a_length_over_the_bound_is_refused(self):
        self.reply({"length": 4}, b"abcd")
        with self.assertRaises(OSError) as caught:
            STORE.read(at("a/t"), limit=3)
        self.assertEqual(caught.exception.errno, errno.EPROTO)

    def test_a_header_with_no_end_is_refused_past_its_bound(self):
        self.reply(raw=b"{" + b" " * (client_state.HEADER_MAX + 1))
        self.assert_broken("read", at("a/t"))

    def test_entry_names_that_are_not_one_part_are_refused(self):
        for name in ("../x", "a/b", "..", "", "x\u0000"):
            with self.subTest(name=name):
                self.reply({"entries": [[name, "file", 1, 0.0]]})
                self.assert_broken("entries", at("a"))

    def test_snapshot_members_other_than_the_requested_files_are_refused(self):
        file = at("a/x.db")
        link = tarfile.TarInfo("x.db")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/hosts"
        hard = tarfile.TarInfo("x.db")
        hard.type, hard.linkname = tarfile.LNKTYPE, "x.db-wal"
        sparse = tarfile.TarInfo("x.db")
        sparse.type, sparse.size = tarfile.GNUTYPE_SPARSE, 0
        named = {}
        for name in ("../x.db", "/tmp/x.db", "other", "a/x.db"):
            named[name] = tarfile.TarInfo(name)
            named[name].size = 1
        big = tarfile.TarInfo("x.db")
        big.size = 11
        twice = tarfile.TarInfo("x.db")
        twice.size = 1
        cases = {
            "symlink": self.archive(link),
            "hardlink": self.archive(hard),
            "sparse": self.archive(sparse),
            **{name: self.archive(member, data=b"x") for name, member in named.items()},
            "over limit": self.archive(big, data=b"x" * 11),
            "twice": self.archive(twice, twice, data=b"x"),
            "not tar": b"garbage" * 100,
        }
        before = sorted(os.listdir(tempfile.gettempdir()))
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.reply(payload=payload)
                with self.assertRaises(OSError) as caught, STORE.snapshot(file, limit=10):
                    pass
                self.assertEqual(caught.exception.errno, errno.EPROTO)
        self.assertFalse(os.path.lexists(os.path.join(self.home, "x.db")))
        self.assertEqual(sorted(os.listdir(tempfile.gettempdir())), before)


class LocalHomeTest(Case):
    """LocalHome: a home on this machine, read below its root with no link followed."""

    def setUp(self):
        self.root = os.path.join(self.home, "client-home")
        os.makedirs(os.path.join(self.root, "a"))
        self.store = client_state.LocalHome()

    def put(self, rel: str, data: bytes = b"") -> HomePath:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return HomePath(self.root, rel)

    def refused(self, call) -> OSError:
        with self.assertRaises(OSError) as caught:
            call()
        self.assertNotIsInstance(caught.exception, FileNotFoundError)
        return caught.exception

    def test_reads_whole_offset_limit_and_tail(self):
        path = self.put("a/t.jsonl", b"0123456789")
        self.assertEqual(self.store.read(path), b"0123456789")
        self.assertEqual(self.store.read(path, offset=4), b"456789")
        self.assertEqual(self.store.read(path, limit=3), b"012")
        self.assertEqual(self.store.read(path, limit=3, tail=True), b"789")
        self.assertEqual(self.store.read(path, offset=8, limit=5, tail=True), b"89")

    def test_a_one_part_path_reads(self):
        self.assertEqual(self.store.read(self.put("t", b"x")), b"x")

    def test_stat_gives_size_and_mtime(self):
        path = self.put("a/t", b"abc")
        os.utime(os.path.join(self.root, "a/t"), (100, 200))
        self.assertEqual(self.store.stat(path), StateStat(3, 200))

    def test_entries_list_kinds_by_lstat_sorted(self):
        self.put("a/f", b"12")
        os.mkdir(os.path.join(self.root, "a", "d"))
        os.symlink(os.path.join(self.root, "a", "d"), os.path.join(self.root, "a", "l"))
        listed = self.store.entries(HomePath(self.root, "a"))
        assert listed is not None
        self.assertEqual(
            [(e.name, e.kind) for e in listed],
            [("d", StateKind.DIR), ("f", StateKind.FILE), ("l", StateKind.OTHER)],
        )
        self.assertEqual(listed[1].size, 2)

    def test_entries_past_the_limit_are_refused(self):
        for name in "abc":
            self.put(f"a/{name}")
        error = self.refused(lambda: self.store.entries(HomePath(self.root, "a"), limit=2))
        self.assertEqual(error.errno, errno.EFBIG)

    def test_snapshot_yields_the_file_itself_and_none_when_absent(self):
        path = self.put("a/db", b"rows")
        with self.store.snapshot(path, limit=10, siblings=("-wal",)) as found:
            self.assertEqual(found, os.path.join(self.root, "a", "db"))
        with self.store.snapshot(HomePath(self.root, "a/none"), limit=10) as found:
            self.assertIsNone(found)

    def test_absent_is_none(self):
        missing = HomePath(self.root, "a/none")
        self.assertIsNone(self.store.read(missing))
        self.assertIsNone(self.store.stat(missing))
        self.assertIsNone(self.store.entries(HomePath(self.root, "none")))
        self.assertIsNone(self.store.read(HomePath(os.path.join(self.home, "no-home"), "a/t")))

    def test_a_wrong_type_is_refused(self):
        self.put("a/f")
        for call in (
            lambda: self.store.read(HomePath(self.root, "a")),
            lambda: self.store.stat(HomePath(self.root, "a")),
            lambda: self.store.entries(HomePath(self.root, "a/f")),
        ):
            self.refused(call)

    def test_an_invalid_rel_is_refused_before_opening(self):
        for rel in ("", "a/../t", "./t", "a//t", "/etc/hosts", "a/\0"):
            with self.subTest(rel=rel), mock.patch("os.open", side_effect=AssertionError):
                error = self.refused(lambda rel=rel: self.store.read(HomePath(self.root, rel)))
                self.assertEqual(error.errno, errno.EINVAL)

    def test_a_linked_file_or_middle_dir_is_refused(self):
        secret = os.path.join(self.home, "secret")
        os.makedirs(os.path.join(secret, "d"))
        with open(os.path.join(secret, "d", "t"), "wb") as f:
            f.write(b"SECRET")
        os.symlink(os.path.join(secret, "d", "t"), os.path.join(self.root, "a", "t"))
        os.symlink(os.path.join(secret, "d"), os.path.join(self.root, "m"))
        for call in (
            lambda: self.store.read(HomePath(self.root, "a/t")),
            lambda: self.store.stat(HomePath(self.root, "a/t")),
            lambda: self.store.read(HomePath(self.root, "m/t")),
            lambda: self.store.entries(HomePath(self.root, "m")),
        ):
            self.assertEqual(self.refused(call).errno, errno.ELOOP)
        with (
            self.assertRaises(OSError) as caught,
            self.store.snapshot(HomePath(self.root, "a/t"), limit=10),
        ):
            pass
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_a_linked_root_is_followed(self):
        self.put("a/t", b"ok")
        linked = os.path.join(self.home, "linked-home")
        os.symlink(self.root, linked)
        self.assertEqual(self.store.read(HomePath(linked, "a/t")), b"ok")

    def test_persists_as_local(self):
        self.assertEqual(self.store.persisted(), {"kind": "local"})
        self.assertIsInstance(client_state.home_store({"kind": "local"}), client_state.LocalHome)


class VolumeHomeTest(StateCase):
    """VolumeHome: a Docker session's client home in its state volume; the root is not read."""

    def setUp(self):
        super().setUp()
        self.store = client_state.VolumeHome("s1", HOME_DIR)
        self.root = "/nowhere/home"

    def test_reads_through_the_reader_with_no_mac_file_opened(self):
        self.put("a/t", b"0123456789")
        os.utime(os.path.join(self.client, "a", "t"), (100, 200))
        path = HomePath(self.root, "a/t")
        self.assertEqual(self.store.read(path, offset=2, limit=3), b"234")
        self.assertEqual(self.store.read(path, limit=2, tail=True), b"89")
        self.assertEqual(self.store.stat(path), StateStat(10, 200))
        listed = self.store.entries(HomePath(self.root, "a")) or ()
        self.assertEqual([(e.name, e.kind) for e in listed], [("t", StateKind.FILE)])
        with self.store.snapshot(path, limit=100) as copy:
            assert copy is not None
            with open(copy, "rb") as f:
                self.assertEqual(f.read(), b"0123456789")
        self.assertIsNone(self.store.read(HomePath(self.root, "a/none")))

    def test_a_session_record_linked_to_etc_hosts_is_refused_and_writes_nothing(self):
        hosts = os.path.join(self.home, "etc", "hosts")  # /etc/hosts, inside the test sandbox
        os.makedirs(os.path.dirname(hosts))
        with open(hosts, "wb") as f:
            f.write(b"127.0.0.1 localhost\n")
        os.symlink(hosts, os.path.join(self.client, "a", "s.jsonl"))
        temp = os.path.join(self.home, "tmp")
        os.mkdir(temp)
        before = sorted(os.listdir(self.home))
        path = HomePath(self.root, "a/s.jsonl")
        with mock.patch.object(tempfile, "tempdir", temp):
            for call in (
                lambda: self.store.read(path),
                lambda: self.store.stat(path),
                lambda: self.store.snapshot(path, limit=1 << 20).__enter__(),
            ):
                with self.assertRaises(OSError) as caught:
                    call()
                self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(sorted(os.listdir(self.home)), before)
        self.assertEqual(os.listdir(temp), [])

    def test_persists_its_session_and_client(self):
        fields = self.store.persisted()
        self.assertEqual(fields, {"kind": "volume", "session": "s1", "client": HOME_DIR})
        self.assertEqual(client_state.home_store(fields).persisted(), fields)


class HomePathTest(Case):
    def test_a_path_below_the_root_is_its_rel(self):
        with (
            mock.patch("os.stat", side_effect=AssertionError),
            mock.patch("os.lstat", side_effect=AssertionError),
        ):
            found = client_state.home_path("/h/claude", "/h/claude/projects/p/s.jsonl")
            self.assertEqual(found, HomePath("/h/claude", "projects/p/s.jsonl"))
            self.assertEqual(
                client_state.home_path("/h/claude/", "/h/claude/./a//b"),
                HomePath("/h/claude", "a/b"),
            )

    def test_outside_the_root_is_refused(self):
        for path in ("/h/claude", "/h/claude2/x", "/h/claude/../x", "/elsewhere/x", "rel/x"):
            with self.subTest(path=path), self.assertRaises(OSError) as caught:
                client_state.home_path("/h/claude", path)
            self.assertEqual(caught.exception.errno, errno.EXDEV)


class HomeStoreFieldsTest(Case):
    def test_anything_but_a_persisted_store_is_refused(self):
        for fields in (
            None,
            [],
            {},
            {"kind": "other"},
            {"kind": "local", "extra": "x"},
            {"kind": "volume", "session": "s1"},
            {"kind": "volume", "session": "s1", "client": "a/b"},
            {"kind": "volume", "session": "..", "client": "claude"},
            {"kind": "volume", "session": 1, "client": "claude"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                client_state.home_store(fields)


class RecordedHomeTest(Case):
    def test_a_daemon_record_gives_the_sessions_volume(self):
        daemon.save_record("s1", DaemonRecord(LOCAL, DOCKER))
        found = client_state.recorded_home("s1", "claude")
        self.assertEqual(found.persisted(), {"kind": "volume", "session": "s1", "client": "claude"})

    def test_no_record_or_no_name_is_local(self):
        for name in ("s2", None):
            with self.subTest(name=name):
                found = client_state.recorded_home(name, "claude")
                self.assertEqual(found.persisted(), {"kind": "local"})

    def test_an_invalid_name_is_local_even_with_a_record_at_its_path(self):
        os.makedirs(os.path.join(self.ensemble, "docker-state", "a"))
        for name in ("../x", "a/b"):
            daemon.save_record(name, DaemonRecord(LOCAL, DOCKER))
        for name in ("../x", "", "a/b"):
            with self.subTest(name=name):
                found = client_state.recorded_home(name, "c")
                self.assertEqual(found.persisted(), {"kind": "local"})

    def test_a_malformed_record_is_refused(self):
        path = os.path.join(self.ensemble, "docker-state", "s1", daemon.RECORD_FILE)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{")
        with self.assertRaises(OSError):
            client_state.recorded_home("s1", "claude")
