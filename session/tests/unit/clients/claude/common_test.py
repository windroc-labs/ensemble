"""Claude common behavior."""

import contextlib
import dataclasses
import errno
import io
import json
import os
import shlex
import stat
import time
import unittest
from functools import partial
from unittest import mock

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.common import HOME
from ensemble.session.clients.claude.usage import NO_PANEL
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    Mode,
    Permission,
    SessionInfo,
    SessionStatus,
)
from ensemble.utils import LOCAL_ENV_VARS, docker_home

from session.tests.case import SPAWNS, Case, packaged_hosts
from session.tests.unit.clients.claude.support import (
    CEILING,
    CONFIG,
    HOOK,
    MODES,
    PROBE,
    SID,
    SKIP_DIALOG,
    TUI_DEFAULTS,
    UNSET,
    Base,
    ClaudeCommon,
    Spec,
    fixture,
    reference,
    store,
    tui_launch,
)

PLUGIN = '{"enabledPlugins": {"ensemble@windroc": false}}'


def holds(argv, words) -> bool:
    return any(tuple(argv[i : i + len(words)]) == tuple(words) for i in range(len(argv)))


class PermissionTest(Base):
    def test_only_the_runs_permission_modes_flags_reach_claude_settings_merged(self):
        config = {"flags": ["--settings", PLUGIN], "modes": MODES}
        plugin = json.loads(PLUGIN)
        rows = (
            (Permission.AUTO, MODES["auto"], "--dangerously-skip-permissions", plugin),
            (
                Permission.YOLO,
                ["--dangerously-skip-permissions"],
                "--permission-mode",
                {**plugin, **json.loads(SKIP_DIALOG)},
            ),
        )
        for permission, present, absent, settings in rows:
            for mode, launch in (
                ("headless", self.launch),
                ("tui", partial(tui_launch, self.work)),
            ):
                with self.subTest(permission=permission, mode=mode):
                    argv = launch(config=config, permission=permission).argv
                    self.assertTrue(holds(argv, present), argv)
                    self.assertNotIn(absent, argv)
                    self.assertEqual(argv.count("--settings"), 1)
                    merged = json.loads(argv[argv.index("--settings") + 1])
                    merged.pop("hooks", None)
                    self.assertEqual(merged, settings)

    def test_the_packaged_config_gives_docker_claude_only_yolo_flags_every_other_host_only_auto(
        self,
    ):
        table = dict(read_config(local=os.devnull)["clients"]["claude"])
        table.pop("implementation")
        words = {
            Permission.AUTO: "--permission-mode",
            Permission.YOLO: "--dangerously-skip-permissions",
        }
        for name, permission in packaged_hosts().items():
            for mode, launch in (
                ("headless", self.launch),
                ("tui", partial(tui_launch, self.work)),
            ):
                with self.subTest(host=name, mode=mode):
                    argv = launch(config=table, permission=permission).argv
                    self.assertEqual([w for w in argv if w in words.values()], [words[permission]])
                    settings = json.loads(argv[argv.index("--settings") + 1])
                    self.assertEqual(
                        settings.get("skipDangerousModePermissionPrompt"),
                        True if permission == Permission.YOLO else None,
                    )

    def test_without_modes_neither_permission_adds_a_flag(self):
        for permission in Permission:
            with self.subTest(permission=permission):
                self.assertEqual(
                    self.launch(config={}, permission=permission).argv,
                    self.launch(config={}).argv,
                )


class ModelEffortTest(Base):
    def test_none_omits_the_flag_and_a_value_is_the_tables(self):
        common = ClaudeCommon(CONFIG)
        self.assertEqual((common.model(None), common.effort(None)), (None, None))
        self.assertEqual(
            (common.model(1), common.model(4), common.effort("max")), ("fable", "haiku", "mx")
        )

    def test_a_missing_entry_names_the_table(self):
        for config in (CONFIG, {}):
            with self.subTest(config=config):
                common = ClaudeCommon(config)
                self.refused(
                    lambda *, common=common: common.model(2),
                    "no model for tier 2 in [clients.claude].tiers",
                )
                self.refused(
                    lambda *, common=common: common.effort("medium"),
                    "no effort for 'medium' in [clients.claude].efforts",
                )
        self.refused(lambda: self.launch(tier=3), "no model for tier 3 in [clients.claude].tiers")
        self.refused(
            lambda: self.launch(effort="high"), "no effort for 'high' in [clients.claude].efforts"
        )


class HomeTest(Case):
    def test_the_credential_kind_a_container_host_injects(self):
        self.assertEqual(HOME.credential, "claude-setup-token")


