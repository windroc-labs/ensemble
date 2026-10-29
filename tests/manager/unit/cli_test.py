"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ensemble.manager import Roster, Spec, State, cli
from ensemble.manager.cli import main
from ensemble.manager.engine.pr import VIEW_FIELDS
from ensemble.utils import Record, append_line
from tests.manager.case import Case, own, status
from tests.manager.cli_case import CliCase
from tests.manager.roster_case import mark
from tests.manager.starters import answered, finished
from tests.utils.cli_help import check_examples, help_text, parse_only
from tests.utils.fakes import ctl


class CliTest(CliCase):
    def test_pending_snapshot_cannot_include_events_after_takeover(self):
        with self.roster.hold("first") as lease, contextlib.ExitStack() as successors:
            events = Path(self.roster.events)
            events.write_text("12:00:00 previous done\n")
            other = Roster(self.directory)
            native_open, native_close = os.open, os.close
            watched = None
            switched = False

            def open_file(path, *args, **kwargs):
                nonlocal watched
                fd = native_open(path, *args, **kwargs)
                if path == str(Path(self.directory, "roster.lock")) and watched is None:
                    watched = fd
                return fd

            def close_file(fd):
                nonlocal switched
                native_close(fd)
                if fd == watched and not switched:
                    switched = True
                    successors.enter_context(other.hold("second", force=True))
                    with events.open("a") as stream:
                        stream.write("12:00:01 successor done\n")

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                mock.patch("os.open", side_effect=open_file),
                mock.patch("os.close", side_effect=close_file),
            ):
                result, output, error = self.invoke("pending", "--manager", "mgr")
            self.assertEqual(result, 0, error)
            self.assertTrue(switched)
            self.assertIn("previous done", output)
            self.assertNotIn("successor done", output)
            self.assertEqual(
                other.backlog(), (0, 0, ["12:00:00 previous done", "12:00:01 successor done"])
            )

    def test_ack_is_the_only_handling_verb_without_notification_attestation(self):
        for argv in (
            ("attach", "--after", "1", "--gen", "0"),
            ("monitor", "--receipt", str(uuid.uuid4())),
            ("next-event", "--after", "0", "--gen", "0", "--follow", "--notify"),
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.invoke(*argv, "--manager", "mgr")
        self.assertFalse(Path(self.directory).exists())

    def test_invalid_or_partial_holder_environment_is_usage_error_without_writes(self):
        identity = str(uuid.uuid4())
        for env in (
            {"ENSEMBLE_HOLDER_ID": identity},
            {"ENSEMBLE_HOLDER_GENERATION": "1"},
            {"ENSEMBLE_HOLDER_ID": "", "ENSEMBLE_HOLDER_GENERATION": "1"},
            {"ENSEMBLE_HOLDER_ID": "invalid", "ENSEMBLE_HOLDER_GENERATION": "1"},
            {"ENSEMBLE_HOLDER_ID": str(uuid.uuid1()), "ENSEMBLE_HOLDER_GENERATION": "1"},
            {"ENSEMBLE_HOLDER_ID": identity.upper(), "ENSEMBLE_HOLDER_GENERATION": "1"},
            {"ENSEMBLE_HOLDER_ID": identity, "ENSEMBLE_HOLDER_GENERATION": "0"},
            {"ENSEMBLE_HOLDER_ID": identity, "ENSEMBLE_HOLDER_GENERATION": "-1"},
            {"ENSEMBLE_HOLDER_ID": identity, "ENSEMBLE_HOLDER_GENERATION": "01"},
            {"ENSEMBLE_HOLDER_ID": identity, "ENSEMBLE_HOLDER_GENERATION": "text"},
        ):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
                self.assertEqual(result, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("crew: "))
                self.assertFalse(os.path.exists(self.directory))

    def test_attach_uses_holder_generation_and_does_not_take_new_lease(self):
        with self.roster.hold("label") as lease:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 0, stderr)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation + 1),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)

    def test_invalid_manager_names_from_flag_and_environment_write_nowhere(self):
        outside = Path(self.home, "outside")
        before = sorted(Path(self.home).rglob("*"))
        names = (
            ".",
            "..",
            "../outside",
            "../../outside",
            str(outside),
            "a/b",
            "a\\b",
            "name space",
            "name\n",
        )
        for name in names:
            for flag, env in (((f"--manager={name}",), {}), ((), {"ENSEMBLE_MANAGER": name})):
                with self.subTest(name=name, env=env), mock.patch.dict(os.environ, env):
                    result, stdout, stderr = self.invoke("attach", *flag)
                    self.assertEqual((result, stdout), (2, ""), stderr)
                    self.assertIn("manager name", stderr)
                    self.assertEqual(sorted(Path(self.home).rglob("*")), before)
                    self.assertFalse(Path(self.ensemble, "managers").exists())
                    self.assertFalse(outside.exists())

    def test_unleased_attach_cannot_access_live_holder(self):
        with self.roster.hold("label"), self.listed():
            result, _, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual(result, 1, stderr)

    def test_removed_owner_and_missing_holder_command_are_usage_errors(self):
        for argv in (
            ("attach", "--manager", "mgr", "--owner", "label"),
            ("hold", "--manager", "mgr"),
            ("hold", "--manager", "mgr", "--"),
            ("attach", "--manager", "mgr", "--", "unused"),
            ("hide", "--manager", "mgr"),
            ("show", "--manager", "mgr", "a", "--all"),
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(argv)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_missing_entry_is_usage_error_without_controller_or_writes(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")) as run:
            for argv in (
                ("close",),
                ("status",),
                ("forget",),
                ("resume",),
                ("forget", "--force"),
                ("hide",),
                ("show",),
            ):
                with self.subTest(argv=argv):
                    command, *extra = argv
                    result, stdout, stderr = self.invoke(
                        command, "missing", "--manager", "mgr", *extra
                    )
                    self.assertEqual(result, 2, stderr)
                    self.assertEqual(stdout, "")
                    self.assertIn("not in roster", stderr)
                    self.assertEqual(
                        "ensemble-crew reopen --manager=mgr missing" in stderr,
                        command == "resume",
                        stderr,
                    )
                    self.assertFalse(os.path.exists(self.directory))
        run.assert_not_called()

    def test_empty_close_all_reports_nothing_without_creating_a_manager(self):
        with self.listed():
            result, stdout, stderr = self.invoke("close-all", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, "nothing to close\n")
        self.assertFalse(os.path.exists(self.directory))


class Launch(CliCase):
    def start(self, *arguments, code=0, output=None, stderr="layer message\n"):
        self.calls = []

        def run(argv, **options):
            self.calls.append((tuple(argv), options["env"]))
            record = self.fake_record(
                "returned-" + options["env"]["ENSEMBLE_SID"][:8],
                sid=options["env"]["ENSEMBLE_SID"],
            )
            text = record.to_line() + "\n" if output is None else output
            os.write(options["stdout"].fileno(), text.encode())
            return SimpleNamespace(returncode=code, stderr=stderr)

        with mock.patch("subprocess.Popen", side_effect=answered(run)):
            return self.invoke(*arguments)

    def test_valid_manager_names_work_from_flag_and_environment(self):
        for name in ("a", "A_9-z", "_leading", "9", "-dash", "trailing-"):
            for alias, flag, env in (
                ("sample-flag", (f"--manager={name}",), {}),
                ("sample-environment", (), {"ENSEMBLE_MANAGER": name}),
            ):
                with self.subTest(name=name, alias=alias), mock.patch.dict(os.environ, env):
                    result, stdout, stderr = self.start("run", alias, *flag, "--", "fake", alias)
                    self.assertEqual(result, 0, stderr)
                    path = Path(self.ensemble, "managers", name, "roster.json")
                    self.assertEqual(
                        json.loads(path.read_text())["entries"][alias]["record"],
                        json.loads(Record.parse(stdout).to_line()),
                    )

    def test_worker_and_issue_forward_every_word_after_the_key(self):
        for verb, key, prefix, tail in (
            (
                "worker",
                "w1",
                ("ensemble-session", "start", "w1"),
                (
                    "--client",
                    "claude",
                    "--credentials",
                    ' {"future-kind": [1]}\n',
                    "--prompt",
                    "hi",
                    "--",
                    "--permission-mode",
                    "auto",
                ),
            ),
            (
                "issue",
                "TASK-12",
                ("ensemble-orchestrator", "router", "--issue", "TASK-12", "--tui"),
                ("--display", "D", "--note", "belongs to layer", "--manager", "also layer"),
            ),
        ):
            with self.subTest(verb=verb):
                result, stdout, stderr = self.start(
                    verb, "--manager", "mgr", "--note", "Crew note", key, *tail
                )
                self.assertEqual((result, stderr), (0, "layer message\n"))
                self.assertEqual(self.calls[0][0], (*prefix, *tail))
                entry = self.roster.entries()[key]
                self.assertEqual(entry.note, "Crew note")
                self.assertEqual(entry.record, Record.parse(stdout))

    def test_role_defaults_are_queried_for_the_last_task(self):
        for task_options in (
            (),
            ("--task", "build"),
            ("--task=build",),
            ("--task", "old", "--task=build"),
        ):
            declared = ' {"future-kind": [1]} '
            reply = SimpleNamespace(returncode=0, stdout=declared + "\n", stderr="")
            with (
                self.subTest(task_options=task_options),
                mock.patch("subprocess.run", return_value=reply) as query,
            ):
                code, _, err = self.start(
                    "role",
                    "--manager",
                    "mgr",
                    "--name",
                    "r",
                    *task_options,
                    "engineer",
                    "--input",
                    "x",
                )
                self.assertEqual(code, 0, err)
                query.assert_called_once()
                self.assertEqual(
                    query.call_args.args[0],
                    (
                        "ensemble-orchestrator",
                        "credentials",
                        "--role",
                        "engineer",
                        *(("--task=build",) if task_options else ()),
                    ),
                )
                self.assertEqual(query.call_args.kwargs["input"], "")
                selected = ("--task", "build") if task_options else ()
                self.assertEqual(
                    self.calls[0][0],
                    (
                        "ensemble-delegate",
                        "run",
                        "--role",
                        "engineer",
                        "--detach",
                        "--name",
                        "r",
                        *selected,
                        "--credentials",
                        declared,
                        "--input",
                        "x",
                    ),
                )
                self.roster.remove("r")

    def test_explicit_role_credentials_skip_default_lookup(self):
        for options in (
            ("--credentials", ""),
            ("--credentials={}",),
            ("--credentials", ' {"future-kind": [1]} '),
        ):
            with self.subTest(options=options), mock.patch("subprocess.run") as query:
                code, _, err = self.start(
                    "role", "--manager", "mgr", "--name", "r", *options, "engineer", "--input", "x"
                )
                self.assertEqual(code, 0, err)
                query.assert_not_called()
                value = options[1] if len(options) == 2 else "{}"
                self.assertEqual(self.calls[0][0][-4:], ("--credentials", value, "--input", "x"))
                self.roster.remove("r")

    def test_role_defaults_use_crew_task_without_scanning_delegate_words(self):
        for index, options in enumerate(
            (
                ("--input", "--task wrong"),
                ("--input", "--task=wrong"),
                ("--input", "--credentials={}"),
                ("--input=x", "--on-event", "--task=wrong"),
                ("--input", "--task", "wrong"),
                ("--input", "--credentials", "{}"),
                ("--future-switch", "--input", "x"),
                ("--dry-run", "--input", "x"),
                ("--detach", "--input=x"),
                ("--input=x", "--", "--task=wrong", "--credentials={}"),
            )
        ):
            reply = SimpleNamespace(returncode=0, stdout="{}\n", stderr="")
            with (
                self.subTest(options=options),
                mock.patch("subprocess.run", return_value=reply) as query,
            ):
                code, _, err = self.start(
                    "role",
                    "--manager",
                    "mgr",
                    "--name",
                    f"r{index}",
                    "--task",
                    "build",
                    "engineer",
                    *options,
                )
                self.assertEqual(code, 0, err)
                query.assert_called_once()
                self.assertEqual(
                    query.call_args.args[0],
                    ("ensemble-orchestrator", "credentials", "--role", "engineer", "--task=build"),
                )
                self.assertEqual(
                    self.calls[0][0][7:], ("--task", "build", "--credentials", "{}", *options)
                )
                self.roster.remove(f"r{index}")

    def test_role_default_config_failure_starts_nothing(self):
        reply = SimpleNamespace(returncode=2, stdout="", stderr="invalid credentials overlay")
        with mock.patch("subprocess.run", return_value=reply):
            code, out, err = self.start("role", "--manager", "mgr", "engineer", "--input", "x")
        self.assertEqual((code, out, self.calls), (2, "", []))
        self.assertIn("invalid credentials overlay", err)
        self.assertFalse(Path(self.directory).exists())

    def test_worker_and_run_resume_pass_the_given_sid(self):
        sid = str(uuid.uuid4())
        for arguments, argv in (
            (
                ("worker", "--manager", "mgr", "--resume", sid, "w1", "--client", "claude"),
                ("ensemble-session", "start", "w1", "--resume", sid, "--client", "claude"),
            ),
            (
                ("worker", "--manager", "mgr", "--resume", sid, "w2", "--client", "codex"),
                ("ensemble-session", "start", "w2", "--resume", sid, "--client", "codex"),
            ),
            (
                ("run", "--manager", "mgr", "--resume", sid, "r1", "--", "start", "--resume", sid),
                ("start", "--resume", sid),
            ),
        ):
            with self.subTest(verb=arguments[0]):
                result, stdout, stderr = self.start(*arguments)
                self.assertEqual(result, 0, stderr)
                self.assertEqual(self.calls[0][0], argv)
                self.assertEqual(self.calls[0][1]["ENSEMBLE_SID"], sid)
                self.assertEqual(Record.parse(stdout).sid, sid)
                self.assertEqual(self.roster.entries()[arguments[5]].sid, sid)
                self.roster.remove(arguments[5])

    def test_a_resume_sid_that_is_not_a_canonical_uuid4_is_a_usage_error(self):
        for sid in ("x", str(uuid.uuid1()), str(uuid.uuid4()).upper()):
            for arguments in (
                ("worker", "--manager", "mgr", "--resume", sid, "w1", "--client", "claude"),
                ("run", "--manager", "mgr", "--resume", sid, "r1", "--", "start"),
            ):
                with (
                    self.subTest(sid=sid, verb=arguments[0]),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main(arguments)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_role_names_share_the_generated_launch_sid(self):
        for explicit in ((), ("--name", "chosen")):
            with self.subTest(explicit=explicit):
                result, stdout, _ = self.start(
                    "role",
                    "--manager",
                    "mgr",
                    *explicit,
                    "--task",
                    "product-design",
                    "--credentials",
                    ' {"future-kind": [1]}\n',
                    "pm",
                    "--input",
                    "x",
                )
                self.assertEqual(result, 0)
                sid = Record.parse(stdout).sid
                name = "chosen" if explicit else f"pm-{sid[:8]}"
                self.assertEqual(self.roster.entries()[name].sid, sid)
                self.assertEqual(
                    self.calls[0][0],
                    (
                        "ensemble-delegate",
                        "run",
                        "--role",
                        "pm",
                        "--detach",
                        "--name",
                        name,
                        "--task",
                        "product-design",
                        "--credentials",
                        ' {"future-kind": [1]}\n',
                        "--input",
                        "x",
                    ),
                )

    def test_refusals_remove_only_the_shorthand_intent_and_allow_name_reuse(self):
        for code, output in ((2, ""), (0, "dry run\n"), (0, "")):
            with self.subTest(code=code, output=output):
                result, stdout, stderr = self.start(
                    "worker",
                    "--manager",
                    "mgr",
                    "w1",
                    "--note",
                    "layer option",
                    code=code,
                    output=output,
                )
                self.assertEqual((result, stdout), (2, ""))
                self.assertIn("layer message", stderr)
                self.assertEqual(self.roster.entries(), {})

    def test_a_refused_launch_prints_its_layers_reason(self):
        reason = "router.py: pick-none TASK-12 reason=not a Todo issue delegated to a role app"
        for verb, key in (("issue", "TASK-12"), ("role", "pm"), ("worker", "w1")):
            with self.subTest(verb=verb):
                result, stdout, stderr = self.start(
                    verb,
                    "--manager",
                    "mgr",
                    *(("--credentials", "{}") if verb == "role" else ()),
                    key,
                    code=2,
                    output="",
                    stderr=f"{reason}\n",
                )
                self.assertEqual((result, stdout, self.roster.entries()), (2, "", {}))
                [line] = stderr.splitlines()
                self.assertTrue(line.startswith("crew: ensemble-"), line)
                self.assertTrue(line.endswith(f": {reason}"), line)

    def test_a_busy_role_start_exits_75_and_leaves_no_entry(self):
        busy = "delegate: busy: lock dummy-lock is held by ensemble-x-1 (owner x)"
        result, stdout, stderr = self.start(
            "role",
            "--manager",
            "mgr",
            "--credentials",
            "{}",
            "pm",
            code=75,
            output="",
            stderr=f"{busy}\n",
        )
        self.assertEqual((result, stdout, self.roster.entries()), (75, "", {}))
        [line] = stderr.splitlines()
        self.assertTrue(line.startswith("crew: ensemble-delegate run --role pm "), line)
        self.assertTrue(line.endswith(f": {busy}"), line)

    def test_a_busy_run_exits_75_naming_the_holder_and_allows_name_reuse(self):
        busy = "delegate: busy: lock dummy-lock is held by ensemble-x-1 (owner x)"
        for resume in ((), ("--resume", str(uuid.uuid4()))):
            with self.subTest(resume=resume):
                result, stdout, stderr = self.start(
                    "run",
                    "--manager",
                    "mgr",
                    *resume,
                    "dummy-1",
                    "--",
                    "ensemble-delegate",
                    "open",
                    "dummy-1",
                    "--environment",
                    "dummy",
                    code=75,
                    output="",
                    stderr=f"{busy}\n",
                )
                self.assertEqual((result, stdout, self.roster.entries()), (75, "", {}))
                self.assertIn(busy, stderr)
                result, stdout, stderr = self.start(
                    "run", "--manager", "mgr", *resume, "dummy-1", "--", "starter"
                )
                self.assertEqual(result, 0, stderr)
                self.assertEqual(self.roster.entries()["dummy-1"].record, Record.parse(stdout))
                self.roster.remove("dummy-1")

    def test_a_busy_run_without_stderr_keeps_the_exit_code(self):
        result, stdout, stderr = self.start(
            "run",
            "--manager",
            "mgr",
            "r1",
            "--",
            "starter",
            code=75,
            output="",
            stderr="",
        )
        self.assertEqual((result, stdout, self.roster.entries()), (75, "", {}))
        self.assertIn("exit 75", stderr)

    def test_failed_starts_exit_one_and_leave_their_entry_lost(self):
        run = ("run", "--manager", "mgr", "r1", "--", "starter")
        role = ("role", "--manager", "mgr", "--name", "r1", "--credentials", "{}", "pm")
        receipts = (None, "invalid Record\n", " \n")
        for arguments, name, code, output in (
            (("worker", "--manager", "mgr", "w1"), "w1", 75, ""),
            (("issue", "--manager", "mgr", "TASK-12"), "TASK-12", 75, ""),
            *((argv, "r1", 75, output) for argv in (run, role) for output in receipts),
            *((run, "r1", code, "") for code in (1, 2, 3, 74, 76)),
            (("worker", "--manager", "mgr", "a"), "a", 1, ""),
            (("worker", "--manager", "mgr", "b"), "b", 2, "not empty\n"),
            (("run", "--manager", "mgr", "c", "--", "fake"), "c", 2, ""),
        ):
            with self.subTest(arguments=arguments, code=code, output=output):
                result, stdout, stderr = self.start(*arguments, code=code, output=output)
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn("layer message", stderr)
                self.assertEqual(self.roster.entries()[name].state.value, "lost")
                self.roster.remove(name)


class Reopen(CliCase):
    start = Launch.start

    def record(self, client="claude", base=None, *, top=None, **session):
        work = Path(base or self.ensemble, "work", "TASK-9")
        self.agent, run = work / "agent", work / "run"
        self.agent.mkdir(parents=True)
        run.mkdir()
        self.sid = str(uuid.uuid4())
        plan = {
            "client": client,
            "sid": self.sid,
            "workdir": str(run),
            "access": {"cwd": str(self.agent)},
            **session,
        }
        found = {"agent_name": "researcher-0f8fad5b", "host": "docker", **(top or {})}
        (run / "delegate.json").write_text(
            json.dumps({"role": "researcher", "plan": {"session": plan}, **found})
        )
        return run

    def reopen(self, *extra, crew=(), listed=(), alive=(), unlisted=False, failing=False):
        """failing: every status exits 2; unlisted: the listing exits 1."""

        def run(argv, **kwargs):
            if argv[1] == "ls":
                if unlisted:
                    return SimpleNamespace(returncode=1, stdout="", stderr="ls: down")
                return SimpleNamespace(returncode=0, stdout=ctl.lines(*listed), stderr="")
            if failing:
                return SimpleNamespace(returncode=2, stdout="", stderr="status: down")
            names = (Record.parse(line).name for line in kwargs["input"].splitlines())
            statuses = (status(name, alive=name in alive) for name in names)
            return SimpleNamespace(returncode=0, stdout=ctl.lines(*statuses), stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            return self.start("reopen", "--manager", "mgr", *crew, "TASK-9", *extra)

    def test_reopen_resumes_the_recorded_session_with_its_client(self):
        for client, base, option in (
            ("claude", self.ensemble, ()),
            ("codex", self.home, ("--work-dir", self.home)),
        ):
            with self.subTest(client=client):
                self.record(client, base)
                result, stdout, stderr = self.reopen("--display", "none", crew=option)
                self.assertEqual(result, 0, stderr)
                self.assertEqual(
                    self.calls[0][0],
                    (
                        "ensemble-session",
                        "start",
                        "researcher-0f8fad5b",
                        "--host",
                        "docker",
                        "--client",
                        client,
                        "--cwd",
                        str(self.agent),
                        "--resume",
                        self.sid,
                        "--display",
                        "none",
                    ),
                )
                self.assertEqual(self.calls[0][1]["ENSEMBLE_SID"], self.sid)
                entry = self.roster.entries()["researcher-0f8fad5b"]
                self.assertEqual((entry.sid, entry.record), (self.sid, Record.parse(stdout)))
                self.roster.remove("researcher-0f8fad5b")
                shutil.rmtree(Path(base, "work"))

    def test_reopen_refuses_options_the_run_record_sets(self):
        self.record()
        for option in (
            *("--client", "--cwd=/tmp", "--resume", "--c", "--cw", "--cl=codex", "--res"),
            *("--host", "--h=tmux", "--ho"),
        ):
            with self.subTest(option=option):
                result, stdout, stderr = self.reopen(option, "x", "--", "--cwd")
                self.assertEqual((result, stdout), (2, ""))
                self.assertIn("from the run record", stderr)
                self.assertEqual((self.calls, self.roster.entries()), ([], {}))
        result, _, stderr = self.reopen("--", "--client-flag")
        self.assertEqual(result, 0, stderr)

    def test_reopen_leaves_the_account_to_the_session(self):
        """WDRC-367: a recorded account is never restored; one given is passed on."""
        self.record(env={"ENSEMBLE_ACCOUNT": "work"})
        result, _, stderr = self.reopen("--display", "none")
        self.assertEqual(result, 0, stderr)
        self.assertNotIn("--account", self.calls[0][0])
        self.roster.remove("researcher-0f8fad5b")
        result, _, stderr = self.reopen("--account", "home")
        self.assertEqual(result, 0, stderr)
        command = self.calls[0][0]
        self.assertEqual(command[command.index("--account") :][:2], ("--account", "home"))

    def test_reopen_refuses_a_run_that_still_answers_alive(self):
        self.record()
        own(self.roster, self.fake_record("TASK-9"))
        session = self.fake_record("researcher-tui", sid=self.sid)
        for listed, alive, live in (
            ((), ("TASK-9",), "TASK-9"),
            ((session,), ("researcher-tui",), "session researcher-tui"),
        ):
            with self.subTest(live=live):
                result, stdout, stderr = self.reopen(listed=listed, alive=alive)
                self.assertEqual((result, stdout), (2, ""))
                self.assertIn(f"TASK-9: still live ({live}); close it before reopen", stderr)
                self.assertEqual((self.calls, set(self.roster.entries())), ([], {"TASK-9"}))
        result, _, stderr = self.reopen(listed=(session,))
        self.assertEqual(result, 0, stderr)
        self.assertIn("researcher-0f8fad5b", self.roster.entries())

    def test_reopen_refuses_while_liveness_is_unknown(self):
        self.record()
        own(self.roster, self.fake_record("TASK-9"))
        session = self.fake_record("researcher-tui", sid=self.sid)
        for kw, unknown in (
            ({"failing": True}, "TASK-9"),
            ({"failing": True, "listed": (session,)}, "TASK-9, session researcher-tui"),
            ({"unlisted": True}, "session listing"),
        ):
            with self.subTest(unknown=unknown):
                result, stdout, stderr = self.reopen(**kw)
                self.assertEqual((result, stdout), (2, ""))
                self.assertIn(
                    f"TASK-9: liveness unknown ({unknown}); close or forget it before reopen",
                    stderr,
                )
                self.assertEqual((self.calls, set(self.roster.entries())), ([], {"TASK-9"}))

    def test_reopen_never_carries_the_runs_handlers(self):
        handler = {"module": "ensemble.delegate.engine.handler", "state": {}, "roots": []}
        self.record(handlers=[handler])
        result, _, stderr = self.reopen("--display", "none")
        self.assertEqual(result, 0, stderr)
        command = self.calls[0][0]
        self.assertEqual(command[:3], ("ensemble-session", "start", "researcher-0f8fad5b"))
        self.assertFalse(any("handler" in word or "delegate" in word for word in command))

    def test_reopen_without_a_run_record_or_session_id_fails_clearly(self):
        for case, prepare, message in (
            ("no run", lambda: Path(self.ensemble, "work").mkdir(), "no run record"),
            ("no record", lambda: os.remove(self.record() / "delegate.json"), "no run record"),
            ("no sid", lambda: self.record(sid=""), "no role, client, session id or cwd"),
            ("other run", lambda: self.record(workdir="/elsewhere"), "records another run"),
            ("no agent name", lambda: self.record(top={"agent_name": None}), "no run record"),
            ("no host", lambda: self.record(top={"host": ""}), "no run record"),
        ):
            with self.subTest(case=case):
                prepare()
                result, stdout, stderr = self.reopen()
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn("TASK-9", stderr)
                self.assertIn(message, stderr)
                self.assertEqual((self.calls, self.roster.entries()), ([], {}))
                shutil.rmtree(Path(self.ensemble, "work"))

    def test_a_closed_issue_run_reopens_on_its_own_sid(self):
        self.record()
        own(self.roster, self.fake_record("TASK-9", sid=self.sid))
        closed = []

        def close(argv, **kwargs):
            closed.append((argv[-1], *(Record.parse(line).sid for line in kwargs["input"].split())))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=close):
            self.assertEqual(self.invoke("close", "--manager", "mgr", "TASK-9"), (0, "", ""))
        self.assertEqual((closed, self.roster.entries()), ([("close", self.sid)], {}))
        result, _, stderr = self.invoke("resume", "--manager", "mgr", "TASK-9")
        self.assertEqual(result, 2)
        self.assertIn("ensemble-crew reopen --manager=mgr TASK-9", stderr)
        result, stdout, stderr = self.reopen("--display", "none")
        self.assertEqual(result, 0, stderr)
        command = self.calls[0][0]
        self.assertEqual(command[command.index("--resume") :][:2], ("--resume", self.sid))
        entry = self.roster.entries()["researcher-0f8fad5b"]
        self.assertEqual((Record.parse(stdout).sid, entry.sid), (self.sid, self.sid))


class ResumeInput(CliCase):
    def test_multiple_names_with_input_fail_before_any_roster_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["resume", "--manager", "mgr", "a", "b", "--input", "answer"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_refusal_returns_two_and_preserves_waiting(self):
        own(self.roster, self.fake_record("r1"))
        mark(self.roster, "r1", State.WAITING)
        with mock.patch("subprocess.Popen", return_value=finished(2, "refused")):
            code, stdout, stderr = self.invoke("resume", "--manager", "mgr", "r1", "--input", "")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("refused", stderr)
        self.assertIs(self.roster.entries()["r1"].state, State.WAITING)


class NextEventContent(CliCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.answers = {}
        self.inputs = []

    def launched(self, name, *sessions, record_name=None):
        record = replace(self.fake_record(record_name or name), sessions=sessions or (name,))
        return own(self.roster, record, name)

    def next_event(self, event, *extra):
        Path(self.roster.events).write_text(f"{event}\n")

        def run(argv, **options):
            self.calls.append(tuple(argv))
            self.inputs.append(options["input"])
            code, out, err = self.answers.get(tuple(argv), (1, "", "no such session"))
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(
                "next-event", "--manager", "mgr", "--after", "0", "--gen", "0", *extra
            )

    def test_without_content_prints_only_the_event_line(self):
        self.launched("w1")
        self.assertEqual(self.next_event("12:00:00 w1 done"), (0, "1 12:00:00 w1 done\n", ""))
        self.assertEqual(self.calls, [])

    def test_follow_exits_nonzero_on_malformed_line_without_echoing_it(self):
        for invalid in ("", "diagnostic-private-payload"):
            with (
                self.subTest(invalid=invalid),
                mock.patch("time.sleep", side_effect=TimeoutError("polled past malformed line")),
            ):
                code, stdout, stderr = self.next_event(invalid, "--follow")
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn(f"{self.roster.events_path}: invalid event at line 1", stderr)
                self.assertNotIn("diagnostic-private-payload", stderr)
                self.assertEqual(self.roster.backlog()[0], 0)

    def test_each_event_uses_its_owners_ctl_content(self):
        for sessions, event, answer, expected in (
            (("w1",), "w1 done", "the answer\n", "the answer\n"),
            (
                ("test-drive",),
                "test-drive done",
                "the answer\n",
                "the answer\n",
            ),
            (
                ("w1",),
                "w1 blocked",
                "Allow this edit?\n 1. Yes",
                "Allow this edit?\n 1. Yes\n",
            ),
        ):
            with self.subTest(sessions=sessions, event=event):
                record = self.launched("w1", *sessions)
                self.answers = {(*record.ctl, "content"): (0, answer, "")}
                self.assertEqual(
                    self.next_event(f"12:00:00 {event}", "--content"),
                    (0, f"1 12:00:00 {event}\n{expected}", ""),
                )
                self.assertEqual(self.calls[-1], (*record.ctl, "content"))
                self.assertEqual(self.inputs[-1], record.to_line() + "\n")
                self.roster.remove("w1")

    def test_a_harness_event_prints_its_entry_status(self):
        for sessions, record_name in (
            (("engineer-ab12-drive",), "engineer-ab12"),
            (("engineer-ab12", "engineer-ab12-drive"), None),
        ):
            with self.subTest(sessions=sessions):
                record = self.launched("r1", *sessions, record_name=record_name)
                dead = status(record.name, alive=False, state="dead", outcome="done")
                self.answers = {(*record.ctl, "content"): (0, dead.to_line() + "\n", "")}
                self.assertEqual(
                    self.next_event("12:00:00 engineer-ab12-drive outcome done", "--content"),
                    (
                        0,
                        f"1 12:00:00 engineer-ab12-drive outcome done\n{dead.to_line()}\n",
                        "",
                    ),
                )
                self.assertNotIn(("ensemble-session", "read", "engineer-ab12-drive"), self.calls)
                self.roster.remove("r1")

    def test_a_ctl_failure_is_reported_without_falling_back_to_session_commands(self):
        record = self.launched("r1", "tui", "harness")
        self.answers = {(*record.ctl, "content"): (1, "", "not available")}
        code, stdout, _ = self.next_event("12:00:00 tui done", "--content")
        self.assertEqual(code, 0)
        self.assertIn("content unavailable:", stdout)
        self.assertIn("not available", stdout)
        self.assertEqual(self.calls, [(*record.ctl, "content")])

    def test_an_event_naming_only_control_characters_still_exits_0(self):
        self.launched("w1")
        result, stdout, _ = self.next_event("12:00:00 \x01 done", "--content")
        self.assertEqual(result, 0)
        head, content = stdout.split("\n", 1)
        self.assertEqual(head, "1 12:00:00  done")
        self.assertTrue(content.startswith("content unavailable: "), content)
        self.assertEqual(self.calls, [])

    def test_unreadable_content_still_prints_the_event_and_says_why(self):
        self.launched("w1")
        for event in ("12:00:00 gone dead", "12:00:00 gone done", "12:00:00 gone-drive exited 0"):
            with self.subTest(event=event):
                result, stdout, _ = self.next_event(event, "--content")
                self.assertEqual(result, 0)
                head, content = stdout.split("\n", 1)
                self.assertEqual(head, f"1 {event}")
                self.assertTrue(content.startswith("content unavailable: "), content)
                self.assertTrue(content.endswith("\n") and content.count("\n") == 1, content)

    def test_follow_prints_each_event_block_and_keeps_going(self):
        record = self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n12:00:01 w1 blocked\n")
        stdout = io.StringIO()
        writes = []
        stdout.write = lambda text: writes.append(text) or len(text)

        answers = iter(((0, "the answer\nline 2\n", ""), (1, "", "no such session")))

        def run(argv, **options):
            self.assertEqual(tuple(argv), (*record.ctl, "content"))
            self.assertEqual(options["input"], record.to_line() + "\n")
            code, out, err = next(answers)
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        argv = ("next-event", "--manager", "mgr", "--after", "0", "--gen", "0")
        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(KeyboardInterrupt),
        ):
            main([*argv, "--content", "--follow"])
        self.assertEqual(
            [text for text in writes if text],
            [
                "1 12:00:00 w1 done\nthe answer\nline 2\n",
                f"2 12:00:01 w1 blocked\ncontent unavailable: {ctl.COMMAND} content: "
                "no such session\n",
            ],
        )
        self.assertEqual(self.roster.backlog()[0], 0)
        self.assertEqual(len(self.roster.backlog()[2]), 2)

    def test_read_and_pending_retain_events_until_explicit_ack(self):
        self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n12:00:01 w1 blocked\n")
        result, stdout, stderr = self.invoke(
            "next-event", "--manager", "mgr", "--after", "1", "--gen", "0"
        )
        self.assertEqual((result, stdout, stderr), (0, "2 12:00:01 w1 blocked\n", ""))
        self.assertEqual(self.roster.backlog()[0], 0)
        result, stdout, stderr = self.invoke("pending", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertIn("1 12:00:00 w1 done", stdout)
        self.assertIn("2 12:00:01 w1 blocked", stdout)
        self.assertIn("N=0 GEN=0", stdout)
        for _ in range(2):
            result, _, stderr = self.invoke(
                "ack", "--manager", "mgr", "--through", "1", "--gen", "0"
            )
            self.assertEqual(result, 0, stderr)
            self.assertEqual(self.roster.backlog(), (1, 0, ["12:00:01 w1 blocked"]))
        result, _, _ = self.invoke("ack", "--manager", "mgr", "--through", "2", "--gen", "9")
        self.assertEqual(result, 1)
        self.assertEqual(self.roster.backlog()[0], 1)

    def test_pending_content_prints_owner_reply_without_acknowledging(self):
        record = self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n")
        answer = SimpleNamespace(
            returncode=0, stdout="Publication blocked\nNeeds review\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=answer) as run:
            result, output, error = self.invoke("pending", "--manager", "mgr", "--content")
        self.assertEqual(result, 0, error)
        self.assertEqual(
            output, "1 12:00:00 w1 done\nPublication blocked\nNeeds review\nN=0 GEN=0\n"
        )
        self.assertEqual(tuple(run.call_args.args[0]), (*record.ctl, "content"))
        self.assertEqual(run.call_args.kwargs["input"], record.to_line() + "\n")
        self.assertEqual(self.roster.backlog(), (0, 0, ["12:00:00 w1 done"]))

    def test_follow_exits_1_on_stale(self):
        self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n")
        result, stdout, stderr = self.invoke(
            "next-event", "--manager", "mgr", "--after", "0", "--gen", "5", "--follow"
        )
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("crew: stale line numbers", stderr)


LEASE_FREE = "env -u ENSEMBLE_HOLDER_ID -u ENSEMBLE_HOLDER_GENERATION ensemble-crew"
PAIR = ("ENSEMBLE_HOLDER_ID", "ENSEMBLE_HOLDER_GENERATION")


class StaleHolderRecovery(CliCase):
    """A commander whose holder ended keeps its pair: the error prints runnable recovery."""

    def setUp(self):
        super().setUp()
        with self.roster.hold("old") as lease:
            Path(self.roster.events).write_text(
                "12:00:00 w1 done\n12:00:01 w1 done\n12:00:02 w1 done\n"
            )
        self.lease = lease
        self.pair(lease)

    def pair(self, lease):
        values = (lease.id, str(lease.generation))
        self.enterContext(mock.patch.dict(os.environ, dict(zip(PAIR, values, strict=True))))

    @contextlib.contextmanager
    def lease_free(self):
        env = {key: value for key, value in os.environ.items() if key not in PAIR}
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def command(self, stderr, label):
        line = next(line for line in stderr.splitlines() if label in line)
        words = shlex.split(line[line.index("env -u") :].removesuffix(", then attach"))
        self.assertEqual(shlex.join(words[:6]), LEASE_FREE)
        return words[6:]

    def test_attach_after_holder_ended_prints_recovery_notes(self):
        result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(
            stderr.splitlines(),
            [
                f"crew: {self.directory}: stale holder generation; start a new manager holder",
                "crew: this environment's ENSEMBLE_HOLDER_ID/ENSEMBLE_HOLDER_GENERATION name an"
                " ended holder; no holder is active; handled N=0 GEN=0",
                "crew: lasting fix: restart the commander's client under a new holder, then"
                " attach and arm its monitor: command: ensemble-crew hold --manager mgr"
                " --session mgr -- <client resume command> (ensemble-crew hold --help)",
                f"crew: handled events after N=0? first: {LEASE_FREE} ack --manager mgr"
                " --through <line> --gen 0, then attach; unacknowledged events replay",
                "crew: until then, attach without a lease, keeping the prefix on the commands it"
                f" prints: {LEASE_FREE} attach --manager mgr",
                "crew: and monitor with its monitor: line; with no ack first, this is that line:"
                f" {LEASE_FREE} next-event --manager mgr --after 0 --gen 0 --content --follow",
            ],
        )
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()) as help:
            main(["hold", "--help"])
        self.assertIn("-- claude --continue", help.getvalue())
        self.assertIn("codex resume --last", help.getvalue())

    def test_printed_lease_free_commands_run(self):
        _, _, stderr = self.invoke("attach", "--manager", "mgr")
        attach = self.command(stderr, "until then")
        monitor = self.command(stderr, "this is that line")
        self.assertEqual(monitor[-1], "--follow")
        with self.lease_free():
            with self.listed():
                result, stdout, error = self.invoke(*attach)
            self.assertEqual(result, 0, error)
            self.assertTrue(stdout.endswith("N=0 GEN=0\n"), stdout)
            result, stdout, error = self.invoke(*monitor[:-1])
        self.assertEqual(result, 0, error)
        self.assertTrue(stdout.startswith("1 12:00:00 w1 done\n"), stdout)

    def test_next_event_offers_ack_through_its_reader_line(self):
        result, stdout, stderr = self.invoke(
            "next-event", "--manager", "mgr", "--after", "2", "--gen", "0", "--follow"
        )
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn(
            f"crew: handled through 2? first: {LEASE_FREE} ack --manager mgr --through 2"
            " --gen 0, then attach\n",
            stderr,
        )
        self.assertNotIn("<line>", stderr)
        ack = self.command(stderr, "handled through")
        with self.lease_free():
            self.assertEqual(self.invoke(*ack), (0, "", ""))
            self.assertEqual(self.roster.lease_state(), (2, 0, None))
            with self.listed():
                result, stdout, error = self.invoke("attach", "--manager", "mgr")
        self.assertEqual(result, 0, error)
        self.assertNotIn("1 12:00:00", stdout)
        self.assertNotIn("2 12:00:01", stdout)
        self.assertIn("3 12:00:02 w1 done\n", stdout)

    def test_ack_note_keeps_placeholder_without_a_reader_line_past_n(self):
        placeholder = "--through <line> --gen 0, then attach; unacknowledged events replay"
        for argv in (
            ("next-event", "--manager", "mgr", "--after", "end", "--gen", "0"),
            ("next-event", "--manager", "mgr", "--after", "2", "--gen", "1"),
            ("pending", "--manager", "mgr"),
        ):
            with self.subTest(argv=argv):
                result, _, stderr = self.invoke(*argv)
                self.assertEqual(result, 1)
                self.assertIn(placeholder, stderr)
                self.assertNotIn("handled through", stderr)
        with self.lease_free():
            self.assertEqual(
                self.invoke("ack", "--manager", "mgr", "--through", "2", "--gen", "0")[0], 0
            )
        for after in ("1", "2"):
            with self.subTest(after=after):
                result, _, stderr = self.invoke(
                    "next-event", "--manager", "mgr", "--after", after, "--gen", "0"
                )
                self.assertEqual(result, 1)
                self.assertIn("handled events after N=2? first:", stderr)
                self.assertNotIn("handled through", stderr)

    def follow_until_released(self, after, appended=None):
        """Follow under a live holder that ends after the reader drains; appended arrives first."""
        live = Roster(self.directory)
        with live.hold("live") as lease, contextlib.ExitStack() as stack:
            self.pair(lease)
            steps = [live.release]
            if appended is not None:
                steps.insert(0, lambda: append_line(live.events, appended))
            stack.enter_context(mock.patch("time.sleep", side_effect=lambda _: steps.pop(0)()))
            return self.invoke(
                "next-event", "--manager", "mgr", "--after", after, "--gen", "0", "--follow"
            )

    def test_follow_offers_ack_through_last_printed_line(self):
        for after, appended, expected in (
            ("0", None, "1 12:00:00 w1 done\n2 12:00:01 w1 done\n3 12:00:02 w1 done\n"),
            ("end", "12:00:03 w1 done\n", "4 12:00:03 w1 done\n"),
        ):
            with self.subTest(after=after):
                result, stdout, stderr = self.follow_until_released(after, appended)
                self.assertEqual((result, stdout), (1, expected))
                line = expected.splitlines()[-1].split()[0]
                self.assertIn(
                    f"crew: handled through {line}? first: {LEASE_FREE} ack --manager mgr"
                    f" --through {line} --gen 0, then attach\n",
                    stderr,
                )

    def test_takeover_names_the_live_holder_and_force(self):
        with Roster(self.directory).hold("new"):
            result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(
            stderr.splitlines()[1:],
            [
                "crew: this environment's ENSEMBLE_HOLDER_ID/ENSEMBLE_HOLDER_GENERATION name an"
                " ended holder; new holds manager mgr now; handled N=0 GEN=0",
                "crew: use new's commander; take over only on purpose: ensemble-crew hold --force"
                " --manager mgr --session mgr -- <client resume command> (ensemble-crew hold"
                " --help)",
            ],
        )

    def test_resume_prints_notes_once_and_resumes_nothing(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("resumed")):
            result, stdout, stderr = self.invoke("resume", "--manager", "mgr", "a", "b")
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(stderr.count("stale holder generation"), 1)
        self.assertEqual(stderr.count("lasting fix"), 1)

    def test_holder_no_longer_alive_gets_notes(self):
        data = json.loads(Path(self.roster.path).read_text())
        data["holder"] = {"id": self.lease.id, "session": "old"}
        Path(self.roster.path).write_text(json.dumps(data))
        result, _, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual(result, 1)
        self.assertTrue(stderr.startswith(f"crew: {self.directory}: holder no longer alive\n"))
        self.assertIn("no holder is active; handled N=0 GEN=0", stderr)
        self.assertIn("lasting fix", stderr)
        with self.lease_free(), Roster(self.directory).hold("new"):
            result, _, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual((result, stderr), (1, f"crew: {self.directory}: held by new\n"))

    def test_notes_create_no_directory(self):
        result, _, stderr = self.invoke("attach", "--manager", "fresh")
        self.assertEqual(result, 1)
        self.assertIn("no holder is active; handled N=0 GEN=0", stderr)
        self.assertIn(f"{LEASE_FREE} attach --manager fresh", stderr)
        self.assertFalse(Path(self.ensemble, "managers", "fresh").exists())

    def test_unreadable_roster_prints_error_without_notes(self):
        holder_lock = Path(self.directory, "holder.lock")
        holder_lock.unlink()
        holder_lock.mkdir()
        result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(
            stderr,
            f"crew: {self.directory}: stale holder generation; start a new manager holder\n",
        )


URL = "https://github.com/o/r/pull/7"
HEADER = "name\tkind\tstate\tnote\treply\tpr\tchecks\tmergeable\treview\n"
VIEW = (
    '{"number":7,"mergeable":"MERGEABLE","reviewDecision":"APPROVED",'
    '"statusCheckRollup":[{"status":"COMPLETED","conclusion":"SUCCESS"}]}'
)


class Report(CliCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.statuses = {}
        self.answers: dict[tuple[str, ...], Any] = {}

    def launched(self, name, *, note=None, record=None, alive=True, outcome=None):
        record = own(
            self.roster,
            replace(record or self.fake_record(name), sid=str(uuid.uuid4())),
            name,
            note,
        )
        self.statuses[record.name] = status(
            record.name, alive=alive, state="working" if alive else "gone", outcome=outcome
        )
        self.answers[(*record.ctl, "status")] = self.status_answer
        return record

    def status_answer(self, text):
        lines = (Record.parse(line).name for line in text.splitlines())
        return 0, "".join(self.statuses[name].to_line() + "\n" for name in lines), ""

    def report(self):
        def run(argv, **kwargs):
            self.calls.append(tuple(argv))
            key = (
                (*argv, Record.parse(kwargs["input"]).name)
                if argv[-1] == "content"
                else tuple(argv)
            )
            answer = self.answers.get(key, (1, "", "no such session"))
            code, out, err = answer if isinstance(answer, tuple) else answer(kwargs.get("input"))
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke("report", "--manager", "mgr")

    def gh_calls(self):
        return [call for call in self.calls if call[0] == "gh"]

    def delegate_record(self, name, workdir="/w"):
        return Record(
            1,
            "role",
            name,
            (name,),
            None,
            workdir,
            ("ensemble-delegate",),
            ("ensemble-delegate", "resume", workdir),
            None,
            "engineer",
        )

    def test_one_row_per_entry_sorted_with_header(self):
        self.launched("b", note="fix the parser")
        self.launched("a", alive=False, outcome="done")
        self.roster.begin(Spec("c", ("start",)), sid=str(uuid.uuid4()))
        self.answers[(ctl.COMMAND, "content", "b")] = (0, "\n  first line  \nsecond\n", "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(
            stdout,
            HEADER
            + "a\tfake\tdone\t-\t-\t-\t-\t-\t-\n"
            + "b\tfake\tworking\tfix the parser\tfirst line\t-\t-\t-\t-\n"
            + "c\t-\tstarting\t-\t-\t-\t-\t-\t-\n",
        )

    def test_reply_is_first_nonblank_line_cut_and_dash_when_unavailable(self):
        self.launched("w1")
        self.launched("w2")
        self.launched("w3")
        self.answers[(ctl.COMMAND, "content", "w1")] = (0, "x" * 100 + "\n", "")
        self.answers[(ctl.COMMAND, "content", "w2")] = (0, " \n\n", "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        replies = [line.split("\t")[4] for line in stdout.splitlines()[1:]]
        self.assertEqual(replies, ["x" * 80, "-", "-"])

    def test_pr_from_note_url_fills_pr_columns(self):
        self.launched("w1", note=f"TASK-1 {URL}")
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (0, VIEW, "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertTrue(stdout.endswith("\t#7\tpass\tmergeable\tapproved\n"), stdout)

    def test_pr_from_role_outcome_url_when_note_has_none(self):
        record = self.launched(
            "r1", record=self.delegate_record("engineer-ab12"), alive=False, outcome="done"
        )
        self.launched("r2", record=self.delegate_record("engineer-cd34", "/v"))
        self.launched("p1", alive=False, outcome="done")
        outcome = ("ensemble-delegate", "outcome")
        inputs = []

        def answer(text):
            inputs.append(text)
            return 0, f'{{"status":"done","url":"{URL}"}}\n', ""

        self.answers[outcome] = answer
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (0, VIEW, "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        rows = {line.split("\t")[0]: line.split("\t")[5:] for line in stdout.splitlines()[1:]}
        self.assertEqual(rows["r1"], ["#7", "pass", "mergeable", "approved"])
        self.assertEqual(rows["r2"], ["-"] * 4)
        self.assertEqual(rows["p1"], ["-"] * 4)
        self.assertEqual(inputs, [record.to_line() + "\n"])
        self.assertEqual(self.calls.count(outcome), 1)

    def test_no_pr_url_makes_no_gh_call(self):
        for note, env, called in (
            (None, {}, False),
            ("https://ghe.corp/o/r/pull/1", {"GH_HOST": "ghe.corp"}, True),
        ):
            with self.subTest(note=note), mock.patch.dict(os.environ, env):
                self.launched("w1", note=note)
                self.calls = []
                result, stdout, _ = self.report()
                self.assertEqual(result, 0)
                self.assertEqual(bool(self.gh_calls()), called, stdout)
                if not called:
                    self.assertTrue(stdout.endswith("\t-\t-\t-\t-\n"), stdout)
                self.roster.remove("w1")

    def test_pipeline_outcome_status_does_not_query_an_unsupported_outcome_command(self):
        pipeline = replace(
            self.delegate_record("TASK-12"),
            kind="pipeline",
            ctl=("ensemble-orchestrator",),
            resume=("ensemble-orchestrator", "resume", "TASK-12"),
        )
        self.launched("TASK-12", record=pipeline, alive=False, outcome="done")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("\tpipeline\tdone\t", stdout)
        self.assertNotIn(("ensemble-orchestrator", "outcome"), self.calls)

    def test_status_gh_and_outcome_failures_go_to_stderr_and_exit_0(self):
        self.launched("bad-gh", note=URL)
        self.launched("bad-json", note=URL.replace("7", "8"))
        self.launched("bad-outcome", record=self.delegate_record("engineer-ab12"), outcome="done")
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (1, "", "HTTP 404")
        self.answers[("gh", "pr", "view", URL.replace("7", "8"), "--json", VIEW_FIELDS)] = (
            0,
            "not json",
            "",
        )
        self.answers[("ensemble-delegate", "outcome")] = (0, "[]", "")
        result, stdout, stderr = self.report()
        self.assertEqual(result, 0)
        rows = {line.split("\t")[0]: line.split("\t")[5:] for line in stdout.splitlines()[1:]}
        self.assertEqual(rows["bad-gh"], ["#7", "?", "?", "?"])
        self.assertEqual(rows["bad-json"], ["#8", "?", "?", "?"])
        self.assertEqual(rows["bad-outcome"], ["-"] * 4)
        self.assertIn("crew: bad-gh: ", stderr)
        self.assertIn("HTTP 404", stderr)
        self.assertIn("crew: bad-json: ", stderr)
        self.assertNotIn("bad-outcome", stderr)
        self.answers[("ensemble-delegate", "outcome")] = (1, "", "no run")
        self.answers[("ensemble-delegate", "status")] = self.status_answer
        self.answers[(ctl.COMMAND, "status")] = (2, "", "ctl broke")
        result, stdout, stderr = self.report()
        self.assertEqual(result, 0)
        states = {line.split("\t")[0]: line.split("\t")[2] for line in stdout.splitlines()[1:]}
        self.assertEqual(
            states, {"bad-gh": "unknown", "bad-json": "unknown", "bad-outcome": "working"}
        )
        self.assertIn("crew: bad-gh: ", stderr)
        self.assertIn("ctl broke", stderr)
        self.assertIn("crew: bad-outcome: ", stderr)
        self.assertIn("no run", stderr)
        self.answers[("ensemble-delegate", "outcome")] = (0, "not json", "")
        result, _, stderr = self.report()
        self.assertEqual(result, 0)
        self.assertIn("crew: bad-outcome: Expecting value", stderr)

    def test_empty_roster_prints_header_only(self):
        self.assertEqual(self.report(), (0, HEADER, ""))

    def test_tabs_and_control_characters_are_flattened(self):
        self.launched("w1")
        self.answers[(ctl.COMMAND, "content", "w1")] = (0, "a\tb\x1b[31mc\n", "")
        _, stdout, _ = self.report()
        self.assertEqual(stdout.splitlines()[1].split("\t")[4], "a b[31mc")

    def test_report_leaves_roster_unchanged(self):
        self.launched("w1", alive=False)
        self.launched(
            "r1", record=self.delegate_record("engineer-ab12"), alive=False, outcome="needs_input"
        )
        self.answers[("ensemble-delegate", "outcome")] = (0, "null\n", "")
        with self.roster.hold("another commander"):
            before = Path(self.roster.path).read_bytes()
            result, stdout, stderr = self.report()
            after = Path(self.roster.path).read_bytes()
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("\tgone\t", stdout)
        self.assertIn("\twaiting\t", stdout)
        self.assertEqual(after, before)


class View(CliCase):
    def setUp(self):
        super().setUp()
        for name in ("a", "b"):
            own(self.roster, self.fake_record(name))

    def view(self, *argv, answers):
        def run(_argv, **kwargs):
            name = Record.parse(kwargs["input"]).name
            return SimpleNamespace(returncode=answers[name][0], stdout=answers[name][1], stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(*argv, "--manager", "mgr")

    def test_show_prints_each_layers_attach_command_and_exits_0(self):
        answers = {"a": (0, "attach a\n"), "b": (0, "")}
        self.assertEqual(self.view("show", "a", "b", answers=answers), (0, "attach a\n", ""))

    def test_a_failed_entry_is_named_on_stderr_and_exits_1(self):
        answers = {"a": (1, ""), "b": (0, "")}
        result, stdout, stderr = self.view("hide", "a", "b", answers=answers)
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(stderr, f"crew: a: {ctl.COMMAND} hide: exit 1\n")


def _record(kind: str, name: str) -> Record:
    return Record(
        1,
        kind,
        name,
        (),
        str(uuid.uuid4()),
        None,
        ("ensemble-session",),
        ("ensemble-session", "resume", name),
        "opaque",
        None,
    )


class ListCase(CliCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.ensemble, "managers")

    def manager(self, name: str, *entries: tuple[str, str, State]) -> Roster:
        roster = Roster(os.path.join(self.root, name))
        os.makedirs(roster.directory, mode=0o700, exist_ok=True)
        for entry, kind, state in entries:
            record = _record(kind, entry)
            roster.begin(Spec(entry, ("fake-ctl", "start")), sid=record.sid)
            roster.commit(entry, record)
            if state != State.LIVE:
                mark(roster, entry, state)
        return roster

    def session(self, name: str, **fields) -> None:
        record = {"client": "kind-a", "sid": "s-1", "pid": 4101, "cwd": "/home/user/project"}
        Path(self.root, name, "session.json").write_text(json.dumps({**record, **fields}))


class ListTest(ListCase):
    def test_managers_without_records_print_dashes_sorted(self):
        for name in ("updraft-manager", "ensemble-manager", "agent-usage-manager"):
            self.manager(name)
        self.assertEqual(
            self.invoke("list"),
            (
                0,
                "agent-usage-manager\tnot-held\t-\t-\n  -\n"
                "ensemble-manager\tnot-held\t-\t-\n  -\n"
                "updraft-manager\tnot-held\t-\t-\n  -\n",
                "",
            ),
        )

    def test_a_record_prints_its_client_and_cwd(self):
        self.manager("demo")
        self.session("demo", cwd="/home/user/other project")
        self.assertEqual(
            self.invoke("list"), (0, "demo\tnot-held\tkind-a\t/home/user/other project\n  -\n", "")
        )

    def test_entries_print_kind_and_state_under_their_manager(self):
        self.manager("demo", ("w1", "worker", State.LIVE), ("r1", "role", State.GONE))
        self.assertEqual(
            self.invoke("list"),
            (0, "demo\tnot-held\t-\t-\n  r1\trole\tgone\n  w1\tworker\tlive\n", ""),
        )

    def test_an_entry_without_a_record_has_no_kind(self):
        roster = self.manager("demo")
        roster.begin(Spec("s1", ("fake-ctl", "start")), sid=str(uuid.uuid4()))
        self.assertEqual(self.invoke("list"), (0, "demo\tnot-held\t-\t-\n  s1\t-\tstarting\n", ""))

    def test_no_entries_print_one_dash_row(self):
        os.makedirs(os.path.join(self.root, "bare"))
        self.manager("empty")
        self.assertEqual(
            self.invoke("list"),
            (0, "bare\tnot-held\t-\t-\n  -\nempty\tnot-held\t-\t-\n  -\n", ""),
        )

    def test_a_held_manager_shows_its_state(self):
        roster = self.manager("demo")
        self.session("demo", pid=4101)
        with roster.hold("owner"), mock.patch("os.kill"):
            self.assertEqual(
                self.invoke("list")[1].splitlines()[0],
                "demo\tclient-running\tkind-a\t/home/user/project",
            )

    def test_a_corrupt_roster_shows_an_error_row_and_exits_1(self):
        broken = self.manager("broken")
        self.manager("ok", ("w1", "worker", State.LIVE))
        Path(broken.path).write_text("{")
        result, stdout, _ = self.invoke("list")
        lines = stdout.splitlines()
        self.assertEqual(result, 1)
        self.assertTrue(lines[0].startswith(f"broken\tnot-held\t-\t-\terror\t{broken.path}: "))
        self.assertEqual(lines[1:], ["ok\tnot-held\t-\t-", "  w1\tworker\tlive"])

    def test_an_invalid_record_shows_an_error_row_and_exits_1(self):
        self.manager("bad", ("w1", "worker", State.LIVE))
        self.manager("ok")
        Path(self.root, "bad", "session.json").write_text("[]")
        path = os.path.join(self.root, "bad", "session.json")
        self.assertEqual(
            self.invoke("list"),
            (
                1,
                f"bad\tnot-held\t-\t-\terror\t{path}: want exactly client, sid, pid and cwd\n"
                "  w1\tworker\tlive\nok\tnot-held\t-\t-\n  -\n",
                "",
            ),
        )

    def test_an_unreadable_manager_prints_dashes_and_its_error(self):
        self.manager("bad")
        self.manager("ok")
        lock = os.path.join(self.root, "bad", "roster.lock")
        os.mkdir(lock)
        self.assertEqual(
            self.invoke("list"),
            (1, f"bad\t-\t-\t-\terror\t{lock}: not a regular file\nok\tnot-held\t-\t-\n  -\n", ""),
        )

    def test_no_managers_prints_the_new_manager_usage(self):
        usage = (0, "no managers yet; start one: ensemble <name>\n", "")
        self.assertEqual(self.invoke("list"), usage)
        os.makedirs(self.root)
        Path(self.root, "file").touch()
        self.assertEqual(self.invoke("list"), usage)

    def test_list_takes_no_arguments_or_manager(self):
        for argv in (("list", "extra"), ("list", "--manager", "demo")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)
        self.manager("demo")
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1", "ENSEMBLE_MANAGER": "x"}):
            self.assertEqual(self.invoke("list"), (0, "demo\tnot-held\t-\t-\n  -\n", ""))

    def test_fields_cannot_break_a_row(self):
        roster = self.manager("demo")
        self.session("demo", cwd="/a\tb\x1bc\nd")
        roster.begin(Spec("s1", ("fake-ctl", "start")), sid=str(uuid.uuid4()))
        self.assertEqual(
            self.invoke("list"),
            (0, "demo\tnot-held\tkind-a\t/a bcd\n  s1\t-\tstarting\n", ""),
        )


def _dead(pid, _signal):
    raise ProcessLookupError(pid)


class SelectTest(ListCase):
    PROMPT = "select a manager [1-{}], Enter cancels: "

    def select(self, answers, *, kill=_dead):
        with (
            mock.patch("sys.stdin", io.StringIO(answers)),
            mock.patch("os.kill", side_effect=kill),
        ):
            return self.invoke("list", "--select")

    def test_rows_are_numbered_on_stderr_and_a_number_selects(self):
        self.manager("alpha", ("w1", "worker", State.LIVE))
        self.manager("demo")
        self.assertEqual(
            self.select("2\n"),
            (
                0,
                "open demo\n",
                "1) alpha\tnot-held\t-\t-\n  w1\tworker\tlive\n2) demo\tnot-held\t-\t-\n  -\n"
                + self.PROMPT.format(2),
            ),
        )

    def test_a_listed_name_selects(self):
        self.manager("alpha")
        self.manager("demo")
        self.assertEqual(self.select("demo\n")[:2], (0, "open demo\n"))

    def test_an_empty_line_eof_or_interrupt_cancels(self):
        self.manager("demo")
        for answers in ("\n", "", "  \n"):
            with self.subTest(answers=answers):
                self.assertEqual(self.select(answers)[:2], (0, ""))
        with mock.patch("sys.stdin", mock.Mock(readline=mock.Mock(side_effect=KeyboardInterrupt))):
            self.assertEqual(self.invoke("list", "--select")[:2], (0, ""))

    def test_an_unknown_answer_prompts_again(self):
        self.manager("demo")
        result, stdout, stderr = self.select("9\nno\x1bpe\n1\n")
        self.assertEqual((result, stdout), (0, "open demo\n"))
        self.assertIn("crew: no manager 9; give 1-1 or a name\n", stderr)
        self.assertIn("crew: no manager nope; give 1-1 or a name\n", stderr)
        self.assertEqual(stderr.count(self.PROMPT.format(1)), 3)

    def test_cancel_after_a_listing_error_exits_1(self):
        Path(self.manager("broken").path).write_text("{")
        self.assertEqual(self.select("\n")[:2], (1, ""))

    def test_no_managers_prints_the_usage_without_a_prompt(self):
        self.assertEqual(
            self.select("1\n"), (0, "", "no managers yet; start one: ensemble <name>\n")
        )

    def test_each_holder_state_and_record_gives_its_plan(self):
        roster = self.manager("demo")
        self.assertEqual(self.select("1\n")[1], "open demo\n")
        self.session("demo")
        self.assertEqual(self.select("1\n")[1], "resume-new demo\n")
        with roster.hold("demo"):
            self.assertEqual(self.select("1\n")[1], "resume-held demo\n")
            self.assertEqual(self.select("1\n", kill=None)[1], "attach demo\n")

    def test_refusals_open_nothing_and_exit_1(self):
        roster = self.manager("demo")
        self.session("demo")
        refused = "crew: manager demo: {}; nothing opened\n"
        result, stdout, stderr = self.select("1\n", kill=None)
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(
            stderr.endswith(refused.format("its client 4101 runs without a holder; exit it first"))
        )
        with roster.hold("work"):
            result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.endswith(refused.format("held by work")))
        lock = os.path.join(self.root, "demo", "roster.lock")
        os.unlink(lock)
        os.mkdir(lock)
        result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.endswith(refused.format(f"{lock}: not a regular file")))

    def test_an_unreadable_record_warns_and_opens_without_resuming(self):
        self.manager("demo")
        Path(self.root, "demo", "session.json").write_text("[]")
        path = os.path.join(self.root, "demo", "session.json")
        result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (0, "open demo\n"))
        self.assertTrue(
            stderr.endswith(
                f"crew: manager demo: {path}: want exactly client, sid, pid and cwd;"
                " its session is not resumed\n"
            )
        )


class ResumeCommanderTest(CliCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.directory, mode=0o700)
        self.cwd = os.path.join(self.home, "a b'c\x15d")
        os.mkdir(self.cwd)
        record = {"client": "kind-a", "sid": "s-1", "pid": 1 << 40, "cwd": self.cwd}
        Path(self.directory, "session.json").write_text(json.dumps(record))
        argv = '["kind-a", "--resume", "s-1"]'
        self.run = self.enterContext(
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=argv))
        )
        self.chdir = self.enterContext(mock.patch("os.chdir"))
        self.execvp = self.enterContext(mock.patch("os.execvp"))

    def under(self, lease):
        return mock.patch.dict(
            os.environ,
            {
                "ENSEMBLE_MANAGER": "mgr",
                "ENSEMBLE_HOLDER_ID": lease.id,
                "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
            },
        )

    def test_under_its_hold_it_execs_the_recorded_client_in_its_cwd(self):
        with self.roster.hold("mgr") as lease, self.under(lease):
            self.invoke("resume-commander")
        self.chdir.assert_called_once_with(self.cwd)
        self.execvp.assert_called_once_with("kind-a", ("kind-a", "--resume", "s-1"))

    def test_without_a_holder_pair_it_is_a_usage_error(self):
        result, stdout, stderr = self.invoke("resume-commander", "--manager", "mgr")
        self.assertEqual((result, stdout), (2, ""))
        self.assertIn("run resume-commander under the manager's hold: ensemble list", stderr)
        self.execvp.assert_not_called()

    def test_an_ended_holder_is_refused(self):
        with self.roster.hold("mgr") as lease:
            pass
        with self.under(lease):
            result, _, stderr = self.invoke("resume-commander")
        self.assertEqual(result, 1)
        self.assertIn("holder", stderr)
        self.execvp.assert_not_called()

    def test_a_refused_target_execs_nothing(self):
        Path(self.directory, "session.json").unlink()
        with self.roster.hold("mgr") as lease, self.under(lease):
            result, _, stderr = self.invoke("resume-commander")
        self.assertEqual(result, 1)
        self.assertIn("no session record", stderr)
        self.chdir.assert_not_called()
        self.execvp.assert_not_called()


class InsideAnAgentSession(CliCase):
    def test_run_hold_and_resume_exit_2_and_write_nothing(self):
        for argv in (
            ("run", "--manager", "mgr", "w1", "--", "true"),
            ("hold", "--manager", "mgr", "--", "true"),
            ("resume", "--manager", "mgr", "w1"),
            ("worker", "--manager", "mgr", "w1", "--client", "claude"),
            ("reopen", "--manager", "mgr", "TASK-12"),
            ("issue", "--manager", "mgr", "TASK-12"),
            ("role", "--manager", "mgr", "pm", "--input", "x"),
            ("resume-commander", "--manager", "mgr"),
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}),
            ):
                result, stdout, stderr = self.invoke(*argv)
                self.assertEqual((result, stdout), (2, ""))
                self.assertTrue(stderr.startswith("crew: refused inside an agent session"), stderr)
                self.assertFalse(os.path.exists(self.directory))

    def test_help_and_control_commands_still_run(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            help_text(self, main, ["--help"])
            self.assertNotIn("refused", self.invoke("status", "--manager", "mgr")[2])


HELP_COMMANDS = [
    "hold",
    "run",
    "worker",
    "reopen",
    "issue",
    "role",
    "attach",
    "status",
    "report",
    "resume",
    "hide",
    "show",
    "close",
    "forget",
    "close-all",
    "note",
    "release",
    "pending",
    "ack",
    "next-event",
    "list",
    "resume-commander",
]


class HelpContracts(Case):
    def test_role_options_are_owned_before_role_and_remainder_is_opaque(self):
        parsed = cli._parser().parse_args(
            [
                "role",
                "--task=build",
                "--credentials",
                "",
                "engineer",
                "--future-switch",
                "--input",
                "--task wrong",
            ]
        )
        self.assertEqual((parsed.key, parsed.task, parsed.credentials), ("engineer", "build", ""))
        self.assertEqual(parsed.arguments, ["--future-switch", "--input", "--task wrong"])
        text = self.help("role")
        self.assertIn("--task TASK", text)
        self.assertIn("--credentials JSON", text)
        self.assertIn("before ROLE", text)

    def test_visibility_help_does_not_require_roster_ownership(self):
        for command in ("show", "hide"):
            text = self.help(command)
            self.assertNotIn("lease access", text)
            self.assertIn("no holder lease", text)

    def test_published_operation_examples_use_real_parser_before_dispatch(self):
        check_examples(
            self,
            self.help,
            [None, *HELP_COMMANDS],
            check=lambda command: parse_only(cli.main, command[1:]),
        )

    def help(self, command=None, flag="--help"):
        return help_text(self, cli.main, ([] if command is None else [command]) + [flag])

    def test_every_surface_has_purpose_and_safe_help_under_invalid_runtime_context(self):
        before = sorted(Path(self.home).rglob("*"))
        source = mock.Mock()
        source.read.side_effect = AssertionError("help read stdin")
        with (
            mock.patch.dict(
                os.environ,
                {"ENSEMBLE_AGENT_SESSION": "1", "ENSEMBLE_HOLDER_ID": "invalid"},
            ),
            mock.patch("sys.stdin", source),
            mock.patch("subprocess.run") as run,
            mock.patch("subprocess.Popen") as popen,
        ):
            for command in [None, *HELP_COMMANDS]:
                for flag in ("--help", "-h"):
                    with self.subTest(command=command, flag=flag):
                        text = self.help(command, flag)
                        self.assertIn("ensemble-crew", text)
                        if command is not None:
                            self.assertIn("Examples:", text)
                            self.assertIn("ensemble-crew --help", text)
            run.assert_not_called()
            popen.assert_not_called()
        self.assertEqual(sorted(Path(self.home).rglob("*")), before)

    def test_root_lists_each_command_with_a_purpose(self):
        text = self.help()
        self.assertEqual(text.split("{")[1].split("}")[0].split(","), HELP_COMMANDS)
        self.assertEqual(re.findall(r"^    (\S+) +\S", text, re.MULTILINE), HELP_COMMANDS)

    def test_boundary_help_and_recovery_are_explicit(self):
        for verb, owner in (
            ("worker", "ensemble-session start --help"),
            ("reopen", "ensemble-session start --help"),
            ("issue", "ensemble-orchestrator router --help"),
            ("role", "ensemble-delegate run --help"),
        ):
            with self.subTest(verb=verb):
                text = self.help(verb)
                self.assertIn(owner, text)
                self.assertIn("before", text)
                self.assertIn("Forwarded --help", text)
        self.assertIn("-- COMMAND [ARG ...]", self.help("hold"))
        self.assertIn("-- COMMAND [ARG ...]", self.help("run"))
        self.assertNotIn("--after", self.help("attach"))
        self.assertIn("does not acknowledge events", self.help("attach"))
        self.assertIn("monitor expired", self.help("next-event"))
        self.assertIn(
            "A locked run's environment lock goes only once its container is confirmed removed.",
            " ".join(self.help("close").split()),
        )

    def test_published_worker_example_keeps_session_and_client_tokens(self):
        parser = cli._parser()
        argv = shlex.split(
            "worker --manager demo helper --client codex --display none -- --model 'a b'"
        )
        parsed = parser.parse_args(argv)
        self.assertEqual((parsed.manager, parsed.key), ("demo", "helper"))
        self.assertEqual(
            parsed.arguments, ["--client", "codex", "--display", "none", "--", "--model", "a b"]
        )
        self.assertEqual(parser.parse_args(["worker", "helper", "--help"]).arguments, ["--help"])

    def test_command_tail_is_not_interpreted_as_outer_help(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.assertEqual(cli.main(["hold", "--manager", "demo", "--", "codex", "--help"]), 0)
        self.assertEqual(popen.call_args.args[0], ("codex", "--help"))
