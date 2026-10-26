"""delegate/engine/plan.py: main's drive_test Cwd cases (place); plan's refusals, Access, Spec, sid and
prompt; its team dirs, the Composer's, refused overlapping the cwd, the client's writable dirs asked
only with team dirs (their other overlap cases: plan_team_integration_test.py).
"""

import io
import json
import os
import tempfile
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from ensemble.delegate import ConfigError, Spec
from ensemble.delegate.composer import Composer, RunConfig, RunParams, teams
from ensemble.delegate.composer.base import TEAM
from ensemble.delegate.composer.prompt import RESUME
from ensemble.delegate.engine.drivers import AgentDriver, InstructionDriver
from ensemble.delegate.engine.plan import place, plan
from ensemble.delegate.engine.types import Layout, Resolved
from ensemble.delegate.event.reply import OUTCOME, PROGRESS
from ensemble.session import Access, EnvironmentSpec, RunLog
from tests.delegate.case import REPO, SRC, packaged
from tests.delegate.plans import SID, TASKS, Base, write_team
from tests.delegate.repo import fail, ok
from tests.delegate.unit.engine.instruction import CLIENTS, GITHUB, ROLES, InstructionCase
from tests.session.unit.clients.codex.support import UNIFIED, committed_headless
from tests.session.unit.clients.codex.support import packaged as codex_client
from tests.utils.case import local_section


def notice(cwd):
    return f"delegate: cwd {cwd} is not in trusted_dirs: its project settings are off\n"


class ReplyTags(Base):
    def test_the_spec_carries_the_delegates_reply_protocol(self):
        got, _ = self.planned()
        self.assertEqual(got.session.reply_tags, (PROGRESS, OUTCOME))


