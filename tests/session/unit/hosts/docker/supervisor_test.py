"""hosts/docker/supervisor.py: each relay request checked against the shared protocol fixtures, its
generation, dir and seq, run as the host hook, its reply and rejected line, the dead watch, the
GitHub token refresher and main's lifecycle on an inherited lock, with fake processes.
"""

import base64
import contextlib
import fcntl
import io
import json
import os
import signal
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.session import cli
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker import relay, supervisor
from ensemble.session.hosts.docker.broker import Broker
from ensemble.session.hosts.docker.supervisor import Supervisor
from tests.session.case import REPO, Case
from tests.session.tui_hook_support import state as hook_state
from tests.session.tui_hook_support import write_state
from tests.session.unit.hosts.docker.support import FakeBroker, FakePopen
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

HOOK = "/plugin/bin/ensemble-session"
GEN = "0123456789abcdef0123456789abcdef"
FIXTURES = os.path.join(REPO, "src", "broker", "protocol", "docker-relay", "fixtures")
TUI = "/Users/example/.ensemble/tui/worker_1"


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
        self.supervisor = Supervisor(
            "worker_1", self.state, TUI, self.events, HOOK, generation=GEN, run=self.hook_run
        )

    def hook_run(self, argv, **kw):
        self.ran.append((argv, kw))
        return SimpleNamespace(returncode=self.code)

    def rejected(self):
        try:
            with open(os.path.join(self.state, "rejected.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class RefreshCapabilityTest(Base):
    def test_audit_warning_is_visible_in_the_session_event_file(self):
        response = {
            "container": f"ensemble-worker_1-{GEN}",
            "github": [],
            "credential_expires_at": None,
            "audit_warning": "audit_write_failed",
        }
        fake = FakeBroker((0, json.dumps(response), ""))
        self.supervisor.broker.runner = fake
        result = self.supervisor.broker.refresh(f"ensemble-worker_1-{GEN}", "/manifest")
        self.assertEqual(result.github, [])
        with open(self.events) as stream:
            event = stream.read()
        self.assertIn("worker_1 credential_audit_warning audit_write_failed:", event)
        self.assertIn("Do not repeat a successful start", event)
        self.assertEqual(len(fake.calls), 1)

    def test_only_a_true_capability_enables_refresh(self):
        path = os.path.join(self.state, "host.json")
        self.assertFalse(supervisor.refreshes_client(self.state))
        for record, expected in (
            ({"credential_refresh": True}, True),
            ({"credential_refresh": False}, False),
            ({"client": "agent"}, False),
            ({"client": "agent", "credential_refresh": None}, False),
            ({"client": "agent", "credential_refresh": 1}, False),
            ({"client": "agent", "credential_refresh": "true"}, False),
            ([], False),
            (None, False),
            (True, False),
        ):
            with self.subTest(record=record):
                with open(path, "w") as stream:
                    json.dump(record, stream)
                self.assertEqual(supervisor.refreshes_client(self.state), expected)
        with open(path, "w") as stream:
            stream.write("{")
        self.assertFalse(supervisor.refreshes_client(self.state))


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
        ((argv, kw),) = self.ran
        self.assertEqual(argv, [HOOK, "hook", "--strict", TUI, "Stop"])
        self.assertEqual(json.loads(kw["input"]), {"session_id": "s"})
        self.assertEqual(kw["timeout"], supervisor.HOOK_WAIT)
        self.assertEqual(kw["stdout"], kw["stderr"])
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
        def run(argv, **kw):
            self.assertFalse(os.path.exists(self.supervisor.seq_file))
            return self.hook_run(argv, **kw)

        self.supervisor.run = run
        self.supervisor.handle(request(seq=4))
        with open(self.supervisor.seq_file) as f:
            self.assertEqual(f.read(), "4")

    def test_a_missing_or_corrupt_seq_file_counts_as_0(self):
        with open(self.supervisor.seq_file, "w") as f:
            f.write("junk")
        self.supervisor.handle(request(seq=1))
        self.assertEqual(len(self.ran), 1)

    def test_hook_failure_is_retried_logged_and_only_success_is_recorded(self):
        self.code = 3
        for seq, error, reason in (
            (1, None, "hook exit 3"),
            (2, OSError(2, "gone"), "hook error"),
            (3, TimeoutExpired(HOOK, 60), "hook timeout"),
        ):
            with self.subTest(reason=reason):
                if error is not None:
                    self.supervisor.run = mock.Mock(side_effect=error)
                self.assertEqual(
                    self.supervisor.handle(request(seq=seq)),
                    {"gen": GEN, "seq": seq, "ok": False, "retry": True},
                )
                self.assertEqual(self.rejected()[-1], {"gen": GEN, "seq": seq, "reason": reason})
                self.assertFalse(os.path.exists(self.supervisor.seq_file))
        self.supervisor.run = self.hook_run
        self.code = 0
        self.assertEqual(self.supervisor.handle(request(seq=3))["ok"], True)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(self.supervisor.handle(request(seq=3))["ok"], True)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(self.supervisor.handle(request(seq=4))["ok"], True)
        self.assertEqual(len(self.ran), 3)

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
        outcomes = iter(outcomes)

        def run(_argv, **_kw):
            outcome = next(outcomes)
            attempts.append(outcome)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == 0:
                SessionStatusLog(self.events).write("worker_1", "done")
            return SimpleNamespace(returncode=outcome)

        def write(line):
            replies.append(json.dumps(self.supervisor.handle(line)).encode() + b"\n")

        self.supervisor.run = run
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
            relay.pump(queue, reader, writer, idle=0)
        return queue, attempts

    def test_hook_fails_once_then_delivers_one_manager_event_and_replays_harmlessly(self):
        queue, attempts = self.pump([3, 0])
        self.assertEqual(attempts, [3, 0])
        self.assertIsNone(queue.first(0))
        restarted = Supervisor(
            "worker_1", self.state, TUI, self.events, HOOK, generation=GEN, run=self.supervisor.run
        )
        self.assertTrue(restarted.handle(request())["ok"])
        with open(self.events) as stream:
            self.assertEqual([line.split()[1:] for line in stream], [["worker_1", "done"]])

    def test_persistent_hook_failures_remain_queued_and_log_each_reason(self):
        queue, attempts = self.pump([3, OSError(2, "gone"), TimeoutExpired(HOOK, 60), 3])
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
        self.supervisor.tui_dir = folder
        calls = []

        def invoke(argv, **options):
            calls.append(argv)
            stdin = SimpleNamespace(buffer=io.BytesIO(options["input"]))
            with mock.patch("sys.stdin", stdin):
                return SimpleNamespace(returncode=cli.main(argv[1:]))

        self.supervisor.run = invoke
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
        self.assertEqual(calls, [[HOOK, "hook", "--strict", folder, "Stop"]] * 2)
        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(self.rejected()[-1]["reason"], "hook exit 1")
        with open(os.path.join(folder, "hook.log")) as stream:
            self.assertIn("temporary docker failure", stream.read())
        with open(self.events) as stream:
            self.assertEqual([line.split()[1:] for line in stream], [["worker_1", "done"]])


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


class Waits:
    """A stop event set after `n` waits; records each timeout."""

    def __init__(self, n):
        self.n, self.timeouts = n, []

    def wait(self, timeout):
        self.timeouts.append(timeout)
        return len(self.timeouts) > self.n


class TokenBroker:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def refresh(self, container, manifest):
        self.calls.append((container, manifest))
        found = self.results.pop(0)
        if isinstance(found, Exception):
            raise found
        return SimpleNamespace(github=found, credential_expires_at=None)


NOW = 1_792_238_400.0  # 2026-10-17T12:00:00Z


def expiry(seconds, account="acme"):
    at = datetime.fromtimestamp(NOW + seconds, UTC)
    return {"account": account, "expires_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}


class RefreshTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.state, f"{GEN}.github.json")
        self.supervisor.clock = lambda: NOW

    def refresh(self, saved, *results, waits=1):
        if saved is not None:
            with open(self.path, "w") as f:
                json.dump(saved, f)
        self.supervisor.broker = fake = TokenBroker(*results)
        stop = Waits(waits)
        self.supervisor.refresh(stop)
        return fake, stop.timeouts

    def saved(self):
        with open(self.path) as f:
            return json.load(f)

    def test_no_expiry_file_or_an_empty_list_never_refreshes(self):
        for saved in (None, [], "corrupt"):
            with self.subTest(saved=saved):
                fake, timeouts = self.refresh(saved)
                self.assertEqual((fake.calls, timeouts), ([], []))

    def test_it_refreshes_ten_minutes_before_the_earliest_expiry_and_saves_the_new_list(self):
        new = [expiry(3600)]
        fake, timeouts = self.refresh([expiry(3000, "b"), expiry(2400, "a")], new)
        container = f"ensemble-worker_1-{GEN}"
        self.assertEqual(fake.calls, [(container, os.path.join(self.state, f"{GEN}.json"))])
        self.assertEqual(timeouts, [1800.0, 3000.0])
        self.assertEqual(self.saved(), new)

    def test_a_past_due_expiry_refreshes_at_once(self):
        _, timeouts = self.refresh([expiry(60)], [expiry(3600)])
        self.assertEqual(timeouts[0], 0.0)

    def test_a_failure_writes_the_event_and_retries_every_minute(self):
        new = [expiry(3600)]
        error = SessionError("broker: GitHub request failed: GitHub answered HTTP 500.")
        fake, timeouts = self.refresh([expiry(600)], error, error, new, waits=3)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(timeouts, [0.0, 60.0, 60.0, 3000.0])
        with open(self.events) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(
                line,
                r"^\d\d:\d\d:\d\d worker_1 github_refresh_failed broker: GitHub request failed:",
            )
        self.assertEqual(self.saved(), new)

    def test_without_an_events_file_a_failure_still_retries(self):
        self.supervisor.events = ""
        fake, timeouts = self.refresh([expiry(600)], SessionError("x"), [], waits=2)
        self.assertEqual((len(fake.calls), timeouts), (2, [0.0, 60.0]))
        self.assertEqual(self.saved(), [])

    def test_a_stop_before_the_due_time_never_refreshes(self):
        fake, timeouts = self.refresh([expiry(3600)], waits=0)
        self.assertEqual((fake.calls, timeouts), ([], [3000.0]))

    def test_github_retries_across_expiry_and_recovers(self):
        now, waits = NOW, []
        self.supervisor.clock = lambda: now

        def wait(delay):
            nonlocal now
            waits.append(delay)
            if len(waits) > 12:
                return True
            now += delay
            return False

        with open(self.path, "w") as stream:
            json.dump([expiry(3600)], stream)
        renewed = [expiry(7260)]
        self.supervisor.broker = fake = TokenBroker(*([SessionError("offline")] * 11), renewed)
        self.supervisor.refresh(SimpleNamespace(wait=wait))
        self.assertEqual(now, NOW + 3660)
        self.assertEqual(waits, [3000] + [60] * 11 + [3000])
        self.assertEqual(len(fake.calls), 12)
        self.assertEqual(self.saved(), renewed)
        with open(self.events) as stream:
            lines = stream.read().splitlines()
        self.assertEqual(len(lines), 11)
        self.assertTrue(
            all(line.endswith("worker_1 github_refresh_failed offline") for line in lines)
        )


class MainTest(Base):
    def main(self, relay, fd, *args):
        argv = ["w", self.state, TUI, "", HOOK, GEN, str(fd), '["relay"]', '["wait"]', '["check"]']
        with (
            mock.patch("subprocess.Popen", return_value=relay) as popen,
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
        self.assertEqual(threads.keys(), {"watch", "refresh"})
        self.assertTrue(threads["refresh"][0].is_set())

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


class CodexRenewalTest(Base):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.supervisor.clock = lambda: self.now
        self.credential = os.path.join(self.state, f"{GEN}.credential.json")
        self.github = os.path.join(self.state, f"{GEN}.github.json")
        with open(os.path.join(self.state, "host.json"), "w") as stream:
            json.dump({"client": "codex", "generation": GEN, "credential_refresh": True}, stream)
        with open(self.github, "w") as stream:
            json.dump([], stream)
        self.supervisor.broker = mock.Mock()
        self.waits = []

    def deadline(self, seconds):
        with open(self.credential, "w") as stream:
            json.dump(expiry(seconds)["expires_at"], stream)

    def stop(self, count):
        def wait(delay):
            self.waits.append(delay)
            if len(self.waits) > count:
                return True
            self.now += delay
            return False

        return SimpleNamespace(wait=wait)

    def renewed(self, client=3600, github=3600):
        self.deadline(client)
        return SimpleNamespace(
            github=[expiry(github)], credential_expires_at=expiry(client)["expires_at"]
        )

    def test_earliest_client_or_github_deadline_schedules_refresh(self):
        for client, github, delay in ((1800, 3600, 1200), (3600, 1800, 1200)):
            with self.subTest(client=client):
                self.now, self.waits = NOW, []
                self.deadline(client)
                with open(self.github, "w") as stream:
                    json.dump([expiry(github)], stream)
                self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed(7200, 7200)
                self.supervisor.refresh(self.stop(1))
                self.assertEqual(self.waits, [delay, 5400])
                self.supervisor.broker.close.assert_not_called()

    def test_wire_refresh_keeps_both_credentials_valid_beyond_two_hours(self):
        self.deadline(3600)
        with open(self.github, "w") as stream:
            json.dump([expiry(3000)], stream)
        deadlines, calls = [NOW + 3600, NOW + 3000], []

        def run(argv, **_kwargs):
            self.assertEqual(argv[1], "refresh")
            self.assertLess(self.now, min(deadlines))
            calls.append(self.now)
            offset = self.now - NOW
            self.deadline(offset + 3600)
            deadlines[:] = [self.now + 3600, self.now + 4200]
            response = {
                "container": f"ensemble-worker_1-{GEN}",
                "github": [expiry(offset + 4200)],
                "credential_expires_at": expiry(offset + 3600)["expires_at"],
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")

        self.supervisor.broker = Broker(run)
        self.supervisor.refresh(self.stop(3))
        self.assertEqual(calls, [NOW + 2400, NOW + 5400, NOW + 8400])
        self.assertEqual(self.waits, [2400, 3000, 3000, 3000])
        self.assertGreater(self.now, NOW + 7200)
        self.assertGreater(min(deadlines), self.now)
        with open(self.github) as stream:
            self.assertEqual(json.load(stream), [expiry(12600)])
        with open(self.events) as stream:
            self.assertEqual(stream.read(), "")

    def test_wire_failure_retries_and_recovers_beyond_initial_expiry(self):
        for code, exit_code in (("refresh_failed", 16), ("github_failed", 15)):
            with self.subTest(code=code):
                self.now, self.waits = NOW, []
                self.deadline(3600)
                with open(self.github, "w") as stream:
                    json.dump([expiry(3600)], stream)
                with open(self.events, "w"):
                    pass
                calls = []

                def run(argv, *, calls=calls, code=code, exit_code=exit_code, **_kwargs):
                    self.assertEqual(argv[1], "refresh")
                    calls.append(self.now)
                    if len(calls) == 1:
                        error = {
                            "command": "refresh",
                            "code": code,
                            "message": "Renewal unavailable",
                            "cause": "Retry the broker",
                            "cleanup": "not_needed",
                        }
                        return SimpleNamespace(
                            returncode=exit_code,
                            stdout="",
                            stderr=json.dumps({"exit_code": exit_code, "error": error}),
                        )
                    offset = self.now - NOW
                    self.deadline(offset + 3600)
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "container": f"ensemble-worker_1-{GEN}",
                                "github": [expiry(offset + 3600)],
                                "credential_expires_at": expiry(offset + 3600)["expires_at"],
                            }
                        ),
                        stderr="",
                    )

                self.supervisor.broker = Broker(run)
                self.supervisor.refresh(self.stop(3))
                self.assertEqual(calls, [NOW + 3000, NOW + 3060, NOW + 6060])
                self.assertEqual(self.waits, [3000, 60, 3000, 3000])
                with open(self.github) as stream:
                    self.assertEqual(json.load(stream), [expiry(9660)])
                with open(self.events) as stream:
                    lines = stream.read().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(
                    lines[0].endswith(
                        "worker_1 client_refresh_failed broker: Renewal unavailable (Retry the broker)"
                    )
                )

    def test_expired_recovery_reconciles_before_closing(self):
        self.deadline(-1)
        self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed()
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 3000])
        self.supervisor.broker.close.assert_not_called()

    def test_failure_retries_until_expiry_then_full_event_and_log_and_close(self):
        self.deadline(100)
        self.supervisor.broker.refresh.side_effect = SessionError("offline")
        self.supervisor.broker.close.side_effect = [SessionError("offline"), False]
        with self.assertLogs("session", level="WARNING") as logged:
            self.supervisor.refresh(self.stop(4))
        reason = "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login codex on the host, then resume"
        self.assertEqual(self.waits, [0, 60, 40, 60])
        self.assertEqual(self.supervisor.broker.refresh.call_count, 3)
        self.assertEqual(self.supervisor.broker.close.call_count, 2)
        with open(self.events) as stream:
            lines = stream.read().splitlines()
        self.assertEqual(lines[-1].split(" ", 1)[1], f"worker_1 client_auth_expired {reason}")
        self.assertIn(
            f"WARNING:session:docker session worker_1: client_auth_expired {reason}", logged.output
        )

    def test_failure_event_carries_the_brokers_message_and_cause(self):
        self.deadline(100)
        error = {
            "command": "refresh",
            "code": "refresh_failed",
            "message": "Writing Codex credentials into the container failed",
            "cleanup": "not_needed",
            "cause": "bootstrap: cannot write managed Codex credentials: No space left on device",
        }
        reply = (16, "", json.dumps({"exit_code": 16, "error": error}))
        self.supervisor.broker = Broker(FakeBroker(reply, reply))
        self.supervisor.refresh(self.stop(1))
        with open(self.events) as stream:
            lines = stream.read().splitlines()
        self.assertEqual(
            lines[0].split(" ", 1)[1],
            "worker_1 client_refresh_failed broker: Writing Codex credentials into the container"
            " failed (bootstrap: cannot write managed Codex credentials: No space left on device)",
        )

    def test_partial_publication_survives_lost_reply_and_github_failure(self):
        self.deadline(30)

        def partial(*_args):
            self.deadline(120)
            raise SessionError("GitHub unavailable")

        self.supervisor.broker.refresh.side_effect = partial
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 60])
        self.supervisor.broker.close.assert_not_called()

    def test_missing_client_retains_full_fallback_event_and_log(self):
        with open(os.path.join(self.state, "host.json"), "w") as stream:
            json.dump({"credential_refresh": True}, stream)
        self.deadline(-1)
        self.supervisor.broker.refresh.side_effect = SessionError("offline")
        with self.assertLogs("session", level="WARNING") as logged:
            self.supervisor.refresh(self.stop(1))
        reason = "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login for this client on the host, then resume"
        with open(self.events) as stream:
            lines = stream.read().splitlines()
        self.assertEqual(lines[-1].split(" ", 1)[1], f"worker_1 client_auth_expired {reason}")
        self.assertIn(
            f"WARNING:session:docker session worker_1: client_auth_expired {reason}", logged.output
        )
        self.supervisor.broker.close.assert_called_once_with(f"ensemble-worker_1-{GEN}")

    def test_lost_reply_with_long_publication_retries_after_one_minute(self):
        self.deadline(30)

        def partial(*_args):
            self.deadline(3600)
            raise SessionError("response lost")

        self.supervisor.broker.refresh.side_effect = partial
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 60])
        self.supervisor.broker.close.assert_not_called()

    def test_successful_short_lifetime_has_no_busy_loop(self):
        self.deadline(30)
        self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed(120)
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 60])

    def test_missing_or_bad_deadline_reconciles_and_fails_closed(self):
        for value in (None, "bad", []):
            with self.subTest(value=value):
                self.waits = []
                with open(self.credential, "w") as stream:
                    json.dump(value, stream)
                self.supervisor.broker.refresh.side_effect = SessionError("unavailable")
                self.supervisor.broker.close.reset_mock()
                self.supervisor.refresh(self.stop(1))
                self.supervisor.broker.close.assert_called_once_with(f"ensemble-worker_1-{GEN}")

    def test_stop_cancels_due_refresh(self):
        self.deadline(30)
        self.supervisor.refresh(self.stop(0))
        self.supervisor.broker.refresh.assert_not_called()
        self.supervisor.broker.close.assert_not_called()
        self.assertEqual(self.waits, [0])
