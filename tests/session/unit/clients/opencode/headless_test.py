"""OpenCode's headless launches: the launcher's argv, the run's flags and its OpenCode config."""

import json
import os
import re
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from ensemble.session.clients.opencode import OpenCodeClient, launcher
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    Activity,
    ClientBound,
    ClientContext,
    ConfigError,
    Effort,
    Launch,
    Permission,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import (
    DOCKER_CONTEXT,
    IMAGE_CLIENT,
    SES,
    fake_run,
    packaged_table,
    titled_session,
)
from tests.session.unit.clients.support import holds

MODEL = "ollama/qwen3.6:35b-mlx"
STRATA = {"strata": {"base_url": "http://10.0.0.5:8080/v1", "models": ["m"]}}
LIMIT = {"context": 65536, "input": 32768, "output": 16384}  # the default window's


def decide(rules: dict, path: str) -> str | None:
    """OpenCode's verdict for an edit of `path` (relative to its worktree): its last matching rule,
    a rule's `*` any text (permission/index.ts evaluate, util/wildcard.ts match at v1.19.3).
    """
    found = None
    for pattern, action in rules.items():
        if re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), path, re.S):
            found = action
    return found


class Base(Case):
    def setUp(self):
        self.cwd = os.path.realpath(os.path.join(self.home, "project"))
        os.mkdir(self.cwd)
        self.client = OpenCodeClient(packaged_table())
        self.enterContext(mock.patch("subprocess.run", side_effect=fake_run))

    def spec(self, **fields) -> Spec:
        access = fields.pop("access", Access(project=True, cwd=self.cwd))
        return Spec(workdir=self.home, access=access, sid="run-1", prompt="Hi there", **fields)

    def launch(self, spec: Spec, context: ClientContext = LOCAL_CONTEXT, client=None) -> Launch:
        run = (client or self.client).headless()
        assert run is not None
        run._context = context
        return run.launch(spec)

    def made(self, name: str) -> str:
        path = os.path.join(self.home, name)
        os.mkdir(path)
        return os.path.realpath(path)

    def command(self, launch: Launch) -> tuple[str, ...]:
        return launch.argv[launch.argv.index("--") + 1 :]

    def inline(self, launch: Launch) -> dict:
        return json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])