class AccountsTest(Base):
    def found(self, accounts) -> dict[str, tuple[str, str]]:
        common = ClaudeCommon({"accounts": accounts})
        return {name: (a.dir, a.display) for name, a in common.accounts.items()}

    def test_the_default_account_is_always_registered_with_the_home_claude_dir(self):
        default = os.path.join(self.home, ".claude")
        self.assertEqual(
            {n: (a.dir, a.display) for n, a in ClaudeCommon({}).accounts.items()},
            {"default": (default, "")},
        )
        self.assertEqual(self.found({}), {"default": (default, "")})
        self.assertEqual(
            self.found({"default": {"account": "me@home"}}), {"default": (default, "me@home")}
        )
        self.assertEqual(ClaudeClient({})._accounts(), frozenset({"default"}))

    def test_the_packaged_config_registers_only_the_default_account(self):
        table = dict(read_config(local=os.devnull)["clients"]["claude"])
        table.pop("implementation")
        self.assertEqual(table["accounts"], {})
        self.assertEqual(ClaudeClient(table)._accounts(), frozenset({"default"}))

    def test_each_registered_account_keeps_its_expanded_dir_and_display(self):
        accounts = {
            "work": {"dir": "~/.claude-work", "account": "me@work"},
            "lab.2_x-y": {"dir": "/abs/lab"},
            "a" * 64: {},
        }
        self.assertEqual(
            self.found(accounts),
            {
                "default": (os.path.join(self.home, ".claude"), ""),
                "work": (os.path.join(self.home, ".claude-work"), "me@work"),
                "lab.2_x-y": ("/abs/lab", ""),
                "a" * 64: (os.path.join(self.home, ".claude"), ""),
            },
        )
        self.assertEqual(
            ClaudeClient({"accounts": accounts})._accounts(),
            frozenset({"default", "work", "lab.2_x-y", "a" * 64}),
        )

    def test_the_dir_is_expanded_through_the_users_home(self):
        with mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/u")):
            self.assertEqual(self.found({"w": {"dir": "~/w"}})["w"], ("/u/w", ""))
            self.refused(
                lambda: self.found({"w": {"dir": "x~/w"}}),
                "[clients.claude].accounts.w.dir must be an absolute path after ~ expansion",
            )

    def test_a_bad_registry_names_the_key(self):
        name = (
            "not an account name (1–64 lowercase letters, digits, '.', '_' or '-', starting with a"
            " letter or digit, and not setup-token)"
        )
        absolute = "must be an absolute path after ~ expansion"
        cases = (
            ("SECRET", "[clients.claude].accounts must be a table of account tables"),
            (["work"], "[clients.claude].accounts must be a table of account tables"),
            *(
                ({bad: {}}, f"[clients.claude].accounts {bad!r}: {name}")
                for bad in ("Work", "setup-token", "", "-w", ".w", "a" * 65, "w/x", "w x")
            ),
            ({"work": "~/w"}, "[clients.claude].accounts.work must be a table"),
            ({"work": ["~/w"]}, "[clients.claude].accounts.work must be a table"),
            (
                {"work": {"token": "SECRET"}},
                "[clients.claude].accounts.work.token: unknown key; an account has only dir and"
                " account",
            ),
            ({"work": {"dir": 1}}, "[clients.claude].accounts.work.dir must be a string"),
            (
                {"work": {"account": None}},
                "[clients.claude].accounts.work.account must be a string",
            ),
            *(
                ({"work": {"dir": d}}, f"[clients.claude].accounts.work.dir {absolute}")
                for d in ("", "rel/w", "~user-that-does-not-exist-x/w", "/a\nb")
            ),
            (
                {"default": {"dir": "/abs"}},
                "[clients.claude].accounts.default.dir: the default account's dir is ~/.claude;"
                " remove it",
            ),
        )
        for accounts, message in cases:
            with self.subTest(accounts=accounts):
                self.refused(lambda *, a=accounts: self.found(a), message)
                self.refused(lambda *, a=accounts: ClaudeClient({"accounts": a}), message)


