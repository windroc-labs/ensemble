"""Session behavior tests with caller-owned dependencies."""

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from ensemble.session import Session
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    Exited,
    LimitHit,
    Mode,
    Reply,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.session.case import REPO, Case, configured
from tests.session.integration.clients.codex import fake_codex
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import git_run

NATIVE = fake_codex.NATIVE

CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class HeadlessSessions(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work, self.bin = (self.root / name for name in ("home", "work", "bin"))
        self.cwd, self.extra = self.work / "project", self.root / "extra"
        self.tasks = self.root / "team" / "tasks"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.tasks, self.codex_home, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        executable = self.bin / "codex"
        executable.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(fake_codex.__file__)
            + ' "$@"\n'
        )
        executable.chmod(0o755)
        self.log, self.scenario = self.root / "exec.jsonl", self.root / "scenario.json"
        self.scene()
        self.client = CodexClient(
            {
                "tiers": {"1": "gpt-test"},
                "efforts": {"low": "low"},
            },
        )
        self.common = self.client.common
        self.sid = "caller-id"
        self.spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(
                cwd=str(self.cwd),
                project=True,
                dirs=(str(self.extra),),
                reads=(str(self.tasks),),
                commands=("git status",),
            ),
        )
        cfg = self.client.config | {
            "env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)}
        }
        self.clients = {
            "codex": {"implementation": "ensemble.session.clients.codex:CodexClient", **cfg}
        }
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "CODEX_HOME": str(self.codex_home),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                },
            )
        )
        self.session_cls = configured(self.clients)

    def scene(self, steps=None, **fields):
        if steps is not None:
            fields["steps"] = steps
        self.scenario.write_text(json.dumps(fields))

    def drain(self, handle, on=lambda _event: None):
        self.addCleanup(handle.stop)
        found = []

        def consume():
            for event in handle.events():
                found.append(event)
                on(event)

        executor = ThreadPoolExecutor(max_workers=1)
        result = executor.submit(consume)
        try:
            result.result(timeout=10)
        except TimeoutError:
            handle.stop()
            self.fail("the session did not reach Exited within 10s")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return found

    def start(self, prompt: str, spec: Spec, *, resume: bool = False) -> Session:
        """A headless codex session of `spec` on `prompt`; resume: continue session spec.sid."""
        return self.session_cls.start(
            dataclasses.replace(
                spec, client="codex", mode=Mode.HEADLESS, prompt=prompt, resume=resume
            )
        )

    def inspect(self, sid: str) -> SessionStatus:
        return self.session_cls.inspect(
            Spec(workdir=str(self.work), access=Access(cwd=str(self.work)), client="codex", sid=sid)
        )

    def logged(self):
        return read_jsonl(self.log, missing_ok=True)

    def written(self):
        return RunLog(str(self.work)).read()

    def record(self):
        return next(Path(SessionStore().root).glob("*.json"))

    def expected_args(self, prompt, *, resume=False, sandbox="danger-full-access"):
        args = [
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-s",
            sandbox,
            "-c",
            'approval_policy="on-request"',
            "-c",
            'approvals_reviewer="auto_review"',
        ]
        if sandbox == "workspace-write":
            args.extend(
                [
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.writable_roots=[]",
                ]
            )
        args.extend(["-m", "gpt-test", "-c", "model_reasoning_effort=low"])
        args.extend(["--add-dir", str(self.extra)])
        return [
            *args,
            *(["resume", NATIVE] if resume else []),
            "--",
            prompt + "\n\n" + self.client.client_note,
        ]

    def direct_launch(self, prompt, *, resume=False):
        client = CodexClient(
            {
                key: value
                for key, value in self.clients["codex"].items()
                if key not in {"implementation", "max_runs"}
            },
        )
        run = client.headless()
        launch = run.launch(
            dataclasses.replace(self.spec, sid=self.sid, prompt=prompt, resume=resume)
        )
        run.commit()
        env = {key: value for key, value in os.environ.items() if key not in launch.unset}
        result = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env={**env, **launch.env, "PWD": launch.cwd},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(run.events(result.stdout.splitlines()))[-1], TurnEnd("three"))
        entry = self.logged()[-1]
        expected = self.expected_args(prompt, resume=resume)[:-1] + [prompt]
        self.assertEqual(entry["argv"], expected)
        self.assertEqual(entry["prompt"], prompt)
        self.assertEqual(entry["resume_id"], NATIVE if resume else None)


