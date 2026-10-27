"""Session behavior tests with caller-owned dependencies."""

import ast
import fcntl
import io
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest
import unittest.mock
import uuid
from contextlib import redirect_stderr

from ensemble.session.engine import types as engine_types
from ensemble.session.hosts.base import PROMPTED
from ensemble.session.modes import tui_hook
from ensemble.utils import StatusState
from tests.session.case import SRC, Case
from tests.session.tui_hook_support import (
    PROCESS,
    identity,
    install,
    state,
    write_state,
)
from tests.utils.process_fixtures import DEVNULL

CLIENT = re.compile(r"claude|codex", re.I)
HOOK = os.path.join(SRC["session"], "ensemble", "session", "modes", "tui_hook.py")
ROOTS = (SRC["session"], SRC["utils"])


class Append(Case):
    """append's hooks.jsonl: one record per write, rotated to hooks.jsonl.1 past HOOKS_MAX."""

    def setUp(self):
        self.path = os.path.join(self.home, tui_hook.HOOKS)
        self.rotated = os.path.join(self.home, tui_hook.ROTATED)
        self.pad = "x" * (tui_hook.HOOKS_MAX * 2 // 5)  # two records fit, a third rotates

    def lines(self, path):
        with open(path, "rb") as f:
            return [json.loads(line)["n"] for line in f.read().splitlines()]

    def test_a_record_past_the_cap_starts_a_new_file_and_the_old_becomes_dot_1(self):
        for n in range(4):
            tui_hook.append(self.path, {"n": n, "pad": self.pad})
        self.assertEqual((self.lines(self.rotated), self.lines(self.path)), ([0, 1], [2, 3]))
        for n in range(4, 7):
            tui_hook.append(self.path, {"n": n, "pad": self.pad})
        self.assertEqual((self.lines(self.rotated), self.lines(self.path)), ([4, 5], [6]))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_one_record_over_the_cap_still_goes_to_an_empty_file(self):
        tui_hook.append(self.path, {"n": 0, "pad": self.pad * 3})
        self.assertEqual((self.lines(self.path), os.path.exists(self.rotated)), ([0], False))
        tui_hook.append(self.path, {"n": 1})
        self.assertEqual((self.lines(self.rotated), self.lines(self.path)), ([0], [1]))

    def test_a_writer_whose_file_was_rotated_while_it_waited_writes_the_new_one(self):
        tui_hook.append(self.path, {"n": 0})
        flock, rotated = fcntl.flock, []

        def rotate_first(fd, operation):
            if not rotated:
                rotated.append(True)
                os.replace(self.path, self.rotated)
            flock(fd, operation)

        with unittest.mock.patch("fcntl.flock", rotate_first):
            tui_hook.append(self.path, {"n": 1})
        self.assertEqual((self.lines(self.rotated), self.lines(self.path)), ([0], [1]))


class InProcess(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.events = os.path.join(self.root, "events")
        self.tmux = install(os.path.join(self.root, "bin"), "")
        self.state = state(self.root, self.events)
        write_state(self.dir, self.state)
        self.lookup = f"{PROCESS}\t{identity(self.state)}\n"
        self.mark = unittest.mock.Mock(
            side_effect=[
                unittest.mock.Mock(returncode=0, stdout=self.lookup, stderr=""),
                unittest.mock.Mock(returncode=0, stdout="", stderr=""),
            ]
        )

    def faked(self, mode=None, *, executable=True):
        real_stat, real_access = os.stat, os.access

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path != self.tmux:
                return st
            return types.SimpleNamespace(st_mode=st.st_mode if mode is None else mode)

        def fake_access(path, how, *args, **kwargs):
            return executable if path == self.tmux else real_access(path, how, *args, **kwargs)

        with (
            unittest.mock.patch("os.stat", fake_stat),
            unittest.mock.patch("os.access", fake_access),
            unittest.mock.patch("subprocess.run", self.mark),
        ):
            tui_hook.main([self.dir, "tool"], io.BytesIO(b"{}"), roots=ROOTS)

    def test_a_non_regular_or_non_executable_tmux_is_refused_after_the_record(self):
        for case, kwargs in (
            ("directory", {"mode": stat.S_IFDIR | 0o755}),
            ("not executable", {"executable": False}),
        ):
            with (
                self.subTest(case),
                self.assertRaisesRegex(tui_hook.HookError, re.escape(self.tmux)),
            ):
                self.faked(**kwargs)
        self.mark.assert_not_called()
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    def test_one_write_under_an_exclusive_flock_then_the_mark(self):
        calls = unittest.mock.Mock()
        with (
            unittest.mock.patch("fcntl.flock", wraps=fcntl.flock) as flock,
            unittest.mock.patch("os.write", wraps=os.write) as write,
            unittest.mock.patch("subprocess.run", self.mark),
        ):
            calls.attach_mock(flock, "flock")
            calls.attach_mock(write, "write")
            calls.attach_mock(self.mark, "mark")
            tui_hook.main([self.dir, "tool"], io.BytesIO(b'{"tool_name": "Bash"}'), roots=ROOTS)
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            data = f.read()
        fd = calls.mock_calls[0].args[0]
        self.assertEqual(
            calls.mock_calls,
            [
                unittest.mock.call.flock(fd, fcntl.LOCK_EX),
                unittest.mock.call.write(fd, data),
                unittest.mock.call.mark(
                    [
                        self.tmux,
                        "display-message",
                        "-p",
                        "-t",
                        "=w1:",
                        "#{@process}\t#{@sid}\t#{@generation}",
                    ],
                    capture_output=True,
                    text=True,
                    stdin=DEVNULL,
                    timeout=5,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key not in ("TMUX", "TMUX_TMPDIR")
                        },
                        "TMUX_TMPDIR": self.root,
                    },
                ),
                unittest.mock.call.mark(
                    [self.tmux, "set-option", "-t", "%3", "@state", "working"],
                    capture_output=True,
                    text=True,
                    stdin=DEVNULL,
                    timeout=5,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key not in ("TMUX", "TMUX_TMPDIR")
                        },
                        "TMUX_TMPDIR": self.root,
                    },
                ),
            ],
        )
        self.assertEqual(json.loads(data)["payload"], {"tool_name": "Bash"})

    def test_read_state_of_no_regular_file_leaves_no_fd_open(self):
        path = os.path.join(self.dir, "session.json")
        os.unlink(path)
        for make in (lambda: os.mkdir(path), lambda: os.mkfifo(path)):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                before = len(os.listdir("/dev/fd"))
                for _ in range(5):
                    with self.assertRaises(tui_hook.HookError):
                        tui_hook.read_state(path)
                self.assertEqual(len(os.listdir("/dev/fd")), before)
            (os.rmdir if os.path.isdir(path) else os.unlink)(path)


