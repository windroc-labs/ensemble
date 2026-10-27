"""hosts/docker/supervisor.py: each relay request checked against the shared protocol fixtures, its
generation, dir and seq, run by the resident hook worker, its reply and rejected line, the dead
watch, the credential tick and main's lifecycle on an inherited lock, with fake processes.
"""

import base64
import contextlib
import fcntl
import inspect
import io
import json
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker import maintenance, relay, supervisor
from ensemble.session.hosts.docker.supervisor import Supervisor
from ensemble.session.modes import tui_hook
from tests.session.case import REPO, SRC, Case
from tests.session.tui_hook_support import state as hook_state
from tests.session.tui_hook_support import write_state
from tests.session.unit.hosts.docker.support import FakeBroker, FakePopen
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

HOOK = "/plugin/bin/ensemble-session"
GEN = "0123456789abcdef0123456789abcdef"
FIXTURES = os.path.join(REPO, "src", "broker", "protocol", "docker-relay", "fixtures")
TUI = "/Users/example/.ensemble/tui/worker_1"
THREAD = threading.Thread  # MainTest patches threading.Thread
BROKER_FIXTURES = os.path.join("src", "broker", "protocol", "docker-broker", "fixtures")


class WorkerProc:
    """A `hook --serve` process: `serve(requests, replies)` in a thread over real pipes; exits 0
    when serve returns, or as kill() or exit() say.
    """

    def __init__(self, serve):
        (requests, self.stdin_fd), (self.stdout_fd, replies) = os.pipe(), os.pipe()
        self.stdin, self.stdout = os.fdopen(self.stdin_fd, "wb"), os.fdopen(self.stdout_fd, "rb")
        self.returncode, self.ended, self.lock = None, threading.Event(), threading.Lock()
        self.replies = replies
        self.thread = THREAD(target=self.run, args=(serve, requests), daemon=True)
        self.thread.start()

    def run(self, serve, requests):
        with os.fdopen(requests, "rb") as stream, os.fdopen(os.dup(self.replies), "wb") as out:
            if serve is not None:
                serve(stream, out)
                self.exit(0)
            self.ended.wait()

    def exit(self, code):
        with self.lock:
            if self.returncode is None:
                self.returncode = code
                os.close(self.replies)
                self.ended.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.ended.wait(timeout):
            raise TimeoutExpired("hook", timeout)
        return self.returncode

    def kill(self):
        self.exit(-9)


class WorkerPopen:
    """subprocess.Popen for the hook worker, counting launches: each pops the next of `script`, an
    OSError it raises, "deaf" a process that never reads, else a serve(requests, replies). Without
    one, a process whose answer(request) gives each reply: True or False replies ok, an int exits
    with that code unreplied, "hang" never replies.
    """

    def __init__(self, answer=None, script=()):
        self.answer, self.script, self.calls, self.procs = answer, list(script), [], []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        found = self.script.pop(0) if self.script else self.serve
        if isinstance(found, OSError):
            raise found
        self.procs.append(WorkerProc(None if found == "deaf" else found))
        return self.procs[-1]

    def serve(self, requests, replies):
        for line in requests:
            proc, outcome = self.procs[-1], self.answer(json.loads(line))
            if outcome == "hang":
                proc.ended.wait()
                return
            if outcome is not True and outcome is not False:
                proc.exit(outcome)
                return
            replies.write(json.dumps({"ok": outcome}).encode() + b"\n")
            replies.flush()


def request(body=b'{"session_id": "s", "transcript_path": "/x"}', *, label="Stop", **changes):
    found = {
        "id": 7,
        "gen": GEN,
        "seq": 1,
        "argv": ["hook", TUI, label],
        "payload": base64.b64encode(body).decode(),
        **changes,
    }
    return json.dumps(found).encode() + b"\n"