class GitIsolationIntegrationTest(HeadlessSessions):
    def check_start_and_resume(self, expected):
        deployment = self.root / "deployment"
        deployment.mkdir()
        git_run("init", str(deployment), check=True)
        git_env = {"GIT_DIR": str(deployment / ".git"), "GIT_WORK_TREE": str(deployment)}
        self.clients["codex"]["env"].update(git_env)
        self.session_cls = configured(self.clients)
        spec = dataclasses.replace(
            self.spec,
            sid=self.sid,
            env=git_env,
            access=dataclasses.replace(self.spec.access, env=git_env),
        )
        self.scene(git_probe=True)
        with mock.patch.dict(os.environ, git_env):
            first = self.start("first", spec)
            self.assertEqual(self.drain(first)[-1], Exited(0))
            again = self.start("again", spec, resume=True)
            self.assertEqual(self.drain(again)[-1], Exited(0))
        first_entry, resumed_entry = self.logged()
        self.assertIsNone(first_entry["resume_id"])
        self.assertEqual(resumed_entry["resume_id"], NATIVE)
        for entry in (first_entry, resumed_entry):
            self.assertEqual(entry["git_probe"], expected)
            self.assertNotEqual(entry["git_probe"]["stdout"], str(deployment))

    def test_start_and_resume_git_select_the_run_checkout(self):
        git_run("init", str(self.cwd), check=True)
        self.check_start_and_resume({"returncode": 0, "stdout": str(self.cwd)})

    def test_start_and_resume_git_cannot_select_deployment_from_an_unrelated_cwd(self):
        self.check_start_and_resume({"returncode": 128, "stdout": ""})


class HeadlessEventsIntegrationTest(HeadlessSessions):
    def test_session_replies_native_sid_and_snapshot(self):
        handle = self.start("hello", dataclasses.replace(self.spec, sid=self.sid))

        self.assertEqual(
            self.drain(handle),
            [
                TurnStarted(),
                Reply("one\n\nnext line\n"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three"),
                Exited(0),
            ],
        )
        self.assertEqual(handle.sid, self.sid)
        self.assertEqual(handle.reply, "three")
        (entry,) = self.logged()
        self.assertEqual(
            (entry["snapshot"]["sid"], entry["snapshot"]["native_id"]), (self.sid, None)
        )
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual(
            [
                (entry["kind"], entry.get("client_id"))
                for entry in self.written()
                if entry["kind"] != "session_metadata"
            ],
            [("session", None), ("client_session", NATIVE)],
        )

    def test_raw_progress_outcome_and_json_reply_are_uninterpreted(self):
        texts = [
            "[ensemble-progress:start] working\n\n",
            json.dumps({"kind": "outcome", "outcome": DONE}),
            json.dumps({"status": "needs_input", "questions": ["Which greeting?"]}),
            json.dumps({"kind": "outcome", "outcome": {"status": "maybe"}}),
            json.dumps(DONE),
        ]
        self.scene(
            [*(fake_codex.message(text) for text in texts), {"out": {"type": "turn.completed"}}]
        )
        handle = self.start("hello", dataclasses.replace(self.spec, sid=self.sid))
        self.assertEqual(
            self.drain(handle),
            [TurnStarted(), *(Reply(text) for text in texts), TurnEnd(texts[-1]), Exited(0)],
        )
        self.assertEqual(handle.reply, texts[-1])
        self.assertEqual(
            [entry["kind"] for entry in self.written() if entry["kind"] != "session_metadata"],
            ["session", "client_session"],
        )
        self.assertFalse((self.work / "out.md").exists())

    def test_stderr_is_an_event_and_run_record(self):
        self.scene(
            [
                {"err": "Codex diagnostic\nsecond line"},
                fake_codex.message("done"),
                {"out": {"type": "turn.completed"}},
            ]
        )
        found = self.drain(self.start("hello", dataclasses.replace(self.spec, sid=self.sid)))
        self.assertEqual(
            [event for event in found if isinstance(event, Stderr)],
            [Stderr("Codex diagnostic"), Stderr("second line")],
        )
        self.assertEqual(
            [entry["text"] for entry in self.written() if entry["kind"] == "stderr"],
            ["Codex diagnostic", "second line"],
        )
        self.assertEqual(found[-1], Exited(0))

    def test_nonzero_exit_preserves_reply_but_fails_the_turn(self):
        text = json.dumps(DONE)
        self.scene([fake_codex.message(text), {"out": {"type": "turn.completed"}}, {"exit": 7}])
        handle = self.start("hello", dataclasses.replace(self.spec, sid=self.sid))
        self.assertEqual(
            self.drain(handle),
            [TurnStarted(), Reply(text), TurnFailed("process_exit", "worker exited 7"), Exited(7)],
        )
        self.assertEqual(handle.reply, text)
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)

    def test_nonzero_exit_without_final_turn_is_not_inferred(self):
        self.scene([fake_codex.message("Thinking."), {"exit": 7}])
        self.assertEqual(
            self.drain(self.start("hello", dataclasses.replace(self.spec, sid=self.sid))),
            [
                TurnStarted(),
                Reply("Thinking."),
                TurnFailed("process_exit", "worker exited 7"),
                Exited(7),
            ],
        )

    def test_activity_and_usage_limit_pass_through_the_session(self):
        error = "You’ve hit your usage limit. Try again later."
        self.scene(
            [
                {"out": {"type": "item.started", "item": {"type": "command_execution"}}},
                {"out": {"type": "error", "message": error}},
                {"out": {"type": "turn.completed"}},
            ]
        )
        self.assertEqual(
            self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid))),
            [
                TurnStarted(),
                Activity("item.started"),
                Stderr(error),
                LimitHit(None),
                TurnFailed("usage_limit"),
                Exited(0),
            ],
        )

    def test_caller_sid_and_handle_info_inspect_native_binding(self):
        spec = dataclasses.replace(self.spec, sid=None)
        handle = self.start("hello", dataclasses.replace(spec, sid="caller-owned"))
        self.drain(handle)
        info = handle.info()
        self.assertEqual(
            (
                handle.sid,
                info.sid,
                info.cwd,
                info.project,
                info.transcript,
                info.resume,
            ),
            (
                "caller-owned",
                "caller-owned",
                str(self.cwd),
                True,
                str(fake_codex.session_path(NATIVE)),
                info.resume,
            ),
        )
        self.assertIn(NATIVE, info.resume)
        self.assertEqual(RunLog(str(self.work)).session(handle.sid).transcript, info.transcript)
        status = self.inspect(handle.sid)
        self.assertEqual(status.resumable, "yes")
        self.assertIsNotNone(status.last_active)
        self.assertIn(NATIVE, status.resume_command)
        fake_codex.session_path(NATIVE).unlink()
        self.assertEqual(
            self.inspect(handle.sid).resumable,
            "no",
        )
        self.record().write_text("bad json")
        self.assertEqual(
            self.inspect(handle.sid).resumable,
            "unknown",
        )


