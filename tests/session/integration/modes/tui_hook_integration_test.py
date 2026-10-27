"""modes/tui_hook.py as a client runs it: `ensemble-session hook DIR LABEL`."""

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr

from ensemble.session.modes import tui_hook
from tests.session.case import SRC
from tests.session.tui_hook_support import (
    COMMAND,
    HookBase,
    PayloadCases,
    RecordCases,
    descriptor,
    write_state,
)

MISSING = object()
ROOTS = (SRC["session"], SRC["utils"])
RUN_HOOK = """import runpy, sys
def run_hook(*arguments):
    sys.argv = ["ensemble-session", "hook", *arguments]
    try:
        runpy.run_module("ensemble.session.cli", run_name="__main__", alter_sys=True)
    except SystemExit as error:
        assert error.code == 0, error.code
"""


def python(case, argv, **kwargs):
    """`argv` on this tree's session packages, case.env its env."""
    env = {**case.env, "PYTHONPATH": os.pathsep.join(ROOTS), **kwargs.pop("env", {})}
    return subprocess.run(argv, capture_output=True, env=env, **kwargs)


class ClientErrorTest(HookBase):
    def test_disabled_logging_reports_exactly_one_error_line_and_exits_zero(self):
        script = RUN_HOOK + "import logging\nlogging.disable(logging.CRITICAL)\nrun_hook()\n"
        result = python(self, [sys.executable, "-P", "-s", "-c", script], input=b"{}", check=True)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith(b"tui_hook: usage: ensemble-session hook "))

    def test_client_error_is_one_stderr_line_without_a_record(self):
        callback = os.path.join(self.root, "client.py")
        with open(callback, "w") as file:
            file.write('def on_hook(call):\n    raise RuntimeError("first\\nsecond")\n')
        self.state["client_hook"] = self.callback("client")
        write_state(self.dir, self.state)
        self.assertEqual(
            self.hook("stop", {"text": "payload"}), "tui_hook: RuntimeError: first second\n"
        )
        self.assertNothingWritten()


class ErrorLoggingTest(HookBase):
    def test_package_import_failures_keep_a_traceback_and_one_error_line(self):
        package = os.path.join(self.root, "broken_package")
        os.mkdir(package)
        initializer = os.path.join(package, "__init__.py")
        with open(initializer, "w") as file:
            file.write('raise RuntimeError("first\\nsecond")\n')
        with open(os.path.join(package, "callback.py"), "w") as file:
            file.write("def on_hook(*args): pass\n")
        write_state(
            self.dir, {**self.state, "client_hook": self.callback("broken_package.callback")}
        )
        self.assertEqual(self.hook("stop"), "tui_hook: RuntimeError: first second\n")
        with open(os.path.join(self.dir, "hook.log")) as file:
            trace = file.read()
        self.assertEqual(trace.count("Traceback (most recent call last):"), 1)
        self.assertIn(initializer, trace)
        self.assertIn("RuntimeError: first\nsecond\n", trace)
        self.assertNothingWritten()

    def test_client_failures_append_full_tracebacks_without_a_record(self):
        callback = os.path.join(self.root, "client.py")
        with open(callback, "w") as file:
            file.write('def on_hook(call):\n    raise RuntimeError("first\\nsecond")\n')
        self.state["client_hook"] = self.callback("client")
        write_state(self.dir, self.state)
        error_log = os.path.join(self.dir, "hook.log")
        for count in (1, 2):
            self.assertEqual(self.hook("stop"), "tui_hook: RuntimeError: first second\n")
            with open(error_log) as file:
                trace = file.read()
            self.assertEqual(trace.count("Traceback (most recent call last):"), count)
            self.assertEqual(trace.count("RuntimeError: first\nsecond\n"), count)
            self.assertIn(callback, trace)
            self.assertIn("in on_hook", trace)
            self.assertNothingWritten()

    def test_unusable_or_invalid_session_dirs_keep_one_stderr_line(self):
        for folder in (
            os.path.join(self.root, "missing"),
            os.path.join(self.dir, "session.json"),
            "relative",
        ):
            with self.subTest(folder=folder):
                self.assertError(self.hook("stop", args=[folder, "stop"]), "tui_hook")
                self.assertFalse(os.path.exists(os.path.join(folder, "hook.log")))
        self.assertNothingWritten()

    def test_unusable_log_file_keeps_one_stderr_line(self):
        os.mkdir(os.path.join(self.dir, "hook.log"))
        os.unlink(os.path.join(self.dir, "session.json"))
        self.assertError(self.hook("stop"), "session.json")
        self.assertNothingWritten()

    def test_failed_file_write_keeps_one_stderr_line(self):
        script = (
            RUN_HOOK
            + """import builtins, io, os
from unittest import mock
class BrokenFile(io.StringIO):
    def write(self, value):
        raise OSError("disk full")
folder = sys.argv[1]
real_open = builtins.open
def open_file(name, *args, **kwargs):
    if os.fspath(name) == os.path.join(folder, "hook.log"):
        return BrokenFile()
    return real_open(name, *args, **kwargs)
with mock.patch("builtins.open", side_effect=open_file):
    run_hook(folder, "stop")
"""
        )
        os.unlink(os.path.join(self.dir, "session.json"))
        result = python(
            self, [sys.executable, "-P", "-s", "-c", script, self.dir], input=b"{}", check=True
        )
        self.assertEqual(result.stdout, b"")
        self.assertError(result.stderr.decode(), "session.json")
        self.assertNothingWritten()

    def test_success_keeps_the_journal_without_an_error_log(self):
        self.assertEqual(self.hook("stop", {"text": "success"}), "")
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "hook.log")))
        self.assertEqual(self.records()[0]["payload"], {"text": "success"})


