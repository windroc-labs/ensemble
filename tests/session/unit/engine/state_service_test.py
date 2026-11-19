"""session/engine/state_service.py: the reader service's answers through the fake Docker of
tests/session/state_volumes.py: request checks, one container per owner mounting every read
session, restarts, losses and broken replies, and its socket path.
"""

import errno
import hashlib
import json
import os
import select
import threading
import time
from unittest import mock

from ensemble.session.engine import daemon, state_reader, state_service
from ensemble.session.engine.state_service import Service
from tests.session.case import Case
from tests.session.state_volumes import DOCKER, IMAGE, LOCAL, OWNER, record, serve, stored

SESSIONS = ("s1", "v", "w")


def line(session: str, request: dict, image: str = IMAGE) -> bytes:
    return json.dumps({"session": session, "image": image, "request": request}).encode() + b"\n"


def read(session: str, limit: int = 1 << 20) -> dict:
    return {"op": "read", "path": f"{session}/app/t", "offset": 0, "limit": limit, "tail": False}


def parsed(reply: bytes) -> tuple[dict, bytes]:
    head, _, payload = reply.partition(b"\n")
    found = json.loads(head)
    assert found["length"] == len(payload), found
    return found, payload


def mounts(run: list[str]) -> list[str]:
    return [run[i + 1] for i, arg in enumerate(run) if arg == "--mount"]


def mount(session: str) -> str:
    return f"type=volume,source=ensemble-state-{session},target=/state/{session},readonly,volume-nocopy"


class ServiceCase(Case):
    def setUp(self):
        self.volumes = self.enterContext(serve(SESSIONS))
        self.service = Service(LOCAL.id)
        self.addCleanup(self.service.close)
        for session in SESSIONS:
            os.makedirs(stored(session, "app"))
            with open(os.path.join(stored(session, "app"), "t"), "wb") as f:
                f.write(session.encode())

    def ask(self, session: str, request: dict | None = None, **kw) -> tuple[dict, bytes]:
        return parsed(self.service.answer(line(session, request or read(session), **kw)))

    def errno_of(self, session: str, request: dict | None = None, **kw) -> int | None:
        return self.ask(session, request, **kw)[0]["errno"]

    def runs(self) -> list[list[str]]:
        return [call for call in self.volumes.calls if call[3] == "run"]

    def alive(self) -> list:
        return [reader for reader in self.volumes.readers if not reader.killed]

    def wait_for(self, check) -> None:
        deadline = time.monotonic() + 10
        while not check():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)


class RequestTest(ServiceCase):
    def test_bad_requests_are_refused_before_any_docker_call(self):
        good = {"session": "s1", "image": IMAGE, "request": read("s1")}
        snapshot = {"op": "snapshot", "paths": ["s1/app/t", "v/app/t"], "limit": 10}
        for bad in (
            b"x\n",
            b"[]\n",
            json.dumps({**good, "extra": 1}).encode(),
            json.dumps({**good, "session": "../x"}).encode(),
            json.dumps({**good, "image": "latest"}).encode(),
            json.dumps({**good, "request": []}).encode(),
            line("s1", {"op": "chmod", "path": "s1/app/t"}),
            line("s1", read("v")),
            line("s1", {**read("s1"), "path": "s1"}),
            line("s1", {**read("s1"), "path": 3}),
            line("s1", snapshot),
            line("s1", {"op": "snapshot", "paths": [], "limit": 10}),
            line("s1", read("s1", limit=state_service.READ_MAX + 1)),
            line("s1", {**read("s1"), "limit": True}),
        ):
            with self.subTest(line=bad):
                self.assertEqual(parsed(self.service.answer(bad))[0]["errno"], errno.EINVAL)
        self.assertEqual(self.volumes.calls, [])

    def test_no_record_is_absent_with_no_docker_call(self):
        self.assertEqual(self.errno_of("zz"), errno.ENOENT)
        self.assertEqual(self.volumes.calls, [])

    def test_a_record_on_another_daemon_is_refused(self):
        record("x", LOCAL._replace(id="daemon-b"))
        self.assertEqual(self.errno_of("x"), errno.EINVAL)
        self.assertEqual(self.volumes.calls, [])

    def test_an_absent_or_foreign_volume_is_absent_and_starts_nothing(self):
        self.volumes.volumes = {"v", "w"}
        self.volumes.foreign = {"v"}
        self.assertEqual(self.errno_of("s1"), errno.ENOENT)
        self.assertEqual(self.errno_of("v"), errno.ENOENT)
        self.assertEqual(self.runs(), [])

    def test_a_volume_with_no_owner_is_an_io_error(self):
        self.volumes.owner = ""
        head, _ = self.ask("s1")
        self.assertEqual((head["errno"], head["message"]), (errno.EIO, "state volume has no owner"))
        self.assertEqual(self.runs(), [])

    def test_a_failing_docker_call_is_an_io_error_naming_it(self):
        self.volumes.error = "Cannot connect to the Docker daemon"
        head, _ = self.ask("s1")
        self.assertEqual(head["errno"], errno.EIO)
        self.assertEqual(head["message"], "docker volume ls: Cannot connect to the Docker daemon")
        self.assertEqual(self.runs(), [])

    def test_a_refused_session_leaves_wanted(self):
        self.volumes.volumes = {"v"}
        self.assertEqual(self.errno_of("s1"), errno.ENOENT)
        self.volumes.owner, self.volumes.owners = "", {"v": OWNER}
        self.volumes.volumes = None
        self.assertEqual(self.errno_of("w"), errno.EIO)
        self.assertEqual(self.service.wanted, {})
        self.assertEqual(self.ask("v")[1], b"v")
        (run,) = self.runs()
        self.assertEqual(mounts(run), [mount("v")])

    def test_two_waiting_requests_keep_their_session_wanted_until_both_exit(self):
        self.volumes.hold = threading.Event()
        found: dict[int, bytes] = {}
        targets = ("s1", "v", "v")

        def ask(index: int) -> None:
            found[index] = self.ask(targets[index])[1]

        threads = [threading.Thread(target=ask, args=(i,)) for i in range(len(targets))]
        threads[0].start()
        self.wait_for(lambda: self.volumes.calls)
        for thread in threads[1:]:
            thread.start()
        self.wait_for(lambda: self.service.wanted == {"s1": 1, "v": 2})
        self.volumes.hold.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(found, {0: b"s1", 1: b"v", 2: b"v"})
        self.assertEqual(self.service.wanted, {})
        (run,) = self.runs()
        self.assertEqual(mounts(run), [mount("s1"), mount("v")])