class ResumeReferenceIntegrationTest(HeadlessSessions):
    def test_fake_credentials_stay_out_of_public_text_and_replay_on_resume(self):
        secrets = {"CFG_TOKEN": "cfg-1", "ACCESS_TOKEN": "access-2", "SPEC_TOKEN": "spec-3"}
        config = self.clients["codex"]
        self.clients["codex"] = {
            **config,
            "env": {**config["env"], "CFG_TOKEN": "cfg-1"},
        }
        spec = dataclasses.replace(
            self.spec,
            sid="secret-run",
            env={"SPEC_TOKEN": "spec-3"},
            access=dataclasses.replace(self.spec.access, env={"ACCESS_TOKEN": "access-2"}),
        )
        handle = configured(self.clients).start(
            dataclasses.replace(spec, client="codex", mode=Mode.HEADLESS, prompt="hello")
        )
        self.drain(handle)
        resume = handle.info().resume
        status = self.inspect("secret-run")
        log = (self.work / "run.jsonl").read_text(encoding="utf-8")
        for text in (resume, status.resume_command, log):
            for value in secrets.values():
                self.assertNotIn(value, text)
        self.assertEqual(status.resume_command, resume)
        record = self.root / "resumed.json"
        recorder = self.root / "recorder.py"
        recorder.write_text(
            "import json, os, sys\n"
            "with open(os.environ['RESUME_RECORD'], 'w') as f:\n"
            "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}, f)\n"
        )
        (self.bin / "codex").write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n'
        )
        result = subprocess.run(
            ["/bin/sh", "-c", resume],
            env={**os.environ, "RESUME_RECORD": str(record)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((resumed["argv"][0], resumed["argv"][-1]), ("resume", NATIVE))
        self.assertEqual(os.path.realpath(resumed["cwd"]), str(self.cwd))
        env = resumed["env"]
        self.assertEqual({key: env.get(key) for key in secrets}, secrets)
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))