class Base(Case):
    def setUp(self):
        self.state = os.path.join(self.home, "state")
        os.makedirs(self.state)
        self.events = os.path.join(self.home, "events")
        open(self.events, "w").close()
        self.ran = []
        self.code = 0
        self.supervisor = Supervisor("worker_1", self.state, TUI, self.events, HOOK, generation=GEN)
        self.workers = self.supervisor.worker.popen = WorkerPopen(self.answer)
        self.addCleanup(self.end_worker)

    def answer(self, found):
        self.ran.append(found)
        return self.code or True

    def end_worker(self):
        if self.supervisor.worker.proc is not None:
            self.supervisor.worker.proc.stdin.close()

    def rejected(self):
        try:
            with open(os.path.join(self.state, "rejected.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class AuditWarningTest(Base):
    def test_audit_warning_is_visible_in_the_session_event_file(self):
        response = {
            "container": f"ensemble-worker_1-{GEN}",
            "next_check": None,
            "deadline": None,
            "results": [],
            "notices": [],
            "audit_warning": "audit_write_failed",
        }
        fake = FakeBroker((0, json.dumps(response), ""))
        self.supervisor.broker.runner = fake
        result = self.supervisor.broker.maintain(f"ensemble-worker_1-{GEN}", "/manifest", root=REPO)
        self.assertEqual(result.results, [])
        with open(self.events) as stream:
            event = stream.read()
        self.assertIn("worker_1 credential_audit_warning audit_write_failed:", event)
        self.assertIn("Do not repeat a successful start", event)
        self.assertEqual(len(fake.calls), 1)


class HandleTest(Base):
    def test_relay_fixtures_get_their_expected_reply(self):
        with open(os.path.join(FIXTURES, "index.json")) as f:
            found = [c for c in json.load(f)["cases"] if c["definition"] == "relay_request"]
        for case in found:
            with self.subTest(fixture=case["file"]):
                with open(os.path.join(FIXTURES, case["file"])) as f:
                    data = json.load(f)
                if "payload_bytes" in case:
                    data["payload"] = base64.b64encode(bytes(case["payload_bytes"])).decode()
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.supervisor.seq_file)
                line = json.dumps(data).encode() + b"\n"
                self.assertEqual(self.supervisor.handle(line), case["expected"]["reply"])

    def test_transcript_path_is_dropped_before_the_hook_runs(self):
        self.assertEqual(self.supervisor.handle(request()), {"gen": GEN, "seq": 1, "ok": True})
        self.assertEqual(
            self.ran,
            [{"label": "Stop", "event": f"{GEN}.1", "payload": {"session_id": "s"}, "sent": None}],
        )
        ((argv, kw),) = self.workers.calls
        self.assertEqual(argv, [HOOK, "hook", "--serve", TUI])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (PIPE, PIPE, DEVNULL))
        self.assertEqual(self.rejected(), [])

    def test_malformed_lines_are_rejected_unrun_with_no_gen_seq_or_content(self):
        for line in (
            b"not json\n",
            b"[]\n",
            request(id=True),
            request(extra=1),
            request(gen="X" * 32),
            request(seq=0),
            request(argv=["hook", TUI]),
            request(label="a b"),
            request(payload="not base64!"),
            request(b"[1]"),
            request(b"{"),
        ):
            with self.subTest(line=line[:60]):
                self.assertEqual(self.supervisor.handle(line), {"ok": False})
        self.assertEqual(self.ran, [])
        self.assertEqual(self.rejected(), [{"gen": None, "seq": None, "reason": "malformed"}] * 11)

    def test_old_generation_and_other_dirs_are_rejected_and_logged(self):
        old = "f" * 32
        for line, gen, reason in (
            (request(gen=old), old, "generation"),
            (
                request(argv=["hook", "/Users/example/.ensemble/tui/other", "Stop"]),
                GEN,
                "directory",
            ),
            (request(argv=["hook", "/Users/example/src/repo/x", "Stop"]), GEN, "directory"),
        ):
            with self.subTest(reason=reason, gen=gen):
                self.assertEqual(self.supervisor.handle(line), {"gen": gen, "seq": 1, "ok": False})
        self.assertEqual(self.ran, [])
        self.assertEqual(
            [r["reason"] for r in self.rejected()], ["generation", "directory", "directory"]
        )
        self.assertFalse(os.path.exists(self.supervisor.seq_file))

    def test_a_seen_seq_is_ok_without_running_the_hook(self):
        self.supervisor.handle(request(seq=2))
        self.assertEqual(len(self.ran), 1)
        for seq in (1, 2):
            with self.subTest(seq=seq):
                self.assertEqual(
                    self.supervisor.handle(request(seq=seq)), {"gen": GEN, "seq": seq, "ok": True}
                )
        self.assertEqual(len(self.ran), 1)
        self.supervisor.handle(request(seq=3))
        self.assertEqual(len(self.ran), 2)

    def test_seq_is_recorded_after_the_hook_runs(self):
        seen = []
        self.workers.answer = lambda _: (
            seen.append(os.path.exists(self.supervisor.seq_file)) or True
        )
        self.supervisor.handle(request(seq=4))
        with open(self.supervisor.seq_file) as f:
            self.assertEqual(f.read(), "4")
        self.assertEqual(seen, [False])

    def test_a_missing_or_corrupt_seq_file_counts_as_0(self):
        with open(self.supervisor.seq_file, "w") as f:
            f.write("junk")
        self.supervisor.handle(request(seq=1))
        self.assertEqual(len(self.ran), 1)

    def test_hook_failure_is_retried_logged_and_only_success_is_recorded(self):
        outcomes = iter([3, False, "hang"])
        self.workers.answer = lambda found: self.ran.append(found) or next(outcomes, True)
        self.workers.script = [self.workers.serve, OSError(2, "gone"), self.workers.serve]
        self.supervisor.worker.wait = 0.2
        for seq, reason in (
            (1, "hook exit 3"),
            (2, "hook error"),
            (3, "hook exit 1"),
            (4, "hook timeout"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.supervisor.handle(request(seq=seq)),
                    {"gen": GEN, "seq": seq, "ok": False, "retry": True},
                )
                self.assertEqual(self.rejected()[-1], {"gen": GEN, "seq": seq, "reason": reason})
                self.assertFalse(os.path.exists(self.supervisor.seq_file))
        self.assertEqual(len(self.ran), 3)
        self.assertEqual(self.supervisor.handle(request(seq=4))["ok"], True)
        self.assertEqual(len(self.ran), 4)
        self.assertEqual(self.supervisor.handle(request(seq=4))["ok"], True)
        self.assertEqual(len(self.ran), 4)
        self.assertEqual(self.supervisor.handle(request(seq=5))["ok"], True)
        self.assertEqual(len(self.ran), 5)

    def test_the_rejected_log_stops_at_its_cap(self):
        path = os.path.join(self.state, "rejected.jsonl")
        with open(path, "w") as f:
            f.write("x" * supervisor.LOG_MAX)
        self.supervisor.handle(b"bad\n")
        self.assertEqual(os.path.getsize(path), supervisor.LOG_MAX)


class HookReplayTest(Base):
    def pump(self, outcomes):
        queue = relay.Queue(os.path.join(self.home, "queue"))
        message = relay.relay_request(request())
        relay.answer(
            json.dumps({k: message[k] for k in ("id", "argv", "payload")}).encode() + b"\n",
            queue,
            GEN,
        )
        attempts, replies = [], []
        outcomes = list(outcomes)

        def launch(argv, **kw):
            if isinstance(outcomes[0], OSError):
                attempts.append(outcomes[0])
                raise outcomes.pop(0)
            return WorkerPopen.__call__(self.workers, argv, **kw)

        def answer(_request):
            outcome = outcomes.pop(0)
            attempts.append(outcome)
            if outcome == 0:
                SessionStatusLog(self.events).write("worker_1", "done")
            return outcome or True

        def write(line):
            replies.append(json.dumps(self.supervisor.handle(line)).encode() + b"\n")

        self.workers.answer, self.supervisor.worker.popen = answer, launch
        reader = mock.Mock()
        reader.readline.side_effect = lambda _: replies.pop(0) if replies else b""
        writer = mock.Mock()
        writer.write.side_effect = write
        with mock.patch("time.sleep") as sleep:

            def pause(_seconds):
                self.assertEqual(queue.first(0)[0], 1)
                if len(attempts) == 3:
                    reader.readline.side_effect = lambda _: b""

            sleep.side_effect = pause
            self.supervisor.worker.wait = 0.2
            relay.pump(queue, reader, writer, idle=0)
        return queue, attempts

    def test_hook_fails_once_then_delivers_one_manager_event_and_replays_harmlessly(self):
        queue, attempts = self.pump([3, 0])
        self.assertEqual(attempts, [3, 0])
        self.assertIsNone(queue.first(0))
        restarted = Supervisor("worker_1", self.state, TUI, self.events, HOOK, generation=GEN)
        restarted.worker.popen = WorkerPopen(self.answer)
        self.assertTrue(restarted.handle(request())["ok"])
        self.assertEqual(restarted.worker.popen.calls, [])
        with open(self.events) as stream:
            self.assertEqual([line.split()[1:] for line in stream], [["worker_1", "done"]])

    def test_persistent_hook_failures_remain_queued_and_log_each_reason(self):
        queue, attempts = self.pump([3, OSError(2, "gone"), "hang", 3])
        self.assertEqual(len(attempts), 4)
        self.assertEqual(queue.first(0)[0], 1)
        self.assertFalse(os.path.exists(self.supervisor.seq_file))
        self.assertEqual(
            [item["reason"] for item in self.rejected()],
            ["hook exit 3", "hook error", "hook timeout", "hook exit 3"],
        )
        with open(self.events) as stream:
            self.assertEqual(stream.read(), "")


class HookCommandTest(Base):
    def test_real_callback_failure_retries_then_delivers_once_through_the_cli(self):
        folder = os.path.join(self.home, "tui")
        os.mkdir(folder)
        saved = hook_state(self.home, self.events)
        saved["words"] = {"Stop": "done"}
        saved["host_hook"] = {
            "module": "ensemble.session.hosts.docker.hook",
            "roots": [],
            "state": {
                "docker": "/usr/local/bin/docker",
                "target": "worker_1",
                "generation": GEN,
                "container": f"ensemble-worker_1-{GEN}",
                "endpoint": "unix:///Users/a/.docker/run/docker.sock",
            },
        }
        write_state(folder, saved)
        self.supervisor = Supervisor(
            "worker_1", self.state, folder, self.events, HOOK, generation=GEN
        )
        roots = (SRC["session"], SRC["utils"])
        workers = self.supervisor.worker.popen = WorkerPopen(
            script=[
                lambda requests, replies: tui_hook.serve([folder], requests, replies, roots=roots)
            ]
        )
        popen = FakePopen(
            (b"", b"temporary docker failure", 1),
            (f"4242 $3 %7 4343\t{saved['sid']}\t{GEN}\n".encode(), b"", 0),
            (b"", b"", 0),
        )
        line = request(argv=["hook", folder, "Stop"])
        with (
            mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/Users/a")),
            mock.patch("subprocess.Popen", popen),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                self.supervisor.handle(line), {"gen": GEN, "seq": 1, "ok": False, "retry": True}
            )
            self.assertFalse(os.path.exists(self.supervisor.seq_file))
            self.assertTrue(self.supervisor.handle(line)["ok"])
            self.assertTrue(self.supervisor.handle(line)["ok"])
        self.assertEqual([argv for argv, _ in workers.calls], [[HOOK, "hook", "--serve", folder]])
        self.assertEqual(len(popen.calls), 3)
        with open(os.path.join(folder, "hooks.jsonl")) as stream:
            records = [json.loads(line) for line in stream]
        self.assertEqual([(r["label"], r["event"]) for r in records], [("Stop", f"{GEN}.1")])
        self.assertEqual(self.rejected()[-1]["reason"], "hook exit 1")
        with open(os.path.join(folder, "hook.log")) as stream:
            self.assertIn("temporary docker failure", stream.read())
        with open(self.events) as stream:
            self.assertEqual([line.split()[1:] for line in stream], [["worker_1", "done"]])


class HookWorkerTest(Base):
    """One resident `hook --serve` process for every call, bounded by its wait, replaced once it
    ends.
    """

    def call(self, n=1, body=None):
        body = {"n": n} if body is None else body
        return self.supervisor.worker.call("Stop", body, f"{GEN}.{n}", None)

    def test_n_calls_start_one_process(self):
        for n in range(1, 6):
            self.assertIsNone(self.call(n))
        self.assertEqual(len(self.workers.calls), 1)
        self.assertEqual(
            [found["event"] for found in self.ran], [f"{GEN}.{n}" for n in range(1, 6)]
        )

    def test_a_hung_call_is_killed_at_hook_wait_and_the_next_call_starts_another(self):
        self.code = "hang"
        self.supervisor.worker.wait = 0.2
        self.assertEqual(self.call(), "hook timeout")
        self.assertEqual(self.workers.procs[0].returncode, -9)
        self.code = 0
        self.assertIsNone(self.call(2))
        self.assertEqual(len(self.workers.calls), 2)

    def test_a_worker_that_never_reads_a_large_request_is_still_bounded(self):
        self.workers.script = ["deaf"]
        self.supervisor.worker.wait = 0.2
        self.assertEqual(self.call(body={"x": "y" * (1 << 20)}), "hook timeout")
        self.assertEqual(self.workers.procs[0].returncode, -9)

    def test_a_crash_is_its_exit_code_and_the_next_call_starts_another(self):
        self.code = 7
        self.assertEqual(self.call(), "hook exit 7")
        self.code = 0
        self.assertIsNone(self.call(2))
        self.assertEqual(len(self.workers.calls), 2)

    def test_a_failure_reply_is_exit_1_and_keeps_the_process(self):
        self.workers.answer = lambda found: self.ran.append(found) or False
        self.assertEqual(self.call(), "hook exit 1")
        self.assertEqual(self.call(2), "hook exit 1")
        self.assertEqual(len(self.workers.calls), 1)

    def test_a_worker_that_cannot_start_is_a_hook_error(self):
        self.workers.script = [OSError(2, "gone")]
        self.assertEqual(self.call(), "hook error")
        self.assertIsNone(self.call(2))
        self.assertEqual(len(self.workers.calls), 2)

    def test_stop_ends_the_process_at_its_stdin_eof_without_waiting_out_hook_wait(self):
        self.assertIsNone(self.call())
        proc = self.workers.procs[0]
        began = time.monotonic()
        self.supervisor.worker.stop()
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(self.supervisor.worker.proc)

    def test_a_worker_that_ended_between_calls_is_replaced_unreported(self):
        self.assertIsNone(self.call())
        self.workers.procs[0].kill()
        self.assertIsNone(self.call(2))
        self.assertEqual(len(self.workers.calls), 2)


class ServeTest(Base):
    def test_each_line_gets_one_ack_and_an_over_long_line_is_discarded(self):
        self.supervisor.line_max = 220
        requests = io.BytesIO(b"x" * 500 + b"\n" + request(b"{}") + b"bad\n")
        acks = io.BytesIO()
        self.supervisor.serve(requests, acks)
        self.assertEqual(
            [json.loads(line) for line in acks.getvalue().splitlines()],
            [{"ok": False}, {"gen": GEN, "seq": 1, "ok": True}, {"ok": False}],
        )
        self.assertEqual(len(self.ran), 1)

    def test_once_stopping_it_still_runs_received_hooks_but_writes_no_ack(self):
        self.supervisor.stopping = True
        acks = io.BytesIO()
        self.supervisor.serve(io.BytesIO(request() + request(seq=2)), acks)
        self.assertEqual((len(self.ran), acks.getvalue()), (2, b""))

    def test_a_closed_ack_stream_is_ignored(self):
        acks = io.BytesIO()
        acks.close()
        self.supervisor.serve(io.BytesIO(request()), acks)
        self.assertEqual(len(self.ran), 1)


class Stop:
    def __init__(self, *, stopped=False):
        self.stopped = stopped

    def is_set(self):
        return self.stopped

    def wait(self, _timeout):
        return self.stopped


class WatchTest(Base):
    def popen(self, out):
        def make(argv, **kw):
            self.ran.append((argv, kw))
            read, write = os.pipe()
            os.write(write, out)
            os.close(write)
            stdout = os.fdopen(read, "rb")
            self.addCleanup(stdout.close)
            return mock.MagicMock(stdout=stdout, __enter__=lambda s: s)

        return make

    def watch(self, *, codes, out=b"1\n", stopped=False, start=0.0):
        results = iter(codes)
        self.supervisor.run = lambda argv, **kw: (
            self.ran.append((argv, kw)) or SimpleNamespace(returncode=next(results))
        )
        self.supervisor.popen = self.popen(out)
        self.supervisor.watch(["wait"], ["check"], Stop(stopped=stopped), start)

    def events_text(self):
        with open(self.events) as f:
            return f.read()

    def test_a_dead_pane_writes_one_dead_line(self):
        self.watch(codes=[0])
        self.assertRegex(self.events_text(), r"^\d\d:\d\d:\d\d worker_1 dead\n$")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"]])

    def test_a_forged_signal_with_a_live_pane_writes_nothing_and_waits_again(self):
        self.watch(codes=[0, 1], out=b"0\n", start=60)
        self.assertEqual(self.events_text(), "")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"], ["wait"]])

    def test_a_failed_wait_is_retried_until_the_containers_tmux_starts(self):
        self.watch(codes=[1, 1, 0], start=60)
        self.assertRegex(self.events_text(), r"^\d\d:\d\d:\d\d worker_1 dead\n$")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"]] * 3 + [["check"]])

    def test_a_failed_wait_or_a_stop_writes_nothing(self):
        self.watch(codes=[1])
        self.watch(codes=[0], stopped=True)
        self.supervisor.run = mock.Mock(side_effect=OSError(2, "gone"))
        self.supervisor.watch(["wait"], ["check"], Stop(), 60)
        self.assertEqual(self.events_text(), "")

    def test_no_events_file_writes_nothing(self):
        self.supervisor.events = ""
        self.watch(codes=[0])
        self.assertEqual(self.events_text(), "")


class MainTest(Base):
    def main(self, relay, fd, *args):
        argv = ["w", self.state, TUI, "", HOOK, GEN, str(fd), '["relay"]', '["wait"]', '["check"]']
        workers = WorkerPopen(lambda _: True)
        self.addCleanup(lambda: [proc.stdin.close() for proc in workers.procs])
        launches = iter([relay])
        with (
            mock.patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: next(launches, None) or workers(*a, **kw),
            ) as popen,
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
            mock.patch("signal.signal") as handler,
            mock.patch("threading.Thread") as self.threads,
        ):
            code = supervisor.main(list(args) or argv)
        return code, popen, handler

    def lock(self):
        fd = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        return fd

    def test_main_runs_on_the_inherited_lock_and_ends_at_eof(self):
        fd = self.lock()
        fcntl.flock(fd, fcntl.LOCK_EX)
        relay = mock.Mock(stdout=io.BytesIO(request(b"{}")), stdin=io.BytesIO())
        code, popen, handler = self.main(relay, fd)
        self.assertEqual(code, 0)
        argv, kw = popen.call_args_list[0]
        self.assertEqual(argv[0], ["relay"])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (PIPE, PIPE, DEVNULL))
        self.assertFalse(os.path.exists(os.path.join(self.state, "supervisor.pid")))
        (signum, stop), _ = handler.call_args
        self.assertEqual(signum, signal.SIGTERM)
        stop(signal.SIGTERM, None)
        self.assertTrue(relay.stdin.closed)
        threads = {
            c.kwargs["target"].__name__: c.kwargs["args"] for c in self.threads.call_args_list
        }
        self.assertEqual(threads.keys(), {"watch", "maintain"})
        self.assertTrue(threads["maintain"][0].is_set())

    def test_an_unheld_lock_fd_or_bad_args_exit_nonzero(self):
        self.assertEqual(supervisor.main(["too", "few"]), 2)
        other = self.lock()
        fcntl.flock(other, fcntl.LOCK_EX)
        mine = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR)
        self.addCleanup(os.close, mine)
        code, popen, _ = self.main(mock.Mock(), mine)
        self.assertEqual(code, 1)
        code, _, _ = self.main(mock.Mock(), 9999)
        self.assertEqual(code, 1)
        popen.assert_not_called()