class SharedReaderTest(ServiceCase):
    def test_two_sessions_share_one_reader_with_both_mounts(self):
        self.assertEqual(self.ask("s1")[1], b"s1")
        self.assertEqual(self.ask("v")[1], b"v")
        for _ in range(3):
            self.assertEqual(self.ask("s1")[1], b"s1")
            self.assertEqual(self.ask("v")[1], b"v")
        first, second = self.runs()
        self.assertEqual(mounts(first), [mount("s1")])
        self.assertEqual(mounts(second), [mount("s1"), mount("v")])
        self.assertEqual(len(self.alive()), 1)

    def test_sessions_with_two_owners_get_a_reader_each(self):
        self.volumes.owners = {"v": "2001:2002"}
        self.assertEqual(self.ask("s1")[1], b"s1")
        self.assertEqual(self.ask("v")[1], b"v")
        self.assertEqual(self.ask("s1")[1], b"s1")
        first, second = self.runs()
        self.assertEqual(first[first.index("--user") + 1], OWNER)
        self.assertEqual(second[second.index("--user") + 1], "2001:2002")
        self.assertEqual((mounts(first), mounts(second)), ([mount("s1")], [mount("v")]))
        self.assertEqual(len(self.alive()), 2)

    def test_concurrent_first_reads_coalesce_into_one_start(self):
        self.volumes.hold = threading.Event()
        found, errors = {}, []

        def ask(index: int) -> None:
            session = SESSIONS[index % 3]
            try:
                found[index] = self.ask(session)[1] == session.encode()
            except AssertionError as error:
                errors.append(error)

        threads = [threading.Thread(target=ask, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        self.wait_for(lambda: set(self.service.wanted) == set(SESSIONS))
        self.volumes.hold.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual((errors, found), ([], dict.fromkeys(range(10), True)))
        (run,) = self.runs()
        self.assertEqual(mounts(run), [mount(s) for s in sorted(SESSIONS)])

    def test_the_reader_runs_confined_on_the_records_daemon(self):
        self.ask("s1")
        (run,) = self.runs()
        self.assertEqual(run[:3], [DOCKER, "--host", LOCAL.endpoint])
        for flag in (
            ["--label", "ensemble.state-reader=1"],
            ["--log-driver", "none"],
            ["--network", "none"],
            ["--cap-drop", "ALL"],
            ["--security-opt", "no-new-privileges"],
            ["--user", OWNER],
            ["--mount", mount("s1")],
            ["--entrypoint", state_service.IMAGE_PYTHON],
        ):
            self.assertTrue(any(run[i : i + 2] == flag for i in range(len(run))), f"{flag} {run}")
        self.assertIn("--read-only", run)
        self.assertIn("--rm", run)
        self.assertEqual(run[run.index(IMAGE) + 1 : run.index(IMAGE) + 3], ["-I", "-c"])
        with open(state_service.READER, encoding="utf-8") as f:
            self.assertEqual(run[run.index(IMAGE) + 3 :], [f.read(), "/state"])

    def test_the_requests_image_runs_the_reader(self):
        image = "sha256:" + "e" * 64
        self.assertEqual(self.ask("s1", image=image)[1], b"s1")
        (run,) = self.runs()
        self.assertIn(image, run)


class LossTest(ServiceCase):
    def test_a_reader_that_exited_is_replaced_on_the_next_request(self):
        self.ask("s1")
        self.volumes.readers[0].kill()
        self.assertEqual(self.ask("s1")[1], b"s1")
        self.assertEqual(len(self.runs()), 2)

    def test_a_reader_that_leaves_mid_request_is_replaced_and_asked_again(self):
        self.ask("s1")
        self.volumes.readers[0].leave = True
        self.assertEqual(self.ask("s1")[1], b"s1")
        self.assertEqual(len(self.runs()), 2)

    def test_a_new_reader_that_leaves_answers_epipe(self):
        self.volumes.leave = True
        head, _ = self.ask("s1")
        self.assertEqual((head["errno"], head["message"]), (errno.EPIPE, "state reader gone"))
        self.assertEqual(len(self.runs()), 1)

    def test_a_silent_reader_times_out_and_is_stopped(self):
        self.ask("s1")
        self.volumes.readers[0].hang = True
        wait = select.select

        def short(readable, writable, broken, timeout=None):
            return wait(readable, writable, broken, min(timeout or 0.05, 0.05))

        with mock.patch("select.select", short):
            self.assertEqual(self.errno_of("s1"), errno.ETIMEDOUT)
        self.assertTrue(self.volumes.readers[0].killed)
        self.assertEqual(self.ask("s1")[1], b"s1")

    def test_broken_replies_stop_the_reader(self):
        base = {"errno": None, "message": "", "size": 0, "mtime": 0.0, "entries": []}
        for raw in (
            b"{}\n",
            b"not json\n",
            json.dumps({**base, "length": 10}).encode() + b"\n" + b"x" * 10,
            b"x" * (state_service.HEADER_MAX + 1),
        ):
            with self.subTest(raw=raw[:40]):
                self.volumes.reply = raw
                self.assertEqual(self.errno_of("s1", read("s1", limit=3)), errno.EPROTO)
                self.assertTrue(self.volumes.readers[-1].killed)

    def test_reset_stops_the_reader_mounting_the_session(self):
        self.ask("s1")
        self.ask("w")
        self.assertEqual(self.errno_of("v", {"op": "reset"}), None)
        self.assertFalse(self.volumes.readers[-1].killed)
        self.assertEqual(self.errno_of("s1", {"op": "reset"}), None)
        self.assertEqual(self.alive(), [])

    def test_after_a_loss_only_requested_sessions_are_mounted(self):
        self.ask("s1")
        self.ask("v")
        self.volumes.readers[-1].kill()
        self.assertEqual(self.ask("v")[1], b"v")
        self.assertEqual(mounts(self.runs()[-1]), [mount("v")])

    def test_a_mounted_session_whose_volume_is_gone_is_dropped_at_the_next_start(self):
        self.ask("s1")
        self.volumes.volumes = {"v", "w"}
        self.assertEqual(self.ask("v")[1], b"v")
        self.assertEqual(mounts(self.runs()[-1]), [mount("v")])

    def test_close_stops_every_reader(self):
        self.volumes.owners = {"v": "2001:2002"}
        self.ask("s1")
        self.ask("v")
        self.service.close()
        self.assertEqual(self.alive(), [])


class SocketPathTest(Case):
    def test_keyed_by_daemon_and_code_below_state_readers(self):
        key = hashlib.sha256(b"daemon-a").hexdigest()[:16]
        folder = os.path.join(self.ensemble, "state-readers")
        digest = state_service.code_digest()
        self.assertRegex(digest, r"\A[0-9a-f]{8}\Z")
        self.assertEqual(
            state_service.socket_path("daemon-a"), os.path.join(folder, f"{key}-{digest}.sock")
        )
        self.assertEqual(
            state_service.lock_path("daemon-a"), os.path.join(folder, f"{key}-{digest}.lock")
        )
        self.assertNotEqual(
            state_service.socket_path("daemon-b"), state_service.socket_path("daemon-a")
        )

    def test_the_code_digest_covers_the_service_reader_and_daemon_code(self):
        found = hashlib.sha256()
        for module in (state_service, state_reader, daemon):
            with open(os.path.abspath(module.__file__ or ""), "rb") as f:
                found.update(f.read())
        self.assertEqual(state_service.code_digest(), found.hexdigest()[:8])

    def test_a_path_over_the_af_unix_limit_is_refused(self):
        long = os.path.join(self.home, "x" * 100)
        with mock.patch.dict(os.environ, {"HOME": long}), self.assertRaises(OSError) as caught:
            state_service.socket_path("daemon-a")
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertIn("over the 103-byte AF_UNIX limit", caught.exception.strerror)


class FramesTest(Case):
    def test_a_header_over_its_bound_or_an_end_is_refused(self):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        os.write(write_end, b"x" * 10)
        frames = state_service.Frames(read_end, 1, "peer")
        with self.assertRaises(OSError) as caught:
            frames.line(5)
        self.assertEqual(caught.exception.errno, errno.EPROTO)
        os.close(write_end)
        with self.assertRaises(BrokenPipeError):
            frames.take(20)

    def test_header_refuses_anything_but_a_reply_header(self):
        for text in (b"", b"[]", b'{"errno": -1}'):
            with self.subTest(text=text), self.assertRaises(OSError) as caught:
                state_service.header(text)
            self.assertEqual(caught.exception.errno, errno.EPROTO)