class HeadlessLaunchIntegrationTest(HeadlessSessions):
    def test_fixture_cleanup_removes_owned_home_outside_writable_roots(self):
        self.assertNotEqual(
            os.path.commonpath((self.home, tempfile.gettempdir())), tempfile.gettempdir()
        )
        self.assertNotEqual(os.path.commonpath((self.home, self.work)), str(self.work))
        root = self.root
        self.doCleanups()
        self.assertFalse(root.exists())

    def test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof(self):
        prompt = "\n# Input\n\nHello.\n"
        self.drain(self.start(prompt, dataclasses.replace(self.spec, sid=self.sid)))
        (entry,) = self.logged()
        self.assertEqual(entry["argv"], self.expected_args(prompt))
        self.assertEqual(entry["prompt"], self.expected_args(prompt)[-1])
        self.assertTrue(entry["stdin_eof"])
        self.assertEqual(
            (entry["cwd"], entry["env"]["PWD"], entry["env"]["CODEX_HOME"]),
            (str(self.cwd), str(self.cwd), str(self.codex_home)),
        )

    def test_workspace_write_exact_flags_and_saved_policy_through_the_session(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=(
                "-s",
                "workspace-write",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
            ),
        )
        self.session_cls = configured(self.clients)
        self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid)))
        self.spec = dataclasses.replace(self.spec, flags=("-s", "danger-full-access"))
        self.session_cls = configured(self.clients)
        self.drain(self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True))
        first, second = self.logged()
        self.assertEqual(first["argv"], self.expected_args("one", sandbox="workspace-write"))
        self.assertEqual(
            second["argv"], self.expected_args("again", resume=True, sandbox="workspace-write")
        )
        self.assertEqual(second["snapshot"]["snapshot"]["flags"][1], "workspace-write")

    def test_fresh_native_prompt_boundary_preserves_subcommands_and_options(self):
        for prompt in (
            "review",
            "resume",
            "fork",
            "--dangerously-bypass-approvals-and-sandbox",
            "--last",
            "--",
        ):
            with self.subTest(prompt=prompt):
                try:
                    self.direct_launch(prompt)
                finally:
                    self.record().unlink()

    def test_resumed_native_prompt_boundary_preserves_options(self):
        self.direct_launch("first")
        for prompt in ("--dangerously-bypass-approvals-and-sandbox", "--last", "review", "--"):
            with self.subTest(prompt=prompt):
                self.direct_launch(prompt, resume=True)

    def test_configured_tmpdir_override_is_forwarded_fresh_and_resume(self):
        native_tmp = self.root / "native-tmp"
        native_tmp.mkdir()
        self.clients["codex"]["env"]["TMPDIR"] = str(native_tmp)
        self.session_cls = configured(self.clients)
        for resume in (False, True):
            with self.subTest(resume=resume):
                found = self.drain(
                    self.start("hello", dataclasses.replace(self.spec, sid=self.sid), resume=resume)
                )
                self.assertEqual(found[-1], Exited(0))
                self.assertEqual(self.logged()[-1]["env"]["TMPDIR"], str(native_tmp))

    def test_configured_tmpdir_store_overlap_refused_before_fresh_spawn(self):
        alias = self.root / "home-alias"
        alias.symlink_to(self.home)
        store = Path(SessionStore().root)
        for index, root in enumerate((self.home, self.home / ".ensemble", store, alias)):
            with self.subTest(root=root):
                self.clients["codex"]["env"]["TMPDIR"] = str(root)
                self.session_cls = configured(self.clients)
                with mock.patch(
                    "subprocess.Popen", side_effect=AssertionError("spawned before TMPDIR guard")
                ) as spawn:
                    with self.assertRaisesRegex(ConfigError, "writable"):
                        self.start(
                            "hello", dataclasses.replace(self.spec, sid=f"{self.sid}-{index}")
                        )
                    spawn.assert_not_called()
                self.assertFalse((self.home / ".ensemble").exists())
        self.assertEqual(self.logged(), [])

    def test_tui_factory_is_supported_without_starting_a_process(self):
        with mock.patch("subprocess.Popen") as spawn:
            self.assertIsNotNone(self.session_cls._config().registry.client("codex").tui())
        spawn.assert_not_called()
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_inherited_session_markers_are_removed_and_headless_defaults_retained(self):
        inherited = {key: "inherited" for key in (*fake_codex.MARKERS, *fake_codex.DEFAULTS)}
        with mock.patch.dict(os.environ, inherited):
            for resume in (False, True):
                with self.subTest(resume=resume):
                    self.drain(
                        self.start(
                            "hello", dataclasses.replace(self.spec, sid=self.sid), resume=resume
                        )
                    )
                    environment = self.logged()[-1]["env"]
                    self.assertEqual(
                        {key: environment[key] for key in fake_codex.MARKERS},
                        dict.fromkeys(fake_codex.MARKERS),
                    )
                    self.assertEqual(
                        {key: environment[key] for key in fake_codex.DEFAULTS}, fake_codex.DEFAULTS
                    )