class HomeFilesTest(Base):
    MODEL = {"model": "opus[1m]", "effortLevel": "xhigh", "modelSettings": {"opus": {"a": 1}}}

    def settings(self, text):
        os.makedirs(self.config_dir, exist_ok=True)
        with open(os.path.join(self.config_dir, "settings.json"), "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())

    UNATTENDED = {
        "skipWorkflowUsageWarning": True,
        "autoCompactEnabled": True,
        "autoContinueAtUsageLimit": True,
        "promptSuggestionEnabled": False,
        "tui": "fullscreen",
        "outputStyle": "Concise",
    }
    SEARCHES = {"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "50"}

    def container_settings(self):
        ((name, text),) = ClaudeCommon({}).home_files()
        self.assertEqual(name, "settings.json")
        return json.loads(text)

    def test_only_the_hosts_model_keys_and_search_cap_reach_a_container_home(self):
        other = {
            "hooks": {"Stop": []},
            "permissions": {"allow": ["Bash"]},
            "statusLine": {"type": "command", "command": "x"},
            "enabledPlugins": {"p@m": True},
            "extraKnownMarketplaces": {"m": {}},
            "theme": "dark",
            "env": {"K": "v", **self.SEARCHES},
        }
        self.settings(json.dumps({**self.MODEL, **other, "apiKeyHelper": "/bin/key"}))
        ((name, text),) = ClaudeClient(CONFIG).tui()._home().files
        self.assertEqual(
            (name, json.loads(text)),
            ("settings.json", {**self.UNATTENDED, **self.MODEL, "env": self.SEARCHES}),
        )
        self.assertEqual(dataclasses.replace(ClaudeClient(CONFIG).tui()._home(), files=()), HOME)

    def test_the_unattended_keys_are_fixed_whatever_the_host_sets(self):
        host = {
            **dict.fromkeys(self.UNATTENDED, False),
            "promptSuggestionEnabled": True,
            "tui": "default",
            "outputStyle": "Explanatory",
        }
        self.settings(json.dumps(host))
        self.assertEqual(self.container_settings(), self.UNATTENDED)

    def test_each_models_auto_compact_window_stays_on_the_host(self):
        models = {
            "claude-opus-5-5": {"effortLevel": "medium", "autoCompactWindow": 400000},
            "sonnet": {"autoCompactWindow": 1},
            "haiku": "x",
        }
        self.settings(json.dumps({"modelSettings": models}))
        self.assertEqual(
            self.container_settings(),
            {
                **self.UNATTENDED,
                "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}, "haiku": "x"},
            },
        )

    def test_no_model_settings_when_every_entry_is_left_empty(self):
        for models in ({"opus": {"autoCompactWindow": 1}, "sonnet": {}}, {}):
            with self.subTest(models=models):
                self.settings(json.dumps({**self.MODEL, "modelSettings": models}))
                expected = {k: v for k, v in self.MODEL.items() if k != "modelSettings"}
                self.assertEqual(self.container_settings(), {**self.UNATTENDED, **expected})

    def test_model_settings_that_are_not_an_object_reach_a_container_home_unchanged(self):
        self.settings(json.dumps({"modelSettings": "x"}))
        self.assertEqual(self.container_settings(), {**self.UNATTENDED, "modelSettings": "x"})

    def test_no_env_without_the_hosts_search_cap(self):
        for env in ({"K": "v"}, {}, ["x"], "x"):
            with self.subTest(env=env):
                self.settings(json.dumps({**self.MODEL, "env": env}))
                self.assertEqual(self.container_settings(), {**self.UNATTENDED, **self.MODEL})

    def test_missing_or_invalid_host_settings_give_only_the_unattended_keys(self):
        self.assertEqual(self.container_settings(), self.UNATTENDED)
        for text in ("not json", "[]", '{"theme": "dark"}', b"\xff"):
            with self.subTest(text=text):
                self.settings(text)
                self.assertEqual(self.container_settings(), self.UNATTENDED)
        os.remove(os.path.join(self.config_dir, "settings.json"))
        os.mkdir(os.path.join(self.config_dir, "settings.json"))
        self.assertEqual(self.container_settings(), self.UNATTENDED)

    def test_an_explicit_model_or_effort_flag_still_wins(self):
        self.settings(json.dumps(self.MODEL))
        argv = tui_launch(self.work, tier=1, effort="max").argv
        self.assertEqual(
            (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("fable", "mx")
        )
        self.assertFalse(any("opus[1m]" in arg for arg in argv))


class SettingSourcesTest(Base):
    def test_session_flags_cannot_override_access_project_in_either_mode(self):
        for flags in (
            ("--setting-sources", "user,project,local"),
            ("--setting-sources=user,project,local",),
        ):
            for resume in (False, True):
                spec = Spec(
                    workdir=self.work,
                    access=Access(cwd=self.work),
                    sid=SID,
                    flags=flags,
                    resume=resume,
                )
                client = ClaudeClient({})
                for launch in (
                    lambda client=client, spec=spec: client.headless().launch(spec),
                    lambda client=client, spec=spec: client.tui().launch(spec, HOOK),
                ):
                    with self.subTest(flags=flags, resume=resume):
                        self.refused(
                            launch,
                            "--setting-sources is set by the Claude client: use Access.project",
                        )


class ReadRulesTest(Base):
    def modes(self, *, resume=False, config=None, **access):
        spec = Spec(
            workdir=self.work,
            sid=SID,
            access=Access(cwd=self.work, **access),
            prompt="hi",
            resume=resume,
        )
        client = ClaudeClient({} if config is None else config)
        return (
            lambda client=client, spec=spec: client.headless().launch(spec),
            lambda client=client, spec=spec: client.tui().launch(spec, HOOK),
        )

    def both(self, *, resume=False, config=None, **access) -> list[tuple[str, ...]]:
        return [launch().argv for launch in self.modes(resume=resume, config=config, **access)]

    def test_reads_alone_are_allowed_tools_never_added_dirs(self):
        for argv in self.both(reads=("/r",)):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//r/**)", "--", "hi"))
            self.assertNotIn("--add-dir", argv)

    def test_the_root_is_one_rule(self):
        for argv in self.both(reads=("/", "//")):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//**)", "--", "hi"))

    def test_a_symlinked_read_dir_gets_a_rule_for_both_spellings_each_once(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for reads, dirs in (
            ((link,), (link, target)),
            ((link + "/", target), (link, target)),
            ((target, link), (target, link)),
        ):
            with self.subTest(reads=reads):
                for argv in self.both(reads=reads):
                    self.assertEqual(
                        argv[-5:],
                        ("--allowedTools", *(f"Read(/{d}/**)" for d in dirs), "--", "hi"),
                    )

    def test_a_resume_keeps_the_rules(self):
        for argv in self.both(resume=True, config=CONFIG, commands=("ls",), reads=("/r",)):
            self.assertEqual(argv[1 + argv.index("--resume")], SID)
            self.assertEqual(
                argv[-7:],
                ("--allowedTools", "Bash(ls)", "Read(//r/**)", "Read", "Edit(/x)", "--", "hi"),
            )

    def test_a_read_dir_a_rule_cannot_hold_literally_is_refused_before_any_launch(self):
        bad = ["", "r", "./r", "/r\n", "/r\0", *(f"/r{c}s" for c in "*?[]{}()\\,"), "/" + "*" * 99]
        for read in bad:
            for launch in self.modes(reads=("/ok", read)):
                with self.subTest(read=read):
                    self.refused(
                        launch,
                        f"claude: read dir {read!r:.80} must be a printable absolute path "
                        "without *?[]{}()\\,",
                    )

        self.session_dependencies = {
            "host": mock.Mock(tmux_path=mock.Mock(return_value="/protected/tmux"))
        }
        self.configure_session(
            {"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}}
        )
        for read in bad:
            for mode in (Mode.HEADLESS, Mode.TUI):
                with self.subTest(read=read, mode=mode):
                    launched = [self.enterContext(mock.patch(target)) for target in SPAWNS]
                    spec = Spec(
                        workdir=self.work,
                        sid=SID,
                        access=Access(cwd=self.work, reads=("/ok", read)),
                        client="claude",
                        mode=mode,
                        prompt="hi",
                    )
                    self.refused(
                        partial(self.session_cls.start, spec),
                        f"claude: read dir {read!r:.80} must be a printable absolute path without *?[]{{}}()\\,",
                    )
                    for spawn in launched:
                        spawn.assert_not_called()
                    self.assertFalse(os.path.exists(os.path.join(self.work, "run.jsonl")))

    def test_a_read_dir_whose_real_path_a_rule_cannot_hold_is_refused(self):
        target = os.path.join(self.tmp, "a*b")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for launch in self.modes(reads=(link,)):
            self.refused(
                launch,
                f"claude: read dir {target!r:.80} must be a printable absolute path without "
                "*?[]{}()\\,",
            )


class EnvTest(Base):
    def test_repository_selection_environment_is_removed_from_every_launch_source(self):
        git_env = dict.fromkeys(LOCAL_ENV_VARS, "/deployment")
        config = {"env": {**git_env, "KEEP": "configured"}}
        for resume in (False, True):
            spec = Spec(
                workdir=self.work,
                sid=SID,
                resume=resume,
                access=Access(cwd=self.work, env=git_env),
                env=git_env,
            )
            client = ClaudeClient(config)
            for launch in (
                client.headless().launch(spec),
                client.tui().launch(spec, HOOK),
                client.usage_probe(),
            ):
                with self.subTest(resume=resume, argv=launch.argv):
                    self.assertTrue(set(launch.unset) >= LOCAL_ENV_VARS)
                    self.assertFalse(LOCAL_ENV_VARS & launch.env.keys())
                    self.assertEqual(launch.env["KEEP"], "configured")

    def test_headless_defaults_overlaid_by_the_config_env_tui_only_the_config_env(self):
        self.assertEqual(ClaudeCommon({}).env(), {})
        config = {"env": {"K": "v", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1"}}
        self.assertEqual(dict(self.launch(config=config).env), config["env"])
        self.assertEqual(dict(self.launch(config={}).env), CEILING)
        self.assertEqual(dict(tui_launch(self.work, config={}).env), TUI_DEFAULTS)

    def test_agent_view_default_is_tui_only(self):
        key = "CLAUDE_CODE_DISABLE_AGENT_VIEW"
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(tui_launch(self.work, config={}, resume=resume).env.get(key), "1")
                self.assertNotIn(key, self.launch(config={}, resume=resume).env)

    def test_agent_view_default_yields_to_explicit_environment(self):
        key = "CLAUDE_CODE_DISABLE_AGENT_VIEW"
        for kwargs in (
            {"config": {"env": {key: "0"}}},
            {"access": Access(cwd=self.work, env={key: "0"})},
            {"env": {key: "0"}},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(tui_launch(self.work, **kwargs).env[key], "0")

    def test_the_launch_env_unset_and_cwd(self):
        launch = self.launch()
        self.assertEqual(dict(launch.env), {**CEILING, "K": "v"})
        self.assertEqual(launch.unset, UNSET)
        self.assertEqual(ClaudeCommon.UNSET, UNSET)
        self.assertEqual(launch.cwd, "")

    def test_headless_uses_session_flags_and_env(self):
        flags = ("--permission-mode", "auto")
        launch = self.launch(flags=flags, env={"K": "session", "CUSTOM": "value"})
        self.assertEqual(launch.argv[-4:], (*flags, "--", "hi"))
        self.assertEqual(dict(launch.env), {**CEILING, "K": "session", "CUSTOM": "value"})

    def test_the_session_env_wins_over_the_config_env(self):
        config = {**CONFIG, "env": {"K": "v", "PYTHONPATH": "/config"}}
        access = Access(cwd=self.work, env={"PYTHONPATH": "/root", "S": "s"})
        self.env({"PYTHONPATH": "/parent"})
        for launch, defaults in (
            (self.launch(config=config, access=access), CEILING),
            (
                ClaudeClient(config)
                .tui()
                .launch(Spec(workdir=self.work, sid=SID, access=access, prompt="hi"), HOOK),
                TUI_DEFAULTS,
            ),
        ):
            self.assertEqual(
                dict(launch.env), {**defaults, "K": "v", "PYTHONPATH": "/root", "S": "s"}
            )


class UsageProbeTest(Base):
    def test_the_probe_strips_api_credentials_and_keeps_the_account_dir(self):
        ambient = {"ANTHROPIC_API_KEY": "sk-1", "ANTHROPIC_BASE_URL": "u", "HOME": "/h"}
        config = {"env": {"CLAUDE_CONFIG_DIR": "/acct", "ANTHROPIC_AUTH_TOKEN": "x", "K": "v"}}
        with mock.patch.dict(os.environ, ambient, clear=True):
            launch = ClaudeClient(config).usage_probe()
        self.assertEqual(launch.argv, PROBE)
        self.assertEqual(
            dict(launch.env), {"CLAUDE_CONFIG_DIR": "/acct", "K": "v", "DISABLE_AUTOUPDATER": "1"}
        )
        self.assertEqual(
            launch.unset[len(UNSET) :],
            ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"),
        )
        self.assertEqual(launch.unset[: len(UNSET)], UNSET)

    def test_each_account_probes_its_own_dir_and_never_an_inherited_one(self):
        ambient = {"ANTHROPIC_API_KEY": "sk-1", "HOME": self.home, "CLAUDE_CONFIG_DIR": "/w"}
        accounts = {"work": {"dir": "/w"}, "same": {}, "default": {"account": "me"}}
        env = {"K": "v", "ANTHROPIC_AUTH_TOKEN": "x", "CLAUDE_CONFIG_DIR": "/acct"}
        with mock.patch.dict(os.environ, ambient, clear=True):
            client = ClaudeClient({"env": env, "accounts": accounts})
            usual = client.usage_probe()
            probes = client._usage_probes()
        home = os.path.join(self.home, ".claude")
        credentials = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        own = Launch(
            PROBE,
            env={"K": "v", "DISABLE_AUTOUPDATER": "1"},
            unset=(*UNSET, *credentials, "CLAUDE_CONFIG_DIR"),
        )
        work = Launch(
            PROBE,
            env={"K": "v", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CONFIG_DIR": "/w"},
            unset=(*UNSET, *credentials),
        )
        self.assertEqual(
            probes, {"default": (home, own), "same": (home, own), "work": ("/w", work)}
        )
        self.assertEqual(usual.env["CLAUDE_CONFIG_DIR"], "/acct")
        self.assertEqual(usual.unset, (*UNSET, *credentials))

    def test_a_usage_panel_is_the_usage(self):
        for name, five, week in (
            ("usage-default.txt", 17.0, 1.0),
            ("usage-config-dir.txt", 13.0, 14.0),
        ):
            with self.subTest(name), contextlib.redirect_stderr(io.StringIO()) as err:
                found = ClaudeCommon.parse_usage(fixture(name))
                self.assertEqual(
                    (found.five_hour.used_percent, found.weekly.used_percent), (five, week)
                )
                self.assertIsNotNone(found.five_hour.resets_at)
                self.assertEqual(err.getvalue(), "")

    def test_output_without_a_usage_panel_is_unknown_and_says_why(self):
        for output, reason in (
            ("", "no output"),
            ("Hi! It looks like you typed /usage. How can I help?\n", NO_PANEL),
            ('{"type":"result","result":"OK"}\n', NO_PANEL),
            ("Failed to load usage data\n", "could not load usage data"),
        ):
            with self.subTest(output), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(ClaudeClient({}).usage(output))
                self.assertEqual(err.getvalue(), f"claude usage: unknown: {reason}\n")


class TranscriptTest(Base):
    def test_projects_dir_named_by_the_mangled_cwd(self):
        self.assertEqual(
            ClaudeCommon({}).transcript("/ensemble-absent/a.b_c d-é", SID),
            os.path.join(self.config_dir, "projects", "-ensemble-absent-a-b-c-d--", f"{SID}.jsonl"),
        )

    def test_the_cwds_realpath(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        common = ClaudeCommon({})
        self.assertEqual(common.transcript(link, SID), common.transcript(self.work, SID))
        self.assertNotIn("link", common.transcript(link, SID))

    def test_the_config_dir_from_the_config_env_then_the_process_env_then_home(self):
        given = {"env": {"CLAUDE_CONFIG_DIR": "/given"}}
        self.assertEqual(
            ClaudeCommon(given).transcript("/w", SID), f"/given/projects/-w/{SID}.jsonl"
        )
        self.assertEqual(ClaudeCommon(given).config_dir(), "/given")
        self.assertEqual(ClaudeCommon({}).config_dir(), self.config_dir)
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(
            ClaudeCommon({}).transcript("/w", SID),
            os.path.join(self.home, ".claude", "projects", "-w", f"{SID}.jsonl"),
        )

    def test_the_launch_transcript_is_in_its_cwd(self):
        cwd = os.path.join(self.tmp, "cwd")
        common = ClaudeCommon({})
        self.assertEqual(self.launch().transcript, common.transcript(self.work, SID))
        self.assertEqual(
            self.launch(access=Access(cwd=cwd)).transcript, common.transcript(cwd, SID)
        )

    def test_the_launch_transcript_follows_the_runs_config_dir(self):
        given = {**CONFIG, "env": {"CLAUDE_CONFIG_DIR": "/given"}}
        for launch in (self.launch, partial(tui_launch, self.work)):
            for fields, root in (
                (
                    {"access": Access(cwd=self.work, env={"CLAUDE_CONFIG_DIR": "/access"})},
                    "/access",
                ),
                ({"env": {"CLAUDE_CONFIG_DIR": "/policy"}}, "/policy"),
                (
                    {
                        "access": Access(cwd=self.work, env={"CLAUDE_CONFIG_DIR": "/access"}),
                        "env": {"CLAUDE_CONFIG_DIR": "/policy"},
                    },
                    "/policy",
                ),
            ):
                with self.subTest(launch=launch, fields=fields):
                    made = launch(config=given, **fields)
                    self.assertEqual(made.env["CLAUDE_CONFIG_DIR"], root)
                    project = os.path.basename(os.path.dirname(made.transcript))
                    self.assertEqual(
                        made.transcript, os.path.join(root, "projects", project, f"{SID}.jsonl")
                    )


SECRETS = {
    "CFG_TOKEN": "sk-fake-config",
    "ACCESS_TOKEN": "sk-fake-access",
    "SPEC_TOKEN": "sk-fake-spec",
}


class ResumeTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.tmp, "cwd d")
        os.mkdir(self.cwd)
        git_env = dict.fromkeys(LOCAL_ENV_VARS, "/deployment")
        self.client = ClaudeClient({"env": {**git_env, "CFG_TOKEN": SECRETS["CFG_TOKEN"]}})
        self.spec = Spec(
            workdir=self.work,
            sid=SID,
            prompt="hi",
            access=Access(
                cwd=self.cwd,
                dirs=("/o x", "/p"),
                env={**git_env, "ACCESS_TOKEN": SECRETS["ACCESS_TOKEN"], "K": "access"},
            ),
            env={"SPEC_TOKEN": SECRETS["SPEC_TOKEN"], "K": "spec"},
        )

    def committed(self, mode="headless", **fields):
        spec = dataclasses.replace(self.spec, **fields)
        run = getattr(self.client, mode)()
        launch = run.launch(spec) if mode == "headless" else run.launch(spec, HOOK)
        run.commit()
        return launch

    def resumed(self, argv):
        """resume_main(argv)'s exit code, stderr, and its chdir and execvpe calls."""
        with (
            mock.patch("os.chdir") as chdir,
            mock.patch("os.execvpe") as execute,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = self.client.resume_main(argv)
        return code, err.getvalue(), chdir.call_args_list, execute.call_args_list

    def test_the_command_names_only_the_state(self):
        self.assertEqual(ClaudeCommon({}).resume("/w d", SID), reference("/w d"))

    def test_client_resume_defaults_agent_view_off(self):
        self.client.common.save(self.spec)
        code, _, _, execute = self.resumed([store(), self.cwd, SID])
        self.assertEqual(code, 0)
        self.assertEqual(execute[0].args[2]["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "1")

    def test_client_resume_keeps_saved_agent_view_override(self):
        spec = dataclasses.replace(self.spec, env={"CLAUDE_CODE_DISABLE_AGENT_VIEW": "0"})
        self.client.common.save(spec)
        code, _, _, execute = self.resumed([store(), self.cwd, SID])
        self.assertEqual(code, 0)
        self.assertEqual(execute[0].args[2]["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "0")

    def test_no_env_value_reaches_a_resume_command_or_status(self):
        texts = []
        for mode in ("headless", "tui"):
            for resume in (False, True):
                launch = self.committed(mode, resume=resume)
                texts += [launch.resume, "\0".join(launch.argv)]
                self.assertEqual(launch.resume, reference(self.cwd))
        env = {**self.spec.access.env, **self.spec.env}
        status = self.client.session_status(
            SessionInfo(sid=SID, cwd=self.cwd, project=False, transcript="", resume=""), env=env
        )
        self.assertEqual(status.resume_command, reference(self.cwd))
        for text in (*texts, status.resume_command):
            for secret in SECRETS.values():
                self.assertNotIn(secret, text)

    def test_the_resume_replays_the_committed_launch_in_its_cwd(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                words = shlex.split(self.committed(mode).resume)
                self.assertEqual(
                    words[:6], ["cd", self.cwd, "&&", "ensemble-session", "client-resume", "claude"]
                )
                code, err, chdir, execute = self.resumed(words[6:])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(chdir, [mock.call(self.cwd)])
                self.assertEqual(
                    execute,
                    [
                        mock.call(
                            "claude",
                            [
                                *("claude", "--resume", SID, "--setting-sources", "user"),
                                *("--add-dir", "/o x", "--add-dir", "/p"),
                            ],
                            {**os.environ, **TUI_DEFAULTS, **SECRETS, "K": "spec"},
                        )
                    ],
                )

    def test_client_resume_uses_the_last_existing_continuation(self):
        self.committed()
        common = ClaudeCommon({})
        path = common.transcript(self.cwd, SID)
        target = os.path.join(self.config_dir, "projects", "other", f"{NEXT}.jsonl")
        for each in (path, target):
            os.makedirs(os.path.dirname(each), exist_ok=True)
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"type": "continued-in", "sessionId": SID, "continuedInSessionId": NEXT}, out)
        with open(target, "w", encoding="utf-8"):
            pass
        code, err, _, execute = self.resumed([store(), self.cwd, SID])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(execute[0].args[1][:3], ["claude", "--resume", NEXT])

    def test_the_resume_replays_the_configs_flags_with_one_settings(self):
        with open(os.path.join(self.cwd, "s.json"), "w", encoding="utf-8") as f:
            json.dump({"model": "x"}, f)
        plugin = '{"enabledPlugins": {"ensemble@windroc": false}}'
        flags = ["--permission-mode", "auto", "--settings", plugin, "--settings", "s.json"]
        self.client = ClaudeClient({**self.client.config, "flags": flags})
        words = shlex.split(self.committed().resume)
        _, _, _, execute = self.resumed(words[6:])
        argv = execute[0].args[1]
        self.assertEqual(argv[:5], ["claude", "--resume", SID, "--permission-mode", "auto"])
        self.assertEqual(argv.count("--settings"), 1)
        self.assertEqual(
            json.loads(argv[argv.index("--settings") + 1]),
            {"enabledPlugins": {"ensemble@windroc": False}, "model": "x"},
        )
        self.assertEqual(argv[-4:], ["--add-dir", "/o x", "--add-dir", "/p"])

    def test_the_resume_command_runs_in_auto_mode_here(self):
        self.client = ClaudeClient({"flags": ["--settings", PLUGIN], "modes": MODES})
        words = shlex.split(self.committed().resume)
        _, _, _, execute = self.resumed(words[6:])
        argv = execute[0].args[1]
        self.assertEqual(argv[:5], ["claude", "--resume", SID, "--permission-mode", "auto"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), json.loads(PLUGIN))

    def resumed_argv(self, mode="headless", **fields) -> list[str]:
        words = shlex.split(self.committed(mode, **fields).resume)
        code, err, _, execute = self.resumed(words[6:])
        self.assertEqual((code, err), (0, ""))
        return execute[0].args[1]

    def test_the_resume_replays_the_runs_strict_mcp_and_system_prompt(self):
        flags = ("--strict-mcp-config", "--append-system-prompt=Reply with the outcome alone.")
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                argv = self.resumed_argv(mode, flags=flags)
                self.assertEqual(argv[-2:], list(flags))
                self.assertNotIn("-p", argv)

    def test_the_resume_keeps_the_runs_setting_sources_and_project_mcp(self):
        with open(os.path.join(self.cwd, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        mcp = ("--mcp-config", os.path.join(self.cwd, ".mcp.json"))
        rows = (
            (True, ("--strict-mcp-config",), "user,project,local", True),
            (True, (), "user,project,local", False),
            (False, ("--strict-mcp-config",), "user", False),
        )
        for project, flags, sources, kept in rows:
            with self.subTest(project=project, flags=flags):
                access = dataclasses.replace(self.spec.access, project=project)
                argv = self.resumed_argv(access=access, flags=flags)
                self.assertTrue(holds(argv, ("--setting-sources", sources)))
                self.assertEqual(holds(argv, mcp), kept)

    def test_the_resume_replays_the_runs_model_effort_and_name(self):
        tables = {"tiers": {"1": "fable"}, "efforts": {"high": "high"}}
        self.client = ClaudeClient({**self.client.config, **tables})
        argv = self.resumed_argv(tier=1, effort="high", name="w-1")
        for words in (("--model", "fable"), ("--effort", "high"), ("--name", "w-1")):
            self.assertTrue(holds(argv, words), words)
        self.assertNotIn("--model", self.resumed_argv())

    def test_the_resume_replays_the_runs_allowed_tools_and_the_configs(self):
        self.client = ClaudeClient({**self.client.config, "allow": ["Read"]})
        access = dataclasses.replace(self.spec.access, commands=("git status",), reads=("/r",))
        argv = self.resumed_argv(access=access)
        self.assertEqual(argv[-4:], ["--allowedTools", "Bash(git status)", "Read(//r/**)", "Read"])

    def test_the_resume_merges_the_runs_settings_without_its_hooks(self):
        self.client = ClaudeClient({**self.client.config, "flags": ["--settings", PLUGIN]})
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                argv = self.resumed_argv(mode, flags=("--settings", '{"model": "x"}'))
                self.assertEqual(argv.count("--settings"), 1)
                self.assertEqual(
                    json.loads(argv[argv.index("--settings") + 1]),
                    {**json.loads(PLUGIN), "model": "x"},
                )

    def test_a_hand_edited_state_cannot_set_setting_sources(self):
        self.committed()
        (path,) = (os.path.join(store(), name) for name in os.listdir(store()))
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**state, "flags": ["--setting-sources=user,project,local"]}, f)
        code, err, _, execute = self.resumed([store(), self.cwd, SID])
        self.assertEqual(
            (code, err, execute),
            (
                1,
                "claude resume: --setting-sources is set by the Claude client: use Access.project\n",
                [],
            ),
        )

    def test_a_state_over_the_limit_is_a_config_error(self):
        big = "--append-system-prompt=" + "x" * (1 << 20)
        with self.assertRaisesRegex(ConfigError, "^claude resume state: over 1 MiB$"):
            self.committed(flags=(big,))

    def test_the_state_is_private(self):
        self.committed()
        (name,) = os.listdir(store())
        self.assertEqual(stat.S_IMODE(os.stat(store()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store(), name)).st_mode), 0o600)

    def test_a_store_it_cannot_write_is_a_config_error(self):
        os.makedirs(os.path.dirname(store()), exist_ok=True)
        with open(store(), "w", encoding="utf-8"):
            pass
        with self.assertRaisesRegex(ConfigError, "^claude resume state: "):
            self.committed()

    def test_a_resume_it_cannot_run_prints_one_line_and_runs_nothing(self):
        self.committed()
        (path,) = (os.path.join(store(), name) for name in os.listdir(store()))
        with open(path, encoding="utf-8") as f:
            saved = f.read()
        other = "1f8fad5b-d9cb-469f-a165-70867728950e"
        rows = [
            ([], "expected STORE CWD SID"),
            ([store(), self.cwd, "x"], "claude session ids are UUIDs, got 'x'"),
            (["relative", self.cwd, SID], "the store must be an absolute path"),
            ([store(), self.cwd, other], f"no resume state for session {other}"),
            ([store(), self.work, SID], f"no resume state for session {SID}"),
        ]
        for argv, message in rows:
            with self.subTest(argv=argv):
                code, err, chdir, execute = self.resumed(argv)
                self.assertEqual((code, err), (1, f"claude resume: {message}\n"))
                self.assertEqual((chdir, execute), ([], []))
        os.remove(path)
        os.mkfifo(path)
        self.assertEqual(
            self.resumed([store(), self.cwd, SID])[:2],
            (1, f"claude resume: {path}: not a regular file\n"),
        )
        os.remove(path)
        for data in (
            "not json",
            "{}",
            json.dumps({"version": 1, "cwd": self.cwd, "sid": SID, "dirs": [], "env": {}}),
            *(
                json.dumps({**json.loads(saved), **bad})
                for bad in (
                    {"version": 3},
                    {"flags": "--strict-mcp-config"},
                    {"project": "yes"},
                    {"commands": [1]},
                    {"reads": None},
                    {"model": 1},
                    {"name": ["w"]},
                )
            ),
        ):
            with self.subTest(data=data):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
                self.assertEqual(
                    self.resumed([store(), self.cwd, SID])[:2],
                    (1, f"claude resume: {path}: bad resume state\n"),
                )

    def aged(self, path: str, days: float) -> float:
        when = time.time() - days * 86400
        os.utime(path, (when, when))
        return when

    def test_a_save_drops_state_unused_for_30_days(self):
        os.makedirs(store())
        for name, days in (("old.json", 31), ("young.json", 29)):
            with open(os.path.join(store(), name), "w", encoding="utf-8"):
                pass
            self.aged(os.path.join(store(), name), days)
        self.committed()
        left = sorted(os.listdir(store()))
        self.assertNotIn("old.json", left)
        self.assertEqual(len(left), 2)
        self.assertIn("young.json", left)

    def test_a_cleanup_error_is_one_line_and_the_state_is_saved(self):
        os.makedirs(store())
        old = os.path.join(store(), "old.json")
        with open(old, "w", encoding="utf-8"):
            pass
        self.aged(old, 31)
        refused = OSError(errno.EPERM, "refused", old)
        with (
            mock.patch("os.unlink", side_effect=refused),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.committed()
        self.assertEqual(err.getvalue(), f"claude resume: {refused}\n")
        self.assertEqual(len(os.listdir(store())), 2)

    def test_a_resume_renews_its_state_so_the_next_save_keeps_it(self):
        self.committed()
        (path,) = (os.path.join(store(), name) for name in os.listdir(store()))
        old = self.aged(path, 29)
        self.assertEqual(self.resumed([store(), self.cwd, SID])[:2], (0, ""))
        self.assertGreater(os.stat(path).st_mtime, old + 28 * 86400)
        os.utime(path, (old + 3 * 86400,) * 2)
        self.committed(sid="1f8fad5b-d9cb-469f-a165-70867728950e")
        self.assertIn(os.path.basename(path), os.listdir(store()))

    def test_a_bad_state_is_not_renewed(self):
        self.committed()
        (path,) = (os.path.join(store(), name) for name in os.listdir(store()))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        old = self.aged(path, 29)
        self.assertEqual(self.resumed([store(), self.cwd, SID])[0], 1)
        self.assertEqual(os.stat(path).st_mtime, old)

    def test_a_renew_error_still_resumes(self):
        self.committed()
        (path,) = (os.path.join(store(), name) for name in os.listdir(store()))
        refused = OSError(errno.EPERM, "refused")
        with mock.patch("os.utime", side_effect=refused):
            code, err, _, execute = self.resumed([store(), self.cwd, SID])
        self.assertEqual((code, err), (0, f"claude resume: cannot renew {path}: {refused}\n"))
        self.assertEqual(len(execute), 1)


class SessionStatusTest(Base):
    def setUp(self):
        super().setUp()
        self.common = ClaudeCommon({})
        self.path = self.common.transcript(self.work, SID)
        self.resume = reference(self.work)

    def write(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.utime(path, (mtime, mtime))

    def status(self, sid=SID) -> SessionStatus:
        return self.common.session_status(sid, self.work)

    def test_a_transcript_is_resumable_with_its_mtime(self):
        self.write(self.path, 1000)
        self.assertEqual(self.status(), SessionStatus("yes", 1000.0, self.resume))

    def test_the_runs_env_config_dir_holds_its_transcript(self):
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "account"), "S": "s"}
        path = self.common.transcript(self.work, SID, env)
        self.assertTrue(path.startswith(env["CLAUDE_CONFIG_DIR"] + os.sep))
        self.write(path, 3000)
        for recorded in ("", path):
            with self.subTest(recorded=recorded):
                self.assertEqual(
                    self.common.session_status(SID, self.work, transcript=recorded, env=env),
                    SessionStatus("yes", 3000.0, self.resume),
                )
        self.assertEqual(self.status(), SessionStatus("no", None, self.resume))

    def test_recorded_transcripts_cannot_escape_the_cwd_store(self):
        self.write(self.path, 1000)
        outside = os.path.join(self.tmp, "outside.jsonl")
        self.write(outside, 9000)
        inside = os.path.join(os.path.dirname(self.path), "other.jsonl")
        self.write(inside, 8000)
        link = os.path.join(os.path.dirname(self.path), "link.jsonl")
        os.symlink(inside, link)
        directory = os.path.join(os.path.dirname(self.path), "directory")
        os.mkdir(directory)
        fifo = os.path.join(os.path.dirname(self.path), "fifo")
        os.mkfifo(fifo)
        parent_link = os.path.join(os.path.dirname(self.path), "outside-link")
        os.symlink(self.tmp, parent_link)
        for path in (
            outside,
            directory,
            fifo,
            os.path.join(parent_link, "outside.jsonl"),
            "/abcdef",
            "relative.jsonl",
            "\0",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.common.session_status(SID, self.work, transcript=path),
                    SessionStatus("yes", 1000.0, self.resume),
                )
        self.assertEqual(
            self.common.session_status(SID, self.work, transcript=link),
            SessionStatus("yes", 8000.0, self.resume),
        )

    def test_a_recorded_filename_without_a_suffix_keeps_the_scan_inside_the_store(self):
        path = os.path.join(os.path.dirname(self.path), "a")
        self.write(path, 2000)
        self.assertEqual(
            self.common.session_status(SID, self.work, transcript=path),
            SessionStatus("yes", 2000.0, self.resume),
        )

    def test_a_symlink_session_directory_cannot_expand_the_activity_scan(self):
        self.write(self.path, 1000)
        outside = os.path.join(self.tmp, "outside")
        self.write(os.path.join(outside, "child.jsonl"), 9000)
        os.symlink(outside, os.path.splitext(self.path)[0])
        self.assertEqual(self.status(), SessionStatus("yes", 1000.0, self.resume))

    def test_the_newest_file_under_the_session_dir_wins(self):
        self.write(self.path, 1000)
        sub = self.path[: -len(".jsonl")]
        self.write(os.path.join(sub, "subagents", "old.jsonl"), 500)
        self.assertEqual(self.status().last_active, 1000.0)
        self.write(os.path.join(sub, "subagents", "workflows", "r", "journal.jsonl"), 2000)
        self.write(os.path.join(sub, "subagents", "mid.jsonl"), 1500)
        self.assertEqual(self.status(), SessionStatus("yes", 2000.0, self.resume))

    def test_a_missing_transcript_is_not_resumable(self):
        self.assertEqual(self.status(), SessionStatus("no", None, self.resume))
        os.makedirs(os.path.dirname(os.path.dirname(self.path)))
        with open(os.path.dirname(self.path), "w", encoding="utf-8"):
            pass
        self.assertEqual(self.status().resumable, "no")

    @unittest.skipIf(os.geteuid() == 0, "root reads a 000 dir")
    def test_an_unreadable_project_dir_is_unknown(self):
        self.write(self.path, 1000)
        project = os.path.dirname(self.path)
        os.chmod(project, 0)
        self.addCleanup(os.chmod, project, 0o700)
        self.assertEqual(self.status(), SessionStatus("unknown", None, self.resume))

    def test_a_transcript_that_is_no_regular_file_is_unknown(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.assertEqual(self.status().resumable, "unknown")

    def test_a_sid_that_is_no_uuid_is_not_resumable(self):
        for sid in ("abc", SID.upper(), "../" + SID):
            with self.subTest(sid=sid):
                self.assertEqual(self.status(sid), SessionStatus("no", None, ""))


NEXT = "1f8fad5b-d9cb-469f-a165-70867728950e"
LAST = "2f8fad5b-d9cb-469f-a165-70867728950e"


class ContinuationTest(Base):
    def setUp(self):
        super().setUp()
        self.common = ClaudeCommon({})
        self.path = self.common.transcript(self.work, SID)
        self.target = os.path.join(self.config_dir, "projects", "elsewhere", f"{NEXT}.jsonl")

    def write(self, path, *records):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as out:
            out.write("".join(json.dumps(r) + "\n" for r in records))

    def marker(self, source=SID, target=NEXT, timestamp="2026-10-15T12:00:00Z"):
        return {
            "type": "continued-in",
            "sessionId": source,
            "continuedInSessionId": target,
            "timestamp": timestamp,
        }

    def test_walk_keeps_a_missing_target(self):
        self.write(self.path, self.marker())
        self.assertEqual(
            self.common.walk(self.path, SID, self.config_dir),
            [(SID, self.path, None), (NEXT, None, "2026-10-15T12:00:00Z")],
        )

    def test_walk_crosses_projects_then_stops_at_a_cycle(self):
        last = os.path.join(self.config_dir, "projects", "third", f"{LAST}.jsonl")
        self.write(self.path, self.marker())
        self.write(self.target, self.marker(NEXT, LAST))
        self.write(last, self.marker(LAST, SID))
        self.assertEqual(
            [hop[:2] for hop in self.common.walk(self.path, SID, self.config_dir)],
            [(SID, self.path), (NEXT, self.target), (LAST, last)],
        )

    def test_walk_uses_the_last_valid_matching_marker(self):
        self.write(
            self.path,
            self.marker(target=LAST),
            self.marker(),
            self.marker(source=LAST, target=LAST),
            self.marker(target="../bad"),
            {"type": "assistant"},
            [1],
        )
        self.write(self.target)
        self.assertEqual(
            self.common.walk(self.path, SID, self.config_dir)[-1][:2], (NEXT, self.target)
        )

    def test_a_linked_target_under_a_docker_home_is_absent(self):
        config_dir = docker_home("w", "claude")
        path = os.path.join(config_dir, "projects", "p", f"{SID}.jsonl")
        self.write(path, self.marker())
        inside = os.path.join(config_dir, "projects", "p", "real.jsonl")
        outside = os.path.join(self.work, "host.jsonl")
        self.write(inside, self.marker(NEXT, LAST))
        self.write(outside, self.marker(NEXT, LAST))
        target = os.path.join(config_dir, "projects", "elsewhere", f"{NEXT}.jsonl")
        os.makedirs(os.path.dirname(target))
        for real in (inside, outside):
            with self.subTest(real=real):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(target)
                os.symlink(real, target)
                self.assertEqual(
                    self.common.walk(path, SID, config_dir),
                    [(SID, path, None), (NEXT, None, "2026-10-15T12:00:00Z")],
                )

    def test_resume_launch_uses_the_latest_file_in_both_modes(self):
        self.write(self.path, self.marker())
        self.write(self.target)
        spec = Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID, resume=True)
        client = ClaudeClient({})
        for launch in (client.headless().launch(spec), client.tui().launch(spec, HOOK)):
            self.assertEqual(launch.argv[launch.argv.index("--resume") + 1], NEXT)
            self.assertEqual(launch.transcript, self.target)
            self.assertEqual(launch.resume, reference(self.work))

    def test_a_resume_under_another_workdir_finds_the_conversation_by_cwd_and_sid(self):
        self.write(self.path, self.marker())
        self.write(self.target)
        worker = os.path.join(self.work, "workers", SID)
        spec = Spec(workdir=worker, access=Access(cwd=self.work), sid=SID, resume=True)
        client = ClaudeClient({})
        for launch in (client.headless().launch(spec), client.tui().launch(spec, HOOK)):
            self.assertEqual(launch.argv[launch.argv.index("--resume") + 1], NEXT)
            self.assertEqual(launch.transcript, self.target)

    def test_session_status_uses_the_latest_file_only(self):
        self.write(self.path, self.marker())
        self.write(self.target)
        os.utime(self.path, (9000, 9000))
        os.utime(self.target, (1000, 1000))
        for recorded in ("", self.target):
            self.assertEqual(
                self.common.session_status(SID, self.work, transcript=recorded),
                SessionStatus("yes", 1000.0, reference(self.work)),
            )
