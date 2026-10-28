"""Pure logic over fake objects and in-process clients."""

import contextlib
import io
import json
import os
import re
import shlex
import sys
import types
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ensemble.delegate import ConfigError, Outcome, cli
from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.composer.base import TEAM
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Layout, Result
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import NotShowable, SessionBusy, SessionError
from ensemble.utils import Record, Status
from tests.delegate.case import SID, Case, committed
from tests.delegate.cli_case import (
    HARNESS,
    HEADS,
    KEYS,
    PLACEMENT,
    TUI,
    Base,
    RefusingClient,
)
from tests.delegate.fakes.harness_session import (
    DrivenDelegate,
    HarnessSession,
    RecordingHost,
    harness_launches,
)
from tests.delegate.harness import HarnessCase
from tests.delegate.unit.runs import DONE
from tests.session import fake_client
from tests.session.case import SID as HARNESS_SID
from tests.session.case import no_spawn
from tests.session.modules import client_loader
from tests.utils.case import local_section
from tests.utils.cli_help import check_examples, help_text, parse_only


class Detaching:
    """A delegate class for `run --detach`: records each create's role, Spec and placement, and
    its environment; its detach returns RECORD or raises `failure`.
    """

    made: list = []
    environments: list = []
    failure: Exception | None = None
    construct_failure: Exception | None = None

    @classmethod
    def create(cls, role, spec, **placement):
        Detaching.made.append((role, spec, placement))
        Detaching.environments.append(dict(os.environ))
        return cls(role, spec, **placement)

    def __init__(self, role, spec, **placement):
        if isinstance(Detaching.failure, ConfigError):
            raise Detaching.failure
        if Detaching.construct_failure is not None:
            raise Detaching.construct_failure

    def detach(self, *, outer=None):
        if Detaching.failure is not None:
            raise Detaching.failure
        return RECORD


RECORD = Record(
    1,
    "role",
    TUI,
    (HARNESS, TUI),
    SID,
    "/work",
    ("ensemble-delegate",),
    ("ensemble-delegate", "resume", "/work"),
    "mgr",
    "dummy-tester",
)


