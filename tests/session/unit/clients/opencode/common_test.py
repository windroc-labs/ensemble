"""OpenCode's config checks and session bindings."""

import json
import os
import re
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from ensemble.session.clients.opencode import OpenCodeClient, probe, transcript
from ensemble.session.clients.opencode import common as common_module
from ensemble.session.clients.opencode.common import (
    HOME,
    NATIVE,
    Bindings,
    OpenCodeCommon,
    worktree,
)
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    ClientHome,
    ConfigError,
    Effort,
    Permission,
    Resumable,
    SessionInfo,
    SessionStatus,
    Spec,
    Usage,
)
from ensemble.session.hosts.docker.host import DockerHost
from ensemble.session.modes.base import BaseMode
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import (
    DOCKER_CONTEXT,
    SES,
    make_db,
    packaged_table,
    titled_session,
)

STRATA = {"base_url": "http://10.0.0.5:8080/v1", "models": ["m"]}


def common(**config) -> OpenCodeCommon:
    return OpenCodeClient({**packaged_table(), **config}).common


class ConfigTest(Case):
    def test_the_packaged_table_builds_the_client(self):
        found = OpenCodeClient(packaged_table()).common
        self.assertEqual(found.port, 11434)
        for tier in range(1, 5):
            self.assertEqual(found.model(tier), "ollama/qwen3.6:35b-mlx")
        self.assertEqual(found.variant(Effort.HIGH), "high")
        self.assertEqual(found.modes, {"auto": (), "yolo": ("--auto",)})

    def test_an_unknown_key_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown key 'small_model'"):
            OpenCodeClient({**packaged_table(), "small_model": "x"})

    def test_ollama_port_must_be_an_integer_port(self):
        for port in (True, 0, 65536, "11434", 1.5):
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(ConfigError, r"ollama_port must be an integer port"),
            ):
                common(ollama_port=port)
        self.assertEqual(common(ollama_port=11500).port, 11500)

    def test_tiers_efforts_and_env_are_checked(self):
        for key, value in (("tiers", {"1": 3}), ("efforts", {"low": ""}), ("env", {"A": 1})):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, rf"\.{key} must be"):
                common(**{key: value})

    def test_the_model_names_ollama_or_a_listed_provider(self):
        self.assertEqual(common(model="ollama/b").model(None), "ollama/b")
        for model in ("", " ", 3, "other/b", "b"):
            with self.subTest(model=model), self.assertRaisesRegex(ConfigError, r"\.model"):
                common(model=model)

    def test_the_packaged_table_has_no_provider(self):
        self.assertEqual(common().providers, {})

    def test_a_provider_table_is_checked(self):
        good = {"base_url": "http://10.0.0.5:8080/v1", "models": ["m"]}
        for providers in (
            {"strata": "x"},
            {"strata": {"base_url": good["base_url"]}},
            {"strata": {**good, "api_key": "k"}},
            {"strata": {**good, "base_url": "10.0.0.5:8080/v1"}},
            {"strata": {**good, "base_url": "http://a b/v1"}},
            {"strata": {**good, "models": "m"}},
            {"strata": {**good, "models": [""]}},
            {"strata": {**good, "models": ["a b"]}},
            {"str/ata": good},
            {"": good},
            {"ollama": good},
            [good],
        ):
            with (
                self.subTest(providers=providers),
                self.assertRaisesRegex(ConfigError, r"\.providers must be"),
            ):
                common(providers=providers)
        self.assertEqual(common(providers={"strata": good}).providers, {"strata": good})

    def test_a_provider_name_is_at_most_64_characters(self):
        self.assertIn("p" * 64, common(providers={"p" * 64: STRATA}).providers)
        with self.assertRaisesRegex(ConfigError, r"\.providers must be"):
            common(providers={"p" * 65: STRATA})

    def test_a_tier_names_ollama_or_a_listed_provider(self):
        good = {"base_url": "https://strata.lan/v1", "models": []}
        found = common(providers={"strata": good}, tiers={"1": "strata/m", "2": "ollama/a"})
        self.assertEqual(found.model(1), "strata/m")
        for model in ("other/b", "b", "strata/", "/m", "opencode/big-pickle"):
            with (
                self.subTest(model=model),
                self.assertRaisesRegex(ConfigError, rf"tiers\.1 {re.escape(repr(model))}: not"),
            ):
                common(providers={"strata": good}, tiers={"1": model})

    def test_without_model_a_run_without_a_tier_is_refused(self):
        table = packaged_table()
        del table["model"]
        found = OpenCodeClient(table).common
        self.assertEqual(found.model(1), "ollama/qwen3.6:35b-mlx")
        with self.assertRaisesRegex(ConfigError, r"no model for a run without a tier"):
            found.model(None)

    def test_a_missing_tier_or_effort_is_a_config_error(self):
        found = common(tiers={"1": "ollama/a"}, efforts={"low": "low"})
        self.assertEqual(found.model(None), "ollama/qwen3.6:35b-mlx")
        self.assertIsNone(found.variant(None))
        with self.assertRaisesRegex(ConfigError, r"no model for tier 3"):
            found.model(3)
        with self.assertRaisesRegex(ConfigError, r"no variant for effort 'high'"):
            found.variant(Effort.HIGH)

    def test_refused_flags(self):
        for flags in (
            ["-s", SES],
            ["--session=x"],
            ["-c"],
            ["--continue"],
            ["--fork"],
            ["--format", "default"],
            ["--attach", "http://127.0.0.1:4096"],
            ["--dir", "/tmp"],
            ["--auto"],
            ["--yolo"],
            ["--dangerously-skip-permissions"],
            ["--dangerouslySkipPermissions"],
            ["--continue"],
            ["-m", "ollama/b"],
            ["--model=ollama/b"],
        ):
            with self.subTest(flags=flags), self.assertRaises(ConfigError):
                common(flags=flags)
        for mode in (["--session", SES], ["--format", "json"], ["--dir", "/tmp"], ["-m", "x"]):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                common(modes={"auto": mode})
        self.assertEqual(common(flags=["--agent", "build"]).flags, ("--agent", "build"))

    def test_env_overriding_the_inline_config_is_refused(self):
        for key in (
            "OPENCODE_PERMISSION",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_DIR",
            "OPENCODE_CONFIG_CONTENT",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                common(env={key: "{}"})


class StoreTest(Case):
    def setUp(self):
        self.common = common()
        self.cwd = os.path.realpath(self.home)

    def test_a_binding_round_trips_by_cwd_and_sid(self):
        self.common.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.common.native(self.cwd, "run-1"), SES)
        self.assertIsNone(self.common.native(self.cwd, "run-2"))
        self.assertIsNone(self.common.native(os.path.join(self.cwd, "x"), "run-1"))
        (name,) = os.listdir(self.common.store)
        self.assertEqual(os.stat(self.common.store).st_mode & 0o777, 0o700)
        self.assertTrue(name.endswith(".json"))

    def test_bind_refuses_an_invalid_session_id(self):
        for native in ("--auto", "ses_", "ses_a b", "x" * 10):
            with self.subTest(native=native), self.assertRaises(ConfigError):
                self.common.bind(self.cwd, "run-1", native)

    def test_a_tampered_binding_is_a_config_error(self):
        self.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.common.store)
        path = os.path.join(self.common.store, name)
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        for change in (
            {"native": "--auto"},
            {"sid": "run-2"},
            {"cwd": "/elsewhere"},
            {"version": 2},
            {"extra": 1},
        ):
            with self.subTest(change=change):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({**record, **change}, f)
                with self.assertRaises(ConfigError):
                    self.common.native(self.cwd, "run-1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        with self.assertRaises(ConfigError):
            self.common.native(self.cwd, "run-1")

    def test_a_symlinked_binding_is_not_followed(self):
        self.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.common.store)
        path = os.path.join(self.common.store, name)
        target = os.path.join(self.home, "target.json")
        os.replace(path, target)
        os.symlink(target, path)
        self.assertIsNone(self.common.native(self.cwd, "run-1"))

    def test_native_pattern(self):
        self.assertTrue(NATIVE.fullmatch(SES))
        self.assertFalse(NATIVE.fullmatch("ses_-x"))

    def test_unbind_removes_the_binding_and_a_missing_one_is_fine(self):
        self.common.bind(self.cwd, "run-1", SES)
        self.common.unbind(self.cwd, "run-1")
        self.assertIsNone(self.common.native(self.cwd, "run-1"))
        self.common.unbind(self.cwd, "run-1")

    def test_unbind_fails_on_any_other_error(self):
        with (
            mock.patch("os.unlink", side_effect=PermissionError(13, "denied")),
            self.assertRaisesRegex(ConfigError, "opencode session binding"),
        ):
            self.common.unbind(self.cwd, "run-1")

    def test_bindings_by_store_share_the_records(self):
        bindings = Bindings(self.common.store)
        bindings.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.common.native(self.cwd, "run-1"), SES)
        self.assertEqual(bindings.native(self.cwd, "run-1"), SES)