class DeclaredRootCallback(Case):
    """A callback outside sys.path loads in this process from its declared root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)

    def run_hook(self, module, roots):
        state_ = {**state(self.root, None), "words": {}}
        del state_["host_hook"]
        write_state(
            self.dir, {**state_, "client_hook": {"module": module, "state": {}, "roots": roots}}
        )
        with redirect_stderr(io.StringIO()) as stderr:
            tui_hook.run(
                [self.dir, "stop"], types.SimpleNamespace(buffer=io.BytesIO(b"{}")), roots=ROOTS
            )
        with open(os.path.join(self.dir, "hooks.jsonl"), encoding="utf-8") as f:
            return stderr.getvalue(), [json.loads(line)["payload"] for line in f]

    def write(self, *parts, text="def on_hook(call):\n    return {'loaded': __name__}\n"):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_nested_namespace_callback_split_over_two_roots(self):
        top = f"ns_{uuid.uuid4().hex}"
        self.write("left", top, "nested", "other.py", text="")
        self.write("right", top, "nested", "callback.py")
        roots = [os.path.join(self.root, "left"), os.path.join(self.root, "right")]
        module = f"{top}.nested.callback"
        self.assertEqual(self.run_hook(module, roots), ("", [{"loaded": module}]))

    def test_a_callback_is_found_over_another_callbacks_roots(self):
        module, host = f"client_{uuid.uuid4().hex}", f"host_{uuid.uuid4().hex}"
        self.write("other", f"{module}.py")
        self.write("other", f"{host}.py", text="def on_hook(call):\n    pass\n")
        other = os.path.join(self.root, "other")
        state_ = {
            **state(self.root, None),
            "words": {},
            "client_hook": {"module": module, "state": {}, "roots": []},
            "host_hook": {"module": host, "state": {}, "roots": [other]},
        }
        write_state(self.dir, state_)
        with redirect_stderr(io.StringIO()) as stderr:
            tui_hook.run(
                [self.dir, "stop"], types.SimpleNamespace(buffer=io.BytesIO(b"{}")), roots=ROOTS
            )
        with open(os.path.join(self.dir, "hooks.jsonl"), encoding="utf-8") as f:
            payloads = [json.loads(line)["payload"] for line in f]
        self.assertEqual((stderr.getvalue(), payloads), ("", [{"loaded": module}]))

    def test_a_callback_loads_by_its_named_entry(self):
        module = f"entry_{uuid.uuid4().hex}"
        self.write(f"{module}.py", text="def on_event(call):\n    return None\n")
        specs = {module: tui_hook.find_module(module, [self.root], "handler", ValueError)}
        loaded = tui_hook.load_callback(module, "handler", specs, entry="on_event")
        self.assertTrue(callable(loaded.on_event))
        with self.assertRaisesRegex(engine_types.HookError, f"handler {module}: missing on_hook"):
            tui_hook.load_callback(module, "handler", specs)


class Callbacks(Case):
    """A session dir whose client callback tags the payload and whose Host callback fails once."""

    CLIENT = (
        "calls = []\n"
        "def on_hook(call):\n"
        "    calls.append(call)\n"
        "    return {**call.payload, 'seen': 1}\n"
    )
    HOST = (
        "calls, fail = [], [RuntimeError('host down')]\n"
        "def on_hook(word, call):\n"
        "    calls.append((word, call.payload))\n"
        "    if fail:\n"
        "        raise fail.pop()\n"
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        saved = {**state(self.root, None), "words": {"stop": "done"}}
        self.names = {}
        for kind, text in (("client", self.CLIENT), ("host", self.HOST)):
            name = self.names[kind] = f"{kind}_{uuid.uuid4().hex}"
            with open(os.path.join(self.root, f"{name}.py"), "w", encoding="utf-8") as f:
                f.write(text)
            saved[f"{kind}_hook"] = {"module": name, "state": {}, "roots": [self.root]}
        write_state(self.dir, saved)

    def calls(self, kind):
        return sys.modules[self.names[kind]].calls

    def run_hook(self, event, body=b'{"n": 1}'):
        stdin = types.SimpleNamespace(buffer=io.BytesIO(body))
        with redirect_stderr(io.StringIO()):
            return tui_hook.run([self.dir, "stop"], stdin, roots=ROOTS, event=event)

    def records(self):
        with open(os.path.join(self.dir, tui_hook.HOOKS), encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class RelayEvent(Callbacks):
    """A relay event's retry after its Host callback failed reruns only that callback."""

    def test_a_retried_event_journals_once_and_reruns_only_the_host(self):
        self.assertFalse(self.run_hook("g1.1"))
        self.assertTrue(self.run_hook("g1.1"))
        records = self.records()
        self.assertEqual(
            [(r["label"], r["payload"], r["event"]) for r in records],
            [("stop", {"n": 1, "seen": 1}, "g1.1")],
        )
        self.assertEqual(len(self.calls("client")), 1)
        self.assertEqual(self.calls("host"), [("done", {"n": 1, "seen": 1})] * 2)

    def test_another_event_or_none_journals_and_runs_both_callbacks(self):
        self.assertFalse(self.run_hook(None))
        for event in ("g1.1", "g1.2", None):
            self.assertTrue(self.run_hook(event))
        self.assertEqual([r.get("event") for r in self.records()], [None, "g1.1", "g1.2", None])
        self.assertEqual((len(self.calls("client")), len(self.calls("host"))), (4, 4))

    def test_a_record_carries_the_send_marker_read_before_it(self):
        self.run_hook(None)
        with open(os.path.join(self.dir, PROMPTED), "w", encoding="utf-8") as f:
            f.write("g1 2 1")
        self.run_hook(None)
        self.assertEqual([r.get("sent") for r in self.records()], [None, "g1 2 1"])

    def test_a_bad_event_is_a_usage_error_before_any_callback(self):
        for event in ("", "a b", "x" * 65):
            with self.subTest(event=event), self.assertRaisesRegex(tui_hook.HookError, "usage"):
                tui_hook.main([self.dir, "stop"], io.BytesIO(b"{}"), roots=ROOTS, event=event)
        self.assertNotIn(self.names["client"], sys.modules)
        self.assertFalse(os.path.exists(os.path.join(self.dir, tui_hook.HOOKS)))