class PayloadTest(PayloadCases, HookBase):
    def test_a_payload_of_64_mib_is_read(self):
        data = b'{"a": "' + b"x" * ((64 << 20) - 9) + b'"}'
        self.assertEqual(len(data), 64 << 20)
        callback = os.path.join(self.root, "client.py")
        with open(callback, "w") as file:
            file.write(
                'def on_hook(call):\n    return {**call.payload, "length": len(call.payload["a"])}\n'
            )
        self.state["client_hook"] = self.callback("client")
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", data), "")
        self.assertEqual(
            [r["payload"] for r in self.records()], [{"a": "x" * 4096, "length": (64 << 20) - 9}]
        )

    def test_overflow_drains_stdin_and_still_journals(self):
        stream = io.BytesIO(b"x" * (tui_hook.PAYLOAD_MAX + 1))
        with redirect_stderr(io.StringIO()) as stderr:
            tui_hook.main([self.dir, "stop"], stream, roots=ROOTS)
        self.assertEqual(
            stderr.getvalue(), f"tui_hook: payload: over {tui_hook.PAYLOAD_MAX} bytes\n"
        )
        self.assertEqual(stream.read(), b"")
        self.assertEqual(self.records()[0]["payload"], {})

    def test_client_callback_receives_full_payload_before_truncation_and_can_skip(self):
        callback = os.path.join(self.root, "client.py")
        with open(callback, "w") as file:
            file.write(
                """def on_hook(call):
    if call.label == "ask":
        return None
    return {**call.payload, "original_length": len(call.payload.get("text", ""))}
"""
            )
        self.state["client_hook"] = self.callback("client")
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", {"text": "x" * 5000}), "")
        record = self.records()[0]
        self.assertEqual(record["payload"], {"text": "x" * 4096, "original_length": 5000})
        self.assertEqual(self.hook("ask", {"text": "skip"}), "")
        self.assertEqual(len(self.records()), 1)

    def test_callback_module_requires_regular_file_with_on_hook(self):
        callback = os.path.join(self.root, "client.py")
        with open(callback, "w") as file:
            file.write("value = 1\n")
        os.mkdir(os.path.join(self.root, "namespace"))
        for name in (
            "relative.py/",
            callback,
            "absent",
            "namespace",
            "sys",
            "client",
        ):
            with self.subTest(module=name):
                write_state(self.dir, {**self.state, "client_hook": self.callback(name)})
                self.assertError(self.hook("stop"), "client")
        self.assertNothingWritten()


