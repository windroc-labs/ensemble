"""Image plugin installation with fake client commands, never host credentials."""

import io
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.case import REPO, Case


class PluginInstall(Case):
    def setUp(self):
        self.code = runpy.run_path(
            str(Path(REPO, "src/broker/internal/launcher/assets/plugin-install.py"))
        )
        self.root = Path(self.home, "image")
        self.system = Path(self.home, "etc/codex/config.toml")
        self.calls = []
        self.markets = {"vendor/catalogue": "example-market"}
        self.plugins = [
            {"name": "updraft", "repo": "vendor/catalogue", "commit": "a" * 40},
            {"name": "beta", "repo": "vendor/catalogue", "commit": "a" * 40},
        ]
        self.remote = {
            "name": "updraft",
            "repo": "windroc-labs/agent-plugins-marketplace",
            "commit": "a" * 40,
            "plugin_repo": "windroc-labs/updraft",
            "plugin_commit": "b" * 40,
        }
        self.remote_market = "windroc"

    def client_run(self, argv, **kw):
        self.calls.append((argv, kw))
        env = kw["env"]
        client = argv[0]
        home = Path(env["CLAUDE_CONFIG_DIR"] if client == "claude" else env["CODEX_HOME"])
        if client == "git" and "fetch" in argv:
            repo = argv[-2].removeprefix("https://github.com/").removesuffix(".git")
            market = Path(argv[2])
            (market / ".claude-plugin").mkdir(parents=True)
            (market / ".claude-plugin/marketplace.json").write_text(
                json.dumps(
                    {
                        "name": self.markets[repo],
                        "plugins": [{"name": name, "source": "./"} for name in ("updraft", "beta")],
                    }
                )
            )
        if argv[1:3] in (["plugin", "install"], ["plugin", "add"]):
            name, market = argv[3].split("@")
            version = "2.0.0" if client == "codex" else "1.0.0"
            installed = home / "plugins/cache" / market / name / version
            (installed / ".claude-plugin").mkdir(parents=True)
            (installed / ".claude-plugin/plugin.json").write_text(
                json.dumps({"name": name, "version": version})
            )
            (installed / "skills/build").mkdir(parents=True)
            (installed / "skills/build/SKILL.md").write_text("original skill")
            if client == "codex":
                with (home / "config.toml").open("a") as stream:
                    stream.write(f'[plugins."{name}@{market}"]\nenabled = true\n')
        return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)

    def install(self, plugins=None, run=None):
        with (
            mock.patch("subprocess.run", side_effect=run or self.client_run),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.code["install"](
                self.plugins if plugins is None else plugins, self.root, self.system
            )
        return output.getvalue()

    def remote_run(self, argv, **kw):
        self.markets[self.remote["repo"]] = self.remote_market
        result = self.client_run(argv, **kw)
        if argv[0] == "git" and "fetch" in argv:
            manifest = Path(argv[2], ".claude-plugin/marketplace.json")
            data = json.loads(manifest.read_text())
            data["plugins"][0]["name"] = self.remote["name"]
            data["plugins"][0]["source"] = {
                "source": "github",
                "repo": self.remote["plugin_repo"],
            }
            manifest.write_text(json.dumps(data))
        if argv[:3] == ["claude", "plugin", "install"]:
            home = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
            name, market = argv[3].split("@")
            location = home / "plugins/cache" / market / name / "1.0.0"
            (home / "plugins/installed_plugins.json").write_text(
                json.dumps(
                    {
                        "plugins": {
                            argv[3]: [
                                {
                                    "installPath": str(location),
                                    "gitCommitSha": self.remote["plugin_commit"],
                                }
                            ]
                        }
                    }
                )
            )
        if argv[0] == "git" and "rev-parse" in argv and "cache" in Path(argv[2]).parts:
            return SimpleNamespace(stdout=self.remote["plugin_commit"], returncode=0)
        return result

    def test_shared_marketplace_installs_remote_plugin_with_independent_pins(self):
        output = self.install([self.remote], run=self.remote_run)
        commands = [argv for argv, _ in self.calls]
        for client, verb in (("claude", "install"), ("codex", "add")):
            checkout = self.root / f"{client}-plugins/sources" / self.remote["repo"]
            manifest = json.loads((checkout / ".claude-plugin/marketplace.json").read_text())
            self.assertEqual(manifest["name"], "windroc")
            self.assertEqual(
                manifest["plugins"][0]["source"],
                {
                    "source": "url",
                    "url": "https://github.com/windroc-labs/updraft.git",
                    "sha": "b" * 40,
                },
            )
            self.assertIn([client, "plugin", "marketplace", "add", str(checkout)], commands)
            self.assertIn([client, "plugin", verb, "updraft@windroc"], commands)
            self.assertIn(f"{client} updraft@windroc version=", output)
        fetches = [argv for argv in commands if "fetch" in argv]
        self.assertEqual(len(fetches), 2)
        self.assertTrue(all(argv[-1] == "a" * 40 for argv in fetches))
        self.assertTrue(all(argv[-2].endswith("agent-plugins-marketplace.git") for argv in fetches))
        self.assertEqual(output.count("commit=" + "b" * 40), 2)

    def test_remote_plugin_repository_must_match_catalogue_before_registration(self):
        for index, source in enumerate((None, "./", {"source": "github", "repo": "other/repo"})):
            with self.subTest(source=source):
                self.root = Path(self.home, f"remote-source-{index}")
                self.calls = []

                def wrong_source(argv, *, source=source, **kw):
                    result = self.remote_run(argv, **kw)
                    if argv[0] == "git" and "fetch" in argv:
                        path = Path(argv[2], ".claude-plugin/marketplace.json")
                        data = json.loads(path.read_text())
                        data["plugins"][0]["source"] = source
                        path.write_text(json.dumps(data))
                    return result

                with self.assertRaisesRegex(ValueError, "source must name windroc-labs/updraft"):
                    self.install([self.remote], run=wrong_source)
                self.assertFalse(any(argv[0] in ("claude", "codex") for argv, _ in self.calls))

    def test_shared_catalogue_can_mix_pinned_remote_and_local_sources(self):
        local = {key: self.remote[key] for key in ("repo", "commit")}
        self.install([self.remote, {"name": "beta", **local}], run=self.remote_run)
        commands = [argv for argv, _ in self.calls]
        self.assertEqual(len([argv for argv in commands if "fetch" in argv]), 2)
        for client, verb in (("claude", "install"), ("codex", "add")):
            self.assertIn([client, "plugin", verb, "beta@windroc"], commands)

    def test_arbitrary_plugin_installs_through_its_configured_marketplace(self):
        self.remote |= {
            "name": "widget",
            "repo": "another/catalogue",
            "plugin_repo": "different/plugin",
        }
        self.remote_market = "custom-market"
        output = self.install([self.remote], run=self.remote_run)
        commands = [argv for argv, _ in self.calls]
        for client, verb in (("claude", "install"), ("codex", "add")):
            self.assertIn([client, "plugin", verb, "widget@custom-market"], commands)
            self.assertIn(f"{client} widget@custom-market version=", output)
            checkout = self.root / f"{client}-plugins/sources/another/catalogue"
            source = json.loads((checkout / ".claude-plugin/marketplace.json").read_text())
            self.assertEqual(
                source["plugins"][0]["source"]["url"], "https://github.com/different/plugin.git"
            )

    def test_each_client_rejects_wrong_installed_remote_plugin_commit(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.root = Path(self.home, f"remote-drift-{client}")

                def drift(argv, *, client=client, **kw):
                    result = self.remote_run(argv, **kw)
                    if client == "claude" and argv[:3] == ["claude", "plugin", "install"]:
                        path = Path(
                            kw["env"]["CLAUDE_CONFIG_DIR"], "plugins/installed_plugins.json"
                        )
                        path.write_text(path.read_text().replace("b" * 40, "c" * 40))
                    if client == "codex" and argv[0] == "git" and "cache" in Path(argv[2]).parts:
                        return SimpleNamespace(stdout="c" * 40, returncode=0)
                    return result

                with self.assertRaisesRegex(ValueError, client + ".*expected .*b.*actual .*c"):
                    self.install([self.remote], run=drift)

    def test_remote_pin_fields_are_required_together_and_validated_before_commands(self):
        for change in (
            {"plugin_repo": "../outside"},
            {"plugin_repo": None},
            {"plugin_commit": None},
            {"plugin_commit": "main"},
            {"plugin_commit": "A" * 40},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "remote plugin"):
                self.install([self.remote | change])
            self.assertEqual(self.calls, [])

    def test_each_client_installs_from_its_own_verified_pinned_checkout(self):
        output = self.install()
        commands = [argv for argv, _ in self.calls]
        fetches = [argv for argv in commands if "fetch" in argv]
        self.assertEqual(len(fetches), 2)
        for client, verb in (("claude", "install"), ("codex", "add")):
            source = self.root / f"{client}-plugins/sources/vendor/catalogue"
            fetch = [
                "git",
                "-C",
                str(source),
                "fetch",
                "--depth=1",
                "https://github.com/vendor/catalogue.git",
                "a" * 40,
            ]
            self.assertIn(fetch, commands)
            init = ["git", "init", str(source)]
            checkout = ["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"]
            self.assertIn(init, commands)
            self.assertIn(checkout, commands)
            self.assertLess(commands.index(init), commands.index(fetch))
            self.assertLess(commands.index(fetch), commands.index(checkout))
            register = [client, "plugin", "marketplace", "add", str(source)]
            self.assertIn(register, commands)
            verify = ["git", "-C", str(source), "rev-parse", "HEAD"]
            self.assertLess(commands.index(checkout), commands.index(verify))
            self.assertLess(commands.index(verify), commands.index(register))
            for name in ("updraft", "beta"):
                self.assertIn([client, "plugin", verb, f"{name}@example-market"], commands)
                self.assertIn(f"{client} {name}@example-market version=", output)
        self.assertEqual(output.count("commit=" + "a" * 40), 4)

    def test_wrong_checkout_fails_before_registration_and_after_install_for_each_client(self):
        for client in ("claude", "codex"):
            for drift in (False, True):
                with self.subTest(client=client, drift=drift):
                    self.root = Path(self.home, client, str(drift))
                    self.calls = []
                    reads = 0

                    def wrong_revision(argv, *, client=client, drift=drift, **kw):
                        nonlocal reads
                        result = self.client_run(argv, **kw)
                        if (
                            argv[0] == "git"
                            and "rev-parse" in argv
                            and f"{client}-plugins" in Path(argv[2]).parts
                        ):
                            reads += 1
                            if not drift or reads > 1:
                                return SimpleNamespace(stdout="b" * 40, returncode=0)
                        return result

                    with self.assertRaisesRegex(
                        ValueError,
                        client
                        + ".*vendor/catalogue.*expected "
                        + "a" * 40
                        + ".*actual .*"
                        + "b" * 40,
                    ):
                        self.install([self.plugins[0]], run=wrong_revision)
                    installed = [
                        argv
                        for argv, _ in self.calls
                        if argv[:3] in ([client, "plugin", "install"], [client, "plugin", "add"])
                    ]
                    self.assertEqual(bool(installed), drift)

    def test_invalid_pins_duplicates_and_conflicts_fail_before_commands(self):
        for plugins in (
            [{"name": "updraft", "repo": "vendor/catalogue"}],
            [self.plugins[0] | {"commit": "main"}],
            [self.plugins[0] | {"commit": "a" * 39}],
            [self.plugins[0] | {"commit": "A" * 40}],
            [self.plugins[0] | {"repo": "../outside"}],
            [self.plugins[0], self.plugins[0]],
            [self.plugins[0], self.plugins[1] | {"commit": "b" * 40}],
        ):
            with (
                self.subTest(plugins=plugins),
                self.assertRaisesRegex(ValueError, "plugin-install:"),
            ):
                self.install(plugins)
            self.assertEqual(self.calls, [])

    def test_remote_missing_or_escaping_plugin_source_fails_before_install(self):
        for index, source in enumerate(
            (
                {"source": "github", "repo": "vendor/repo"},
                "../outside",
                "/tmp/outside",
                "./missing",
                None,
            )
        ):
            with self.subTest(source=source):
                self.root = Path(self.home, f"source-{index}")
                self.calls = []

                def bad_source(argv, *, source=source, **kw):
                    result = self.client_run(argv, **kw)
                    if argv[0] == "git" and "fetch" in argv:
                        manifest = Path(argv[2], ".claude-plugin/marketplace.json")
                        data = json.loads(manifest.read_text())
                        data["plugins"][0]["source"] = source
                        manifest.write_text(json.dumps(data))
                    return result

                with self.assertRaisesRegex(ValueError, "plugin-install:.*source"):
                    self.install([self.plugins[0]], run=bad_source)
                self.assertFalse(any(argv[0] in ("claude", "codex") for argv, _ in self.calls))

    def test_relative_and_symlink_sources_cannot_escape_checkout(self):
        for mode in ("parent", "symlink"):
            with self.subTest(mode=mode):
                root = Path(self.home, mode)
                checkout, outside = root / "source", root / "outside"
                (checkout / ".claude-plugin").mkdir(parents=True)
                outside.mkdir()
                (checkout / "linked").symlink_to(outside, target_is_directory=True)
                source = "./../outside" if mode == "parent" else "./linked"
                (checkout / ".claude-plugin/marketplace.json").write_text(
                    json.dumps(
                        {
                            "name": "test-market",
                            "plugins": [{"name": "updraft", "source": source}],
                        }
                    )
                )
                with self.assertRaisesRegex(ValueError, "source must stay inside its checkout"):
                    self.code["marketplace"](checkout, ["updraft"])

    def test_opencode_reads_original_claude_skill_without_host_install(self):
        self.install()
        original = self.root / "claude-plugins/plugins/cache/example-market/updraft/1.0.0"
        shared = self.root / "plugins/updraft"
        self.assertEqual(shared.resolve(), original)
        self.assertEqual((shared / "skills/build/SKILL.md").read_text(), "original skill")
        self.assertFalse(Path(self.home, ".claude").exists())
        self.assertTrue(
            (self.root / "codex-plugins/plugins/cache/example-market/updraft/2.0.0").is_dir()
        )

    def test_client_commands_have_only_image_environment_and_closed_stdin(self):
        with mock.patch.dict(os.environ, {"PERSONAL_SECRET": "do-not-copy"}):
            self.install()
        for _, kw in self.calls:
            self.assertNotIn("PERSONAL_SECRET", kw["env"])
            self.assertEqual(kw["stdin"], self.code["subprocess"].DEVNULL)
            self.assertTrue(kw["check"])

    def test_empty_plugins_still_prepare_native_empty_homes(self):
        self.assertEqual(self.install([]), "")
        self.assertEqual(self.calls, [])
        self.assertTrue((self.root / "plugins").is_dir())
        self.assertTrue((self.root / "codex-plugins/plugins/cache").is_dir())
        self.assertEqual((self.root / "codex-plugins/config.toml").read_text(), "")
        self.assertEqual(self.system.read_text(), "")

    def test_the_system_config_enables_each_installed_codex_plugin(self):
        self.install()
        self.assertEqual(
            self.system.read_text(),
            '[plugins."beta@example-market"]\nenabled = true\n'
            '[plugins."updraft@example-market"]\nenabled = true\n',
        )
        self.assertEqual(self.system.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.system.parent.stat().st_mode & 0o777, 0o755)

    def test_missing_metadata_and_command_failures_fail_the_build(self):
        def no_metadata(_argv, **_kw):
            return SimpleNamespace(stdout="", returncode=0)

        with self.assertRaises((ValueError, FileNotFoundError)):
            self.install(run=no_metadata)
        error = self.code["subprocess"].CalledProcessError
        with self.assertRaisesRegex(ValueError, "claude.*vendor/catalogue.*command failed"):
            self.install(run=mock.Mock(side_effect=error(1, "fake")))

    def test_distinct_repositories_use_their_manifest_marketplace_names(self):
        self.markets["vendor/catalogue-repo"] = "team-market"
        self.install(
            [self.plugins[0], {"name": "beta", "repo": "vendor/catalogue-repo", "commit": "a" * 40}]
        )
        commands = [argv for argv, _ in self.calls]
        for client, verb in (("claude", "install"), ("codex", "add")):
            self.assertIn(
                [
                    client,
                    "plugin",
                    "marketplace",
                    "add",
                    str(self.root / f"{client}-plugins/sources/vendor/catalogue-repo"),
                ],
                commands,
            )
            self.assertIn([client, "plugin", verb, "updraft@example-market"], commands)
            self.assertIn([client, "plugin", verb, "beta@team-market"], commands)
        self.assertEqual(
            (self.root / "plugins/beta/skills/build/SKILL.md").read_text(), "original skill"
        )

    def test_bad_installed_metadata_and_revision_fail_for_either_client(self):
        for client, verb, home, version in (
            ("claude", "install", "claude-plugins", "1.0.0"),
            ("codex", "add", "codex-plugins", "2.0.0"),
        ):
            for case in (
                "missing-version",
                "invalid-version",
                "ambiguous",
                "missing-manifest",
                "revision",
            ):
                with self.subTest(client=client, case=case):
                    self.root = Path(self.home, client, case)

                    def bad_metadata(
                        argv,
                        *,
                        client=client,
                        verb=verb,
                        home=home,
                        version=version,
                        case=case,
                        **kw,
                    ):
                        result = self.client_run(argv, **kw)
                        if argv[:3] == [client, "plugin", verb]:
                            installed = (
                                self.root / home / "plugins/cache/example-market/updraft" / version
                            )
                            manifest = installed / ".claude-plugin/plugin.json"
                            if case == "missing-version":
                                manifest.write_text('{"name":"updraft"}')
                            elif case == "invalid-version":
                                manifest.write_text('{"name":"updraft","version":42}')
                            elif case == "ambiguous":
                                (installed.parent / "3.0.0").mkdir()
                            elif case == "missing-manifest":
                                manifest.unlink()
                        if case == "revision" and argv[0] == "git" and home in Path(argv[2]).parts:
                            return SimpleNamespace(stdout="invalid revision", returncode=0)
                        return result

                    with self.assertRaisesRegex(ValueError, "plugin-install:"):
                        self.install([self.plugins[0]], run=bad_metadata)

    def test_main_reads_the_sources_file(self):
        source = Path(self.home, "sources.json")
        source.write_text(json.dumps([self.plugins[0]]))
        with (
            mock.patch("subprocess.run", side_effect=self.client_run),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.code["main"]([str(source), str(self.root), str(self.system)])
        self.assertTrue((self.root / "plugins/updraft/skills/build/SKILL.md").is_file())
        self.assertIn("updraft@example-market", self.system.read_text())

    def test_fetch_failure_names_client_repo_and_expected_commit(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.root = Path(self.home, f"failed-{client}")

                def fetch_failure(argv, *, client=client, **kw):
                    if "fetch" in argv and f"{client}-plugins" in Path(argv[2]).parts:
                        raise self.code["subprocess"].CalledProcessError(1, argv)
                    return self.client_run(argv, **kw)

                with self.assertRaisesRegex(
                    ValueError, client + ".*vendor/catalogue.*" + "a" * 40 + ".*fetch"
                ):
                    self.install([self.plugins[0]], run=fetch_failure)

    def test_main_reports_invalid_input_without_running_a_client(self):
        source = Path(self.home, "bad.json")
        source.write_text('[{"name":"updraft"}]')
        with (
            mock.patch("subprocess.run") as run,
            self.assertRaisesRegex(SystemExit, "plugin-install:.*full commit"),
        ):
            self.code["main"]([str(source), str(self.root), str(self.system)])
        run.assert_not_called()

    def test_marketplace_name_collision_between_repositories_fails(self):
        self.markets["vendor/catalogue-repo"] = "example-market"
        with self.assertRaisesRegex(ValueError, "duplicate marketplace name"):
            self.install(
                [
                    self.plugins[0],
                    {"name": "beta", "repo": "vendor/catalogue-repo", "commit": "a" * 40},
                ]
            )

    def test_invalid_marketplace_names_stop_before_other_client_or_plugin_install(self):
        for index, name in enumerate((None, 42, [], {}, "", "../escape", "a" * 65)):
            with self.subTest(name=name):
                self.root = Path(self.home, f"market-{index}")
                self.calls = []

                def bad_marketplace(argv, *, name=name, **kw):
                    result = self.client_run(argv, **kw)
                    if argv[0] == "git" and "fetch" in argv:
                        manifest = Path(argv[2], ".claude-plugin/marketplace.json")
                        manifest.write_text(json.dumps({"name": name}))
                    return result

                with self.assertRaisesRegex(ValueError, "invalid marketplace name"):
                    self.install([self.plugins[0]], run=bad_marketplace)
                self.assertFalse(any(argv[0] in ("claude", "codex") for argv, _ in self.calls))

    def test_main_requires_input_file_and_absolute_root_and_system_config(self):
        for argv in (
            [],
            ["input"],
            ["input", "relative", "/etc/codex/config.toml"],
            ["input", "/image"],
            ["input", "/image", "relative"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.code["main"](argv)