class Serve(Callbacks):
    """serve: one reply line per request line, each request run as one hook call."""

    def serve(self, *lines):
        replies = io.BytesIO()
        with redirect_stderr(io.StringIO()):
            tui_hook.serve([self.dir], io.BytesIO(b"".join(lines)), replies, roots=ROOTS)
        return [json.loads(line) for line in replies.getvalue().splitlines()]

    def request(self, event, label="stop", payload=None, sent=None):
        found = {
            "label": label,
            "event": event,
            "payload": {"n": 1} if payload is None else payload,
            "sent": sent,
        }
        return json.dumps(found).encode() + b"\n"

    def test_a_record_carries_the_requests_send_count_not_the_files(self):
        with open(os.path.join(self.dir, PROMPTED), "w", encoding="utf-8") as f:
            f.write("g1 2 2")
        self.serve(self.request(None), self.request(None, sent="g1 1 1"))
        self.assertEqual([r.get("sent") for r in self.records()], [None, "g1 1 1"])

    def test_each_request_gets_its_reply_in_order_and_a_retry_journals_once(self):
        replies = self.serve(self.request("g1.1"), self.request("g1.1"), self.request(None))
        self.assertEqual(replies, [{"ok": False}, {"ok": True}, {"ok": True}])
        self.assertEqual([r.get("event") for r in self.records()], ["g1.1", None])
        self.assertEqual((len(self.calls("client")), len(self.calls("host"))), (2, 3))

    def test_a_malformed_request_replies_false_and_serving_goes_on(self):
        self.assertEqual(
            self.serve(self.request("g1.1")), [{"ok": False}]
        )  # the Host's one failure
        good = self.request("g1.2")
        for line in (
            b"junk\n",
            b"[]\n",
            b'{"label": "stop", "payload": {}}\n',
            b'{"label": 1, "event": null, "payload": {}}\n',
            b'{"label": "stop", "event": 2, "payload": {}}\n',
            b'{"label": "stop", "event": null, "payload": []}\n',
            b'{"label": "stop", "event": null, "payload": {}, "sent": null, "x": 1}\n',
            b'{"label": "stop", "event": null, "payload": {}, "sent": 1}\n',
            b'{"label": "stop", "event": null, "payload": {}}\n',
        ):
            with self.subTest(line=line):
                self.assertEqual(self.serve(line, good), [{"ok": False}, {"ok": True}])

    def test_a_call_that_fails_its_own_checks_replies_false(self):
        self.assertEqual(self.serve(self.request(None, label="a b")), [{"ok": False}])
        with open(os.path.join(self.dir, "hook.log"), encoding="utf-8") as f:
            self.assertIn("usage", f.read())

    def test_no_request_no_reply(self):
        self.assertEqual(self.serve(), [])