class RecordsTest(RecordCases, HookBase):
    def test_two_synchronized_writers_keep_every_complete_journal_record(self):
        script = """import json, subprocess, sys
folder, writer = sys.argv[1:]
print("ready", flush=True)
sys.stdin.buffer.read(1)
hooks = [
    subprocess.Popen(
        [sys.executable, "-P", "-m", "ensemble.session.cli", "hook", folder, "other"],
        stdin=subprocess.PIPE,
    )
    for _ in range(20)
]
for number, hook in enumerate(hooks):
    hook.stdin.write(json.dumps({"writer": writer, "number": number}).encode())
    hook.stdin.close()
codes = [hook.wait() for hook in hooks]
if any(codes):
    sys.exit(f"hook exit codes {codes}")
"""
        processes = []
        for writer in ("first", "second"):
            process = subprocess.Popen(
                [sys.executable, "-P", "-s", "-c", script, self.dir, writer],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**self.env, "PYTHONPATH": os.pathsep.join(ROOTS)},
            )
            self.addCleanup(process.kill)
            self.addCleanup(process.wait)
            self.assertEqual(process.stdout.readline(), b"ready\n")
            processes.append(process)
        for process in processes:
            process.stdin.write(b"x")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))
        records = self.records()
        self.assertEqual(len(records), 40)
        self.assertEqual(
            {(r["payload"]["writer"], r["payload"]["number"]) for r in records},
            {(writer, number) for writer in ("first", "second") for number in range(20)},
        )
        self.assertEqual(self.calls(), [])

    def test_valid_edge_states_are_accepted(self):
        write_state(
            self.dir,
            {
                **self.state,
                "words": {},
                "project": True,
                "cwd": "",
                "transcript": "/t.jsonl",
                "resume": "abc",
                "later": [1],
            },
        )
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.calls(), [])