class Detach(Base):
    """`run --detach` on a Detaching delegate class; events file `events`."""

    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.tmp, "events")
        Detaching.made, Detaching.environments, Detaching.failure = [], [], None
        Detaching.construct_failure = None
        self.detaching = True

    def delegate_cls(self):
        return Detaching if self.detaching else super().delegate_cls()

    def detaches(self, *extra, events=None, **kw):
        return self.runs("--sid", SID, "--detach", "--events", events or self.events, *extra, **kw)

    def test_inside_an_agent_session_a_run_detached_or_not_exits_2_and_constructs_nothing(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            for code, out, err in (self.detaches(), self.runs("--sid", SID)):
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("delegate: refused inside an agent session"), err)
        self.assertEqual(Detaching.made, [])

    def test_inside_an_agent_session_list_dry_run_and_help_still_run(self):
        self.detaching = False
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            self.assertEqual(self.cli("run", "--list")[0], 0)
            code, _, err = self.runs("--sid", SID, "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.cli("--help")[0], 0)

    def test_usage_errors_exit_2_and_construct_nothing(self):
        e = self.events
        for argv, out, want in (
            (("--detach",), None, "--detach needs --events"),
            (("--events", e), None, "--events needs --detach"),
            (("--events", e, "--mode", "tui"), None, "--events needs --detach"),
            ((), "-", "--out takes a PATH, not -"),
            (("--dry-run",), "-", "--out takes a PATH, not -"),
            (("--detach", "--events", e), "-", "--out takes a PATH, not -"),
            (("--detach", "--events", e, "--dry-run"), "-", "--out takes a PATH, not -"),
            (
                ("--detach", "--events", e, "--mode", "in_process"),
                None,
                "--detach needs --mode headless or tui",
            ),
        ):
            with self.subTest(argv=argv, out=out):
                self.assertEqual(self.runs(*argv, out=out), (2, "", f"delegate: {want}\n"))
                self.assertFalse(os.path.exists(self.work))
        self.assertEqual(Detaching.made, [])

    def test_without_out_the_spec_out_is_none(self):
        code, out, err = self.cli(
            "run",
            "--role",
            "dummy-tester",
            "--input",
            "x",
            "--workdir",
            self.work,
            "--sid",
            SID,
            "--detach",
            "--events",
            self.events,
        )
        self.assertEqual(
            (code, out, err), (0, RECORD.to_line() + "\n", f"delegate: session {SID}\n")
        )
        ((_, spec, _),) = Detaching.made
        self.assertIsNone(spec.out)

    def test_cwd_is_the_specs_cwd(self):
        cwd = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(self.detaches("--cwd", cwd)[0], 0)
        self.assertEqual(self.detaches()[0], 0)
        self.assertEqual([spec.cwd for _, spec, _ in Detaching.made], [cwd, None])

    def test_detach_prints_one_record_line_and_names_its_session(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                Detaching.made = []
                code, out, err = self.detaches("--mode", mode)
                self.assertEqual(
                    (code, out, err), (0, RECORD.to_line() + "\n", f"delegate: session {SID}\n")
                )
                ((role, spec, placement),) = Detaching.made
                self.assertEqual((role, placement), ("dummy-tester", {"display": None}))
                self.assertEqual(
                    (spec.mode, spec.sid, spec.events, spec.workdir, spec.out),
                    (mode, SID, self.events, self.work, self.out),
                )

    def test_detach_without_mode_is_tui_and_a_name_is_the_specs(self):
        for extra, want in (((), ("tui", None)), (("--name", "r1"), ("tui", "r1"))):
            with self.subTest(extra=extra):
                Detaching.made = []
                self.assertEqual(self.detaches(*extra)[0], 0)
                ((_, spec, _),) = Detaching.made
                self.assertEqual((spec.mode, spec.name), want)

    def test_a_name_names_the_tui(self):
        self.detaching = False
        for mode, agent in (("headless", None), ("tui", "r1")):
            with self.subTest(mode=mode):
                code, out, err = self.detaches("--mode", mode, "--name", "r1", "--dry-run")
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["sessions"], {"agent": agent})

    def test_a_name_breaking_the_session_name_rule_exits_2_and_constructs_nothing(self):
        for name in ("none", "a b", "a.b", ""):
            with self.subTest(name=name):
                code, out, err = self.detaches("--name", name)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("want [A-Za-z0-9_-]+ but not none", err)
        self.assertEqual(Detaching.made, [])

    def test_detach_events_are_the_given_file_else_the_launchers(self):
        for extra, want in (((), "launcher/events"), (("--events", self.events), self.events)):
            with self.subTest(extra=extra):
                Detaching.made, Detaching.environments = [], []
                os.environ["ENSEMBLE_EVENTS"] = "launcher/events"
                code, _, err = self.runs("--sid", SID, "--detach", *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(Detaching.made[0][1].events, want)
                self.assertNotIn("ENSEMBLE_EVENTS", Detaching.environments[0])
                self.assertNotIn("ENSEMBLE_EVENTS", os.environ)

    def test_the_launchers_sid_is_the_runs_and_a_different_sid_exits_2(self):
        other = "99999999-8888-7777-6666-555555555555"
        for extra in ((), ("--sid", SID), ("--sid", other)):
            with self.subTest(extra=extra):
                Detaching.made, Detaching.environments = [], []
                os.environ["ENSEMBLE_SID"] = SID
                code, out, err = self.runs("--detach", "--events", self.events, *extra)
                self.assertNotIn("ENSEMBLE_SID", os.environ)
                if other in extra:
                    want = f"delegate: $ENSEMBLE_SID {SID} differs from --sid {other}\n"
                    self.assertEqual((code, out, err, Detaching.made), (2, "", want, []))
                    continue
                self.assertEqual(code, 0, err)
                ((_, spec, _),) = Detaching.made
                self.assertEqual((spec.sid, spec.resume), (SID, False))
                self.assertNotIn("ENSEMBLE_SID", Detaching.environments[0])

    def test_an_unusable_events_file_given_or_inherited_exits_2_writing_nothing(self):
        self.detaching = False
        os.mkdir(self.events)
        misread = os.path.join(self.tmp, "ev#1")
        for given, why in (
            (self.events, f"events file {self.events}: Is a directory"),
            (misread, f"events file {misread}: tmux would misread it"),
        ):
            for extra, launcher in ((("--events", given), {}), ((), {"ENSEMBLE_EVENTS": given})):
                with self.subTest(extra=extra, launcher=launcher):
                    os.environ.update(launcher)
                    code, out, err = self.runs("--sid", SID, "--detach", *extra)
                    self.assertEqual((code, out, err), (2, "", f"delegate: {why}\n"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_a_display_is_given_to_the_delegate_unread(self):
        code, _, err = self.detaches("--mode", "tui", "--display", "mgr:%1 right")
        self.assertEqual(code, 0, err)
        self.assertEqual(Detaching.made[0][2], {"display": "mgr:%1 right"})

    def test_a_refusal_exits_2_and_a_start_failure_1_without_a_record(self):
        for failure, code in (
            (ConfigError(f"{self.work}: delegate is still alive"), 2),
            (SessionError(f"detach {HARNESS}: duplicate session"), 1),
            (SessionError(f"tui session {TUI} did not start"), 1),
            (OSError("No space left on device"), 1),
        ):
            with self.subTest(failure=failure):
                Detaching.failure = failure
                self.assertEqual(self.detaches(), (code, "", f"delegate: {failure}\n"))

    def test_a_busy_create_or_start_exits_75_naming_the_holder(self):
        busy = SessionBusy("dummy-lock", "ensemble-x-1 (owner x)")
        for attr in ("construct_failure", "failure"):
            with self.subTest(attr=attr):
                Detaching.construct_failure = Detaching.failure = None
                setattr(Detaching, attr, busy)
                self.assertEqual(
                    self.detaches(),
                    (75, "", "delegate: busy: lock dummy-lock is held by ensemble-x-1 (owner x)\n"),
                )

    def test_on_event_is_split_once_into_the_callback_argv_never_a_shell(self):
        code, out, err = self.detaches("--on-event", "writeback --issue 'A B' $X;rm *")
        self.assertEqual((code, out), (0, RECORD.to_line() + "\n"), err)
        ((_, _, placement),) = Detaching.made
        self.assertEqual(
            placement,
            {"display": None, "callback": ("writeback", "--issue", "A B", "$X;rm", "*")},
        )

    def test_an_unusable_on_event_exits_2_and_constructs_nothing(self):
        for extra in (
            ("--on-event", "writeback 'A B"),
            ("--on-event", ""),
            ("--on-event", " \t "),
            ("--on-event", "a", "--on-event", "b"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.detaches(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--on-event", err)
        self.assertEqual(
            self.runs("--on-event", "writeback"),
            (2, "", "delegate: --on-event needs --detach\n"),
        )
        self.assertEqual(Detaching.made, [])

    def test_a_dry_run_shows_the_callback_argv_and_writes_nothing(self):
        self.detaching = False
        code, out, err = self.detaches("--dry-run", "--on-event", "writeback --issue 'A B'")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["callback"], ["writeback", "--issue", "A B"])
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_dry_run_adds_the_session_names_and_the_events_file(self):
        self.detaching = False
        for mode, tui in (("headless", None), ("tui", TUI)):
            with self.subTest(mode=mode):
                code, out, err = self.detaches("--mode", mode, "--dry-run", events="events")
                self.assertEqual(code, 0, err)
                shown = json.loads(out)
                self.assertEqual(set(shown), KEYS | PLACEMENT | {"events"})
                self.assertEqual(
                    (shown["mode"], shown["sessions"], shown["hosts"], shown["events"]),
                    (
                        mode,
                        {"agent": tui},
                        {"agent": "tmux"},
                        self.events,
                    ),
                )
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))
        self.assertFalse(os.path.exists(self.events))


class Shown(HarnessSession):
    """Completes a display as `shown:<value>`, recording each (value, host) it is asked."""

    asked: list = []

    @classmethod
    def display(cls, value=None, *, host=None):
        cls.asked.append((value, host))
        return f"shown:{value}"


class Harnessed(HarnessCase):
    """cli.main `run` on HarnessDelegate over Shown sessions on RecordingHosts (default `agent`),
    cwd the test's HOME: no process starts.
    """

    def setUp(self):
        super().setUp()
        Shown.asked = []
        for key in ("ENSEMBLE_SID", "ENSEMBLE_EVENTS"):
            os.environ.pop(key, None)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)
        self.events = os.path.join(self.home, "events")

    def cli(
        self, *extra, input="Hello.", stdin=None, role="dummy-tester", task="echo", client="fake"
    ):
        class Bound(DrivenDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, session_cls=Shown, **placement)

        argv = ["run", "--role", role, "--task", task, "--input", input]
        argv += ["--out", os.path.join(self.work, "out.md"), "--workdir", self.work]
        argv += ["--mode", "headless"] + (["--client", client] if client else [])
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", stdin or io.StringIO()),
        ):
            try:
                code = cli.main([*argv, *extra], delegate_cls=Bound)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()


ACCEPTANCE = """
[delegate.environments.dummy]
image = "dummy-env:test"
plugins = ["dummy-plugin"]
lock = "dummy-lock"

[[delegate.environments.dummy.secrets]]
item = "ensemble-broker-env/dummy"
file = "dummy.txt"

[delegate.roles.dummy-tester.environment]
name = "dummy"
prefetch = true
"""


class Environments(Harnessed):
    """run's environment: resolved from the delegate config only (WDRC-109 FR-57, FR-60)."""

    TARGETED = "Reference: T-1\nRepo: acme/app\nRef: release/1\n\nHello."

    def configure(self, default):
        self.settings(default=default)
        # the fake resolves no --host to its "agent" entry: make that the default Host
        Shown.hosts["agent"] = RecordingHost(default)
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(ACCEPTANCE)

    def test_dry_run_prints_environment(self):
        self.configure("docker")
        code, out, err = self.cli("--dry-run", input=self.TARGETED)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out)["environment"],
            {
                "image": "dummy-env:test",
                "gateway": "workers",
                "secrets": [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}],
                "plugins": ["dummy-plugin"],
                "lock": "dummy-lock",
                "prefetch": {"repo": "acme/app", "ref": "release/1"},
                "github_repo": None,
            },
        )
        self.assertFalse(os.path.exists(self.work))

    def test_run_refuses_environment_on_tmux(self):
        self.configure("tmux")
        code, out, err = self.cli(input=self.TARGETED)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("[environment.tmux]", err)
        self.assertFalse(os.path.exists(self.work))