class Source(Case):
    def test_no_client_name(self):
        with open(HOOK) as f:
            self.assertEqual(CLIENT.findall(f.read()), [])


class WordTest(Case):
    def test_the_words_a_label_may_mark(self):
        self.assertEqual(tui_hook.MARKS, ("done", "blocked", "working"))
        check = tui_hook.SESSION_STATE["words"]
        self.assertTrue(check({"stop": StatusState.DONE, "tool": "working"}))
        for bad in ({"stop": "dead"}, {"stop": ["done"]}, {"stop": None}):
            with self.subTest(words=bad):
                self.assertFalse(check(bad))


class SharedTypes(Case):
    """The hook uses engine/types' HookError and MARKS."""

    def test_each_shared_hook_type_has_one_definition(self):
        package = os.path.join(SRC["session"], "ensemble", "session")
        definitions = {"HookError": []}
        for top, _, names in os.walk(package):
            for name in sorted(n for n in names if n.endswith(".py")):
                path = os.path.join(top, name)
                for node in ast.walk(ast.parse(self.source(path))):
                    if isinstance(node, ast.ClassDef) and node.name in definitions:
                        definitions[node.name].append(path)
        self.assertEqual(
            definitions,
            dict.fromkeys(definitions, [engine_types.__file__]),
        )

    def test_read_state_raises_the_error_it_is_given(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "session.json")
            with self.assertRaises(engine_types.HookError) as caught:
                tui_hook.read_state(path, error=engine_types.HookError)
            self.assertEqual(str(caught.exception), f"{path}: No such file or directory")
            with self.assertRaises(engine_types.HookError):
                tui_hook.read_state(path)

    def test_no_normal_session_module_takes_a_type_from_the_hook(self):
        classes = {"HookError", "MARKS"}
        package = os.path.join(SRC["session"], "ensemble", "session")
        found = []
        for top, _, names in os.walk(package):
            for name in sorted(n for n in names if n.endswith(".py")):
                path = os.path.join(top, name)
                if path != HOOK:
                    found += self.hook_types(path, classes)
        self.assertEqual(found, [])

    @staticmethod
    def source(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def hook_types(self, path: str, classes: set[str]) -> list[str]:
        tree, bound, found = ast.parse(self.source(path)), set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ensemble.session.modes":
                bound |= {a.asname or a.name for a in node.names if a.name == "tui_hook"}
            elif isinstance(node, ast.ImportFrom) and node.module == tui_hook.__name__:
                found += [f"{path}: {a.name}" for a in node.names if a.name in classes]
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in bound
                and node.attr in classes
            ):
                found.append(f"{path}:{node.lineno}: tui_hook.{node.attr}")
        return found