class SecurityTest(HookBase):
    def test_invalid_arguments_drain_a_large_pipe_before_exiting(self):
        data = b'{"data":"' + b"x" * (2 << 20) + b'"}'
        env = {
            **self.env,
            "PATH": os.pathsep.join((os.path.dirname(sys.executable), "/usr/bin", "/bin")),
        }
        for arguments in ([], [self.dir, "stop", "extra"], ["relative", "stop"]):
            with self.subTest(arguments=arguments):
                process = subprocess.Popen(
                    [COMMAND, "hook", *arguments],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                self.addCleanup(process.kill)
                self.addCleanup(process.wait)
                with process.stdin as stream:
                    self.assertEqual(stream.write(data), len(data))
                process.stdin = None
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout), (0, b""))
                self.assertError(stderr.decode(), "tui_hook")
        self.assertNothingWritten()

    def test_a_non_regular_callback_is_checked_before_the_session_package_initializer(self):
        package = os.path.join(self.root, "ensemble")
        os.mkdir(package)
        marker = os.path.join(self.root, "package-imported")
        with open(os.path.join(package, "__init__.py"), "w") as file:
            file.write(f"open({marker!r}, 'w').close()\n")
        callback = os.path.join(package, "callback.py")
        with open(callback, "w") as file:
            file.write("def on_hook(*args): pass\n")
        script = (
            RUN_HOOK
            + """import os, stat, types, unittest.mock
rejected, folder = sys.argv[1:]
original = os.stat
def non_regular(filename, *args, **kwargs):
    if filename == rejected:
        return types.SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)
    return original(filename, *args, **kwargs)
with unittest.mock.patch("os.stat", non_regular):
    run_hook(folder, "stop")
"""
        )
        write_state(self.dir, {**self.state, "client_hook": self.callback("ensemble.callback")})
        result = python(
            self,
            [sys.executable, "-P", "-s", "-c", script, callback, self.dir],
            input=b"{}",
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stdout, b"")
        self.assertError(result.stderr.decode(), "not a regular file")
        self.assertFalse(os.path.exists(marker))
        self.assertNothingWritten()

    def callback_package(self):
        folder = os.path.join(self.root, "callback_package", "nested")
        os.makedirs(folder)
        imported = os.path.join(self.root, "imported")
        files = [
            os.path.join(self.root, "callback_package", "__init__.py"),
            os.path.join(folder, "__init__.py"),
            os.path.join(folder, "callback.py"),
        ]
        for path in files:
            with open(path, "w") as file:
                file.write(f"with open({imported!r}, 'a') as marker: marker.write('imported\\n')\n")
                file.write(
                    "def on_hook(call):\n    return {**call.payload, 'callback_imported': True}\n"
                )
        self.state.update(client_hook=self.callback("callback_package.nested.callback"), words={})
        write_state(self.dir, self.state)
        return files, imported

    def test_non_regular_callback_files_are_rejected_before_any_package_code(self):
        files, imported = self.callback_package()
        script = (
            RUN_HOOK
            + """import os, stat, types, unittest.mock
rejected, folder = sys.argv[1:]
original = os.stat
def non_regular(filename, *args, **kwargs):
    if filename == rejected:
        return types.SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)
    return original(filename, *args, **kwargs)
with unittest.mock.patch("os.stat", non_regular):
    run_hook(folder, "stop")
assert "callback_package" not in sys.modules
"""
        )
        for rejected in files:
            with self.subTest(path=rejected):
                result = python(
                    self,
                    [sys.executable, "-P", "-s", "-c", script, rejected, self.dir],
                    input=b"{}",
                    timeout=60,
                    check=True,
                )
                self.assertEqual(result.stdout, b"")
                self.assertError(result.stderr.decode(), "not a regular file")
                self.assertFalse(os.path.exists(imported))
                self.assertNothingWritten()

    def test_callback_package_imports_after_the_complete_chain_is_checked(self):
        _, imported = self.callback_package()
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(self.records()[0]["payload"], {"callback_imported": True})
        with open(imported) as file:
            self.assertEqual(file.read(), "imported\n" * 3)

    def test_nested_namespace_callback_imports_from_supplied_roots(self):
        sources = [os.path.join(self.root, name) for name in ("left", "right")]
        for source in sources:
            os.makedirs(os.path.join(source, "namespace", "nested"))
        callback = os.path.join(sources[1], "namespace", "nested", "callback.py")
        with open(callback, "w") as file:
            file.write(
                "def on_hook(call):\n    return {**call.payload, 'callback_imported': True}\n"
            )
        self.state.update(
            client_hook=descriptor("namespace.nested.callback", roots=tuple(sources)), words={}
        )
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(self.records()[0]["payload"], {"callback_imported": True})

    def test_an_ensemble_callback_runs_from_its_declared_root(self):
        source = os.path.join(self.root, "extension")
        package = os.path.join(source, "ensemble", "ext_tui")
        os.makedirs(package)
        with open(os.path.join(package, "__init__.py"), "w") as file:
            file.write("")
        with open(os.path.join(package, "hooks.py"), "w") as file:
            file.write("def on_hook(call):\n    return {**call.payload, 'extension': True}\n")
        self.state.update(
            client_hook=descriptor("ensemble.ext_tui.hooks", roots=(source,)), words={}
        )
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(self.records()[0]["payload"], {"extension": True})

    def test_bad_arguments_write_nothing(self):
        for args in (
            [],
            [self.dir],
            [self.dir, "stop", "x"],
            ["s1", "stop"],
            [self.dir + "\n", "stop"],
            [self.dir, ""],
            [self.dir, "a b"],
            [self.dir, "x" * 65],
            [self.dir, "../x"],
        ):
            with self.subTest(args):
                self.assertError(self.hook(None, args=args), "usage")
        self.assertNothingWritten()

    def test_a_declared_root_callback_runs_with_sys_path_unchanged(self):
        source = os.path.join(self.root, "distribution")
        os.mkdir(source)
        with open(os.path.join(source, "client.py"), "w") as file:
            file.write(
                "import sys\n"
                "def on_hook(call):\n"
                f"    return {{**call.payload, 'on_path': {source!r} in sys.path}}\n"
            )
        self.state.update(client_hook=descriptor("client", roots=(source,)), words={})
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(self.records()[0]["payload"], {"on_path": False})

    def test_a_bad_session_dir_writes_nothing(self):
        afile = os.path.join(self.root, "file")
        open(afile, "w").close()
        for folder in (os.path.join(self.root, "missing"), afile):
            with self.subTest(folder):
                self.assertError(self.hook(None, args=[folder, "stop"]), "session.json")
        self.assertNothingWritten()

    def test_a_session_json_that_is_no_regular_file_writes_nothing(self):
        path = os.path.join(self.dir, "session.json")
        os.unlink(path)
        self.assertError(self.hook("stop"), "session.json")
        for make in (lambda: os.mkfifo(path), lambda: os.mkdir(path)):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                self.assertError(self.hook("stop"), "session.json")
            (os.rmdir if os.path.isdir(path) else os.unlink)(path)
        self.assertNothingWritten()

    def test_each_invalid_session_state_field_writes_nothing(self):
        bad = {
            "words": (
                [],
                "x",
                None,
                {"a b": "done"},
                {"": "done"},
                {"x" * 65: "done"},
                {"a/b": "done"},
                {"stop": "dead"},
                {"stop": None},
                {"stop": ["done"]},
            ),
            "events": ("", "events", "/a\nb", 1, []),
            "sid": ("", "a/b", "a\tb", 1, None),
            "workdir": ("", "rel", "/a\x00b", None),
        }
        self.assertEqual(set(bad), set(tui_hook.SESSION_STATE))
        for field, values in bad.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertError(self.hook("stop"), "session.json")
                    self.assertNothingWritten()

    def test_identity_keys_neither_it_nor_its_client_hook_reads_go_unchecked(self):
        """SessionInfo.from_fields is their one decoder; the hook reads words and events,
        and a codex client_hook workdir and sid.
        """
        unread = {
            "client": ("Fake", None),
            "cwd": ("", "rel", None),
            "transcript": ("rel", None),
            "project": ("true", None),
            "resume": (None, 1),
        }
        for field, values in unread.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertEqual(self.hook("other"), "")
        self.assertEqual(len(self.records()), sum(len(v) + 1 for v in unread.values()))

    def test_a_session_json_not_an_object_of_at_most_1_mib_writes_nothing(self):
        for data in (
            b"",
            b"not json",
            b"[]",
            b'"s"',
            b"\xff",
            json.dumps({**self.state, "pad": "x" * (1 << 20)}).encode(),
        ):
            with self.subTest(data[:20]):
                write_state(self.dir, data)
                self.assertError(self.hook("stop"), "session.json")
        self.assertNothingWritten()

    def test_hooks_jsonl_that_is_no_regular_file_is_refused(self):
        for make in (lambda: os.mkfifo(self.hooks), lambda: os.mkdir(self.hooks)):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertError(self.hook("stop"), "hooks.jsonl")
            (os.rmdir if os.path.isdir(self.hooks) else os.unlink)(self.hooks)
        self.assertNothingWritten()