class Verifier(Harnessed):
    """The built-in verifier on the packaged mini-op environment (WDRC-110 FR-47, FR-50, FR-80)."""

    TARGETED = "Repo: windroc-labs/ensemble\nRef: main"

    def configure(self, default):
        self.settings(default=default)
        Shown.hosts["agent"] = RecordingHost(default)

    def verify(self, *extra, client=None):
        return self.cli(*extra, input=self.TARGETED, role="verifier", task="verify", client=client)

    def test_dry_run_prints_the_mini_op_environment(self):
        self.configure("docker")
        code, out, err = self.verify("--dry-run")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(
            shown["environment"],
            {
                "image": "mini-op:env",
                "gateway": "mini-op",
                "secrets": [
                    {"item": "ensemble-broker-env/mini-op-password", "file": "mini-password"},
                    {"item": "ensemble-broker-env/mini-op-ssh-key", "file": "mini-ssh-key"},
                ],
                "plugins": [],
                "lock": "mini-op",
                "prefetch": {"repo": "windroc-labs/ensemble", "ref": "main"},
                "github_repo": "windroc/team-docs",
            },
        )
        self.assertIn("`mini-op` skill", shown["prompt"])
        self.assertFalse(os.path.exists(self.work))

    def test_each_client_is_accepted_with_the_mini_op_environment(self):
        self.configure("docker")
        for client in ("codex", "opencode", "claude"):
            with self.subTest(client=client):
                code, out, err = self.verify("--dry-run", client=client)
                self.assertEqual(code, 0, err)
                shown = json.loads(out)
                self.assertEqual(shown["client"], client)
                self.assertEqual(shown["environment"]["image"], "mini-op:env")
                self.assertEqual(shown["environment"]["plugins"], [])
                self.assertFalse(os.path.exists(self.work))

    def test_a_local_role_client_is_accepted(self):
        self.configure("docker")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write('[delegate.roles.verifier]\nclient = "codex"\n')
        code, out, err = self.verify("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["client"], "codex")

    def test_global_client_applies_and_the_flag_overrides_it(self):
        self.configure("docker")
        with open(self.local, encoding="utf-8") as file:
            local = file.read()
        with open(self.local, "w", encoding="utf-8") as file:
            file.write(local.replace("[delegate]\n", '[delegate]\nclient = "opencode"\n'))
        for given, expected in ((None, "opencode"), ("codex", "codex")):
            with self.subTest(client=given):
                code, out, err = self.verify("--dry-run", client=given)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["client"], expected)

    def test_missing_repo_is_refused_before_the_task_runs(self):
        self.configure("docker")
        for extra in (("--dry-run",), ()):
            with self.subTest(extra=extra):
                code, out, err = self.cli(
                    *extra, input="Check login", role="verifier", task="verify", client=None
                )
                self.assertEqual((code, out), (2, ""))
                self.assertIn("prefetch: want one Repo: line", err)
                self.assertIn("no Repo: line", err)
                self.assertFalse(os.path.exists(self.work))

    def test_tmux_default_host_refuses_and_still_lists(self):
        self.configure("tmux")
        code, out, err = self.verify()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("role verifier selects an environment", err)
        self.assertIn("[environment.tmux]", err)
        self.assertFalse(os.path.exists(self.work))
        listed = io.StringIO()
        with redirect_stdout(listed), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", "--list"]), 0)
        self.assertIn("\nverifier  github  built-in\n  verify: ", listed.getvalue())