class LaunchTest(Base):
    def test_the_launcher_runs_opencode_with_the_prompt_and_timeout(self):
        launch = self.launch(self.spec())
        self.assertEqual(
            launch.argv[: launch.argv.index("--") + 1],
            (
                os.path.realpath(sys.executable),
                "-I",
                launcher.__file__,
                "--timeout",
                "120",
                "--prompt",
                "Hi there",
                "--",
            ),
        )
        self.assertEqual(self.command(launch)[:4], ("opencode", "run", "--format", "json"))
        self.assertNotIn("Hi there", self.command(launch))
        self.assertEqual((launch.cwd, launch.project, launch.resume), (self.cwd, True, ""))
        self.assertIn("OPENCODE_PERMISSION", launch.unset)
        self.assertIn("OPENCODE_PID", launch.unset)

    def test_tier_model(self):
        self.assertTrue(holds(self.command(self.launch(self.spec(tier=3))), ("--model", MODEL)))

    def test_missing_tier(self):
        client = OpenCodeClient({**packaged_table(), "tiers": {"1": MODEL}})
        with self.assertRaisesRegex(ConfigError, "no model for tier 3"):
            self.launch(self.spec(tier=3), client=client)

    def test_no_tier_runs_the_configured_model(self):
        self.assertTrue(holds(self.command(self.launch(self.spec())), ("--model", MODEL)))
        client = OpenCodeClient({**packaged_table(), "providers": STRATA, "model": "strata/d"})
        launch = self.launch(self.spec(), client=client)
        self.assertTrue(holds(self.command(launch), ("--model", "strata/d")))
        self.assertEqual(list(self.inline(launch)["provider"]["strata"]["models"]), ["m", "d"])
        tiers = {"1": "ollama/a", "2": "ollama/a", "3": "ollama/a", "4": "ollama/a"}
        client = OpenCodeClient({**packaged_table(), "tiers": tiers, "model": "ollama/b"})
        ollama = self.inline(self.launch(self.spec(), client=client))["provider"]["ollama"]
        self.assertEqual(
            {name: model["name"] for name, model in ollama["models"].items()}, {"a": "a", "b": "b"}
        )

    def test_a_runs_model_flag_is_refused(self):
        for flags in (("-m", "ollama/b"), ("--model", "opencode/big-pickle")):
            with self.subTest(flags=flags), self.assertRaisesRegex(ConfigError, "opencode client"):
                self.launch(self.spec(flags=flags))

    def test_variant(self):
        command = self.command(self.launch(self.spec(effort=Effort.HIGH)))
        self.assertTrue(holds(command, ("--variant", "high")))

    def test_no_effort(self):
        self.assertNotIn("--variant", self.command(self.launch(self.spec())))

    def test_missing_effort(self):
        client = OpenCodeClient({**packaged_table(), "efforts": {"low": "low"}})
        with self.assertRaisesRegex(ConfigError, "no variant for effort 'high'"):
            self.launch(self.spec(effort=Effort.HIGH), client=client)

    def test_ollama_url_local(self):
        provider = self.inline(self.launch(self.spec()))["provider"]["ollama"]
        self.assertEqual(
            provider,
            {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                "models": {
                    "qwen3.6:35b-mlx": {
                        "name": "qwen3.6:35b-mlx",
                        "limit": LIMIT,
                    }
                },
            },
        )

    def test_ollama_url_docker_address(self):
        context = ClientContext("host.docker.internal", Permission.YOLO)
        provider = self.inline(self.launch(self.spec(), context))["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://host.docker.internal:11434/v1")

    def test_port(self):
        client = OpenCodeClient({**packaged_table(), "ollama_port": 11500})
        provider = self.inline(self.launch(self.spec(), client=client))["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11500/v1")

    def test_only_ollama_tiers_are_listed(self):
        tiers = {"1": "ollama/a", "2": "ollama/a", "3": "strata/b", "4": "ollama/c"}
        config = {"tiers": tiers, "providers": STRATA, "model": "ollama/c"}
        client = OpenCodeClient({**packaged_table(), **config})
        models = self.inline(self.launch(self.spec(), client=client))["provider"]["ollama"]
        self.assertEqual(list(models["models"]), ["a", "c"])

    def test_only_ollama_is_enabled_by_default(self):
        inline = self.inline(self.launch(self.spec()))
        self.assertEqual(inline["enabled_providers"], ["ollama"])
        self.assertEqual(list(inline["provider"]), ["ollama"])

    def test_a_configured_provider_is_enabled_with_its_and_its_tiers_models(self):
        tiers = {"1": "ollama/a", "2": "strata/b", "3": "strata/m", "4": "strata/b"}
        providers = {**STRATA, "lab": {"base_url": "https://lab.lan/v1", "models": []}}
        config = {"tiers": tiers, "providers": providers, "model": "ollama/a"}
        client = OpenCodeClient({**packaged_table(), **config})
        launch = self.launch(self.spec(tier=2), client=client)
        inline = self.inline(launch)
        self.assertEqual(inline["enabled_providers"], ["ollama", "lab", "strata"])
        self.assertEqual(
            inline["provider"]["strata"],
            {
                "npm": "@ai-sdk/openai-compatible",
                "name": "strata",
                "options": {"baseURL": "http://10.0.0.5:8080/v1"},
                "models": {
                    "m": {"name": "m", "limit": LIMIT},
                    "b": {"name": "b", "limit": LIMIT},
                },
            },
        )
        self.assertEqual(inline["provider"]["lab"]["models"], {})
        self.assertTrue(holds(self.command(launch), ("--model", "strata/b")))
        self.assertNotIn("apiKey", json.dumps(inline))

    def test_a_container_reads_each_providers_key_from_its_secret_file(self):
        providers = {**STRATA, "lab": {"base_url": "https://lab.lan/v1", "models": []}}
        client = OpenCodeClient({**packaged_table(), "providers": providers})
        inline = self.inline(self.launch(self.spec(), DOCKER_CONTEXT, client=client))
        for name in ("strata", "lab"):
            self.assertEqual(
                inline["provider"][name]["options"]["apiKey"],
                f"{{file:/run/ensemble/secrets/opencode/{name}.key}}",
            )
        self.assertNotIn("apiKey", inline["provider"]["ollama"]["options"])

    def test_auto_has_no_auto_flag(self):
        self.assertNotIn("--auto", self.command(self.launch(self.spec())))

    def test_yolo_has_auto(self):
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        self.assertIn("--auto", self.command(self.launch(self.spec(), context)))

    def test_bash_ask(self):
        self.assertEqual(self.inline(self.launch(self.spec()))["permission"]["bash"], "ask")

    def test_yolo_approves_tools_and_retains_edit_boundaries(self):
        spec = self.spec(access=Access(cwd=self.cwd, project=True, reads=(self.made("read"),)))
        permission = self.inline(self.launch(spec, DOCKER_CONTEXT))["permission"]
        for tool in ("bash", "read", "external_directory", "doom_loop"):
            self.assertEqual(permission[tool], "allow", tool)
        self.assertNotIn("*", permission)
        self.assertEqual(permission["edit"], self.inline(self.launch(spec))["permission"]["edit"])

    def test_a_refused_call_does_not_end_the_turn(self):
        inline = self.inline(self.launch(self.spec()))
        self.assertEqual(inline["experimental"], {"continue_loop_on_deny": True})

    def test_commands_allowed(self):
        access = Access(project=True, cwd=self.cwd, commands=("git status", "make test"))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        self.assertEqual(
            permission["bash"], {"*": "ask", "git status": "allow", "make test": "allow"}
        )

    def test_reads_and_dirs_are_external_directories(self):
        extra, read = self.made("extra"), self.made("read")
        access = Access(project=True, cwd=self.cwd, dirs=(extra,), reads=(read,))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        self.assertEqual(
            permission["external_directory"],
            {read: "allow", f"{read}/*": "allow", extra: "allow", f"{extra}/*": "allow"},
        )
        self.assertNotIn("external_directory", self.inline(self.launch(self.spec()))["permission"])

    def test_installed_plugin_task_read_through_links_is_preapproved(self):
        cache = self.made("ensemble@git+https:")
        tasks = os.path.join(cache, "src", "ensemble", "delegate", "team", "tasks")
        os.makedirs(tasks)
        task = os.path.join(tasks, "light-build.md")
        with open(task, "w", encoding="utf-8") as stream:
            stream.write("Build the requirement.")
        links = os.path.join(self.home, ".ensemble", "links")
        os.makedirs(links)
        link = os.path.join(links, "opencode-installed")
        os.symlink(cache, link)
        granted = os.path.join(link, "src", "ensemble", "delegate", "team", "tasks")
        access = Access(project=True, cwd=self.cwd, reads=(granted,))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        for directory in (granted, tasks):
            with self.subTest(directory=directory):
                # read.ts / external-directory.ts v1.19.3 asks for the lexical parent glob.
                self.assertEqual(
                    decide(permission["external_directory"], f"{directory}/*") or "ask",
                    "allow",
                )
                with open(os.path.join(directory, "light-build.md"), encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), "Build the requirement.")
        for directory in (links, cache, os.path.join(tasks, "../roles")):
            with self.subTest(ungranted=directory):
                self.assertEqual(
                    decide(permission["external_directory"], f"{os.path.normpath(directory)}/*")
                    or "ask",
                    "ask",
                )

    def test_symlink_grants_preserve_read_only_and_canonical_write_boundaries(self):
        writable = self.made("writable")
        read = os.path.join(writable, "tasks")
        os.mkdir(read)
        link = os.path.join(self.cwd, "tasks")
        os.symlink(read, link)
        extra = self.made("extra")
        alias = os.path.join(self.home, "extra-link")
        os.symlink(extra, alias)
        access = Access(project=True, cwd=self.cwd, dirs=(writable, alias), reads=(link,))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        self.assert_edits(
            permission["edit"],
            "/",
            {
                os.path.join(link, "light-build.md"): "deny",
                os.path.join(read, "light-build.md"): "deny",
                os.path.join(alias, "output.md"): "deny",
                os.path.join(extra, "output.md"): "allow",
                os.path.join(writable, "output.md"): "allow",
                os.path.join(alias, ".git", "config"): "deny",
            },
        )
        for directory in (alias, extra):
            self.assertEqual(decide(permission["external_directory"], f"{directory}/*"), "allow")

    def test_a_wildcard_in_a_symlink_grant_is_refused(self):
        target = self.made("target")
        for name in ("a*b", "a?b"):
            link = os.path.join(self.home, name)
            os.symlink(target, link)
            for field in ("dirs", "reads"):
                access = Access(project=True, cwd=self.cwd, **{field: (link,)})
                with (
                    self.subTest(name=name, field=field),
                    self.assertRaisesRegex(ConfigError, "wildcard"),
                ):
                    self.launch(self.spec(access=access))

    def test_descendant_grants_keep_writes_on_canonical_paths(self):
        parent = self.made("parent")
        child = os.path.join(parent, "child")
        os.mkdir(child)
        alias = os.path.join(self.home, "parent-link")
        os.symlink(parent, alias)
        for dirs, reads, parent_action, child_action in (
            ((alias,), (child,), "allow", "deny"),
            ((child,), (alias,), "deny", "allow"),
        ):
            with self.subTest(dirs=dirs, reads=reads):
                rules = self.rules_for(Access(project=True, cwd=self.cwd, dirs=dirs, reads=reads))
                self.assert_edits(
                    rules,
                    "/",
                    {
                        os.path.join(parent, "output.md"): parent_action,
                        os.path.join(alias, "output.md"): "deny",
                        os.path.join(child, "light-build.md"): child_action,
                        os.path.join(alias, "child", "light-build.md"): "deny",
                    },
                )

    def test_a_nested_read_symlink_does_not_add_writable_alias_paths(self):
        parent = self.made("parent")
        readonly = self.made("readonly")
        child = os.path.join(parent, "tasks")
        os.symlink(readonly, child)
        alias = os.path.join(self.home, "parent-link")
        os.symlink(parent, alias)
        rules = self.rules_for(Access(project=True, cwd=self.cwd, dirs=(alias,), reads=(child,)))
        self.assert_edits(
            rules,
            "/",
            {
                os.path.join(alias, "tasks", "light-build.md"): "deny",
                os.path.join(parent, "output.md"): "allow",
                os.path.join(alias, "output.md"): "deny",
                os.path.join(child, "light-build.md"): "deny",
                os.path.join(readonly, "light-build.md"): "deny",
            },
        )

    def rules_for(self, access: Access) -> dict:
        return self.inline(self.launch(self.spec(access=access)))["permission"]["edit"]

    def assert_edits(self, rules: dict, worktree: str, expected: dict) -> None:
        for path, action in expected.items():
            found = decide(rules, os.path.relpath(path, worktree)) or "allow"
            self.assertEqual(found, action, path)

    def test_edits_outside_a_checkout_are_the_cwd_and_dirs_alone(self):
        extra, read = self.made("extra"), self.made("read")
        rules = self.rules_for(Access(project=True, cwd=self.cwd, dirs=(extra,), reads=(read,)))
        skills = os.path.join(self.home, ".claude", "skills", "x", "SKILL.md")
        self.assert_edits(
            rules,
            "/",
            {
                os.path.join(self.cwd, "a.py"): "allow",
                os.path.join(extra, "sub", "b.md"): "allow",
                os.path.join(read, "task.md"): "deny",
                skills: "deny",
                os.path.join(self.home, "elsewhere.py"): "deny",
                os.path.join(self.cwd, ".opencode", "plugins", "x.ts"): "deny",
                os.path.join(self.cwd, "pkg", "opencode.jsonc"): "deny",
            },
        )

    def test_edits_in_a_checkout_are_relative_to_its_top(self):
        agent = self.made("agent")
        repo = os.path.join(agent, "src", "repo")
        os.makedirs(os.path.join(repo, ".git"))
        cwd = os.path.join(repo, "sub")
        os.mkdir(cwd)
        team = os.path.join(agent, "team")
        os.mkdir(team)
        other = self.made("other")
        rules = self.rules_for(Access(project=True, cwd=cwd, dirs=(agent, other), reads=(team,)))
        self.assert_edits(
            rules,
            os.path.realpath(repo),
            {
                os.path.join(other, "o.md"): "allow",
                os.path.join(self.home, "unrelated", "u.md"): "deny",
                os.path.join(cwd, "a.py"): "allow",
                os.path.join(repo, "top.py"): "allow",
                os.path.join(agent, "tmp", "x.md"): "allow",
                os.path.join(team, "task.md"): "deny",
                os.path.join(self.home, "repo", "x"): "deny",
                os.path.join(cwd, ".git", "hooks", "pre-commit"): "deny",
                os.path.join(repo, ".git", "config"): "deny",
                os.path.join(cwd, "opencode.json"): "deny",
            },
        )

    def test_a_cwd_inside_a_checkout_edits_only_itself_there(self):
        repo = self.made("repo")
        os.mkdir(os.path.join(repo, ".git"))
        cwd = os.path.join(repo, "sub")
        os.mkdir(cwd)
        rules = self.rules_for(Access(project=True, cwd=cwd))
        self.assert_edits(
            rules,
            repo,
            {
                os.path.join(cwd, "a.py"): "allow",
                os.path.join(repo, "top.py"): "deny",
                os.path.join(self.home, "x.py"): "deny",
            },
        )

    def test_a_cwd_at_the_checkout_top_edits_all_of_it(self):
        repo = self.made("repo")
        os.mkdir(os.path.join(repo, ".git"))
        rules = self.rules_for(Access(project=True, cwd=repo))
        self.assert_edits(
            rules,
            repo,
            {os.path.join(repo, "src", "a.py"): "allow", os.path.join(repo, ".git", "x"): "deny"},
        )

    def test_git_in_any_case_is_never_edited(self):
        repo = self.made("repo")
        os.mkdir(os.path.join(repo, ".git"))
        rules = self.rules_for(Access(project=True, cwd=repo))
        for path in (".GIT/hooks/pre-commit", ".Git/config", "sub/.gIt/x", ".git"):
            with self.subTest(path=path):
                self.assertEqual(decide(rules, path), "deny")

    def test_a_repo_git_cannot_read_is_under_the_root_worktree(self):
        repo = self.made("repo")
        os.mkdir(os.path.join(repo, ".git"))
        read = self.made("read")
        failed = SimpleNamespace(returncode=128, stdout="", stderr="fatal: dubious ownership")

        def run(argv, *_, **__):
            return fake_run(argv) if argv[1:] == ["--version"] else failed

        with mock.patch("subprocess.run", side_effect=run):
            rules = self.rules_for(Access(project=True, cwd=repo, reads=(read,)))
        self.assert_edits(
            rules,
            "/",
            {os.path.join(repo, "a.py"): "allow", os.path.join(read, "task.md"): "deny"},
        )

    def test_a_wildcard_in_a_granted_path_is_refused(self):
        odd = self.made("a*b")
        for access in (
            Access(project=True, cwd=self.cwd, dirs=(odd,)),
            Access(project=True, cwd=self.cwd, reads=(odd,)),
            Access(project=True, cwd=odd),
        ):
            with self.subTest(access=access), self.assertRaisesRegex(ConfigError, "wildcard"):
                self.launch(self.spec(access=access))

    def test_autoupdate_off(self):
        client = OpenCodeClient({**packaged_table(), "env": {"OPENCODE_DISABLE_AUTOUPDATE": "0"}})
        spec = self.spec(env={"OPENCODE_DISABLE_AUTOUPDATE": "0"})
        launch = self.launch(spec, client=client)
        self.assertEqual(launch.env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        self.assertIn('"autoupdate": false', launch.env["OPENCODE_CONFIG_CONTENT"])

    def test_env_layers(self):
        client = OpenCodeClient({**packaged_table(), "env": {"A": "config", "B": "config"}})
        access = Access(project=True, cwd=self.cwd, env={"B": "access", "C": "access"})
        launch = self.launch(self.spec(access=access, env={"C": "spec"}), client=client)
        self.assertEqual(
            {k: launch.env[k] for k in "ABC"}, {"A": "config", "B": "access", "C": "spec"}
        )

    def test_the_executable_is_argv0(self):
        client = OpenCodeClient({**packaged_table(), "executable": "/opt/oc1/bin/opencode"})
        self.assertEqual(
            self.command(self.launch(self.spec(), client=client))[:2],
            ("/opt/oc1/bin/opencode", "run"),
        )

    def test_a_container_launch_runs_the_images_launcher_and_opencode(self):
        client = OpenCodeClient({**packaged_table(), "executable": "/Users/x/oc1/bin/opencode"})
        data = "/Users/x/.ensemble/docker-state/w/opencode"
        with mock.patch("subprocess.run", side_effect=fake_run) as run:
            launch = self.launch(
                self.spec(tier=3, env={"XDG_DATA_HOME": data}), DOCKER_CONTEXT, client
            )
        self.assertEqual(
            launch.argv[:3],
            ("/opt/ensemble/venv/bin/python", "-I", f"{IMAGE_CLIENT}/launcher.py"),
        )
        command = self.command(launch)
        self.assertEqual(command[:4], ("/opt/ensemble/bin/opencode", "run", "--format", "json"))
        self.assertIn("--auto", command)
        self.assertTrue(holds(command, ("--model", MODEL)))
        self.assertNotIn(["--version"], [call.args[0][1:] for call in run.call_args_list])
        provider = self.inline(launch)["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://host.docker.internal:11434/v1")
        self.assertEqual(launch.transcript, f"{data}/opencode/opencode.db")

    def test_a_local_launch_names_no_transcript(self):
        self.assertEqual(self.launch(self.spec()).transcript, "")

    def test_spec_selector_refused(self):
        for flags in (("--session", SES), ("--format", "default"), ("--dir", "/"), ("--auto",)):
            with self.subTest(flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))

    def test_spec_env_overriding_the_config_is_refused(self):
        for spec in (
            self.spec(env={"OPENCODE_PERMISSION": "{}"}),
            self.spec(access=Access(project=True, cwd=self.cwd, env={"OPENCODE_CONFIG": "/x"})),
        ):
            with self.subTest(spec=spec), self.assertRaises(ConfigError):
                self.launch(spec)

    def test_flags_follow_the_config_and_mode(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--agent", "build"]})
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        command = self.command(self.launch(self.spec(flags=("--share",)), context, client))
        self.assertEqual(command[-4:], ("--agent", "build", "--auto", "--share"))

    def test_a_new_run_titles_its_session_by_its_sid(self):
        self.assertTrue(
            holds(self.command(self.launch(self.spec())), ("--title", "ensemble-run-1"))
        )

    def test_a_title_flag_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "--title"):
            self.launch(self.spec(flags=("--title", "t")))
        client = OpenCodeClient({**packaged_table(), "flags": ["--title", "t"]})
        with self.assertRaisesRegex(ConfigError, "--title"):
            self.launch(self.spec(), client=client)