class SessionStatusTest(Case):
    def setUp(self):
        self.cwd = os.path.realpath(self.home)
        self.data = os.path.join(self.home, "data")
        self.db = os.path.join(self.data, "opencode", "opencode.db")
        self.enterContext(mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.data}))
        self.client = OpenCodeClient(packaged_table())
        self.info = SessionInfo("run-1", self.cwd, project=False, transcript="", resume="")

    def status(self, **env):
        return self.client.session_status(self.info, env=env)

    def test_a_bound_session_in_the_db_is_resumable(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        make_db(self.db, {SES: 1_760_000_000_250})
        found = self.status()
        self.assertEqual(found.resumable, Resumable.YES)
        self.assertEqual(found.last_active, 1_760_000_000.25)
        self.assertEqual(found.resume_command, "")

    def test_an_unbound_run_is_resumable_as_a_fresh_session_once_its_db_exists(self):
        self.assertEqual(self.status().resumable, Resumable.NO)
        make_db(self.db, {SES: 1})
        self.assertEqual(self.status(), SessionStatus(Resumable.YES, None, ""))

    def test_a_bound_session_without_its_row_is_not_resumable(self):
        make_db(self.db, {SES: 1})
        self.client.common.bind(self.cwd, "run-1", "ses_notInTheDb")
        self.assertEqual(self.status().resumable, Resumable.NO)

    def test_an_unbound_runs_titled_session_is_resumable_and_bound(self):
        titled_session(self.db, SES, "ensemble-run-1", 1_760_000_000_250)
        self.assertEqual(self.status().resumable, Resumable.YES)
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_session_status_unknown_without_a_db(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.status().resumable, Resumable.UNKNOWN)

    def test_session_status_unknown_on_a_query_error(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        os.makedirs(os.path.dirname(self.db))
        with open(self.db, "w", encoding="utf-8") as f:
            f.write("not sqlite " * 200)
        self.assertEqual(self.status().resumable, Resumable.UNKNOWN)

    def test_session_status_unknown_on_a_corrupt_binding(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.client.common.store)
        with open(os.path.join(self.client.common.store, name), "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(self.status().resumable, Resumable.UNKNOWN)

    def test_the_runs_transcript_is_its_db(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        docker = os.path.join(self.home, "docker", "opencode", "opencode.db")
        make_db(docker, {SES: 1000})
        self.assertEqual(self.status().resumable, Resumable.UNKNOWN)
        info = replace(self.info, transcript=docker)
        self.assertEqual(self.client.session_status(info).resumable, Resumable.YES)
        make_db(self.db, {SES: 1000})
        os.unlink(docker)
        self.assertEqual(self.client.session_status(info).resumable, Resumable.UNKNOWN)

    def test_the_db_follows_the_session_env_over_the_caller(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        other = os.path.join(self.home, "other")
        make_db(os.path.join(other, "opencode", "opencode.db"), {SES: 1000})
        self.assertEqual(self.status().resumable, Resumable.UNKNOWN)
        self.assertEqual(self.status(XDG_DATA_HOME=other).resumable, Resumable.YES)
        configured = OpenCodeClient({**packaged_table(), "env": {"XDG_DATA_HOME": other}})
        self.assertEqual(configured.session_status(self.info).resumable, Resumable.YES)


EXTRA = ("--prompt", "--pure")


class ProgramTest(Case):
    def program(self, result=None, *, error=None, config=None):
        common = OpenCodeCommon({**packaged_table(), **(config or {})}, selector=lambda *_: ())
        with mock.patch("subprocess.run", side_effect=error, return_value=result) as run:
            found = common.program({"PATH": "/opt/oc1/bin", "A": "b"}, self.home)
        return found, run

    def done(self, stdout, code=0):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    def test_executable_is_checked_and_defaults(self):
        self.assertEqual(
            OpenCodeCommon(packaged_table(), selector=lambda *_: ()).executable, "opencode"
        )
        empty = OpenCodeCommon({**packaged_table(), "executable": ""}, selector=lambda *_: ())
        self.assertEqual(empty.executable, "opencode")
        for value in (" ", 3, "a\nb"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "executable"):
                OpenCodeCommon({**packaged_table(), "executable": value}, selector=lambda *_: ())

    def test_a_1x_version_launches(self):
        found, run = self.program(self.done("1.19.3\n"), config={"executable": "/opt/oc1/opencode"})
        self.assertEqual(found, "/opt/oc1/opencode")
        ((args, kwargs),) = run.call_args_list
        self.assertEqual(args[0], ["/opt/oc1/opencode", "--version"])
        self.assertEqual(kwargs["cwd"], self.home)
        self.assertEqual((kwargs["env"]["PATH"], kwargs["env"]["A"]), ("/opt/oc1/bin", "b"))
        self.assertEqual(kwargs["timeout"], 10)

    def test_a_cwd_not_made_yet_is_not_the_checks(self):
        common = OpenCodeCommon(packaged_table(), selector=lambda *_: ())
        with mock.patch("subprocess.run", return_value=self.done("1.19.3")) as run:
            common.program({}, os.path.join(self.home, "not-yet"))
        self.assertIsNone(run.call_args.kwargs["cwd"])

    def test_a_2x_version_is_refused_naming_it_and_the_key(self):
        with self.assertRaises(ConfigError) as raised:
            self.program(self.done("2.0.32\n"))
        message = str(raised.exception)
        self.assertIn("2.0.32", message)
        self.assertIn("is not 1.x", message)
        self.assertIn("[clients.opencode] executable", message)

    def test_a_missing_failing_or_silent_executable_is_refused(self):
        for case, kwargs, words in (
            (
                "missing",
                {"error": FileNotFoundError(2, "No such file or directory")},
                "No such file",
            ),
            (
                "timeout",
                {"error": common_module.subprocess.TimeoutExpired(["opencode"], 10)},
                "timed out",
            ),
            ("failed", {"result": self.done("1.19.3", code=1)}, "no version"),
            ("silent", {"result": self.done("opencode\n")}, "no version"),
        ):
            with self.subTest(case=case), self.assertRaises(ConfigError) as raised:
                self.program(**kwargs)
            self.assertIn(words, str(raised.exception))
            self.assertIn("[clients.opencode] executable", str(raised.exception))


class SelectionTest(Case):
    def setUp(self):
        self.common = common()
        self.cwd = os.path.realpath(self.home)
        self.spec = Spec(workdir=self.home, access=Access(cwd=self.cwd, project=True), sid="run-1")

    def test_a_fresh_run_selects_nothing_and_a_resume_its_bound_session(self):
        self.assertEqual(self.common.selection(self.spec, LOCAL_CONTEXT), ())
        self.common.bind(self.cwd, "run-1", SES)
        resumed = self.common.selection(replace(self.spec, resume=True), LOCAL_CONTEXT)
        self.assertEqual(resumed, ("--session", SES))

    def test_an_unbound_resume_selects_a_fresh_session(self):
        """A run killed before OpenCode saved its session bound none."""
        self.assertEqual(self.common.selection(replace(self.spec, resume=True), LOCAL_CONTEXT), ())
        self.assertIsNone(self.common.native(self.cwd, "run-1"))

    def test_a_container_context_selects_as_a_local_one(self):
        self.assertEqual(self.common.selection(self.spec, DOCKER_CONTEXT), ())
        self.common.bind(self.cwd, "run-1", SES)
        resumed = self.common.selection(replace(self.spec, resume=True), DOCKER_CONTEXT)
        self.assertEqual(resumed, ("--session", SES))

    def test_an_isolated_host_starts_in_an_untrusted_cwd(self):
        untrusted = replace(self.spec, access=Access(cwd=self.cwd))
        self.assertEqual(self.common.selection(untrusted, DOCKER_CONTEXT), ())

    def test_a_non_isolated_host_starts_in_a_trusted_cwd(self):
        self.assertEqual(self.common.selection(self.spec, LOCAL_CONTEXT), ())

    def test_a_non_isolated_host_refuses_an_untrusted_cwd(self):
        self.common.bind(self.cwd, "run-1", SES)
        untrusted = replace(self.spec, access=Access(cwd=self.cwd))
        unisolated_docker = replace(DOCKER_CONTEXT, permission=Permission.AUTO)
        for context in (LOCAL_CONTEXT, unisolated_docker):
            for resume in (False, True):
                with (
                    self.subTest(container=context.container is not None, resume=resume),
                    self.assertRaisesRegex(ConfigError, "isolated Host.*trusted cwd"),
                ):
                    self.common.selection(replace(untrusted, resume=resume), context)

    def test_the_home_is_xdg_data_home_with_no_credential(self):
        self.assertEqual(
            replace(self.common.home(), files=HOME.files),
            ClientHome("opencode_dir", "opencode", "XDG_DATA_HOME", "none"),
        )
        host = DockerHost("docker", {"docker": "/usr/local/bin/docker", "isolated": True})
        data = os.path.join(self.home, ".ensemble", "docker-state", "w", "opencode")
        self.assertEqual(
            host._client_env("w", "opencode", self.common.home(), resume=False),
            {"XDG_DATA_HOME": data},
        )
        self.assertEqual(
            transcript.database({"XDG_DATA_HOME": data}),
            os.path.join(data, "opencode", "opencode.db"),
        )
        listed = common(providers={"strata": STRATA, "lab": STRATA}).home()
        self.assertEqual(listed.providers, ("lab", "strata"))
        _, metadata = BaseMode._hosted(self.spec, listed, host, "w", generation="f" * 32)
        self.assertEqual(json.loads(metadata["home_providers"]), ["lab", "strata"])
        spec, metadata = BaseMode._hosted(
            self.spec, self.common.home(), host, "w", generation="f" * 32
        )
        self.assertEqual(spec.env["XDG_DATA_HOME"], data)
        self.assertEqual(
            {k: metadata[k] for k in ("home_field", "home_dir", "credential", "resume")},
            {
                "home_field": "opencode_dir",
                "home_dir": "opencode",
                "credential": "none",
                "resume": "0",
            },
        )
        self.assertNotIn("home_providers", metadata)

    def test_the_docker_config_is_the_homes_opencode_json_verbatim(self):
        with open(
            os.path.join(os.path.dirname(common_module.__file__), "docker-opencode.json")
        ) as f:
            default = f.read()
        self.assertEqual(self.common.home().files(), {"opencode.json": default})
        self.assertEqual(json.loads(default), {"$schema": "https://opencode.ai/config.json"})
        path = os.path.join(self.home, ".ensemble", "docker-config", "opencode", "opencode.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("// mine\n{}")
        self.assertEqual(self.common.home().files(), {"opencode.json": "// mine\n{}"})

    def test_a_container_launch_points_opencode_config_at_the_homes_copy(self):
        data = os.path.join(self.home, "state", "opencode")
        spec = replace(self.spec, env={"XDG_DATA_HOME": data})
        env = self.common.environment(spec, DOCKER_CONTEXT)
        self.assertEqual(env["OPENCODE_CONFIG"], os.path.join(data, "opencode.json"))
        self.assertFalse(os.path.exists(data))
        self.assertNotIn("OPENCODE_CONFIG", self.common.environment(spec, LOCAL_CONTEXT))

    def test_extra_flags_are_refused_with_the_reserved_ones(self):
        for flags in (("-c",), ("--prompt=x",), ("--pure",)):
            with self.subTest(flags=flags), self.assertRaises(ConfigError):
                self.common.selection(replace(self.spec, flags=flags), LOCAL_CONTEXT, EXTRA)
        self.assertEqual(
            self.common.selection(replace(self.spec, flags=("--pure",)), LOCAL_CONTEXT), ()
        )

    def test_the_prompt_takes_a_value(self):
        with self.assertRaises(ConfigError):
            self.common.selection(
                replace(self.spec, flags=("--prompt", "-c")), LOCAL_CONTEXT, EXTRA
            )
        self.assertEqual(
            self.common.selection(replace(self.spec, flags=("--title", "-c")), LOCAL_CONTEXT), ()
        )

    def test_a_plugin_is_listed_in_the_inline_config(self):
        plugin = "file:///opt/plugin.js"
        env = self.common.environment(self.spec, LOCAL_CONTEXT, plugin=plugin)
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["plugin"], [plugin])
        plain = self.common.environment(self.spec, LOCAL_CONTEXT)
        self.assertNotIn("plugin", json.loads(plain["OPENCODE_CONFIG_CONTENT"]))

    def test_container_loads_image_skills_and_host_has_no_image_path(self):
        container = self.common.inline(self.spec, DOCKER_CONTEXT)
        self.assertEqual(
            container["skills"],
            {"paths": ["/opt/ensemble/plugins", "/opt/windroc/mini-op/skills"]},
        )
        self.assertNotIn("skills", self.common.inline(self.spec, LOCAL_CONTEXT))


class CompactionTest(Case):
    def setUp(self):
        cwd = os.path.realpath(self.home)
        self.spec = Spec(workdir=self.home, access=Access(cwd=cwd, project=True), sid="run-1")

    def inline(self, **config) -> dict:
        found = common(**config)
        return json.loads(found.environment(self.spec, LOCAL_CONTEXT)["OPENCODE_CONFIG_CONTENT"])

    def limits(self, config: dict, provider: str) -> dict:
        return {
            name: model["limit"] for name, model in config["provider"][provider]["models"].items()
        }

    def test_each_model_compacts_its_headroom_below_its_window(self):
        config = self.inline(
            providers={"strata": {**STRATA, "context": 204800}, "lab": STRATA},
            tiers={"1": "strata/big", "2": "lab/n"},
        )
        # OpenCode compacts once a step reaches limit.input - compaction.reserved.
        self.assertEqual(config["compaction"], {"reserved": 0})
        self.assertEqual(
            self.limits(config, "ollama"),
            {"qwen3.6:35b-mlx": {"context": 65536, "input": 32768, "output": 16384}},
        )
        big = {"context": 204800, "input": 152800, "output": 32000}
        self.assertEqual(self.limits(config, "strata"), {"m": big, "big": big})
        small = {"context": 65536, "input": 32768, "output": 16384}
        self.assertEqual(self.limits(config, "lab"), {"m": small, "n": small})

    def test_the_headroom_is_the_reply_and_20000_at_most_half_the_window(self):
        for window, threshold, output in (
            (65536, 32768, 16384),
            (104000, 58000, 26000),
            (131072, 79072, 32000),
            (20000, 10000, 5000),
        ):
            with self.subTest(window=window):
                config = self.inline(ollama_context=window)
                self.assertEqual(
                    self.limits(config, "ollama")["qwen3.6:35b-mlx"],
                    {"context": window, "input": threshold, "output": output},
                )

    def test_a_window_must_be_a_positive_integer(self):
        for window in (True, 0, -1, 1.5, "65536"):
            with self.subTest(window=window):
                with self.assertRaisesRegex(ConfigError, r"ollama_context must be"):
                    common(ollama_context=window)
                with self.assertRaisesRegex(ConfigError, r"\.providers must be"):
                    common(providers={"strata": {**STRATA, "context": window}})
        self.assertEqual(common(ollama_context=8192).ollama_context, 8192)

    def test_only_a_trailing_line_end_is_cut_from_the_toplevel(self):
        repo = os.path.realpath(os.path.join(self.home, "repo "))
        os.makedirs(os.path.join(repo, ".git"))

        def git(argv, *_, **__):
            out = repo + "\r\n" if "--show-toplevel" in argv else ".git\n.git\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        with mock.patch("subprocess.run", side_effect=git):
            self.assertEqual(worktree(repo), repo)


class UsageTest(Case):
    def test_usage_selects_only_the_default_models_provider(self):
        for model, tier_model in (
            ("strata/default", "ollama/local"),
            ("ollama/local", "strata/remote"),
        ):
            client = OpenCodeClient(
                {
                    **packaged_table(),
                    "model": model,
                    "tiers": {"1": tier_model, "2": tier_model},
                    "providers": {"strata": {"base_url": "https://strata.test/v1", "models": []}},
                }
            )
            with self.subTest(model=model):
                request = client.common.usage_provider()
                if model.startswith("strata/"):
                    assert request is not None
                    self.assertEqual(
                        (request.provider, request.base_url), ("strata", "https://strata.test/v1")
                    )
                    self.assertEqual(
                        client.usage_probe().argv[-3:],
                        ("--provider", "strata", "https://strata.test/v1"),
                    )
                else:
                    self.assertIsNone(request)
                    self.assertEqual(client.usage_probe().argv[-1], "11434")
        self.assertEqual(client.usage('{"available": true}'), Usage(None, None))
        for bad in ('{"available": false}', '{"available": 1}', '{"available": true, "key": "x"}'):
            self.assertIsNone(client.usage(bad))

    def test_the_probe_asks_the_hosts_ollama_on_its_port(self):
        client = OpenCodeClient({**packaged_table(), "ollama_port": 11500, "env": {"A": "b"}})
        launch = client.usage_probe()
        assert launch is not None
        self.assertEqual(
            launch.argv, (os.path.realpath(sys.executable), "-I", probe.__file__, "11500")
        )
        self.assertEqual(launch.env, {"A": "b"})

    def test_a_listing_is_unlimited_usage_anything_else_unknown(self):
        client = OpenCodeClient(packaged_table())
        self.assertEqual(client.usage('{"models": []}'), Usage(None, None))
        self.assertEqual(
            client.usage('{"models": [{"name": "qwen3.6:35b-mlx"}]}'), Usage(None, None)
        )
        for output in ("null", "", "nope", "[]", '{"models": {}}', '{"error": "x"}'):
            with self.subTest(output=output):
                self.assertIsNone(client.usage(output))
