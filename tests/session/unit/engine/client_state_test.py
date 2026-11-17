"""session/engine/client_state.py: locate's lexical mapping, and read, stat, entries and snapshot
through a session's reader (state_reader.py over its state volume, served by tests/session/state_volumes.py):
refusing links, names and paths outside a client home, absent paths, size limits and broken replies.
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
from ensemble.session.engine.types import DockerAccess, StateEntry, StateFile, StateKind, StateStat
from tests.session.case import Case
from tests.session.state_volumes import DOCKER, HOST, IMAGE, record, serve
from tests.session.unit.hosts.docker.support import ssh_wrapper

HOME_DIR = "app"
OPS = ("read", "stat", "entries", "snapshot")


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

    def test_refuses_a_linked_client_home(self):
        os.symlink(os.path.join(self.secret, HOME_DIR), os.path.join(self.state, "s1", "linked"))
        file = StateFile("s1", "linked", "a/t")
        for op in OPS:
            target = file._replace(rel="a") if op == "entries" else file
            with self.subTest(op=op):
                self.refused(op, target)

    def test_a_session_record_linked_outside_the_volume_writes_nothing_outside_the_temp_dir(self):
        os.symlink(os.path.join(self.secret, "t"), os.path.join(self.client, "a", "s.jsonl"))
        before = self.tree()
        file = StateFile("s1", HOME_DIR, "a/s.jsonl")
        for op in ("read", "stat", "snapshot"):
            with self.subTest(op=op):
                self.assertEqual(self.refused(op, file).errno, errno.ELOOP)
        listed = client_state.entries(StateFile("s1", HOME_DIR, "a")) or ()
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
            record("s1")
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
            client_state.snapshot(StateFile("s1", HOME_DIR, "a/x.db"), limit=100) as path,
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


class ReaderTest(StateCase):
    def test_one_reader_serves_many_calls_with_no_mac_file_opened(self):
        path = self.put("a/t", b"abc")
        file = StateFile("s1", HOME_DIR, "a/t")
        for _ in range(5):
            self.assertEqual(client_state.read(file), b"abc")
            self.assertEqual(client_state.stat(file), StateStat(3, os.stat(path).st_mtime))
        self.assertEqual(len(self.volumes.readers), 1)
        runs = [c for c in self.volumes.calls if c[3] == "run"]
        self.assertEqual(len(runs), 1)

    def test_the_reader_runs_confined_on_the_records_daemon(self):
        self.put("a/t", b"abc")
        client_state.read(StateFile("s1", HOME_DIR, "a/t"))
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
                    data = client_state.read(StateFile("s1", HOME_DIR, f"a/{name}"))
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
        file = StateFile("s1", HOME_DIR, "a/t")
        self.assertIsNone(client_state.read(file))
        self.assertIsNone(client_state.stat(file))
        self.assertIsNone(client_state.entries(file._replace(rel="a")))
        with client_state.snapshot(file, limit=10) as path:
            self.assertIsNone(path)
        self.assertFalse([c for c in self.volumes.calls if c[3] == "run"])

    def test_no_worker_image_is_refused(self):
        self.volumes.images = "ensemble-worker:latest\n"
        self.put("a/t")
        self.assertEqual(self.refused("read", StateFile("s1", HOME_DIR, "a/t")).errno, errno.EIO)

    def test_an_unreachable_daemon_is_refused(self):
        self.volumes.inspect_error = "Cannot connect to the Docker daemon"
        self.put("a/t")
        self.assertEqual(self.refused("read", StateFile("s1", HOME_DIR, "a/t")).errno, errno.EIO)

    def test_a_reader_that_exited_is_replaced_on_the_next_call(self):
        self.put("a/t", b"abc")
        file = StateFile("s1", HOME_DIR, "a/t")
        self.assertEqual(client_state.read(file), b"abc")
        self.volumes.readers[0].kill()
        self.assertEqual(client_state.read(file), b"abc")
        self.assertEqual(len(self.volumes.readers), 2)

    def test_a_reader_that_leaves_mid_request_is_replaced_and_asked_again(self):
        self.put("a/t", b"abc")
        file = StateFile("s1", HOME_DIR, "a/t")
        self.assertEqual(client_state.read(file), b"abc")
        self.volumes.readers[0].leave = True
        self.assertEqual(client_state.read(file), b"abc")
        self.assertEqual(len(self.volumes.readers), 2)

    def test_a_volume_with_no_owner_is_refused(self):
        self.volumes.owner = ""
        self.put("a/t")
        self.assertEqual(self.refused("read", StateFile("s1", HOME_DIR, "a/t")).errno, errno.EIO)
        self.assertFalse(self.volumes.readers)

    def test_a_large_read_streams_within_its_time(self):
        path = self.put("big")
        os.truncate(path, client_state.READ_MAX)
        started = time.monotonic()
        self.assertEqual(
            len(client_state.read(StateFile("s1", HOME_DIR, "big")) or b""), client_state.READ_MAX
        )
        self.assertLess(time.monotonic() - started, client_state.REPLY_TIMEOUT)

    def test_a_silent_reader_times_out_and_is_stopped(self):
        self.put("a/t", b"abc")
        file = StateFile("s1", HOME_DIR, "a/t")
        self.assertEqual(client_state.read(file), b"abc")
        self.volumes.readers[0].hang = True
        clock = iter([0.0, *[client_state.REPLY_TIMEOUT + 1.0] * 4])
        with mock.patch("time.monotonic", lambda: next(clock)):
            self.assertEqual(self.refused("read", file).errno, errno.ETIMEDOUT)
        self.assertTrue(self.volumes.readers[0].killed)
        self.assertEqual(client_state.read(file), b"abc")


class AccessRecordTest(Case):
    def setUp(self):
        self.client = os.path.join(self.ensemble, "docker-state", "s1", HOME_DIR)
        os.makedirs(self.client)
        with open(os.path.join(self.client, "t"), "wb") as f:
            f.write(b"ok")
        self.volumes = self.enterContext(serve(sessions=()))

    def test_calls_use_the_sessions_recorded_docker_and_daemon(self):
        daemon.save_access("s1", DockerAccess("/opt/docker", "ssh://pc"))
        with ssh_wrapper():
            self.assertEqual(client_state.read(StateFile("s1", HOME_DIR, "t")), b"ok")
        self.assertTrue(self.volumes.calls)
        for call in self.volumes.calls:
            self.assertEqual(call[:3], ["/opt/docker", "--host", "ssh://pc"])

    def test_no_record_is_absent(self):
        self.assertIsNone(client_state.read(StateFile("s1", HOME_DIR, "t")))
        self.assertFalse(self.volumes.calls)

    def test_a_malformed_record_is_refused(self):
        path = os.path.join(self.ensemble, "docker-state", "s1", daemon.ACCESS_FILE)
        for text in (
            "[]",
            '{"docker": "docker", "host": "unix:///s"}',
            '{"docker": "/d", "host": "tcp://x:2375"}',
            '{"docker": "/d", "host": "unix:///s", "extra": "x"}',
            "{",
        ):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.subTest(text=text), self.assertRaises(OSError) as caught:
                client_state.read(StateFile("s1", HOME_DIR, "t"))
            self.assertEqual(caught.exception.errno, errno.EINVAL)


class BrokenReplyTest(StateCase):
    def reply(self, header: dict | None = None, payload: bytes = b"", *, raw: bytes | None = None):
        base = {"errno": None, "message": "", "size": 0, "mtime": 0.0, "entries": []}
        if raw is None:
            found = {**base, **(header or {})}
            found.setdefault("length", len(payload))
            raw = json.dumps(found).encode() + b"\n" + payload
        self.volumes.reply = raw

    def assert_broken(self, op: str, file: StateFile):
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
        file = StateFile("s1", HOME_DIR, "a/t")
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
            client_state.read(StateFile("s1", HOME_DIR, "a/t"), limit=3)
        self.assertEqual(caught.exception.errno, errno.EPROTO)

    def test_a_header_with_no_end_is_refused_past_its_bound(self):
        self.reply(raw=b"{" + b" " * (client_state.HEADER_MAX + 1))
        self.assert_broken("read", StateFile("s1", HOME_DIR, "a/t"))

    def test_entry_names_that_are_not_one_part_are_refused(self):
        for name in ("../x", "a/b", "..", "", "x\u0000"):
            with self.subTest(name=name):
                self.reply({"entries": [[name, "file", 1, 0.0]]})
                self.assert_broken("entries", StateFile("s1", HOME_DIR, "a"))

    def test_snapshot_members_other_than_the_requested_files_are_refused(self):
        file = StateFile("s1", HOME_DIR, "a/x.db")
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
                with self.assertRaises(OSError) as caught, client_state.snapshot(file, limit=10):
                    pass
                self.assertEqual(caught.exception.errno, errno.EPROTO)
        self.assertFalse(os.path.lexists(os.path.join(self.home, "x.db")))
        self.assertEqual(sorted(os.listdir(tempfile.gettempdir())), before)