class Cwd(Base):
    """place: the run's cwd and whether its project settings load."""

    def setUp(self):
        super().setUp()
        self.trusted, self.other = (os.path.join(self.tmp, d) for d in ("trusted", "other"))
        for d in (self.trusted, self.other):
            os.makedirs(d)
        self.layout = Layout(self.work)

    def placed(self, *trusted, cwd=None, key="", resume=None):
        """(place's (cwd, project), stderr) for researcher with run key cwd `key` and trusted_dirs
        `trusted`.
        """
        self.local(f"trusted_dirs = {json.dumps(list(trusted))}\n")
        composition, err = Composer(), io.StringIO()
        with redirect_stderr(err):
            got = place(
                replace(composition.resolve("researcher"), cwd=key),
                self.layout,
                resume_sid=resume,
                cwd=cwd,
                trusted=composition.trusted_dirs(),
            )
        return got, err.getvalue()

    def write_record(self, *entries):
        os.makedirs(self.layout.run, exist_ok=True)
        with open(os.path.join(self.layout.run, "run.jsonl"), "w") as f:
            f.writelines(json.dumps({"ts": "t", "kind": "session", **e}) + "\n" for e in entries)

    def test_empty_is_the_agent_dir_made_later(self):
        self.assertEqual(self.placed(), ((self.layout.agent, False), ""))

    def test_the_run_key_wins_over_the_callers_cwd_and_expands_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertEqual(self.placed(cwd=self.trusted, key="~/other")[0], (self.other, False))

    def test_a_trusted_cwd_loads_project_settings_without_a_notice(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.trusted, link)
        for entry, cwd in (
            (self.trusted, self.trusted),
            (link, self.trusted),
            (self.tmp, self.trusted),
            (self.trusted, link),
        ):
            with self.subTest(entry=entry, cwd=cwd):
                self.assertEqual(self.placed(entry, cwd=cwd), ((cwd, True), ""))

    def test_an_untrusted_cwd_gets_a_notice(self):
        self.assertEqual(self.placed(self.trusted, cwd=self.other), ((self.other, False), ""))
        self.assertEqual(self.planned(cwd=self.other)[1], notice(self.other))

    def test_the_agent_dir_as_cwd_has_no_notice(self):
        self.assertEqual(self.placed(cwd=self.layout.agent), ((self.layout.agent, False), ""))
        self.assertEqual(self.planned()[1], "")

    def test_a_trusted_cwd_in_the_workdir_is_refused(self):
        sub = os.path.join(self.layout.agent, "sub")
        os.makedirs(sub)
        for cwd in (self.layout.agent, sub):
            with self.subTest(cwd=cwd), self.assertRaises(ConfigError) as cm:
                self.placed(self.tmp, cwd=cwd)
            self.assertEqual(
                str(cm.exception),
                f"cwd {cwd} is in trusted_dirs and in the workdir: project settings "
                "must not load from it",
            )
        self.assertEqual(self.placed(cwd=sub)[0], (sub, False))

    def test_a_cwd_that_is_no_directory_or_a_bad_trusted_dirs_is_refused(self):
        nope, file = os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "file")
        open(file, "w").close()
        for trusted, key, want in (
            ((), nope, f"cwd {nope} is not a directory"),
            ((), file, f"cwd {file} is not a directory"),
            (("rel/dir",), "", "trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path"),
        ):
            with self.subTest(want=want), self.assertRaises(ConfigError) as cm:
                self.placed(*trusted, cwd=self.other, key=key)
            self.assertEqual(str(cm.exception), want)

    def test_a_resume_runs_in_the_cwd_and_project_of_its_recorded_session(self):
        os.makedirs(self.layout.run)
        RunLog(self.layout.run).note(
            "session",
            sid=SID,
            cwd=self.trusted,
            project=True,
            transcript="",
            resume="",
        )
        self.assertEqual(
            self.placed(self.trusted, cwd=self.other, key=self.work, resume=SID),
            ((self.trusted, True), ""),
        )
        self.write_record({"sid": SID, "cwd": self.trusted, "project": False})
        self.assertEqual(self.placed(self.trusted, resume=SID)[0], (self.trusted, False))

    def test_a_resume_whose_cwd_is_no_longer_trusted_is_refused(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        with self.assertRaises(ConfigError) as cm:
            self.placed(resume=SID)
        self.assertEqual(str(cm.exception), f"cwd {self.trusted} is no longer in trusted_dirs")

    def test_a_forged_later_session_line_does_not_pass_the_trusted_dir_check(self):
        real = {"sid": SID, "cwd": self.trusted, "project": True, "transcript": "", "resume": ""}
        self.write_record(real, {**real, "cwd": self.other, "transcript": "/t", "resume": "x"})
        with self.assertRaises(ConfigError) as cm:
            self.placed(self.trusted, resume=SID)
        self.assertEqual(str(cm.exception), f"cwd {self.other} is no longer in trusted_dirs")

    def test_a_resume_without_its_recorded_session_runs_in_the_agent_dir(self):
        agent = self.layout.agent
        self.assertEqual(
            self.placed(self.trusted, cwd=self.trusted, resume=SID), ((agent, False), "")
        )
        for entries in (
            (),
            ({"sid": "other", "cwd": self.trusted, "project": True},),
            ({"sid": SID, "cwd": "rel", "project": True},),
            ({"sid": SID, "cwd": self.trusted, "project": 1},),
        ):
            with self.subTest(entries=entries):
                self.write_record(*entries)
                self.assertEqual(
                    self.placed(self.trusted, cwd=self.trusted, resume=SID),
                    ((agent, False), ""),
                )

    def test_prompt_paths_stay_absolute_whatever_the_cwd_and_an_input_naming_a_file_is_text(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "input.md"), "w") as f:
            f.write("Research X.")
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        self.local(f"trusted_dirs = {json.dumps([self.trusted])}\n")
        for cwd in (self.trusted, self.other):
            with self.subTest(cwd=cwd):
                got, _ = self.planned(
                    cwd=cwd, input="work/input.md", workdir="work", out="work/out.md"
                )
                self.assertIn(f"\n- `<Workdir>`: `{self.layout.agent}`\n", got.prompt)
                self.assertTrue(got.prompt.endswith("\n# Input\n\nwork/input.md\n"))
                self.assertEqual(
                    (got.session.workdir, got.params.workdir, got.spec.out),
                    (self.layout.run, self.layout.agent, self.out),
                )