class ResumeTest(Base):
    def test_bound_session_resumes(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        command = self.command(self.launch(self.spec(resume=True)))
        self.assertEqual(command[:6], ("opencode", "run", "--format", "json", "--session", SES))

    def test_a_new_run_selects_no_session(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertNotIn("--session", self.command(self.launch(self.spec())))

    def test_a_resume_titles_nothing(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertNotIn("--title", self.command(self.launch(self.spec(resume=True))))

    def test_an_unbound_resume_takes_and_binds_the_runs_titled_session(self):
        """A harness killed before opencode printed its first sessionID bound nothing."""
        data = os.path.join(self.home, "data")
        titled_session(os.path.join(data, "opencode", "opencode.db"), SES, "ensemble-run-1", 1)
        command = self.command(self.launch(self.spec(resume=True, env={"XDG_DATA_HOME": data})))
        self.assertEqual(command[4:6], ("--session", SES))
        self.assertNotIn("--title", command)
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_an_unreadable_db_fails_an_unbound_resume_as_such(self):
        data = os.path.join(self.home, "data")
        os.makedirs(os.path.join(data, "opencode"))
        with open(os.path.join(data, "opencode", "opencode.db"), "w", encoding="utf-8") as f:
            f.write("not sqlite " * 200)
        with self.assertRaisesRegex(ConfigError, "opencode session lookup"):
            self.launch(self.spec(resume=True, env={"XDG_DATA_HOME": data}))

    def test_an_unbound_resume_takes_no_other_runs_session(self):
        data = os.path.join(self.home, "data")
        titled_session(os.path.join(data, "opencode", "opencode.db"), SES, "ensemble-run-2", 1)
        command = self.command(self.launch(self.spec(resume=True, env={"XDG_DATA_HOME": data})))
        self.assertNotIn("--session", command)
        self.assertIsNone(self.client.common.native(self.cwd, "run-1"))

    def test_an_unbound_resume_starts_a_fresh_titled_session(self):
        """A run killed before OpenCode saved its session resumes as a fresh one."""
        command = self.command(self.launch(self.spec(resume=True)))
        self.assertEqual(command[4:6], ("--title", "ensemble-run-1"))
        self.assertNotIn("--session", command)

    def test_tampered_binding_refused(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.client.common.store)
        path = os.path.join(self.client.common.store, name)
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**record, "native": "--auto"}, f)
        with self.assertRaises(ConfigError):
            self.launch(self.spec(resume=True))


def line(kind: str, **fields) -> str:
    return json.dumps({"type": kind, "timestamp": 1, "sessionID": SES, **fields})


def text(value: str) -> str:
    return line("text", part={"type": "text", "text": value, "time": {"start": 1, "end": 2}})


class EventsTest(Base):
    def test_text_and_eof_without_final_step_is_not_success(self):
        found = self.events(text("partial"))
        self.assertEqual(found[:3], [ClientBound(SES), TurnStarted(), Reply("partial")])
        self.assertIsInstance(found[-1], TurnFailed)
        self.assertEqual(found[-1].error, "incomplete_turn")

    def test_foreign_session_cannot_supply_a_completion(self):
        foreign = json.dumps(
            {"type": "step_finish", "sessionID": "ses_foreign", "part": {"reason": "stop"}}
        )
        found = self.events(text("partial"), foreign)
        self.assertFalse(any(isinstance(event, TurnEnd) for event in found))

    def events(self, *lines: str, spec: Spec | None = None) -> list:
        run = self.client.headless()
        assert run is not None
        run.launch(spec or self.spec())
        return list(run.events(lines))

    def test_a_run_binds_replies_and_ends_with_its_last_text(self):
        found = self.events(
            line("step_start", part={"type": "step-start"}),
            text("one"),
            line("tool_use", part={"tool": "read", "state": {"status": "completed"}}),
            text("two"),
            line("step_finish", part={"reason": "stop"}),
        )
        self.assertEqual(
            found,
            [
                ClientBound(SES),
                TurnStarted(),
                Activity("step_start"),
                Reply("one"),
                Activity("read"),
                Reply("two"),
                TurnEnd("two"),
            ],
        )
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_a_blank_text_is_no_reply(self):
        self.assertEqual(
            self.events(text("  "), line("step_finish", part={"reason": "stop"})),
            [ClientBound(SES), TurnStarted(), TurnEnd("")],
        )

    def test_an_uninteractive_permission_denial_fails_the_turn(self):
        rejected = "The user rejected permission to use this specific tool call."
        found = self.events(
            line(
                "tool_use", part={"tool": "bash", "state": {"status": "error", "error": rejected}}
            ),
            text("I cannot run it."),
        )
        self.assertEqual(
            found,
            [
                ClientBound(SES),
                TurnStarted(),
                Activity("bash"),
                Stderr(f"opencode: bash: {rejected}"),
                TurnFailed("permission_required", rejected),
                Reply("I cannot run it."),
            ],
        )

    def test_an_error_fails_the_turn(self):
        error = {"name": "UnknownError", "data": {"message": "Model not found: p/m."}}
        found = self.events(line("error", error=error))
        self.assertEqual(
            found,
            [ClientBound(SES), TurnStarted(), TurnFailed("UnknownError", "Model not found: p/m.")],
        )

    def test_an_error_without_data_keeps_its_name(self):
        found = self.events(line("error", error={"name": "APIError"}), text("late"))
        self.assertEqual(
            found, [ClientBound(SES), TurnStarted(), TurnFailed("APIError", ""), Reply("late")]
        )

    def test_no_session_fails_the_turn(self):
        found = self.events("not json", json.dumps({"type": "text"}))
        self.assertEqual(
            found,
            [
                Stderr("opencode: invalid JSON event"),
                TurnFailed("invalid_output", "opencode: invalid JSON event"),
                Activity("text"),
                TurnFailed("no_session", "opencode ended before it started a session"),
            ],
        )
        self.assertIsNone(self.client.common.native(self.cwd, "run-1"))

    def test_an_invalid_session_id_binds_nothing(self):
        found = self.events(json.dumps({"type": "text", "sessionID": "--auto"}))
        self.assertEqual(found[0], Stderr("opencode: invalid sessionID '--auto'"))
        self.assertEqual(
            found[-1], TurnFailed("no_session", "opencode ended before it started a session")
        )

    def test_the_first_session_binds_once(self):
        other = "ses_zzzzzzzzzzzzzzzzzzzzzzzzzz"
        found = self.events(text("a"), json.dumps({"type": "text", "sessionID": other}))
        self.assertEqual(found.count(ClientBound(SES)), 1)
        self.assertNotIn(ClientBound(other), found)
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_a_resume_rebinds_its_session(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        found = self.events(
            text("again"), line("step_finish", part={"reason": "stop"}), spec=self.spec(resume=True)
        )
        self.assertEqual(found, [ClientBound(SES), TurnStarted(), Reply("again"), TurnEnd("again")])