class DetachDisplay(Harnessed):
    def test_a_given_or_absent_display_is_completed_once_on_the_agent_host_and_kept(self):
        for given, extra in (("mgr:%1 right", ("--display", "mgr:%1 right")), (None, ())):
            with self.subTest(given=given):
                HarnessSession.reset()
                Shown.asked = []
                self.work = os.path.join(self.home, "given" if given else "absent")
                code, out, err = self.cli(
                    "--sid", HARNESS_SID, "--detach", "--events", self.events, *extra
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(Shown.asked, [(given, "agent")])
                shown = f"shown:{given}"
                self.assertEqual(Record.parse(out).display, shown)
                self.assertEqual(self.metadata()["display"], shown)
                self.assertEqual(harness_launches()[-1]["display"], shown)


class DetachEvents(Harnessed):
    def test_a_relative_events_file_given_or_inherited_is_stored_absolute(self):
        want = os.path.join(os.getcwd(), "events")
        for name, extra, launcher in (
            ("given", ("--events", "events"), {}),
            ("inherited", (), {"ENSEMBLE_EVENTS": "events"}),
        ):
            with self.subTest(name):
                HarnessSession.reset()
                self.work = os.path.join(self.home, name)
                os.environ.update(launcher)
                code, _, err = self.cli("--sid", HARNESS_SID, "--detach", *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.metadata()["events"], want)
                self.assertEqual(harness_launches()[-1]["events"], want)


class ReadOnce(io.StringIO):
    def read(self, size=-1):
        if self.tell():
            raise AssertionError("stdin read twice")
        return super().read(size)


class DetachInput(Harnessed):
    def test_input_dash_is_read_once_by_the_caller_and_stored_and_prompted_verbatim(self):
        with open(os.path.join(self.home, "notes.md"), "w", encoding="utf-8") as file:
            file.write("not the input\n")
        for name, text in (
            ("file", "notes.md"),
            ("lines", "first line\n\nthird line\n"),
            ("dash", "-x --detach\n"),
            ("unicode", "Résumé ✓ 中文\n"),
        ):
            with self.subTest(name):
                HarnessSession.reset()
                self.work = os.path.join(self.home, name)
                code, _, err = self.cli(
                    "--sid",
                    HARNESS_SID,
                    "--detach",
                    "--events",
                    self.events,
                    input="-",
                    stdin=ReadOnce(text),
                )
                self.assertEqual(code, 0, err)
                state = self.metadata()
                self.assertEqual(state["spec"]["input"], text)
                self.assertIn(f"# Input\n\n{text}", state["plan"]["session"]["prompt"])
                harness = "ensemble.delegate.engine.harness"
                self.assertEqual(
                    harness_launches()[-1]["argv"],
                    [sys.executable, "-P", "-s", "-m", harness, self.work],
                )


class DetachSid(Harnessed):
    def test_the_launchers_sid_is_stored_and_printed(self):
        os.environ["ENSEMBLE_SID"] = HARNESS_SID
        code, out, err = self.cli("--detach", "--events", self.events)
        self.assertEqual(code, 0, err)
        self.assertEqual(Record.parse(out).sid, HARNESS_SID)
        self.assertEqual(self.metadata()["spec"]["sid"], HARNESS_SID)
        self.assertNotIn("ENSEMBLE_SID", harness_launches()[0]["env"])


class DryRun(Base):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        code, out, err = self.runs("--sid", SID, "--dry-run")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(set(shown), KEYS | PLACEMENT)
        self.assertEqual(
            {k: v for k, v in shown.items() if k not in ("dirs", "reads", "commands", "prompt")},
            {
                "client": "fake",
                "mode": "headless",
                "sid": SID,
                "workdir": self.work,
                "cwd": Layout(self.work).agent,
                "project": False,
                "tier": 3,
                "effort": "low",
                "sessions": {"agent": None},
                "hosts": {"agent": "tmux"},
                "callback": None,
                "environment": None,
            },
        )
        self.assertEqual(
            (shown["dirs"], shown["reads"], shown["commands"]),
            ([], [os.path.join(TEAM, "tasks")], []),
        )
        self.assertIn("**Your task**: `echo` (`<tasks>/echo.md`).", shown["prompt"])
        self.assertTrue(shown["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(err, "")
        self.assertEqual(
            (self.launches(), os.path.exists(self.work), os.listdir(self.ensemble)),
            ([], False, ["config.local.toml"]),
        )

    def test_a_new_run_gets_a_sid(self):
        code, out, _ = self.runs("--dry-run", task=None)
        shown = json.loads(out)
        self.assertEqual((code, uuid.UUID(shown["sid"]).version), (0, 4))
        self.assertIn("**Your task**: pick it from", shown["prompt"])
        self.assertFalse(os.path.exists(self.work))


class InProcess(Base):
    """
    The in_process-only fake, and RefusingClient (client `refusing`); no child process starts.
    """

    def test_an_adhoc_run_uses_its_derived_workdir_when_reporting_the_result(self):
        with (
            mock.patch("time.strftime", return_value="2026-10-16"),
            mock.patch("uuid.uuid4", return_value=uuid.UUID(SID)),
            no_spawn(),
        ):
            code, out, err = self.cli(
                "run",
                "--role",
                "dummy-tester",
                "--task",
                "echo",
                "--input",
                "Hello.",
                "--client",
                "fake",
                "--mode",
                "in_process",
            )
        self.assertEqual((code, out), (0, ""), err)
        workdir = os.path.join(self.ensemble, "adhoc", "2026-10-16-dummy-tester-echo-" + SID[:8])
        self.assertIn(Layout(workdir).out, err)
        with open(os.path.join(Layout(workdir).run, "delegate.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["spec"]["sid"], SID)

    def setUp(self):
        super().setUp()
        refusing = types.ModuleType("ensemble.session.clients.refusing")
        refusing.RefusingClient = RefusingClient
        self.session_dependencies = {
            "class_loader": client_loader(
                {"ensemble.session.clients.fake": fake_client, refusing.__name__: refusing}
            )
        }
        classes = {"fake": "InProcessFakeClient", "refusing": "RefusingClient"}
        self.configure_session(
            {
                name: {
                    "implementation": f"ensemble.session.clients.{name}:{cls}",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name, cls in classes.items()
            }
        )

    def runs(self, *extra, **kw):
        with no_spawn():
            return super().runs(*extra, **kw)

    def test_a_run_without_sid_takes_the_launchers(self):
        os.environ["ENSEMBLE_SID"] = SID
        code, _, err = self.runs("--mode", "in_process")
        self.assertEqual(code, 0, err)
        self.assertIn(f"delegate: session {SID}\n", err)

    def test_the_launchers_events_are_ignored_without_detach(self):
        os.environ["ENSEMBLE_EVENTS"] = os.path.join(self.tmp, "events")
        code, _, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual(code, 0, err)
        self.assertNotIn("ENSEMBLE_EVENTS", os.environ)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "events")))

    def test_a_done_run_exits_0_naming_its_session_and_status(self):
        code, out, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        lines = err.splitlines()
        self.assertLess(
            lines.index(f"delegate: session {SID}"), lines.index("Progress (start): go")
        )
        self.assertLess(lines.index("Progress (start): go"), lines.index("delegate: status done"))
        self.assertTrue(err.endswith("delegate: status done\n"), err)
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.assertEqual(
            [r["kind"] for r in self.records()], ["session", "input", "progress", "result", "end"]
        )
        self.assertEqual([(e["sid"], e["pid"]) for e in self.launches()], [(SID, os.getpid())])

    def test_a_run_without_out_names_its_default_deliverable_file(self):
        with no_spawn():
            code, out, err = self.cli(
                "run",
                "--role",
                "dummy-tester",
                "--task",
                "echo",
                "--input",
                "Hello.",
                "--workdir",
                self.work,
                "--client",
                "fake",
                "--mode",
                "in_process",
                "--sid",
                SID,
            )
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn(f"delegate: deliverable {Layout(self.work).out}\n", err)

    def test_a_run_reports_its_deliverable_and_leaves_its_delegate_json(self):
        delegate_cls = self.delegate_cls()
        with (
            mock.patch.object(self, "delegate_cls", return_value=delegate_cls),
            mock.patch.object(delegate_cls, "create", wraps=delegate_cls.create) as create,
        ):
            code, out, err = self.runs("--mode", "in_process", "--sid", SID)
        create.assert_called_once()
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn(f"delegate: deliverable {self.out}\n", err)
        with open(RunState(Layout(self.work)).record) as f:
            state = json.load(f)
        self.assertEqual(
            (state["role"], state["attempt"], state["completed"]), ("dummy-tester", 1, True)
        )
        self.assertEqual(
            (sorted(os.listdir(self.work)), sorted(os.listdir(Layout(self.work).run))),
            (
                ["agent", "out.md", "run"],
                ["delegate.json", "delegate.lock", "delegate.run.lock", "run.jsonl"],
            ),
        )

    def test_headless_and_tui_exit_2_and_nothing_runs(self):
        for mode, want in (
            ("headless", "client 'fake' does not support headless mode"),
            ("tui", "client 'fake' does not support tui mode"),
        ):
            with self.subTest(mode=mode):
                code, out, err = self.runs("--mode", mode)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_claude_and_codex_refuse_it_with_exit_2(self):
        self.configure_session(committed())
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                code, out, err = self.runs("--mode", "in_process", client=client)
                self.assertEqual(
                    (code, out, err.splitlines()[-1]),
                    (2, "", f"delegate: client {client!r} does not support in_process mode"),
                )
                self.assertFalse(os.path.exists(self.work))

    def test_dry_run_shows_the_mode(self):
        code, out, err = self.runs("--mode", "in_process", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"]), (0, "in_process"), err)
        self.assertFalse(os.path.exists(self.work))

    def test_a_check_refusal_exits_2_with_no_record(self):
        code, out, err = self.runs("--mode", "in_process", client="refusing")
        self.assertEqual((code, out, err.splitlines()[-1]), (2, "", "delegate: refused by check"))
        self.assertEqual(
            (os.path.exists(os.path.join(Layout(self.work).run, "run.jsonl")), self.launches()),
            (False, []),
        )


def unowned(spec):
    """A preflight that passes all but handlers, as for a start no session harness owns."""
    if spec.handlers:
        raise SessionConfigError("handlers: no session harness")


class Tui(Base):
    def setUp(self):
        super().setUp()
        self.configure_fake(tui=True)

    def test_a_tui_run_whose_handler_preflight_refuses_exits_2_and_starts_nothing(self):
        with (
            mock.patch.object(self.driver, "preflight", side_effect=unowned),
            mock.patch.object(self.driver, "start") as start,
        ):
            code, out, err = self.runs("--mode", "tui")
        self.assertEqual(
            (code, out, err.splitlines()[-1]), (2, "", "delegate: handlers: no session harness")
        )
        start.assert_not_called()

    def test_a_client_without_tui_exits_2_and_a_dry_run_plans_it(self):
        self.configure_fake()
        code, out, err = self.runs("--mode", "tui")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.splitlines()[-1], "delegate: client 'fake' does not support tui mode")
        self.assertFalse(os.path.exists(self.work))
        code, out, _ = self.runs("--mode", "tui", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"], self.launches()), (0, "tui", []))


class List(Base):
    def test_list_prints_each_role_and_task_with_its_source(self):
        code, out, err = self.cli("run", "--list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line for line in lines if not line.startswith("  ")], HEADS)
        echo = Composer().index("dummy-tester")["echo"]
        self.assertTrue(
            out.endswith(f"\ndummy-tester  local  built-in\n  echo: {echo}  [built-in]\n"), out
        )
        self.assertTrue(
            all(line.endswith("  [built-in]") for line in lines if line.startswith("  "))
        )
        self.assertNotIn("<tasks>", out)
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_list_takes_no_abbreviation_and_no_other_option(self):
        code, out, err = self.cli("run", "--li")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --li", err)
        for argv, extra in (
            (["--list", "--role", "pm"], "--role"),
            (["--list", "--dry-run"], "--dry-run"),
            (["--list", "--client=fake"], "--client=fake"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli("run", *argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"--list takes no other option, not {extra}\n", err)
        code, out, err = self.cli("run")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the following arguments are required: --role", err)

    def test_list_stops_at_a_config_defect(self):
        for local, want in (
            (
                "[roles.ghost]\ntier = 3\n",
                "roles.ghost: no charter roles/ghost.md in core or team_dirs",
            ),
            ('team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
            (
                "bogus = 1\n",
                "config: ~/.ensemble/config.local.toml: unknown key 'bogus' in [delegate]",
            ),
            ('tier = "2"\n', "tier must be an integer 1–4, got '2'"),
            (
                'effort = "ultra"\n',
                "effort must be one of low, medium, high, xhigh, max, got 'ultra'",
            ),
            ('read = "~/notes"\n', "read must be a list of strings, got '~/notes'"),
            ("write = 3\n", "write must be a list of strings, got 3"),
            ('[roles.pm]\ncommands = "ls *"\n', "commands must be a list of strings, got 'ls *'"),
            ("[roles.pm]\ntemplates = [3]\n", "templates must be a list of strings, got [3]"),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.assertEqual(self.cli("run", "--list"), (2, "", f"delegate: {want}\n"))


class Configures(Base):
    def test_a_session_config_error_exits_2_and_starts_nothing(self):
        self.configure_session({})
        code, out, err = self.runs()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("[clients.fake]", err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_busy_exits_75_naming_the_holder(self):
        busy = SessionBusy("dummy-lock", "ensemble-x-1 (owner x)")
        line = "delegate: busy: lock dummy-lock is held by ensemble-x-1 (owner x)\n"
        with mock.patch.object(self.driver, "preflight", side_effect=busy):
            self.assertEqual(self.runs(), (75, "", line))
        self.assertFalse(os.path.exists(self.work))
        self.ran = Result(75, None, str(busy))
        code, out, err = self.runs()
        self.assertEqual((code, out), (75, ""))
        self.assertTrue(err.endswith(line), err)
        self.ran = Result(1, None, "the agent run returned no outcome")
        self.assertEqual(self.runs()[0], 1)

    def test_any_delegates_key_exits_2_and_starts_nothing(self):
        for text, want in (
            (
                '[delegates]\nharness_host = "nope"\n',
                "delegate: [delegates] harness_host: the delegate takes no keys",
            ),
            ("[delegates]\nkind = 1\n", "delegate: [delegates] kind: the delegate takes no keys"),
        ):
            with self.subTest(text):
                self.local(text)
                code, out, err = self.runs()
                self.assertEqual((code, out, err), (2, "", f"{want}\n"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_a_role_accounts_table_exits_2_naming_the_session_key(self):
        self.local('[roles.dummy-tester.accounts]\nfake = "work"\n')
        code, out, err = self.runs()
        self.assertEqual(
            (code, out, err.splitlines()[-1]),
            (
                2,
                "",
                "delegate: unknown key 'accounts' in roles.dummy-tester; moved to"
                " [session.clients.<client>] account",
            ),
        )
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_list_works_without_a_client(self):
        self.configure_session({})
        code, _, err = self.cli("run", "--list")
        self.assertEqual((code, err), (0, ""))


class Usage(Base):
    def test_a_run_needs_input(self):
        for argv in (
            ("--out", "o.md"),
            ("--workdir", self.work),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.cli("run", "--role", "dummy-tester", "--client", "fake", *argv),
                    (2, "", "delegate: --input is required\n"),
                )
        self.assertEqual(self.launches(), [])

    @mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_an_unknown_mode_exits_2_naming_each_mode(self):
        code, out, err = self.runs("--mode", "x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn(
            "argument --mode: invalid choice: 'x' (choose from 'headless', 'tui', 'in_process')",
            " ".join(err.split()),
        )
        self.assertEqual(self.launches(), [])

    def test_an_option_given_twice_exits_2_before_anything_starts(self):
        for extra, error in (
            (("--client", "fake"), "--client given twice"),
            (("--client=fake",), "--client given twice"),
            (("--out", "b.md"), "--out given twice"),
            (("--workdir", "b"), "--workdir given twice"),
            (("--role", "dummy-tester"), "--role given twice"),
            (("--mode", "tui", "--mode", "headless"), "--mode given twice"),
            (("--name", "a", "--name", "b"), "--name given twice"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(error, err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_an_abbreviated_or_removed_option_is_a_usage_error(self):
        for extra, flag in (
            (("--mod", "headless"), "--mod"),
            (("--detach", "--rol", "x"), "--rol"),
            (("--detach", "--mode", "tui", "--split", "right"), "--split"),
            (("--detach", "--mode", "tui", "--split-from", "mgr"), "--split-from"),
            (("--environment", "dummy", "--dry-run"), "--environment"),
            (("--resume",), "--resume"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"unrecognized arguments: {flag}", err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_a_config_error_exits_2_before_anything_starts(self):
        cases = [
            (
                ("an unknown role"),
                {"role": "ghost", "task": None},
                (""),
                ("unknown role 'ghost'"),
            ),
            (
                "a task off the index",
                {"role": "engineer", "task": "deep-research"},
                "",
                "task 'deep-research' is not one of engineer's tasks (build, light-build)",
            ),
            (
                "a bad team_dirs",
                {},
                'team_dirs = "x"\n',
                "team_dirs: want a list of absolute or ~ paths, got 'x'",
            ),
            (
                "a bad tier",
                {},
                "[roles.dummy-tester]\ntier = 9\n",
                "tier must be an integer 1–4, got 9",
            ),
            ("an unknown client", {"client": "nope"}, "", "no [clients.nope] table"),
        ]
        for name, kw, local, want in cases:
            self.local(local)
            for dry in ((), ("--dry-run",)) if name != "an unknown client" else ((),):
                with self.subTest(name, dry=dry):
                    code, out, err = self.runs(*dry, **kw)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertTrue(err.splitlines()[-1].startswith(f"delegate: {want}"), err)
                    self.assertFalse(os.path.exists(self.work))
        self.assertEqual(self.launches(), [])


class Prompt(Case):
    def test_write_failures_are_clean_cli_errors(self):
        os.makedirs(self.out)
        code, out, err = self.prompt()
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("delegate: "), err)
        self.assertIn(self.out, err)
        self.assertNotIn("Traceback", err)

    def test_an_unwritable_workdir_is_a_clean_cli_error(self):
        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError("write denied")):
            code, out, err = self.prompt()
        self.assertEqual((code, out, err), (2, "", "delegate: write denied\n"))

    def setUp(self):
        self.work = os.path.join(self.home, "work")
        self.out = os.path.join(self.work, "prompt.md")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)

    def prompt(self, *extra, role="dummy-tester", input="Hello.\n"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(
                    [
                        "prompt",
                        "--role",
                        role,
                        "--task",
                        "echo",
                        "--input",
                        input,
                        "--workdir",
                        self.work,
                        "--out",
                        self.out,
                        *extra,
                    ]
                )
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_instruction_matches_composer_bytes_no_client_or_session_configuration(self):
        Path(self.ensemble, "config.local.toml").write_text(
            local_section('[hosts]\ndefault = "missing"\n', "session"), encoding="utf-8"
        )
        composition = Composer()
        config = composition.resolve("dummy-tester", "echo")
        expected = composition.render(
            config, RunParams(input="Hello.\n", out=self.out, workdir=Layout(self.work).agent)
        )
        with mock.patch("subprocess.Popen", side_effect=AssertionError("session started")):
            self.assertEqual(
                self.prompt(), (0, "", f"delegate: instruction delivered: {self.out}\n")
            )
        self.assertEqual(Path(self.out).read_bytes(), expected.encode("utf-8"))
        log = Path(Layout(self.work).run, "run.jsonl")
        self.assertFalse(log.exists())

    def test_runtime_execution_and_removed_options_are_usage_errors_before_writes(self):
        for argv in (
            ("--client", "claude"),
            ("--mode", "tui"),
            ("--detach",),
            ("--sid", "11111111-2222-3333-4444-555555555555"),
            ("--resume",),
            ("--driver", "obsolete"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.prompt(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"unrecognized arguments: {' '.join(argv)}", err)
                self.assertFalse(os.path.exists(self.work))

    def test_list_is_rejected_and_absent_from_prompt_options(self):
        code, out, err = self.prompt("--list")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --list", err)
        code, out, err = self.prompt("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertNotRegex(out, r"(?m)^  --list(?:[ =]|$)")
        self.assertFalse(os.path.exists(self.work))

    def test_out_dash_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["prompt", "--role", "dummy-tester", "--input", "x"]
                + ["--workdir", self.work, "--out", "-"]
            )
        self.assertEqual(
            (code, out.getvalue(), err.getvalue()),
            (2, "", "delegate: --out takes a PATH, not -\n"),
        )
        self.assertFalse(os.path.exists(self.work))

    def test_unknown_role_is_a_config_error_before_writes(self):
        code, out, err = self.prompt(role="ghost")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown role 'ghost'", err)
        self.assertFalse(os.path.exists(self.work))


class Open(Base):
    def setUp(self):
        super().setUp()
        self.config = Path(self.ensemble, "config.local.toml")
        self.config.write_text(
            '[session.hosts]\ndefault = "docker"\n'
            '[delegate.environments.dummy]\nimage = "dummy-env:test"\nlock = "dummy-lock"\n'
        )
        self.opened = self.enterContext(
            mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0))
        )

    def test_open_has_no_role_and_passes_optional_values_and_launcher_identity(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_SID": SID, "ENSEMBLE_EVENTS": "/events"}):
            self.assertEqual(
                self.cli(
                    "open",
                    "dummy-1",
                    "--environment",
                    "dummy",
                    "--client",
                    "codex",
                    "--account",
                    "work",
                    "--cwd",
                    self.tmp,
                    "--display",
                    "none",
                ),
                (0, "", ""),
            )
            self.assertNotIn("ENSEMBLE_SID", os.environ)
            self.assertNotIn("ENSEMBLE_EVENTS", os.environ)
        argv = self.opened.call_args.args[0]
        self.assertEqual(argv[5:7], ["start", "dummy-1"])
        for key, value in (
            ("--client", "codex"),
            ("--cwd", self.tmp),
        ):
            self.assertEqual(argv[argv.index(key) + 1], value)
        self.assertIn("--account=work", argv)
        self.assertIn("--display=none", argv)
        env = self.opened.call_args.kwargs["env"]
        self.assertEqual((env["ENSEMBLE_SID"], env["ENSEMBLE_EVENTS"]), (SID, "/events"))
        self.assertFalse(os.path.exists(self.work))

    def test_open_preserves_the_session_status(self):
        for code in (0, 1, 2, 75):
            with self.subTest(code=code):
                self.opened.return_value = types.SimpleNamespace(returncode=code)
                self.assertEqual(
                    self.cli("open", "dummy-1", "--environment", "dummy"), (code, "", "")
                )

    def test_config_errors_and_spawn_errors_are_reported_cleanly(self):
        self.opened.side_effect = OSError("session unavailable")
        self.assertEqual(
            self.cli("open", "dummy-1", "--environment", "dummy"),
            (1, "", "delegate: session unavailable\n"),
        )
        self.opened.reset_mock()
        code, out, err = self.cli("open", "dummy-1", "--environment", "unknown")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown", err)
        self.config.write_text("[")
        self.assertEqual(self.cli("open", "dummy-1", "--environment", "dummy")[:2], (2, ""))
        self.opened.assert_not_called()

    def test_invalid_names_missing_unknown_empty_and_repeated_options_start_nothing(self):
        for argv in (
            ("open", "none", "--environment", "dummy"),
            ("open", "a.b", "--environment", "dummy"),
            ("open", "--environment", "dummy", "--", "-worker"),
            ("open", "dummy-1"),
            ("open", "dummy-1", "--environment", "dummy", "--role", "engineer"),
            *(
                ("open", "dummy-1", "--environment", "dummy", flag, "")
                for flag in ("--client", "--account", "--cwd", "--display", "--environment")
            ),
            *(
                ("open", "dummy-1", "--environment", "dummy", flag, "x", flag, "y")
                for flag in ("--client", "--account", "--cwd", "--display", "--environment")
            ),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[:2], (2, ""))
        self.assertIn(
            "not starting with -", self.cli("open", "--environment", "dummy", "--", "-worker")[2]
        )
        self.opened.assert_not_called()

    def test_nested_open_is_refused_and_help_starts_nothing(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            code, out, err = self.cli("open", "dummy-1", "--environment", "dummy")
            self.assertEqual((code, out), (2, ""))
            self.assertIn("refused inside an agent session", err)
            self.assertEqual(self.cli("open", "--help")[0], 0)
        self.opened.assert_not_called()


class Commands(Base):
    def test_help_lists_all_subcommands(self):
        code, out, err = self.cli("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            re.search(r"\{([a-z,-]+)\}", out)[1].split(","),
            [
                "run",
                "open",
                "status",
                "content",
                "outcome",
                "resume",
                "show",
                "hide",
                "close",
                "attach-command",
                "prompt",
            ],
        )

    def test_an_unknown_missing_or_flat_command_exits_2(self):
        for argv, want in (
            (("bogus",), "invalid choice: 'bogus'"),
            ((), "the following arguments are required: command"),
            (("--role", "dummy-tester", "--list"), "invalid choice: 'dummy-tester'"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(want, " ".join(err.split()))


class View:
    """A run's scripted view: its Status name, its outcome, attach command and errors."""

    def __init__(self, name, *, outcome=None, command=None, error=None, show_error=None):
        self.name, self.outcome_value, self.command = name, outcome, command
        self.error, self.show_error, self.closed, self.hidden = error, show_error, 0, 0

    def status(self):
        if self.error is not None:
            raise self.error
        return Status(
            name=self.name,
            alive=False,
            state="gone",
            outcome=None,
            attach=self.command,
            hidden=False,
        )

    def content(self):
        return "content of " + self.name

    def show(self):
        if self.show_error is not None:
            raise self.show_error
        return self.command

    def hide(self):
        if self.show_error is not None:
            raise self.show_error
        self.hidden += 1

    def close(self):
        self.closed += 1

    def outcome(self):
        return self.outcome_value

    def attach_command(self):
        return self.command

    def resume(self, *, mode=None, input=None):
        return record(self.name)


def record(name):
    work = f"/w/{name}"
    return Record(
        1,
        "role",
        name,
        (),
        None,
        work,
        ("ensemble-delegate",),
        ("ensemble-delegate", "resume", work),
        None,
        None,
    )


class Views(DrivenDelegate):
    """attach(key): the View registered under a workdir key or a Record's name."""

    views: dict = {}

    @classmethod
    def attach(cls, record_or_workdir, **_):
        name = record_or_workdir if isinstance(record_or_workdir, str) else record_or_workdir.name
        view = cls.views[name]
        if isinstance(view, Exception):
            raise view
        return view


class Queries(Case):
    def query(self, *argv, stdin="", **views):
        Views.views = views
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv), delegate_cls=Views)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_content_prints_the_views_text_from_key_or_record(self):
        for arguments, stdin in ((("content", "a"), ""), (("content",), record("a").to_line())):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.query(*arguments, stdin=stdin, a=View("a")), (0, "content of a\n", "")
                )

    def test_resume_input_is_forwarded_from_key_or_record(self):
        class InputView(View):
            def ctl(self):
                return ("ensemble-delegate",)

            def resume(self, *, input=None, mode=None):
                self.input = input
                return record(self.name)

        for arguments, stdin in (
            (("resume", "a", "--input", "answer"), ""),
            (("resume", "--input", "answer"), record("a").to_line()),
        ):
            view = InputView("a")
            self.assertEqual(
                self.query(*arguments, stdin=stdin, a=view),
                (0, record("a").to_line() + "\n", f"delegate: session {record('a').sid}\n"),
            )
            self.assertEqual(view.input, "answer")

    def test_content_bad_input_answers_nothing_before_attach(self):
        for arguments, stdin in (
            (("content", "a", "b"), ""),
            (("content",), "bad record"),
            (("content",), record("a").to_line() + "\nbad record"),
        ):
            with (
                self.subTest(arguments=arguments, stdin=stdin),
                mock.patch.object(Views, "attach") as attach,
            ):
                code, out, err = self.query(*arguments, stdin=stdin)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err)
                attach.assert_not_called()

    def test_status_prints_one_line_per_key_in_order_from_arguments_or_stdin(self):
        a, b = View("a"), View("b", command="tmux attach -t =b")
        lines = [
            Status(
                name="a", alive=False, state="gone", outcome=None, attach=None, hidden=False
            ).to_line(),
            Status(
                name="b",
                alive=False,
                state="gone",
                outcome=None,
                attach="tmux attach -t =b",
                hidden=False,
            ).to_line(),
        ]
        self.assertEqual(
            self.query("status", "a", "b", a=a, b=b), (0, f"{lines[0]}\n{lines[1]}\n", "")
        )
        stdin = f"{record('b').to_line()}\n{record('a').to_line()}\n"
        self.assertEqual(
            self.query("status", stdin=stdin, a=a, b=b), (0, f"{lines[1]}\n{lines[0]}\n", "")
        )

    def test_a_failing_key_is_named_and_the_others_answered(self):
        for error, code in (
            (ConfigError("/w/gone: no delegate.json"), 2),
            (SessionError("tmux: boom"), 1),
        ):
            with self.subTest(error=error):
                got = self.query("status", "gone", "a", gone=error, a=View("a"))
                self.assertEqual(
                    got,
                    (
                        code,
                        Status(
                            name="a",
                            alive=False,
                            state="gone",
                            outcome=None,
                            attach=None,
                            hidden=False,
                        ).to_line()
                        + "\n",
                        f"delegate: gone: {error}\n",
                    ),
                )

    def test_two_keys_with_one_name_answer_nothing(self):
        code, out, err = self.query("close", "x", "y", x=View("same"), y=View("same"))
        self.assertEqual((code, out, err), (2, "", "delegate: name same given more than once\n"))
        self.assertEqual((Views.views["x"].closed, Views.views["y"].closed), (0, 0))

    def test_show_prints_each_command_and_exits_1_without_a_showable_session(self):
        shown = View("a", command="tmux attach -t =a")
        none = View("b", show_error=NotShowable("delegate has no live showable session"))
        self.assertEqual(
            self.query("show", "a", "b", a=shown, b=none),
            (1, "tmux attach -t =a\n", "delegate: b: delegate has no live showable session\n"),
        )

    def test_hide_hides_each_key_prints_nothing_and_exits_1_without_a_showable_session(self):
        a = View("a")
        b = View("b", show_error=NotShowable("delegate has no live showable session"))
        self.assertEqual(
            self.query("hide", "a", "b", a=a, b=b),
            (1, "", "delegate: b: delegate has no live showable session\n"),
        )
        self.assertEqual(a.hidden, 1)

    def test_close_closes_each_key_and_prints_nothing(self):
        a, b = View("a"), View("b")
        self.assertEqual(self.query("close", "a", "b", a=a, b=b), (0, "", ""))
        self.assertEqual((a.closed, b.closed), (1, 1))

    def test_outcome_prints_one_json_object_or_null(self):
        done = Outcome("done", "T", "S", deliverable="# Doc\n")
        self.assertEqual(
            self.query("outcome", "a", a=View("a", outcome=done)),
            (
                0,
                '{"status":"done","title":"T","summary":"S","questions":[],"url":"","files":[],"deliverable":"# Doc\\n"}\n',
                "",
            ),
        )
        self.assertEqual(self.query("outcome", "a", a=View("a")), (0, "null\n", ""))
        self.assertEqual(
            self.query("outcome", stdin=record("a").to_line() + "\n", a=View("a")),
            (0, "null\n", ""),
        )

    def test_one_key_commands_refuse_any_other_count(self):
        for command in ("outcome", "attach-command", "resume", "content"):
            with self.subTest(command=command):
                self.assertEqual(
                    self.query(command), (2, "", "delegate: takes exactly one key, got 0\n")
                )
                stdin = record("a").to_line() + "\n" + record("b").to_line() + "\n"
                self.assertEqual(
                    self.query(command, stdin=stdin),
                    (2, "", "delegate: takes exactly one key, got 2\n"),
                )
                code, out, err = self.query(command, "a", "b", a=View("a"), b=View("b"))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("unrecognized arguments: b", err)

    def test_attach_command_prints_one_command_or_exits_1(self):
        self.assertEqual(
            self.query("attach-command", "a", a=View("a", command="tmux attach -t =a")),
            (0, "tmux attach -t =a\n", ""),
        )
        self.assertEqual(
            self.query("attach-command", "a", a=View("a")),
            (1, "", "delegate: a: no showable session\n"),
        )

    def test_resume_inside_an_agent_session_exits_2_and_status_still_answers(self):
        a = View("a")
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            code, out, err = self.query("resume", "a", a=a)
            self.assertEqual((code, out), (2, ""))
            self.assertTrue(err.startswith("delegate: refused inside an agent session"), err)
            self.assertEqual(self.query("status", "a", a=a)[0], 0)

    def test_resume_prints_the_record_line(self):
        self.assertEqual(
            self.query("resume", "a", a=View("a")), (0, record("a").to_line() + "\n", "")
        )

    def test_a_busy_resume_exits_75_any_other_refusal_1(self):
        busy = str(SessionBusy("w", "previous worker generation (live or unknown)"))
        for error, code in ((SessionError(busy), 75), (SessionError("no such run"), 1)):
            view = View("a")
            view.resume = mock.Mock(side_effect=error)
            with self.subTest(error=str(error)):
                self.assertEqual(
                    self.query("resume", "a", a=view), (code, "", f"delegate: a: {error}\n")
                )


class AdhocWorkdir(Base):
    def test_dry_run_derives_workdir_from_date_role_task_and_final_sid(self):
        for task in (None, "echo"):
            for supplied in (False, True):
                with (
                    self.subTest(task=task, supplied=supplied),
                    mock.patch("time.strftime", return_value="2026-10-16"),
                    mock.patch("uuid.uuid4", return_value=uuid.UUID(SID)),
                ):
                    code, out, err = self.cli(
                        "run",
                        "--role",
                        "dummy-tester",
                        "--input",
                        "hello",
                        "--client",
                        "fake",
                        *(("--task", task) if task else ()),
                        *(("--sid", SID) if supplied else ()),
                        "--dry-run",
                    )
                self.assertEqual(code, 0, err)
                shown = json.loads(out)
                suffix = "-echo" if task else ""
                expected = os.path.join(
                    self.ensemble, "adhoc", f"2026-10-16-dummy-tester{suffix}-{SID[:8]}"
                )
                self.assertEqual((shown["sid"], shown["workdir"]), (SID, expected))
                self.assertFalse(os.path.exists(expected))


HELP_COMMANDS = [
    "run",
    "open",
    "status",
    "content",
    "outcome",
    "resume",
    "show",
    "hide",
    "close",
    "attach-command",
    "prompt",
]


class HelpContracts(Case):
    def test_published_operation_examples_use_real_parser_before_dispatch(self):
        def check(command):
            args = parse_only(cli.main, command[1:])
            if args is not None and args.command == "run" and not args.list:
                cli._check(args)

        check_examples(self, self.help, [None, *HELP_COMMANDS], check=check)

    def help(self, command=None, flag="--help"):
        return help_text(self, cli.main, ([] if command is None else [command]) + [flag])

    def test_all_help_ignores_stdin_guards_configuration_and_services(self):
        before = sorted(Path(self.home).rglob("*"))
        source = mock.Mock()
        source.read.side_effect = AssertionError("help read stdin")
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}),
            mock.patch("sys.stdin", source),
            mock.patch("subprocess.Popen") as popen,
            mock.patch("subprocess.run") as run,
        ):
            for command in [None, *HELP_COMMANDS]:
                for flag in ("--help", "-h"):
                    with self.subTest(command=command, flag=flag):
                        text = self.help(command, flag)
                        if command:
                            self.assertIn("Examples:", text)
                            self.assertIn("ensemble-delegate --help", text)
            for boundary in (popen, run):
                boundary.assert_not_called()
        self.assertEqual(sorted(Path(self.home).rglob("*")), before)

    def test_run_help_names_a_locked_runs_release_point(self):
        text = " ".join(self.help("run").split())
        self.assertIn(
            "A locked run's container, TUI and lock outlast its outcome until its user closes it "
            "(ensemble-crew close) or its client exits.",
            text,
        )

    def test_run_help_explains_conditional_inputs_discovery_and_placement(self):
        text = self.help("run")
        for phrase in (
            "--role and --input",
            "--list takes no other",
            "--detach needs --events",
            "ENSEMBLE_EVENTS",
            "ensemble-delegate run --list",
            "ensemble-session --help",
            "--events and --on-event require --detach",
        ):
            self.assertIn(phrase, text)
        self.assertIn("--input, --out and --workdir", self.help("prompt"))

    def test_every_query_explains_cardinality_and_record_input(self):
        for name in HELP_COMMANDS:
            if name in {"run", "open", "prompt"}:
                continue
            with self.subTest(name=name):
                text = self.help(name)
                self.assertIn("Record", text)
                self.assertIn("workdir", text)
                if name in {"content", "outcome", "resume", "attach-command"}:
                    self.assertIn("exactly one", text)
                else:
                    self.assertIn("batch", text)

    def test_detached_example_uses_real_validation_with_fake_delegate(self):
        Detaching.made, Detaching.failure = [], None
        argv = shlex.split(
            "run --role pm --input 'Plan a feature' --detach --display none "
            "--events /tmp/ensemble-events.jsonl"
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv, delegate_cls=Detaching), 0)
        role, spec, placement = Detaching.made[0]
        self.assertEqual(
            (role, spec.input, spec.mode, placement),
            ("pm", "Plan a feature", "tui", {"display": "none"}),
        )
        self.assertEqual(spec.events, "/tmp/ensemble-events.jsonl")