class HeadlessRecoveryIntegrationTest(HeadlessSessions):
    def test_resume_ignores_changed_config_environment(self):
        self.drain(self.start("first", dataclasses.replace(self.spec, sid=self.sid)))
        saved = self.common.load(str(self.work), self.sid).snapshot
        self.clients["codex"]["env"]["TMPDIR"] = str(self.home)
        self.session_cls = configured(self.clients)
        changed = dataclasses.replace(self.spec, sid=self.sid, env={"NEW_POLICY_KEY": "fresh"})
        self.drain(self.start("again", changed, resume=True))
        self.assertEqual(self.common.load(str(self.work), self.sid).snapshot, saved)
        self.assertEqual(len(self.logged()), 2)
        self.assertIsNone(self.logged()[-1]["env"]["NEW_POLICY_KEY"])

    def test_resume_same_native_and_saved_settings(self):
        self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid)))
        alternate = self.root / "changed"
        alternate.mkdir()
        cfg = self.clients["codex"]
        cfg.update(
            tiers={"1": "changed"},
            efforts={"low": "high"},
        )
        cfg["env"]["CODEX_HOME"] = str(alternate)
        self.session_cls = configured(self.clients)
        access = Access(
            cwd=str(alternate), dirs=(str(alternate),), commands=("changed command",), project=False
        )
        spec = dataclasses.replace(self.spec, tier=4, effort="max", access=access)
        handle = self.start("again", dataclasses.replace(spec, sid=self.sid), resume=True)
        self.assertTrue(handle.info().project)
        self.assertEqual(handle.info().transcript, str(fake_codex.session_path(NATIVE)))
        self.assertIn(NATIVE, handle.info().resume)
        self.drain(handle)
        self.assertEqual(
            [entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
            [NATIVE, NATIVE],
        )
        first, second = self.logged()
        self.assertEqual(second["snapshot"]["snapshot"], first["snapshot"]["snapshot"])
        self.assertEqual(second["snapshot"]["native_id"], NATIVE)
        self.assertEqual(second["argv"], self.expected_args("again", resume=True))
        self.assertEqual(
            (second["cwd"], second["env"]["CODEX_HOME"], second["resume_id"], second["stdin_eof"]),
            (str(self.cwd), str(self.codex_home), NATIVE, True),
        )
        transcript = read_jsonl(fake_codex.session_path(NATIVE))
        self.assertEqual(
            [entry["text"] for entry in transcript if entry["type"] == "user"],
            [first["prompt"], second["prompt"]],
        )

    def test_add_dir_order_survives_resume_and_the_tasks_dir_is_read_unadded(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=(
                "-s",
                "workspace-write",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
            ),
        )
        self.session_cls = configured(self.clients)
        prompt = f"Read `{self.tasks}`."
        for resume in (False, True):
            with self.subTest(resume=resume):
                spec = (
                    self.spec
                    if not resume
                    else dataclasses.replace(
                        self.spec, access=dataclasses.replace(self.spec.access, dirs=())
                    )
                )
                self.drain(
                    self.start(prompt, dataclasses.replace(spec, sid=self.sid), resume=resume)
                )
                entry = self.logged()[-1]
                dirs = [
                    entry["argv"][i + 1]
                    for i, arg in enumerate(entry["argv"])
                    if arg == "--add-dir"
                ]
                self.assertEqual(dirs, [str(self.extra)])
                self.assertNotIn(str(self.tasks), dirs)
                self.assertIn(f"`{self.tasks}`", entry["prompt"])
                self.assertFalse((self.work / "tasks").exists())

    def test_interrupted_first_turn_keeps_binding_and_resumes(self):
        before, after = (
            "[ensemble-progress:start] before interruption",
            "[ensemble-progress:review] recovered",
        )
        self.scene([fake_codex.message(before), {"sleep": 60}])
        handle = self.start("first", dataclasses.replace(self.spec, sid=self.sid))
        found = self.drain(
            handle, lambda event: handle.stop() if isinstance(event, Reply) else None
        )
        self.assertEqual(found[:-1], [TurnStarted(), Reply(before), TurnFailed("interrupted")])
        self.assertIsInstance(found[-1], Exited)
        self.assertLess(found[-1].returncode, 0)
        self.assertFalse(handle.alive())
        handle.stop()
        self.assertEqual(list(handle.events()), [])
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.scene([fake_codex.message(after), {"out": {"type": "turn.completed"}}])
        resumed = self.start("recover", dataclasses.replace(self.spec, sid=self.sid), resume=True)
        self.assertEqual(
            self.drain(resumed), [TurnStarted(), Reply(after), TurnEnd(after), Exited(0)]
        )
        self.assertEqual(self.logged()[-1]["resume_id"], NATIVE)
        self.assertEqual(
            [entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
            [NATIVE, NATIVE],
        )

    def test_the_fakes_resume_steps_run_only_on_resume(self):
        self.scene(
            [fake_codex.message("first"), {"out": {"type": "turn.completed"}}],
            resume=[fake_codex.message("again"), {"out": {"type": "turn.completed"}}],
        )
        for resume, said in ((False, "first"), (True, "again")):
            with self.subTest(resume=resume):
                found = self.drain(
                    self.start("hello", dataclasses.replace(self.spec, sid=self.sid), resume=resume)
                )
                self.assertEqual(found, [TurnStarted(), Reply(said), TurnEnd(said), Exited(0)])

    def test_different_native_id_on_resume_is_not_published_or_saved(self):
        self.drain(self.start("first", dataclasses.replace(self.spec, sid=self.sid)))
        self.scene(thread_id=CHANGED)
        with self.assertLogs("session", "ERROR") as logs:  # its harness fails, ending the run
            handle = self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True)
            self.assertIsInstance(self.drain(handle)[-1], Exited)
        self.assertIn("already bound to a different native ID", logs.output[0])
        self.assertFalse(handle.alive())
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual(
            [entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
            [NATIVE],
        )

    def test_missing_mapping_does_not_spawn(self):
        with self.assertRaisesRegex(ConfigError, "missing Codex recovery state"):
            self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True)
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_missing_binding_record_and_fifo_do_not_spawn(self):
        self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid)))
        path = self.record()
        original = path.read_text()
        for mode in ("unbound", "missing", "fifo", "malformed"):
            with self.subTest(mode=mode):
                if path.exists():
                    path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                elif mode != "missing":
                    value = json.loads(original)
                    if mode == "unbound":
                        value["native_id"] = None
                    path.write_text("bad json" if mode == "malformed" else json.dumps(value))
                    path.chmod(0o600)
                with self.assertRaises(ConfigError):
                    self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True)
                self.assertEqual(len(self.logged()), 1)

    def test_missing_session_does_not_spawn(self):
        self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid)))
        fake_codex.session_path(NATIVE).unlink()
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True)
        self.assertEqual(len(self.logged()), 1)

    def test_fifo_and_malformed_native_files_do_not_spawn(self):
        self.drain(self.start("one", dataclasses.replace(self.spec, sid=self.sid)))
        path = fake_codex.session_path(NATIVE)
        for mode in ("fifo", "malformed", "mismatched"):
            with self.subTest(mode=mode):
                path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    metadata = {
                        "type": "session_meta",
                        "payload": {"id": CHANGED, "cwd": str(self.cwd)},
                    }
                    path.write_text(
                        "bad json\n" if mode == "malformed" else json.dumps(metadata) + "\n"
                    )
                with self.assertRaises(ConfigError):
                    self.start("again", dataclasses.replace(self.spec, sid=self.sid), resume=True)
                self.assertEqual(len(self.logged()), 1)

    def test_untrusted_project_settings_are_rejected_before_spawn(self):
        (self.cwd / ".codex").mkdir()
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, project=False)
        )
        with self.assertRaisesRegex(ConfigError, "project Codex settings"):
            self.start("hello", dataclasses.replace(spec, sid=self.sid))
        self.assertEqual(self.logged(), [])