class Layouts(Base):
    """The run's agent dir W/agent, the agent's cwd and write grant, and run dir W/run, the
    session's workdir.
    """

    def setUp(self):
        super().setUp()
        self.agent, self.run_dir = (os.path.join(self.work, d) for d in ("agent", "run"))

    def test_workdir_placeholder_cwd_and_deliverable_are_the_agent_dir(self):
        keys = {"commands": ["x --dir {{workdir}}/src *"], "gate": ""}
        got, _ = self.planned(cwd=None, out=None, keys=keys)
        self.assertIn(f"\n- `<Workdir>`: `{self.agent}`\n", got.prompt)
        self.assertIn("`<Workdir>/tmp/deliverable.md`", got.prompt)
        access = got.session.access
        self.assertEqual(
            (got.params.workdir, got.params.deliverable, access.cwd, access.commands),
            (
                self.agent,
                os.path.join(self.agent, "tmp", "deliverable.md"),
                self.agent,
                (f"x --dir {self.agent}/src *",),
            ),
        )

    def test_default_out_is_run_out_md(self):
        got, _ = self.planned(cwd=None, out=None)
        out = os.path.join(self.run_dir, "out.md")
        self.assertEqual((got.spec.out, got.driver_spec.out, got.params.out), (out, out, out))
        composition = Composer()
        instruction = plan(
            composition.resolve("researcher"),
            Spec("Research X.", self.work, client="fake"),
            driver=InstructionDriver(),
            composition=composition,
        )
        self.assertEqual(
            (instruction.spec.out, instruction.driver_spec.out),
            (None, os.path.join(self.agent, "tmp", "deliverable.md")),
        )

    def test_access_dirs_never_hold_workdir_or_run_dir(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        for kw in (
            {"cwd": None},
            {"cwd": other},
            {"cwd": None, "keys": {"cwd": other}},
            {"cwd": None, "sid": SID, "resume": True},
        ):
            with self.subTest(kw=kw):
                got, _ = self.planned(**kw)
                self.assertEqual(got.session.workdir, self.run_dir)
                dirs = set(map(os.path.realpath, got.session.access.dirs))
                self.assertEqual(dirs & {self.work, self.run_dir}, set())

    def test_cwd_or_write_dir_overlapping_run_dir_is_refused(self):
        inside = os.path.join(self.run_dir, "x")
        for kw, label, path in (
            ({"cwd": self.work}, "cwd", self.work),
            ({"cwd": self.run_dir}, "cwd", self.run_dir),
            ({"cwd": inside}, "cwd", inside),
            ({"cwd": None, "keys": {"write": [self.work]}}, "write dir", self.work),
        ):
            with self.subTest(kw=kw):
                self.assertEqual(
                    self.refused(**kw),
                    f"{label} {path} overlaps the run dir {self.run_dir}: the agent must not "
                    "write it",
                )
                self.assertFalse(os.path.exists(self.work))
        os.makedirs(self.run_dir)
        link = os.path.join(self.tmp, "link")
        os.symlink(self.run_dir, link)
        self.assertEqual(
            self.refused(cwd=link),
            f"cwd {link} overlaps the run dir {self.run_dir}: the agent must not write it",
        )

    def test_role_cwd_elsewhere_adds_agent_dir(self):
        elsewhere = os.path.join(self.home, "src", "x")
        os.makedirs(elsewhere)
        got, _ = self.planned(cwd=None, keys={"cwd": "~/src/x"})
        access = got.session.access
        self.assertEqual(access.cwd, elsewhere)
        self.assertIn(self.agent, access.dirs)
        self.assertNotIn(self.run_dir, access.dirs)


class Refusals(Base):
    def test_each_refusal_names_its_cause_and_nothing_is_written(self):
        tasks = ", ".join(Composer().index("researcher"))
        nobody = RunConfig(
            role="nope",
            tier=2,
            effort="high",
            client="fake",
            output={"type": "local"},
            idle_timeout=60,
            show="",
        )
        for kw, want in (
            ({"mode": "x"}, "mode must be one of headless, tui, in_process, got 'x'"),
            ({"name": "n"}, "name: tui mode only"),
            ({"events": "e"}, "events: tui mode only"),
            ({"mode": "in_process", "name": "n"}, "name: tui mode only"),
            ({"mode": "in_process", "events": "e"}, "events: tui mode only"),
            ({"resume": True}, "resume: needs a sid"),
            ({"run": nobody}, "unknown role 'nope'"),
            ({"task": "nope"}, f"task 'nope' is not one of researcher's tasks ({tasks})"),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.refused(**kw), want)
                self.assertFalse(os.path.exists(self.work))

    def test_a_composer_or_driver_error_is_the_delegates_with_its_message(self):
        self.assertEqual(
            self.refused(keys={"templates": ["nope"]}), "missing file templates/nope.md"
        )
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertIn(
            "workdir must be a printable absolute path, got ",
            self.refused(workdir=self.work + "\n", cwd=other),
        )


class Planned(Base):
    KEYS = {"read": ["{{team}}"], "commands": ["x --dir {{workdir}}/src *"]}

    def setUp(self):
        super().setUp()
        self.layout = Layout(self.work)

    def test_the_access_is_the_intents_with_the_gate_last_and_its_env(self):
        command, gate = f"x --dir {self.layout.agent}/src *", "python3 /u/usage.py *"
        env = {"PYTHONPATH": SRC["delegate"]}
        for keys, commands, want in (
            (self.KEYS | {"gate": gate}, (command, gate), env),
            (self.KEYS | {"gate": ""}, (command,), env),
            (self.KEYS | {"commands": [], "gate": ""}, (), {}),
        ):
            with self.subTest(keys=keys):
                got, _ = self.planned(keys=keys)
                self.assertEqual(
                    got.session.access,
                    Access(
                        dirs=(),
                        commands=commands,
                        cwd=self.layout.agent,
                        project=False,
                        reads=(TASKS, TEAM),
                        env=want,
                    ),
                )

    def test_a_cwd_elsewhere_gets_the_agent_dir_added_and_a_notice(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        got, err = self.planned(keys=self.KEYS, cwd=other)
        access = got.session.access
        self.assertEqual(
            (access.cwd, access.project, access.dirs, access.reads),
            (other, False, (self.layout.agent,), (TASKS, TEAM)),
        )
        self.assertEqual(err, notice(other))

    def test_the_spec_holds_the_runs_tier_and_effort_and_the_absolute_run_dir(self):
        planned = self.planned(keys={"tier": 4, "effort": "low"})[0]
        spec = planned.session
        self.assertEqual(spec.sid, planned.spec.sid)
        self.assertEqual(
            (spec.workdir, spec.tier, spec.effort, spec.sid),
            (self.layout.run, 4, "low", spec.sid),
        )

    def test_a_new_run_gets_a_new_uuid4_sid_unless_given_one_and_a_resume_its_own(self):
        first, second = self.planned()[0], self.planned()[0]
        self.assertEqual((uuid.UUID(first.spec.sid).version, first.spec.resume), (4, False))
        self.assertNotEqual(first.spec.sid, second.spec.sid)
        for kw, resume in (({"sid": SID}, False), ({"sid": SID, "resume": True}, True)):
            with self.subTest(kw=kw):
                got = self.planned(**kw)[0]
                self.assertEqual(
                    (got.spec.sid, got.spec.resume, got.params.resume), (SID, resume, resume)
                )

    def test_a_resumes_prompt_starts_with_the_resume_text(self):
        new, resumed = self.planned()[0].prompt, self.planned(sid=SID, resume=True)[0].prompt
        self.assertFalse(new.startswith(RESUME))
        self.assertEqual(resumed, RESUME + new)

    def test_out_is_absolute_and_outside_the_agent_dir_the_prompt_names_the_default_deliverable(
        self,
    ):
        inside = os.path.join(self.layout.agent, "out.md")
        got = self.planned(out=os.path.relpath(inside))[0]
        self.assertEqual(got.spec.out, inside)
        self.assertIn("`<Workdir>/out.md`", got.prompt)
        got = self.planned()[0]
        self.assertEqual(got.spec.out, self.out)
        self.assertIn("`<Workdir>/tmp/deliverable.md`", got.prompt)

    def test_the_plan_holds_the_run_with_its_task_and_its_params(self):
        got = self.planned()[0]
        self.assertEqual(
            (got.spec.client, got.spec.mode, got.run.task), ("fake", "headless", "research")
        )
        self.assertEqual((got.spec.name, got.spec.events), (None, None))
        self.assertEqual(
            got.params, RunParams(input="Research X.", out=self.out, workdir=self.layout.agent)
        )
        self.assertEqual(self.planned(task=None)[0].run.task, "")

    def test_without_a_client_the_runs_own(self):
        got = self.planned(client=None)[0]
        self.assertEqual(got.spec.client, got.run.client)

    def test_in_process_is_planned_as_headless(self):
        got = self.planned(mode="in_process", sid=SID)[0]
        self.assertEqual(got.spec.mode, "in_process")
        self.assertEqual(
            replace(
                got,
                spec=replace(got.spec, mode="headless"),
                session=replace(got.session, mode="headless"),
            ),
            self.planned(sid=SID)[0],
        )

    def test_tui_takes_name_and_events_unchecked_for_the_driver_to_check(self):
        for name, events in ((None, None), ("mine", "/x/events.log"), ("a b", "relative")):
            with self.subTest(name=name, events=events):
                got = self.planned(mode="tui", name=name, events=events)[0]
                self.assertEqual(
                    (got.spec.mode, got.spec.name, got.spec.events), ("tui", name, events)
                )
                self.assertFalse(os.path.exists(self.work))

    def test_tui_rejects_an_unprintable_events_path_before_any_write(self):
        self.assertIn(
            "events must be a printable path", self.refused(mode="tui", name="a b", events="rel\n")
        )
        self.assertFalse(os.path.exists(self.work))

    def test_plan_creates_and_writes_nothing(self):
        for kw in ({}, {"sid": SID, "resume": True}, {"out": None}):
            with self.subTest(kw=kw):
                self.planned(**kw)
                self.assertFalse(os.path.exists(self.work))
        os.makedirs(self.work)
        self.planned()
        self.assertEqual(os.listdir(self.work), [])


class TeamDirs(Base):
    """A team dir in the test's temp dir, the local `team_dirs` or the run's team: the Composer's
    team.
    """

    def setUp(self):
        super().setUp()
        self.team = os.path.join(self.tmp, "team")
        write_team(self.team)
        self.local(f"team_dirs = {json.dumps([self.team])}\n")

    def test_the_clients_dirs_are_asked_with_the_runs_spec_only_when_there_are_team_dirs(self):
        self.local("")
        with mock.patch.object(self.driver, "writable_dirs", return_value=()) as asked:
            got = self.planned(sid=SID, resume=True)[0]
        asked.assert_not_called()
        self.local(f"team_dirs = {json.dumps([self.team])}\n")
        with mock.patch.object(self.driver, "writable_dirs", return_value=()) as asked:
            self.planned(sid=SID, resume=True)
        asked.assert_called_once_with(replace(got.session, prompt=""))

    def test_configured_it_is_the_runs_team_refused_overlapping_the_cwd(self):
        self.assertEqual(
            self.refused("poet", "verse", cwd=self.team),
            f"team_dirs: {self.team} overlaps the cwd {self.team}: the agent run can write there",
        )

    def test_given_as_the_runs_team_it_is_refused_overlapping_the_cwd(self):
        found = teams.team(TEAM, [self.team])
        self.assertEqual(
            self.refused("poet", "verse", team=found, cwd=self.team),
            f"team_dirs: {self.team} overlaps the cwd {self.team}: the agent run can write there",
        )


class Policy(Base):
    """plan's Spec.flags/env: the selected client's [policy.<client>], the local overlay's
    replacing the core's.
    """

    RULE = (
        f"When you return your outcome, that reply is the `{OUTCOME}` fenced block alone: write no "
        "summary or other text before or after it."
    )
    SHIPPED = {
        "claude": (("--strict-mcp-config", f"--append-system-prompt={RULE}"), {}),
        "codex": (("-c", "mcp_servers={}", "-c", f"developer_instructions={json.dumps(RULE)}"), {}),
        "fake": ((), {}),
    }

    def test_the_default_config_gives_each_client_its_shipped_policy(self):
        for client, want in self.SHIPPED.items():
            with self.subTest(client=client):
                got, _ = self.planned(client=client)
                self.assertEqual((got.session.flags, dict(got.session.env)), want)

    def test_a_codex_role_runs_argv_has_each_codex_policy_flag_once(self):
        got, _ = self.planned(client="codex")
        os.makedirs(got.session.access.cwd)
        # Codex refuses a recovery store under /tmp, the Case's HOME.
        home = self.enterContext(
            tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        )
        codex_home = os.path.join(home, ".codex")
        os.mkdir(codex_home)
        with mock.patch.dict(os.environ, {"HOME": home, "CODEX_HOME": codex_home}):
            argv = committed_headless(codex_client(), replace(got.session, prompt="hi")).argv
        for flag, value in UNIFIED:
            with self.subTest(value=value):
                self.assertEqual(argv.count(value), 1)
                self.assertEqual(argv[argv.index(value) - 1], flag)
        self.assertEqual(argv.count("-s"), 1)
        rule = self.SHIPPED["codex"][0][-1]
        self.assertEqual(argv.count(rule), 1)
        self.assertEqual(argv[argv.index(rule) - 1], "-c")
        self.assertNotIn("mcp_servers={}", argv)

    def test_a_codex_role_run_turns_off_each_mcp_server_its_codex_config_names(self):
        got, _ = self.planned(client="codex")
        os.makedirs(got.session.access.cwd)
        home = self.enterContext(
            tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        )
        codex_home = os.path.join(home, ".codex")
        os.mkdir(codex_home)
        with open(os.path.join(codex_home, "config.toml"), "w") as f:
            f.write('[mcp_servers.tickets]\nurl = "https://tickets/mcp"\n')
        with mock.patch.dict(os.environ, {"HOME": home, "CODEX_HOME": codex_home}):
            argv = committed_headless(codex_client(), replace(got.session, prompt="hi")).argv
        off = (
            "-c",
            "mcp_servers.tickets.enabled=false",
            "-c",
            'mcp_servers.tickets.url="https://tickets/mcp"',
        )
        self.assertIn(off, [argv[i : i + 4] for i in range(len(argv))])

    def test_a_local_policy_replaces_the_shipped_one(self):
        self.local('[policy.codex]\nflags = ["-s", "workspace-write"]\nenv = { K = "v" }\n')
        got, _ = self.planned(client="codex")
        self.assertEqual(
            (got.session.flags, dict(got.session.env)), (("-s", "workspace-write"), {"K": "v"})
        )

    def test_an_invalid_policy_is_a_config_error_before_any_launch(self):
        for text in (
            'policy = "x"\n',
            '[policy]\nclaude = "x"\n',
            '[policy.claude]\nflags = "--x"\n',
            "[policy.claude]\nflags = [1]\n",
            '[policy.claude]\nflags = ["a\\u0000"]\n',
            "[policy.claude]\nenv = { K = 1 }\n",
            '[policy.claude]\nunset = ["K"]\n',
        ):
            with self.subTest(text=text), mock.patch.object(self.driver, "start") as start:
                self.local(text)
                self.assertIn("policy", self.refused(client="claude"))
                start.assert_not_called()


class Accounts(Base):
    """plan puts the effective client's account (its role's accounts table) into the session env."""

    def test_the_effective_clients_entry_only(self):
        accounts = {"claude": "work", "codex": ""}
        for client, keys, want in (
            ("claude", None, {"ENSEMBLE_ACCOUNT": "work"}),
            (None, {"client": "claude"}, {"ENSEMBLE_ACCOUNT": "work"}),
            ("codex", None, {}),
            ("fake", {"client": "claude"}, {}),
        ):
            with self.subTest(client=client, keys=keys):
                got, _ = self.planned(client=client, keys=keys, accounts=accounts)
                self.assertEqual(dict(got.session.env), want)
                self.assertEqual(got.session.client, client or "claude")
        got, _ = self.planned(client="claude")
        self.assertEqual(dict(got.session.env), {})

    def test_it_joins_the_policy_env_and_a_docker_runs_users(self):
        self.local('[policy.claude]\nenv = { K = "v" }\n')
        got, _ = self.planned(client="claude", accounts={"claude": "work"})
        self.assertEqual(dict(got.session.env), {"K": "v", "ENSEMBLE_ACCOUNT": "work"})
        self.driver.docker = True
        got, _ = self.planned(client="claude", mode="tui", out=None, accounts={"claude": "w"})
        self.assertEqual(
            (got.session.env["ENSEMBLE_ACCOUNT"], got.session.env["ENSEMBLE_USERS"]), ("w", "[]")
        )


class InstructionPlans(InstructionCase):
    def test_each_roles_destination_and_prompt_are_the_same_under_any_client(self):
        for local in ("", GITHUB):
            with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
                f.write(local_section(local, "delegate"))
            for role in ROLES:
                with self.subTest(github=bool(local), role=role):
                    first, *others = (
                        plan(*args, driver=self.driver, composition=Composer())
                        for args in (
                            self.given(role, client=client, mode=mode) for client, mode in CLIENTS
                        )
                    )
                    for p in others:
                        self.assertEqual((p.run.output, p.prompt), (first.run.output, first.prompt))
                    agent = AgentDriver(packaged())
                    for client, mode in CLIENTS:
                        run, spec = self.given(role, client=client, mode=mode)
                        got = plan(run, spec, driver=agent, composition=Composer())
                        self.assertEqual(
                            (got.run.output, got.prompt), (first.run.output, first.prompt)
                        )
                        self.assertEqual(
                            (got.session.flags, dict(got.session.env)),
                            Policy.SHIPPED[client or run.client],
                        )
        self.assertEqual(Composer().resolve("researcher").output["type"], "github")

    def test_agent_plans_apply_client_policy(self):
        team = os.path.join(self.tmp, "team")
        write_team(team)
        local = os.path.join(self.ensemble, "config.local.toml")
        for client in ("claude", "codex"):
            flags, env = ["--add-dir", team], {"POLICY": client}
            policy = (
                f'[policy.{client}]\nflags = {json.dumps(flags)}\nenv = {{ POLICY = "{client}" }}\n'
            )
            with self.subTest(client=client):
                with open(local, "w", encoding="utf-8") as stream:
                    stream.write(local_section(policy, "delegate"))
                run, spec = self.given("researcher", client=client)
                agent = AgentDriver(packaged())
                got, instruction = (
                    plan(run, spec, driver=agent, composition=Composer()),
                    plan(run, spec, driver=self.driver, composition=Composer()),
                )
                self.assertEqual((got.session.flags, dict(got.session.env)), (tuple(flags), env))
                self.assertEqual(
                    (instruction.session.flags, dict(instruction.session.env)), ((), {})
                )
                self.assertEqual(got.prompt, instruction.prompt)


class Docker(Base):
    """A run whose agent Host is the Docker Host: only the image's team, in its agent dir, its
    Reference repo by name and its deliverable inline.
    """

    def setUp(self):
        super().setUp()
        self.driver.docker = True
        self.layout = Layout(self.work)

    def planned(self, *args, **kw):
        return super().planned(*args, **{"mode": "tui", **kw})

    def test_a_runtime_gate_is_passed_unchanged_to_each_mode(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                got, _ = self.planned(mode=mode, keys={"gate": "owner-brake researcher"})
                self.assertEqual(got.run.gate, "owner-brake researcher")
                self.assertIn(got.run.gate, got.session.access.commands)
                self.assertIn("`owner-brake researcher`", got.prompt)

    def test_a_headless_run_gets_the_docker_rules_and_an_in_process_one_none(self):
        got, _ = self.planned(mode="headless", out=None)
        self.assertTrue(got.docker)
        self.assertEqual(got.session.env["ENSEMBLE_USERS"], "[]")
        self.assertIn("\n- `<tasks>`: `/opt/ensemble/team/tasks`\n", got.prompt)
        self.assertEqual(got.session.access.cwd, self.layout.agent)
        two = "Reference: T\nRepo: a/b\nRepo: c/d\n\nX"
        self.assertIn("Repo:", self.refused(mode="headless", input=two))
        self.assertIn("read", self.refused(mode="headless", keys={"read": ["~/x"]}, out=None))
        got, _ = self.planned(mode="in_process", input=two)
        self.assertEqual((got.docker, got.repo), (False, None))
        self.assertIn(f"- `<tasks>`: `{TASKS}`", got.prompt)
        self.assertNotIn("ENSEMBLE_USERS", got.session.env)

    def test_the_default_core_roles_run_with_the_images_team(self):
        for role, task in (
            ("researcher", "research"),
            ("pm", "product-design"),
            ("engineer", "build"),
        ):
            with self.subTest(role=role):
                got, _ = self.planned(role, task, out=None)
                self.assertTrue(got.docker)
                self.assertNotIn(TEAM, got.prompt)
                self.assertIn("\n- `<tasks>`: `/opt/ensemble/team/tasks`\n", got.prompt)
                self.assertIn("\n- `<team>`: `/opt/ensemble/team`\n", got.prompt)
                access = got.session.access
                self.assertEqual((access.dirs, access.cwd, access.env), ((), self.layout.agent, {}))
                self.assertEqual(got.session.env["ENSEMBLE_USERS"], "[]")
                self.assertEqual(access.reads[0], "/opt/ensemble/team/tasks")
                self.assertTrue(all(r.startswith("/opt/ensemble/team") for r in access.reads))
                self.assertFalse(any(TEAM in c for c in access.commands), access.commands)

    def test_the_prompt_asks_for_the_deliverable_inline(self):
        for output in ({"type": "local"}, {"type": "orchestrator"}, {"type": "pull-request"}):
            with self.subTest(output=output):
                got, _ = self.planned(keys={"output": output}, out=None)
                self.assertNotIn("deliverable_file", got.prompt)
                self.assertNotIn("deliverable.md", got.prompt)
                if output["type"] == "pull-request":
                    self.assertNotIn("`deliverable`", got.prompt)
                else:
                    self.assertIn("inline in the outcome's `deliverable`", got.prompt)
                    self.assertIn("\n- `deliverable`: the document, when ", got.prompt)
        self.driver.docker = False
        got, _ = self.planned(keys={"output": {"type": "local"}}, out=None)
        self.assertNotIn("ENSEMBLE_USERS", got.session.env)
        self.assertIn("deliverable_file", got.prompt)
        self.assertIn(f"- `<tasks>`: `{TASKS}`", got.prompt)

    def origin(self, url, code=0):
        """subprocess.run answering git's `remote get-url origin` with `url`."""
        done = fail("fatal: no remote") if code else ok(url)
        return mock.patch("subprocess.run", return_value=done)

    def test_a_local_reference_repo_is_rewritten_to_its_name(self):
        clone = os.path.join(self.tmp, "clone")
        text = f"Reference: T\nTitle: x\nRepo: {clone}\n\nBody Repo: {clone}\n"
        with self.origin("git@github.com:acme/ensemble.git\n"):
            got, _ = self.planned(input=text)
        self.assertEqual(got.repo, "acme/ensemble")
        want = f"Reference: T\nTitle: x\nRepo: acme/ensemble\n\nBody Repo: {clone}\n"
        self.assertEqual(got.spec.input, want)
        self.assertTrue(got.prompt.endswith(f"\n{want.strip()}\n"))

    def test_a_reference_repo_off_github_is_refused(self):
        for text, url, code in (
            ("Reference: T\nRepo: ~/src/x\n\nBody", "", 2),
            ("Reference: T\nRepo: ~/src/x\n\nBody", "git@ghe.example.com:o/x.git\n", 0),
            ("Reference: T\nRepo: ghe.example.com/o/x\n\nBody", "", 0),
        ):
            with self.subTest(text=text, url=url), self.origin(url, code):
                self.assertIn("Repo:", self.refused(input=text))

    def test_host_dir_grants_are_refused_naming_their_key(self):
        for keys, word in (
            ({"read": ["~/x"]}, "read"),
            ({"read": ["{{team}}", "repo"]}, "read"),
            ({"write": ["~/y"]}, "write"),
            ({"cwd": self.tmp}, "cwd"),
        ):
            with self.subTest(keys=keys):
                self.assertIn(word, self.refused(keys=keys, out=None))
        project = os.path.join(self.tmp, "project")
        os.mkdir(project)
        self.local(f"trusted_dirs = {json.dumps([project])}\n")
        self.driver.docker = True
        self.assertIn("trusted_dirs", self.refused(cwd=project, out=None))
        self.assertFalse(os.path.exists(self.work))

    def test_a_team_dir_role_is_refused(self):
        top = os.path.join(self.tmp, "team")
        write_team(top)
        team = teams.team(dirs=(top,))
        self.assertIn("team_dirs", self.refused("poet", "verse", team=team, out=None))


SECRET = {"item": "ensemble-env/dummy", "file": "dummy.txt"}


class PrefetchTest(Docker):
    """A resolved environment's prefetch target: the Reference block's one Repo: and optional Ref:,
    syntax-checked before any git call (WDRC-109 FR-52).
    """

    SPEC = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")

    def env(self, *, prefetch=True):
        return Resolved(self.SPEC, prefetch=prefetch)

    def test_the_target_and_ref_join_the_environment(self):
        text = "Reference: T\nRepo: acme/app\nRef: release/1\nBase: dev\n\nBody"
        got, _ = self.planned(input=text, environment=self.env())
        environment = got.session.environment
        self.assertEqual(dict(environment.prefetch), {"repo": "acme/app", "ref": "release/1"})
        self.assertEqual(replace(environment, prefetch=None), self.SPEC)
        self.assertEqual(got.repo, "acme/app")

    def test_without_a_ref_line_the_default_branch(self):
        got, _ = self.planned(input="Reference: T\nRepo: acme/app\n\nB", environment=self.env())
        self.assertEqual(dict(got.session.environment.prefetch), {"repo": "acme/app"})

    def test_a_clone_path_target_is_its_origin(self):
        clone = os.path.join(self.tmp, "clone")
        with self.origin("git@github.com:acme/ensemble.git\n"):
            got, _ = self.planned(input=f"Reference: T\nRepo: {clone}\n\nB", environment=self.env())
        self.assertEqual(dict(got.session.environment.prefetch), {"repo": "acme/ensemble"})
        self.assertIn("Repo: acme/ensemble\n", got.spec.input)

    def test_bad_lines_are_refused_before_any_git_call(self):
        for lines in (
            "Repo: acme/app\nRepo: acme/app",
            "Repo: acme/app\nRef: a\nRef: b",
            "Repo: acme/app\nRef: --upload-pack=x",
            "Repo: acme/app\nRef: refs/pull/1/head",
            "Repo: acme/app\nRef: 0123456789abcdef",
            "Repo: acme/app\nRef: a b",
            "Repo: not a repo!",
            "Ref: v1",
        ):
            with (
                self.subTest(lines=lines),
                mock.patch("subprocess.run", side_effect=AssertionError("git ran")) as run,
            ):
                message = self.refused(input=f"Reference: T\n{lines}\n\nB", environment=self.env())
                self.assertTrue(message.startswith("prefetch"), message)
                run.assert_not_called()

    def test_a_target_off_github_is_refused(self):
        message = self.refused(
            input="Reference: T\nRepo: ghe.example.com/o/x\n\nB", environment=self.env()
        )
        self.assertIn("prefetch", message)

    def test_without_prefetch_ref_lines_are_not_read(self):
        text = "Reference: T\nRepo: acme/app\nRef: --upload-pack=x\n\nB"
        got, _ = self.planned(input=text, environment=self.env(prefetch=False))
        self.assertEqual(got.session.environment, self.SPEC)
        got, _ = self.planned(input=text)
        self.assertIsNone(got.session.environment)

    def test_an_in_process_run_refuses_what_it_would_lose(self):
        for environment, key in (
            (self.env(prefetch=False), "lock"),
            (self.env(), "prefetch"),
            (Resolved(replace(self.SPEC, lock=None, secrets=(SECRET,)), prefetch=False), "secrets"),
        ):
            with self.subTest(key=key):
                message = self.refused(mode="in_process", environment=environment)
                self.assertIn(f"in_process: environment {key}", message)
        gateway = Resolved(EnvironmentSpec(image="@worker", gateway="x"), prefetch=False)
        got, _ = self.planned(mode="in_process", environment=gateway)
        self.assertIsNone(got.session.environment)