class ServeTest(HookBase):
    """`ensemble-session hook --serve DIR`: one process answers each request line."""

    NOISY = (
        "import os, sys\n"
        "def on_hook(call):\n"
        "    print('noise')\n"
        "    sys.stdout.flush()\n"
        "    os.write(1, b'raw noise\\n')\n"
        "    sys.stdin.read()\n"
        "    os.read(0, 64)\n"
        "    if call.payload.get('fail'):\n"
        "        raise RuntimeError('asked to fail')\n"
        "    return call.payload\n"
    )

    def test_one_process_answers_every_request_and_callback_stdio_never_reaches_it(self):
        with open(os.path.join(self.root, "noisy.py"), "w") as file:
            file.write(self.NOISY)
        self.state["client_hook"] = self.callback("noisy")
        write_state(self.dir, self.state)
        payloads = ({"n": 1}, {"n": 2, "fail": True}, {"n": 3})
        lines = [
            json.dumps({"label": "stop", "event": f"g.{n}", "payload": p}).encode() + b"\n"
            for n, p in enumerate(payloads, 1)
        ]
        env = {**self.env, "PATH": os.pathsep.join((self.env["PATH"], "/usr/bin", "/bin"))}
        res = subprocess.run(
            [COMMAND, "hook", "--serve", self.dir],
            input=b"".join(lines),
            capture_output=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [json.loads(line) for line in res.stdout.splitlines()],
            [{"ok": True}, {"ok": False}, {"ok": True}],
        )
        self.assertEqual(
            [(r["event"], r["payload"]["n"]) for r in self.records()], [("g.1", 1), ("g.3", 3)]
        )
        self.assertIn("asked to fail", res.stderr.decode())


if __name__ == "__main__":
    unittest.main()