NOW = 1_792_238_400.0  # 2026-10-17T12:00:00Z
CONTAINER = f"ensemble-worker_1-{GEN}"
REASON = (
    "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login codex"
    " on the host, then resume."
)
NOTICE = {
    "key": "ensemble-broker-env/db@1:7d",
    "text": "ensemble-broker-env/db expires 2026-10-23 (explicit), in 6 days: rerun its login",
}


def at(seconds):
    return datetime.fromtimestamp(NOW + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def deadline(seconds):
    return {"at": at(seconds), "action": "close", "reason": REASON}


def maintained(next_check=None, until=None, results=(), notices=(), err=""):
    response = {
        "container": CONTAINER,
        "next_check": next_check,
        "deadline": until,
        "results": list(results),
        "notices": list(notices),
    }
    return 0, json.dumps(response), err


def failed(credential, message, **extra):
    error = {"code": "refresh_failed", "message": message, **extra}
    return {"credential": credential, "expires_at": None, "error": error}


CLOSED = (0, json.dumps({"container": CONTAINER, "removed": True}), "")
OFFLINE = OSError(2, "No such file or directory")


class MaintainTest(Base):
    """The shared tick, through the real Broker client over a fake broker process."""

    def setUp(self):
        super().setUp()
        self.now, self.waits = NOW, []
        self.supervisor.clock = lambda: self.now
        self.supervisor.hook = os.path.join(REPO, "bin", "ensemble-session")
        self.cache = maintenance.deadline_file(self.state, GEN)

    def run_ticks(self, *responses, ticks=20):
        fake = FakeBroker(*responses)
        self.supervisor.broker.runner = fake

        def wait(delay):
            self.waits.append(delay)
            if len(self.waits) > ticks or not fake.results:
                return True
            self.now += delay
            return False

        self.supervisor.maintain(SimpleNamespace(wait=wait))
        return [call[1:] for call in fake.calls]

    def lines(self):
        with open(self.supervisor.events) as stream:
            return [line.split(" ", 1)[1] for line in stream.read().splitlines()]

    def cached(self):
        with open(self.cache) as stream:
            return json.load(stream)

    def test_unknown_provider_is_scheduled_and_reported_by_metadata(self):
        calls = self.run_ticks(
            maintained(at(3000), deadline(3100), [failed("fake-x", "Fake renewal failed.")]),
            OFFLINE,
            OFFLINE,
            CLOSED,
        )
        self.assertEqual([c[0] for c in calls], ["maintain"] * 3 + ["close"])
        self.assertEqual(self.waits, [0.0, 3000.0, 60.0, 40.0])
        self.assertEqual(
            self.lines(),
            [
                "worker_1 credential_refresh_failed fake-x: Fake renewal failed.",
                "worker_1 credential_maintenance_failed transport",
                f"worker_1 client_auth_expired {REASON}",
            ],
        )
        self.assertEqual(self.cached(), deadline(3100))

    def test_tick_source_names_no_provider(self):
        source = "".join(
            inspect.getsource(f) for f in (maintenance, Supervisor.maintain, Supervisor._report)
        ).lower()
        for name in ("codex", "github", "claude", "opencode"):
            with self.subTest(name=name):
                self.assertNotIn(name, source)

    def test_outage_retries_then_enforces_cached_deadline(self):
        with open(self.cache, "w") as stream:
            json.dump(deadline(150), stream)
        failure = (16, "", "{")
        calls = self.run_ticks(OFFLINE, OFFLINE, failure, SessionError("busy"), CLOSED)
        self.assertEqual([c[0] for c in calls], ["maintain"] * 3 + ["close", "close"])
        self.assertEqual(self.waits, [0.0, 60.0, 60.0, 30.0, 60.0])
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "worker_1 credential_maintenance_failed transport")
        self.assertEqual(lines[1], f"worker_1 client_auth_expired {REASON}")

    def test_a_passed_cached_deadline_still_ticks_first(self):
        with open(self.cache, "w") as stream:
            json.dump(deadline(-100), stream)
        calls = self.run_ticks(maintained(at(3600), deadline(7200)), OFFLINE, ticks=1)
        self.assertEqual([c[0] for c in calls], ["maintain"])
        self.assertEqual(self.waits, [0.0, 3600.0])
        self.assertEqual((self.cached(), self.lines()), (deadline(7200), []))

    def test_a_passed_cached_deadline_closes_after_one_failed_tick(self):
        with open(self.cache, "w") as stream:
            json.dump(deadline(-100), stream)
        calls = self.run_ticks(OFFLINE, CLOSED)
        self.assertEqual([c[0] for c in calls], ["maintain", "close"])
        self.assertEqual(
            self.lines(),
            [
                "worker_1 credential_maintenance_failed transport",
                f"worker_1 client_auth_expired {REASON}",
            ],
        )

    def test_a_reply_without_a_deadline_keeps_the_confirmed_one(self):
        with open(self.cache, "w") as stream:
            json.dump(deadline(100), stream)
        calls = self.run_ticks(maintained(at(60)), OFFLINE, CLOSED)
        self.assertEqual([c[0] for c in calls], ["maintain", "maintain", "close"])
        self.assertEqual(self.waits, [0.0, 60.0, 40.0])
        self.assertEqual(self.cached(), deadline(100))

    def test_no_deadline_never_closes(self):
        calls = self.run_ticks(*[OFFLINE] * 6)
        self.assertEqual([c[0] for c in calls], ["maintain"] * 6)
        self.assertEqual(self.waits, [0.0] + [60.0] * 6)
        self.assertEqual(len(self.lines()), 1)

    def test_later_confirmed_deadline_replaces_cache(self):
        with open(self.cache, "w") as stream:
            json.dump(deadline(100), stream)
        calls = self.run_ticks(maintained(at(4000), deadline(5000)), OFFLINE, ticks=1)
        self.assertEqual([c[0] for c in calls], ["maintain"])
        self.assertEqual(self.waits, [0.0, 4000.0])
        self.assertEqual(self.cached(), deadline(5000))
        self.assertEqual(self.lines(), [])

    def test_a_bad_cache_is_no_deadline(self):
        for cached in ("{", '{"at": "x", "action": "close", "reason": "r"}', '{"at": 1}', "[]"):
            with self.subTest(cached=cached):
                with open(self.cache, "w") as stream:
                    stream.write(cached)
                self.waits = []
                calls = self.run_ticks(OFFLINE, OFFLINE, ticks=1)
                self.assertEqual([c[0] for c in calls], ["maintain"])

    def test_partial_result_through_broker_reaches_event_file(self):
        with open(os.path.join(REPO, BROKER_FIXTURES, "maintain-output.json")) as stream:
            response = json.load(stream)
        calls = self.run_ticks((0, json.dumps(response), ""), OFFLINE, ticks=1)
        self.assertEqual(
            calls[0], ["maintain", "--container", CONTAINER, "--manifest", self.manifest()]
        )
        self.assertEqual(
            self.lines(),
            ["worker_1 credential_refresh_failed codex: Renewal unavailable. (status 503)"],
        )
        self.assertEqual(self.cached(), response["deadline"])
        self.assertEqual(self.waits, [0.0, 60.0])

    def manifest(self):
        return os.path.join(self.state, f"{GEN}.json")

    def test_notice_reported_then_acked_at_once(self):
        calls = self.run_ticks(
            maintained(at(3600), notices=[NOTICE]), maintained(at(3600)), OFFLINE, ticks=2
        )
        self.assertEqual(calls[1][5:], ["--delivered", NOTICE["key"]])
        self.assertEqual(self.waits, [0.0, 0.0, 3600.0])
        self.assertEqual(self.lines(), [f"worker_1 credential_notice {NOTICE['text']}"])

    def test_unwritten_notice_is_not_acked(self):
        self.supervisor.events = self.state  # a directory: every write fails
        calls = self.run_ticks(
            maintained(at(60), notices=[NOTICE]), maintained(at(3600)), OFFLINE, ticks=2
        )
        self.assertEqual(len(calls[1]), 5)
        self.assertEqual(self.waits[:2], [0.0, 60.0])

    def test_whole_failure_reported_once_per_streak(self):
        self.run_ticks(OFFLINE, OFFLINE, maintained(at(60)), OFFLINE, OFFLINE, ticks=5)
        lines = [line for line in self.lines() if "credential_maintenance_failed" in line]
        self.assertEqual(len(lines), 2)

    def test_a_broker_error_keeps_its_message(self):
        error = {
            "exit_code": 3,
            "error": {
                "command": "maintain",
                "code": "manifest_rejected",
                "message": "Manifest rejected: maintain needs the container's manifest.",
                "cleanup": "not_needed",
            },
        }
        self.run_ticks((3, "", json.dumps(error)), OFFLINE, ticks=1)
        self.assertEqual(
            self.lines(),
            [
                "worker_1 credential_maintenance_failed broker: Manifest rejected: maintain"
                " needs the container's manifest."
            ],
        )

    def test_event_file_holds_only_the_brokers_message(self):
        sentinel = "SENTINEL-s3ss10n"
        message = f"Writing a GitHub token into {CONTAINER} failed."
        self.run_ticks(
            maintained(at(60), results=[failed("github", message)], err=sentinel), ticks=1
        )
        with open(self.supervisor.events) as stream:
            written = stream.read()
        self.assertNotIn(sentinel, written)
        self.assertEqual(self.lines(), [f"worker_1 credential_refresh_failed github: {message}"])

    def test_without_events_the_report_goes_to_the_log(self):
        self.supervisor.events = ""
        with self.assertLogs("session", logging.WARNING) as logged:
            self.run_ticks(maintained(at(60), notices=[NOTICE]), OFFLINE, ticks=1)
        self.assertIn(
            f"WARNING:session:docker session worker_1: credential_notice {NOTICE['text']}",
            logged.output,
        )
