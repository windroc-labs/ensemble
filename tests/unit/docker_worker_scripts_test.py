import errno
import hashlib
import http.client
import io
import json
import os
import runpy
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.session.clients.opencode.common import HOME
from tests.case import REPO, Case

ASSETS = Path(REPO, "src/broker/internal/launcher/assets")


def script(name):
    return runpy.run_path(str(ASSETS / name), run_name="docker_worker_script")


class Bootstrap(Case):
    def setUp(self):
        self.bootstrap = script("bootstrap.py")
        self.templates = Path(self.home, "templates")
        self.templates.mkdir()
        (self.templates / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        (self.templates / "settings.json").write_text("{}")
        self.target = Path(self.home, "claude")
        self.target.mkdir()

    def test_bootstrap_claude_copies_templates_and_trusts_cwd(self):
        self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        config = json.loads((self.target / ".claude.json").read_text())
        self.assertEqual(config["hasCompletedOnboarding"], True)
        self.assertEqual(config["projects"]["/work/project"], {"hasTrustDialogAccepted": True})
        self.assertEqual((self.target / "settings.json").read_text(), "{}")

    def test_bootstrap_claude_keeps_existing_files_and_keys(self):
        (self.target / "settings.json").write_text('{"mine": 1}')
        existing = {"theme": "light", "projects": {"/other": {"x": 1}}}
        (self.target / ".claude.json").write_text(json.dumps(existing))
        self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        config = json.loads((self.target / ".claude.json").read_text())
        self.assertEqual(config["theme"], "light")
        self.assertNotIn("hasCompletedOnboarding", config)
        self.assertEqual(config["projects"]["/other"], {"x": 1})
        self.assertTrue(config["projects"]["/work/project"]["hasTrustDialogAccepted"])
        self.assertEqual((self.target / "settings.json").read_text(), '{"mine": 1}')

    def test_bootstrap_claude_refuses_symlinked_config(self):
        elsewhere = Path(self.home, "elsewhere.json")
        elsewhere.write_text("{}")
        (self.target / ".claude.json").symlink_to(elsewhere)
        with self.assertRaises(SystemExit):
            self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        self.assertEqual(elsewhere.read_text(), "{}")

    def test_template_has_onboarding_and_theme(self):
        template = json.loads((ASSETS / "claude.json").read_text())
        self.assertTrue(template["hasCompletedOnboarding"] is True)
        self.assertTrue(template["theme"])


class CodexBootstrap(Case):
    def setUp(self):
        self.code = script("bootstrap.py")
        self.target = Path(self.home, "codex")
        self.target.mkdir()
        self.secrets = Path(self.home, "secrets")
        self.secrets.mkdir()
        secrets = mock.patch.dict(self.code["inject_codex"].__globals__, {"SECRETS": self.secrets})
        secrets.start()
        self.addCleanup(secrets.stop)
        self.plugins = Path(self.home, "image-plugins")
        (self.plugins / "plugins/cache").mkdir(parents=True)
        (self.plugins / "config.toml").write_text("")

    def test_codex_home_has_file_auth_trusted_project_and_only_a_secret_link(self):
        import tomllib

        self.code["bootstrap_codex"](self.target, '/work/"project', self.secrets, self.plugins)
        config = tomllib.loads((self.target / "config.toml").read_text())
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(config["projects"]['/work/"project']["trust_level"], "trusted")
        self.assertTrue((self.target / "auth.json").is_symlink())
        self.assertEqual((self.target / "auth.json").readlink(), self.secrets / "codex/auth.json")
        self.assertNotIn("plugins", config)

    def test_codex_plugins_use_the_image_cache_and_enable_the_native_ids(self):
        import tomllib

        for name in ("beta", "updraft"):
            (self.plugins / "plugins/cache/windroc" / name / "1.0.0").mkdir(parents=True)
        (self.plugins / "config.toml").write_text(
            '[plugins."updraft@windroc"]\nenabled = true\n'
            '[plugins."beta@windroc"]\nenabled = true\n'
            '[marketplaces.private]\nsource = "ignored"\n'
        )
        self.code["bootstrap_codex"](self.target, "/work/project", self.secrets, self.plugins)
        config = tomllib.loads((self.target / "config.toml").read_text())
        self.assertEqual(
            config["plugins"],
            {
                "updraft@windroc": {"enabled": True},
                "beta@windroc": {"enabled": True},
            },
        )
        cache = self.target / "plugins/cache"
        self.assertTrue(cache.is_symlink())
        self.assertEqual(cache.readlink(), self.plugins / "plugins/cache")
        self.assertFalse((cache / "windroc/updraft/1.0.0").is_symlink())
        self.assertNotIn("marketplaces", config)
        self.assertFalse((self.target / "sessions").exists())

    def carrier(self, access="one", account="account"):
        return json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "identity",
                    "access_token": access,
                    "refresh_token": "",
                    "account_id": account,
                },
                "last_refresh": "2026-10-18T12:00:00Z",
            }
        ).encode()

    def test_access_document_is_owner_read_only_and_atomically_replaced(self):
        inject = self.code["inject_codex"]
        self.code["bootstrap_codex"](self.target, "/work/project", self.secrets, self.plugins)
        inject(io.BytesIO(self.carrier()), self.secrets, self.target)
        path = self.target / "auth.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.readlink(), self.secrets / "codex/auth.json")
        self.assertEqual((self.secrets / "codex/auth.json").read_bytes(), self.carrier())
        self.assertEqual(path.stat().st_uid, os.getuid())
        path.unlink()
        path.write_bytes(self.carrier("native"))
        inject(io.BytesIO(self.carrier("two")), self.secrets, self.target)
        self.assertEqual(path.readlink(), self.secrets / "codex/auth.json")
        self.assertEqual(path.read_bytes(), self.carrier("two"))
        self.assertEqual(
            sorted(p.name for p in path.parent.iterdir()), ["auth.json", "config.toml", "plugins"]
        )

    def test_refresh_reaches_native_replaced_effective_file(self):
        path = self.target / "auth.json"
        path.write_bytes(self.carrier("native"))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.target)}):
            self.code["inject_codex"](io.BytesIO(self.carrier("renewed")), self.secrets)
        self.assertEqual(path.read_bytes(), self.carrier("renewed"))

    def managed(self):
        self.code["bootstrap_codex"](self.target, "/work/project", self.secrets, self.plugins)
        self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        return self.target / "auth.json", self.secrets / "codex/auth.json"

    def assert_only(self, directory, *names):
        self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted(names))

    def test_refresh_writes_secret_and_keeps_link_without_codex_home_space(self):
        link, secret = self.managed()
        mkstemp, symlink = tempfile.mkstemp, os.symlink
        full = OSError(errno.ENOSPC, "No space left on device")

        def staged(*args, **kwargs):
            if Path(kwargs["dir"]) == self.target:
                raise full
            return mkstemp(*args, **kwargs)

        def linked(source, destination, *args, **kwargs):
            if Path(destination).parent == self.target:
                raise full
            return symlink(source, destination, *args, **kwargs)

        with (
            mock.patch("tempfile.mkstemp", side_effect=staged),
            mock.patch("os.symlink", side_effect=linked),
        ):
            self.code["inject_codex"](
                io.BytesIO(self.carrier("renewed")), self.secrets, self.target
            )
        self.assertEqual(link.readlink(), secret)
        self.assertEqual(secret.read_bytes(), self.carrier("renewed"))
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o400)
        self.assert_only(self.target, "auth.json", "config.toml", "plugins")
        self.assert_only(secret.parent, "auth.json")

    def test_refresh_after_codex_replaced_link_restores_it(self):
        link, secret = self.managed()
        native = json.loads(self.carrier("native"))
        native["agent_identity"] = {"agent_runtime_id": "runtime"}
        link.unlink()
        link.write_text(json.dumps(native, indent=2))
        link.chmod(0o600)
        self.code["inject_codex"](io.BytesIO(self.carrier("renewed")), self.secrets, self.target)
        self.assertEqual(link.readlink(), secret)
        self.assertEqual(secret.read_bytes(), self.carrier("renewed"))
        self.assert_only(self.target, "auth.json", "config.toml", "plugins")
        self.assert_only(secret.parent, "auth.json")

    def test_refresh_after_codex_removed_link_restores_it(self):
        link, secret = self.managed()
        link.unlink()
        self.code["inject_codex"](io.BytesIO(self.carrier("renewed")), self.secrets, self.target)
        self.assertEqual(link.readlink(), secret)
        self.assertEqual(secret.read_bytes(), self.carrier("renewed"))
        self.assert_only(self.target, "auth.json", "config.toml", "plugins")

    def test_auth_rejects_unsafe_destinations_with_distinct_messages(self):
        link, secret = self.managed()
        inject = self.code["inject_codex"]

        def rejects(message, secrets=self.secrets):
            with self.assertRaisesRegex(SystemExit, f"^bootstrap: {message}$"):
                inject(io.BytesIO(self.carrier("two")), secrets, self.target)

        secret.chmod(0o600)
        secret.write_bytes(self.carrier(account="other"))
        rejects("existing Codex credentials belong to another account")
        secret.write_bytes(b"{")
        rejects("existing Codex credentials are unreadable")
        secret.write_bytes(json.dumps({"tokens": None}).encode())
        rejects("existing Codex credentials are unreadable")
        secret.unlink()
        secret.mkdir()
        rejects("managed Codex credential is not a regular file")
        secret.rmdir()
        link.unlink()
        link.symlink_to(Path(self.home, "unrelated"))
        rejects("CODEX_HOME/auth.json links elsewhere")
        link.unlink()
        os.mkfifo(link)
        rejects("CODEX_HOME/auth.json is not a regular file")
        link.unlink()
        linked = Path(self.home, "linked-secrets")
        linked.symlink_to(self.secrets)
        rejects("managed Codex secret directory is a link", linked)
        shutil.rmtree(secret.parent)
        secret.parent.symlink_to(Path(self.home))
        rejects("managed Codex secret directory is a link")
        self.assertFalse(os.path.lexists(link))
        self.assertFalse(Path(self.home, "auth.json").exists())

    def test_write_failure_exits_with_reason_and_cleans_up(self):
        link, secret = self.managed()
        with (
            mock.patch("os.replace", side_effect=OSError(28, "No space left on device")),
            self.assertRaisesRegex(
                SystemExit,
                "^bootstrap: cannot write managed Codex credentials: No space left on device$",
            ),
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier("two")), self.secrets, self.target)
        self.assertEqual(secret.read_bytes(), self.carrier())
        self.assert_only(secret.parent, "auth.json")
        self.assert_only(self.target, "auth.json", "config.toml", "plugins")

    def test_auth_link_ignores_preexisting_directories(self):
        old = self.target / f".auth-link-{os.getpid()}"
        old.mkdir()
        collision = self.target / (".auth-link-" + "00" * 16)
        collision.mkdir()
        with mock.patch("os.urandom", side_effect=[bytes(16), b"a" * 16]):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        self.assertEqual((self.target / "auth.json").readlink(), self.secrets / "codex/auth.json")
        self.assertTrue(old.is_dir())
        self.assertTrue(collision.is_dir())
        self.assert_only(self.target, "auth.json", old.name, collision.name)

    def test_failed_auth_link_creation_leaves_preexisting_directory_alone(self):
        collision = self.target / (".auth-link-" + "00" * 16)
        collision.mkdir()
        with (
            mock.patch("os.urandom", return_value=bytes(16)),
            mock.patch("os.symlink", side_effect=OSError(errno.EIO, "Input/output error")),
            self.assertRaisesRegex(
                SystemExit,
                "^bootstrap: cannot write managed Codex credentials: Input/output error$",
            ),
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        self.assertTrue(collision.is_dir())
        self.assert_only(self.target, collision.name)
        self.assert_only(self.secrets / "codex", "auth.json")

    def test_failed_auth_link_replace_cleans_link_and_preserves_native_file(self):
        link = self.target / "auth.json"
        link.write_bytes(self.carrier("native"))
        replace = os.replace

        def fail_link(source, destination):
            if destination == link:
                raise OSError(errno.EIO, "Input/output error")
            return replace(source, destination)

        with (
            mock.patch("os.replace", side_effect=fail_link),
            self.assertRaisesRegex(
                SystemExit,
                "^bootstrap: cannot write managed Codex credentials: Input/output error$",
            ),
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        self.assertEqual(link.read_bytes(), self.carrier("native"))
        self.assert_only(self.target, "auth.json")
        self.assert_only(self.secrets / "codex", "auth.json")

    def test_auth_cleanup_does_not_mask_write_failure(self):
        unlink = Path.unlink
        cleaned = []

        def fail_cleanup(path, *args, **kwargs):
            if path.name.startswith(".auth-"):
                cleaned.append(path)
                raise OSError(errno.EACCES, "Permission denied")
            return unlink(path, *args, **kwargs)

        def fail_replace(source, destination):
            if destination == self.target / "auth.json":
                raise OSError(errno.EIO, "Input/output error")
            return replace(source, destination)

        replace = os.replace
        with (
            mock.patch("os.replace", side_effect=fail_replace),
            mock.patch.object(Path, "unlink", fail_cleanup),
            self.assertRaisesRegex(
                SystemExit,
                "^bootstrap: cannot write managed Codex credentials: Input/output error$",
            ),
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        self.assertEqual(len(cleaned), 2)
        self.assertTrue(cleaned[1].is_symlink())

    def test_auth_cleanup_does_not_turn_success_into_failure(self):
        with mock.patch.object(
            Path, "unlink", side_effect=OSError(errno.EACCES, "Permission denied")
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        self.assertEqual((self.target / "auth.json").readlink(), self.secrets / "codex/auth.json")
        self.assert_only(self.target, "auth.json")

    def test_auth_rejects_account_change_secrets_and_arbitrary_links(self):
        inject = self.code["inject_codex"]
        path = self.target / "auth.json"
        path.write_bytes(self.carrier())
        for content in (
            self.carrier(account="other"),
            self.carrier().replace(b'"refresh_token": ""', b'"refresh_token": "secret"'),
        ):
            with self.assertRaises(SystemExit):
                inject(io.BytesIO(content), self.secrets, self.target)
            self.assertEqual(path.read_bytes(), self.carrier())
        elsewhere = Path(self.home, "unrelated")
        elsewhere.write_bytes(self.carrier())
        path.unlink()
        path.symlink_to(elsewhere)
        with self.assertRaises(SystemExit):
            inject(io.BytesIO(self.carrier("two")), self.secrets, self.target)
        self.assertEqual(elsewhere.read_bytes(), self.carrier())

    def test_auth_rejects_malformed_documents_without_leaking_contents(self):
        for raw in (
            b"[]",
            b"{}",
            b"not-json",
            b"x" * ((1 << 16) + 1),
            self.carrier().replace(
                b'"refresh_token": ""', b'"refresh_token": "secret", "refresh_token": ""'
            ),
        ):
            with (
                self.subTest(length=len(raw)),
                self.assertRaisesRegex(SystemExit, "^bootstrap: invalid managed"),
            ):
                self.code["inject_codex"](io.BytesIO(raw), self.secrets, self.target)
        self.assertFalse((self.target / "auth.json").exists())

    def test_auth_rejects_missing_home_and_special_destinations(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets)
        path = self.target / "auth.json"
        path.mkdir()
        with self.assertRaises(SystemExit):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, self.target)
        with self.assertRaises(SystemExit):
            self.code["inject_codex"](io.BytesIO(self.carrier()), self.secrets, Path("relative"))

    def test_refresh_rejects_effective_home_different_from_manifest(self):
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": "/different"}),
            self.assertRaisesRegex(SystemExit, "CODEX_HOME does not match"),
        ):
            self.code["main"](["codex-auth", str(self.target)])
        self.assertFalse((self.target / "auth.json").exists())

    def test_refresh_accepts_matching_home_and_cleans_failed_staging(self):
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.target)}),
            mock.patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(self.carrier()))),
        ):
            self.code["main"](["codex-auth", str(self.target)])
        path = self.target / "auth.json"
        self.assertEqual(path.read_bytes(), self.carrier())
        with (
            mock.patch("os.replace", side_effect=OSError("failed")),
            self.assertRaisesRegex(
                SystemExit, "^bootstrap: cannot write managed Codex credentials"
            ),
        ):
            self.code["inject_codex"](io.BytesIO(self.carrier("two")), self.secrets, self.target)
        self.assertEqual(path.read_bytes(), self.carrier())
        self.assertEqual([p.name for p in self.target.iterdir()], ["auth.json"])
        self.assertEqual([p.name for p in (self.secrets / "codex").iterdir()], ["auth.json"])

    def test_projection_writes_one_record_and_rejects_escape_or_links(self):
        stage = self.code["stage_runtime"]
        root = Path(self.home, "run")
        root.mkdir()
        destination = root / "codex-store" / ("a" * 64 + ".json")
        stage(io.StringIO(json.dumps({str(destination): "{}"})), root)
        self.assertEqual(destination.read_text(), "{}")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        for files in (
            {},
            {str(root / "secrets/x"): "{}"},
            {str(destination): 1},
            {str(destination): "a" * ((1 << 20) + 1)},
        ):
            with self.subTest(paths=list(files)), self.assertRaises(SystemExit):
                stage(io.StringIO(json.dumps(files)), root)
        linked = root / "linked-store"
        linked.symlink_to(destination.parent, target_is_directory=True)
        with self.assertRaises(SystemExit):
            stage(io.StringIO(json.dumps({str(linked / ("b" * 64 + ".json")): "{}"})), root)


class CodexCIGuard(Case):
    def test_tool_probe_creates_private_home_before_starting_codex(self):
        worker = runpy.run_path(str(Path(REPO, ".github/worker_codex.py")))
        active = {}

        def server(_endpoint, handler):
            active["handler"] = handler
            return mock.Mock(server_port=1234)

        def run(_command, **kwargs):
            self.assertTrue(Path(kwargs["env"]["CODEX_HOME"]).is_dir())
            marker = (Path(kwargs["cwd"]) / "input.txt").read_text()
            for body in (
                {"input": []},
                {"input": [{"type": "custom_tool_call_output", "output": marker}]},
            ):
                data = json.dumps(body).encode()
                handler = SimpleNamespace(
                    headers={"Content-Length": str(len(data))},
                    rfile=io.BytesIO(data),
                    wfile=io.BytesIO(),
                    send_response=mock.Mock(),
                    send_header=mock.Mock(),
                    end_headers=mock.Mock(),
                )
                active["handler"].do_POST(handler)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with (
            mock.patch("http.server.HTTPServer", side_effect=server),
            mock.patch("threading.Thread"),
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("builtins.print"),
        ):
            runpy.run_path(str(worker["PROBES"] / "tools.py"))

    def test_ci_probe_refuses_local_execution_and_untrusted_image_without_docker(self):
        main = runpy.run_path(str(Path(REPO, ".github/worker_codex.py")))["main"]
        for ci, image in (("false", "ensemble-worker:" + "a" * 64), ("true", "untrusted")):
            with (
                self.subTest(ci=ci),
                mock.patch.dict(os.environ, {"CI": ci, "GITHUB_ACTIONS": ci}),
                mock.patch("subprocess.run") as run,
                self.assertRaises(SystemExit),
            ):
                main(image)
            run.assert_not_called()

    def test_image_must_hold_only_the_pinned_codex_package(self):
        once = runpy.run_path(str(Path(REPO, ".github/worker_codex.py")))["single_installation"]
        pinned = b"/opt/ensemble/clients/lib/node_modules/@openai/codex\n"
        for found, good in (
            (pinned, True),
            (pinned + b"/home/agent/.npm-global/lib/node_modules/@openai/codex\n", False),
            (b"", False),
        ):
            result = SimpleNamespace(returncode=0, stdout=found, stderr=b"")
            with (
                self.subTest(found=found),
                mock.patch("subprocess.run", return_value=result) as run,
            ):
                if good:
                    once("ensemble-worker:" + "a" * 64)
                else:
                    with self.assertRaisesRegex(RuntimeError, "@openai/codex packages"):
                        once("ensemble-worker:" + "a" * 64)
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["docker", "run"])
            self.assertEqual(command[command.index("--user") + 1], "root")
            self.assertEqual(
                command[-8:],
                [
                    "find",
                    "/",
                    "-xdev",
                    "-type",
                    "d",
                    "-path",
                    "*/node_modules/@openai/codex",
                    "-prune",
                ],
            )


class CodexCIReload(Case):
    def setUp(self):
        self.worker = runpy.run_path(str(Path(REPO, ".github/worker_codex.py")))

    def reload_code(self):
        return runpy.run_path(str(self.worker["PROBES"] / "reload.py"), run_name="fake_ci_probe")

    @staticmethod
    def status(identifier, token):
        return {
            "id": identifier,
            "result": {"authMethod": "chatgpt", "authToken": token, "requiresOpenaiAuth": True},
        }

    def exchange(self, publish=None):
        code = self.reload_code()
        return code["ReloadExchange"]("old-secret", "new-secret", publish or mock.Mock())

    def test_reload_proves_replacement_and_adoption_are_separate_on_one_connection(self):
        publish = mock.Mock()
        exchange = self.exchange(publish)
        self.assertEqual(json.loads(exchange.start())["method"], "initialize")
        initialized, initial = exchange.accept({"id": 1, "result": {}})
        self.assertEqual(json.loads(initialized)["method"], "initialized")
        self.assertEqual(json.loads(initial)["params"]["refreshToken"], False)
        self.assertEqual(json.loads(exchange.accept(self.status(2, "old-secret"))[0])["id"], 3)
        publish.assert_called_once_with()
        refresh = json.loads(exchange.accept(self.status(3, "old-secret"))[0])
        self.assertEqual(refresh["method"], "getAuthStatus")
        self.assertEqual(refresh["params"], {"includeToken": True, "refreshToken": True})
        self.assertEqual(json.loads(exchange.accept(self.status(4, "new-secret"))[0])["id"], 5)
        self.assertEqual(exchange.accept(self.status(5, "new-secret")), [])
        self.assertTrue(exchange.complete)
        self.assertNotIn("secret", json.dumps(exchange.summary()))
        self.assertEqual(exchange.summary()["guarded_reload_completed"], True)

    def test_reload_rejects_protocol_errors_and_never_reports_token_values(self):
        for message in (
            None,
            [],
            {"id": "1", "result": {}},
            {"id": 9, "result": {}},
            {"id": 1, "error": {"message": "secret"}},
            {"id": 1, "result": []},
            {"id": 7, "method": "secret"},
        ):
            with self.subTest(message=message):
                exchange = self.exchange()
                with self.assertRaisesRegex(ValueError, "synthetic native reload failed"):
                    exchange.accept(message)
                self.assertFalse(exchange.complete)

    def test_reload_requires_old_cache_before_trigger_and_new_cache_after_it(self):
        for stage, token in ((2, "new-secret"), (3, "new-secret"), (5, "old-secret")):
            with self.subTest(stage=stage):
                exchange = self.exchange()
                exchange.stage = stage
                with self.assertRaisesRegex(ValueError, "synthetic native reload failed"):
                    exchange.accept(self.status(stage, token))
                self.assertFalse(exchange.complete)
        exchange = self.exchange()
        exchange.stage = 4
        with self.assertRaises(ValueError):
            exchange.accept({"id": 4, "result": {"account": {"type": "apiKey"}}})

    def test_reload_bounds_protocol_lines_total_output_and_notifications(self):
        code = self.reload_code()
        for chunks in ([b"x" * 65537], [b"\n" * (1024 * 1024 + 1)], [b"{bad}\n"]):
            with self.subTest(length=sum(map(len, chunks))):
                lines = code["BoundedLines"]()
                with self.assertRaises(ValueError):
                    for chunk in chunks:
                        lines.feed(chunk)
        lines = code["BoundedLines"]()
        lines.feed(b'{"id":1')
        with self.assertRaises(ValueError):
            lines.finish()
        exchange = self.exchange()
        for _ in range(1024):
            self.assertEqual(exchange.accept({"method": "notice"}), [])
        with self.assertRaises(ValueError):
            exchange.accept({"method": "notice"})

    def test_reload_cleanup_closes_streams_and_kills_its_process_group(self):
        code = self.reload_code()
        process = mock.Mock(pid=1234)
        process.wait.side_effect = [code["subprocess"].TimeoutExpired("native", 2), 0]
        with (
            mock.patch.object(code["os"], "killpg") as killpg,
            mock.patch.object(code["time"], "sleep"),
        ):
            code["stop_native"](process)
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(1234, code["signal"].SIGTERM), mock.call(1234, code["signal"].SIGKILL)],
        )
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called()

    def test_reload_retains_image_node_path_without_personal_environment(self):
        code = self.reload_code()
        documents = [
            json.loads(document)
            for _ in range(2)
            for document in self.worker["synthetic_credentials"]()
        ]
        with (
            mock.patch.dict(os.environ, {"PATH": "/personal/bin", "NODE_OPTIONS": "private"}),
            mock.patch("runpy.run_path", return_value={"inject_codex": mock.Mock()}),
            mock.patch("subprocess.Popen", side_effect=FileNotFoundError("fake start")) as start,
            self.assertRaises(FileNotFoundError),
        ):
            code["run_reload"](Path(self.home), *documents)
        environment = start.call_args.kwargs["env"]
        self.assertIn("/usr/local/bin", environment["PATH"].split(":"))
        self.assertNotIn("/personal/bin", environment["PATH"])
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertEqual(environment["HOME"], self.home)
        self.assertEqual(environment["CODEX_HOME"], self.home)

    def test_ci_generates_distinct_managed_synthetic_values_without_date_changes(self):
        self.assertTrue("synthetic_credentials" in self.worker)
        first, second = self.worker["synthetic_credentials"]()
        documents = [json.loads(value) for value in (first, second)]
        for document in documents:
            self.assertEqual(document["auth_mode"], "chatgpt")
            self.assertEqual(document["tokens"]["refresh_token"], "")
            self.assertEqual(document["tokens"]["account_id"], "ci-fake")
            self.assertIn("last_refresh", document)
        self.assertNotEqual(
            documents[0]["tokens"]["access_token"], documents[1]["tokens"]["access_token"]
        )
        self.assertEqual(documents[0]["last_refresh"], documents[1]["last_refresh"])
        self.assertIn("0.163.0", (ASSETS / "Dockerfile").read_text())

    def test_reload_natural_auth_adopts_replacement_after_cached_near_expiry_value(self):
        code = self.reload_code()
        publish_near, publish_final = mock.Mock(), mock.Mock()
        exchange = code["ReloadExchange"](
            "old-secret",
            "new-secret",
            mock.Mock(),
            proactive={
                "token": "natural-secret",
                "near": "near-secret",
                "publish_near": publish_near,
                "publish_final": publish_final,
            },
        )
        for response in (
            {"id": 1, "result": {}},
            self.status(2, "old-secret"),
            self.status(3, "old-secret"),
            self.status(4, "new-secret"),
        ):
            exchange.accept(response)
        load = json.loads(exchange.accept(self.status(5, "new-secret"))[0])
        publish_near.assert_called_once_with()
        self.assertEqual(load["method"], "getAuthStatus")
        self.assertEqual(load["params"], {"includeToken": True, "refreshToken": True})
        natural = json.loads(exchange.accept(self.status(6, "near-secret"))[0])
        publish_final.assert_called_once_with()
        self.assertEqual(natural["method"], "getAuthStatus")
        self.assertEqual(natural["params"]["refreshToken"], False)
        exchange.accept(self.status(7, "natural-secret"))
        self.assertTrue(exchange.complete)
        self.assertTrue(exchange.summary()["natural_auth_reload_completed"])

    def test_ci_entrypoint_injects_managed_values_and_runs_reload_with_network_disabled(self):
        calls, active, copied = [], {}, []
        version = b"codex-cli 0.163.0"

        def docker(*args, data=None):
            calls.append((args, data))
            if args[:2] == ("image", "inspect"):
                return json.dumps(
                    [
                        {
                            "Architecture": "amd64",
                            "Size": 4620000000,
                            "Config": {
                                "Labels": {
                                    "ensemble.agent-uid": "1000",
                                    "ensemble.agent-gid": "1000",
                                }
                            },
                        }
                    ]
                ).encode()
            if args[0] == "run" and "find" in args:
                return b"/opt/ensemble/clients/lib/node_modules/@openai/codex\n"
            if args[0] == "run":
                active["home"] = Path(
                    next(
                        arg.removeprefix("CODEX_HOME=")
                        for arg in args
                        if arg.startswith("CODEX_HOME=")
                    )
                )
            elif args[0] == "inspect":
                sessions = str(active["home"] / "sessions")
                return json.dumps(
                    [
                        {
                            "Mounts": [
                                {"Type": "bind", "Source": sessions, "Destination": sessions}
                            ],
                            "HostConfig": {
                                "Tmpfs": {
                                    str(active["home"]): "",
                                    "/run/ensemble": "",
                                    "/run/ensemble/secrets": "",
                                }
                            },
                        }
                    ]
                ).encode()
            elif args[-1] == "--version":
                return version
            elif args[0] == "cp":
                copied.append(Path(args[1]).read_bytes())
            elif data == (self.worker["PROBES"] / "reload.py").read_bytes():
                payload = json.loads(args[-1])
                self.assertEqual(set(payload), {"initial", "replacement", "near", "adopted"})
                self.assertTrue(
                    all(document["auth_mode"] == "chatgpt" for document in payload.values())
                )
                return b"Synthetic same-process guarded reload: PASS\n"
            elif data == (self.worker["PROBES"] / "tools.py").read_bytes():
                return b"Offline Codex code-mode command turn: PASS\n"
            return b""

        main = self.worker["main"]
        with (
            mock.patch.dict(
                os.environ, {"CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_TEMP": self.home}
            ),
            mock.patch.dict(
                main.__globals__,
                {"docker": docker, "latest_runtime": lambda *_: ("0.163.0", b"verified-bundle")},
            ),
            mock.patch("builtins.print"),
        ):
            main("ensemble-worker:" + "a" * 64)
        starts = [args for args, _ in calls if args[0] == "run" and "--detach" in args]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len([args for args, _ in calls if "find" in args]), 1)
        self.assertTrue(all(args[args.index("--network") + 1] == "none" for args in starts))
        injected = [(args, data) for args, data in calls if "codex-auth" in args]
        self.assertEqual(len(injected), 2)
        self.assertTrue(all(args[-1].endswith("/codex") for args, _ in injected))
        for program, count in (
            ("reload.py", 2),
            ("tools.py", 4),
            ("skills.py", 2),
            ("state.py", 2),
            ("stage.py", 2),
        ):
            source = (self.worker["PROBES"] / program).read_bytes()
            sent = [args for args, data in calls if data == source]
            self.assertEqual(len(sent), count, program)
            self.assertTrue(all("-i" in args and "-" in args and "-c" not in args for args in sent))
        self.assertEqual(copied, [b"verified-bundle"] * 2)

    def test_reload_cleanup_still_kills_group_after_broken_input(self):
        code = self.reload_code()
        process = mock.Mock(pid=1234)
        process.stdin.close.side_effect = BrokenPipeError("secret")
        with (
            mock.patch.object(code["os"], "killpg") as killpg,
            mock.patch.object(code["time"], "sleep"),
        ):
            code["stop_native"](process)
        self.assertEqual(killpg.call_count, 2)
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()


class CodexUpdate(Case):
    def setUp(self):
        self.code = script("codex-update.py")
        self.update = self.code["update"]
        self.target = Path(self.home, "runtime")
        self.target.mkdir()

    def bundle(self, files=None):
        if files is None:
            files = {
                "bin/codex": b"native",
                "bin/codex-code-mode-host": b"host",
                "lib/data": b"data",
            }
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo("package/vendor/aarch64-unknown-linux-musl/" + name)
                entry.size, entry.mode = len(data), 0o755
                archive.addfile(entry, io.BytesIO(data))
        return stream.getvalue()

    def call(self, action, data=None):
        real_lstat = Path.lstat

        def root_owned(path):
            info = real_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size)

        with mock.patch.object(Path, "lstat", root_owned):
            self.update(action, io.BytesIO(self.bundle() if data is None else data), self.target)

    def test_stage_publish_reset(self):
        self.call("stage")
        stage, active = self.target / "codex.next", self.target / "codex"
        self.assertEqual((stage / "bin/codex").read_bytes(), b"native")
        self.assertEqual(stat.S_IMODE((stage / "bin/codex").stat().st_mode), 0o755)
        self.assertEqual((stage / "bin/codex-code-mode-host").read_bytes(), b"host")
        self.assertEqual((stage / "lib/data").read_bytes(), b"data")
        self.assertFalse(active.exists())
        self.call("publish")
        self.assertEqual((active / "bin/codex").read_bytes(), b"native")
        self.assertFalse(stage.exists())
        self.call("reset")
        self.call("reset")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_refuses_empty_unknown_and_unsafe_paths(self):
        with self.assertRaises(SystemExit):
            self.call("stage", b"")
        self.call("reset")
        with self.assertRaises(SystemExit):
            self.call("unknown")
        self.target.chmod(0o777)
        with self.assertRaises(SystemExit):
            self.call("stage")
        self.target.chmod(0o755)
        elsewhere = self.target / "elsewhere"
        elsewhere.write_bytes(b"untouched")
        (self.target / "codex.next").symlink_to(elsewhere)
        with self.assertRaises(FileExistsError):
            self.call("stage")
        with self.assertRaises(SystemExit):
            self.call("publish")
        self.call("reset")
        self.assertEqual(elsewhere.read_bytes(), b"untouched")

    def test_incomplete_bundle_never_becomes_active(self):
        for files in (
            {"bin/codex": b"native"},
            {"bin/codex-code-mode-host": b"host"},
            {"bin/codex": b"", "bin/codex-code-mode-host": b"host"},
        ):
            with self.subTest(files=files), self.assertRaises(SystemExit):
                self.call("stage", self.bundle(files))
            self.assertFalse((self.target / "codex").exists())
            self.call("reset")

    def test_publish_rechecks_every_file_and_keeps_active_on_failure(self):
        self.call("stage")
        self.call("publish")
        self.call("stage")
        companion = self.target / "codex.next/bin/codex-code-mode-host"
        companion.chmod(0o777)
        with self.assertRaises(SystemExit):
            self.call("publish")
        self.assertEqual((self.target / "codex/bin/codex-code-mode-host").read_bytes(), b"host")
        self.call("reset")
        self.assertFalse((self.target / "codex").exists())

    def test_rejects_traversal_links_duplicates_and_writable_archive_entries(self):
        for kind in ("traversal", "link", "duplicate", "mode"):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                entry = tarfile.TarInfo("package/vendor/aarch64-unknown-linux-musl/bin/codex")
                entry.mode = 0o777 if kind == "mode" else 0o755
                if kind == "traversal":
                    entry.name = "package/../../escaped"
                if kind == "link":
                    entry.type, entry.linkname = tarfile.SYMTYPE, "/elsewhere"
                archive.addfile(entry)
                if kind == "duplicate":
                    archive.addfile(entry)
            with self.subTest(kind=kind), self.assertRaises((SystemExit, ValueError)):
                self.call("stage", stream.getvalue())
            self.call("reset")

    def test_expanded_archive_is_bounded(self):
        with (
            mock.patch.dict(self.update.__globals__, {"LIMIT": 100}),
            self.assertRaises(SystemExit),
        ):
            self.call("stage")

    def test_probe_resolves_npm_companion_and_runs_it(self):
        native = (
            self.target
            / "lib/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex"
        )
        native.parent.mkdir(parents=True)
        native.write_bytes(b"native")
        companion = native.with_name("codex-code-mode-host")
        companion.write_bytes(b"host")
        native.chmod(0o755)
        companion.chmod(0o755)
        with mock.patch("subprocess.run") as run:
            run.return_value.stdout = b"codex-cli 0.163.0\n"
            self.code["probe"](self.target / "bin/codex", "0.163.0")
        self.assertEqual(run.call_args_list[-1].args[0], [str(companion), "--help"])
        companion.unlink()
        with mock.patch("subprocess.run") as run, self.assertRaises(SystemExit):
            self.code["probe"](self.target / "bin/codex", "0.163.0")

    def test_probe_propagates_companion_startup_failure(self):
        native = self.target / "codex.next/bin/codex"
        native.parent.mkdir(parents=True)
        for path in (native, native.with_name("codex-code-mode-host")):
            path.write_bytes(b"binary")
            path.chmod(0o755)
        with mock.patch("subprocess.run") as run:
            run.return_value.stdout = b"codex-cli 0.163.0\n"
            run.side_effect = [run.return_value, OSError("host cannot start")]
            with self.assertRaises(OSError):
                self.code["probe"](native, "0.163.0")


class InstallHook(Case):
    def setUp(self):
        self.install = script("bootstrap.py")["install_hook"]
        self.stub = Path(self.home, "stub")
        self.stub.write_text("#!/bin/sh\nexit 0\n")
        self.uid = os.getuid()

    def test_install_hook_creates_parents_and_writes_stub(self):
        destination = Path(self.home, "usr/local/bin/ensemble-session")
        self.install(self.stub, destination, owner=self.uid, top=Path(self.home))
        self.assertEqual(destination.read_text(), self.stub.read_text())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o755)

    def test_install_hook_rejects_unsafe_parent(self):
        parent = Path(self.home, "open")
        parent.mkdir()
        parent.chmod(0o777)
        with self.assertRaises(SystemExit):
            self.install(self.stub, parent / "hook", owner=self.uid, top=Path(self.home))
        self.assertFalse((parent / "hook").exists())

    def test_install_hook_rejects_parent_of_other_owner(self):
        with self.assertRaises(SystemExit):
            self.install(
                self.stub, Path(self.home, "x/hook"), owner=self.uid + 1, top=Path(self.home)
            )

    def test_install_hook_checks_every_parent_from_root_by_default(self):
        with self.assertRaises(SystemExit):
            self.install(self.stub, Path(self.home, "hook"), owner=self.uid)

    def test_install_hook_rejects_existing_path(self):
        destination = Path(self.home, "hook")
        destination.write_text("old")
        with self.assertRaises(SystemExit):
            self.install(self.stub, destination, owner=self.uid, top=Path(self.home))
        self.assertEqual(destination.read_text(), "old")

    def test_hook_stub_hands_off_to_the_sessions_stub_and_exits_zero(self):
        text = (ASSETS / "hook-stub").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        stub = "src/ensemble/session/hosts/docker/stub.py"
        self.assertIn(f'/opt/ensemble/venv/bin/python -I /opt/ensemble/{stub} "$@"', text)
        self.assertTrue(os.path.isfile(os.path.join(REPO, stub)))
        self.assertIn(">&2", text)
        self.assertTrue(text.rstrip().endswith("exit 0"))


class WithSecrets(Case):
    def setUp(self):
        self.main = script("with-secrets")["main"]
        self.secret = Path(self.home, "claude")

    def test_with_secrets_execs_with_token_in_child_env_only(self):
        self.secret.write_text("tok-123")
        with mock.patch("os.execvpe") as execvpe:
            self.main(["--client", "claude", "claude", "--help"], secret=self.secret)
        (path, argv, env), _ = execvpe.call_args
        self.assertEqual((path, argv), ("claude", ["claude", "--help"]))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-123")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)

    def test_with_secrets_missing_or_empty_token(self):
        for content in (None, "", "\n"):
            if content is not None:
                self.secret.write_text(content)
            with mock.patch("os.execvpe") as execvpe, self.assertRaises(SystemExit) as raised:
                self.main(["--client", "claude", "claude"], secret=self.secret)
            execvpe.assert_not_called()
            self.assertIn("no Claude token", str(raised.exception.code))

    def test_with_secrets_requires_client_and_command(self):
        self.secret.write_text("tok-123")
        for argv in ([], ["claude"], ["--client", "claude"], ["--client", "other", "claude"]):
            with (
                self.subTest(argv=argv),
                mock.patch("os.execvpe") as execvpe,
                self.assertRaises(SystemExit) as raised,
            ):
                self.main(argv, secret=self.secret)
            execvpe.assert_not_called()
            self.assertEqual(
                raised.exception.code,
                "usage: with-secrets --client claude|codex|opencode COMMAND [ARG...]",
            )

    def test_codex_uses_its_file_auth_without_reading_claude(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("os.execvpe") as execvpe,
        ):
            self.main(["--client", "codex", "codex", "--version"], secret=self.secret)
        self.assertEqual(execvpe.call_args.args[:2], ("codex", ["codex", "--version"]))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", execvpe.call_args.args[2])

    def test_opencode_execs_with_unchanged_env_and_reads_no_secret(self):
        self.secret.write_text("tok-123")
        with (
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/state"}, clear=True),
            mock.patch("pathlib.Path.read_text") as read,
            mock.patch("os.execvpe") as execvpe,
        ):
            self.main(["--client", "opencode", "opencode", "--version"], secret=self.secret)
            path, argv, env = execvpe.call_args.args
            self.assertEqual(dict(env), {"XDG_DATA_HOME": "/state"})
        read.assert_not_called()
        self.assertEqual((path, argv), ("opencode", ["opencode", "--version"]))

    def test_claude_inherited_codex_home_still_requires_its_own_token(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/unrelated"}):
            with mock.patch("os.execvpe") as execute, self.assertRaises(SystemExit):
                self.main(["--client", "claude", "claude"], secret=self.secret)
            execute.assert_not_called()
            self.secret.write_text("claude-synthetic")
            with mock.patch("os.execvpe") as execute:
                self.main(["--client", "claude", "claude"], secret=self.secret)
            self.assertEqual(execute.call_args.args[:2], ("claude", ["claude"]))
            self.assertEqual(
                execute.call_args.args[2]["CLAUDE_CODE_OAUTH_TOKEN"], "claude-synthetic"
            )

    def test_invalid_client_or_missing_command_is_rejected(self):
        for argv in (["--client"], ["--client", "codex"], ["--client", "other", "cmd"]):
            with self.subTest(argv=argv), mock.patch("os.execvpe") as execute:
                with self.assertRaises(SystemExit):
                    self.main(argv, secret=self.secret)
                execute.assert_not_called()


class GitHubTokens(Case):
    def setUp(self):
        self.tokens = Path(self.home, "github")
        self.tokens.mkdir()
        for name, token in (
            ("Acme", "ghs_acme\n"),
            ("other", "ghs_other"),
            (".acme.new", "ghs_dot"),
        ):
            (self.tokens / name).write_text(token)


class GitCredential(GitHubTokens):
    def get(self, text, argv=("get",)):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            script("git-credential-ensemble")["main"](
                list(argv), stdin=io.StringIO(text), tokens=self.tokens
            )
        return out.getvalue()

    def test_get_answers_the_owners_token_for_github_https(self):
        for path in ("acme/ensemble.git", "ACME/ensemble", "acme"):
            with self.subTest(path=path):
                found = self.get(f"protocol=https\nhost=github.com\npath={path}\n\n")
                self.assertEqual(found, "username=x-access-token\npassword=ghs_acme\n")

    def test_anything_else_prints_nothing(self):
        for text in (
            "protocol=http\nhost=github.com\npath=acme/x\n",
            "protocol=https\nhost=gitlab.com\npath=acme/x\n",
            "protocol=https\nhost=github.com\n",
            "protocol=https\nhost=github.com\npath=nobody/x\n",
            "protocol=https\nhost=github.com\npath=../github/Acme\n",
            "protocol=https\nhost=github.com\npath=.acme.new/x\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.get(text), "")
        good = "protocol=https\nhost=github.com\npath=acme/x\n"
        for argv in (("store",), ("erase",), ()):
            self.assertEqual(self.get(good, argv), "")


class GhWrapper(GitHubTokens):
    def setUp(self):
        super().setUp()
        lib, wrapper, self.real = (Path(self.home, d) for d in ("lib", "wrapper", "real"))
        for folder in (lib, wrapper, self.real):
            folder.mkdir()
        for name in ("gh", "git-credential-ensemble"):  # as in the image: lib/, linked from bin/
            shutil.copy(ASSETS / name, lib)
        (wrapper / "gh").symlink_to(lib / "gh")
        self.wrapper = str(lib / "gh")
        (self.real / "gh").write_text("#!/bin/sh\n")
        (self.real / "gh").chmod(0o755)
        self.path = f"{wrapper}:{self.real}"
        self.origin = ""

    def gh(self, *argv):
        """The GH_TOKEN the real gh gets, None for none."""
        origin = SimpleNamespace(returncode=0, stdout=self.origin + "\n")
        with (
            mock.patch.dict(os.environ, {"PATH": self.path}),
            mock.patch("os.execve") as execve,
            mock.patch("subprocess.run", return_value=origin),
        ):
            runpy.run_path(self.wrapper)["main"](list(argv), tokens=self.tokens)
        (path, args, env), _ = execve.call_args
        self.assertEqual((path, args), (str(self.real / "gh"), ["gh", *argv]))
        self.assertNotIn("GH_TOKEN", os.environ)
        return env.get("GH_TOKEN")

    def test_the_repo_it_acts_on_picks_the_owners_token(self):
        self.origin = "https://github.com/acme/ensemble.git"
        for argv in (
            ("repo", "clone", "other/x"),
            ("repo", "clone", "https://github.com/Other/x"),
            ("pr", "list", "-R", "other/x"),
            ("pr", "list", "--repo", "github.com/other/x"),
            ("pr", "list", "--repo=other/x"),
            ("api", "repos/other/x/pulls"),
            ("api", "-X", "GET", "/repos/other/x"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.gh(*argv), "ghs_other")

    def test_otherwise_the_origin_then_the_only_token(self):
        for origin in ("https://github.com/other/x.git", "git@github.com:other/x.git"):
            self.origin = origin
            self.assertEqual(self.gh("pr", "view"), "ghs_other")
        self.origin = "https://gitlab.com/other/x.git"
        self.assertIsNone(self.gh("pr", "view"))
        (self.tokens / "other").unlink()
        self.assertEqual(self.gh("pr", "view"), "ghs_acme")
        self.origin = ""
        self.assertEqual(self.gh("pr", "view"), "ghs_acme")

    def test_owner_placeholder_is_the_current_repos(self):
        self.origin = "https://github.com/other/x.git"
        for argv in (
            ("api", "repos/{owner}/{repo}"),
            ("api", "/repos/{owner}/{repo}/pulls"),
            ("api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/1"),
            ("api", "repos/:owner/:repo/pulls"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.gh(*argv), "ghs_other")
        self.origin = "https://gitlab.com/other/x.git"
        self.assertIsNone(self.gh("api", "repos/{owner}/{repo}"))
        (self.tokens / "other").unlink()
        self.origin = ""
        self.assertEqual(self.gh("api", "repos/{owner}/{repo}"), "ghs_acme")

    def test_another_host_or_no_token_adds_none(self):
        self.origin = "https://github.com/acme/x"
        for argv in (
            ("pr", "list", "-R", "gitlab.com/acme/x"),
            ("repo", "clone", "https://gitlab.com/acme/x"),
            ("pr", "list", "-R", "nobody/x"),
            ("pr", "list", "-R", "-bad/x"),
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(self.gh(*argv))

    def test_no_real_gh_fails(self):
        self.path = str(Path(self.home, "wrapper"))
        with (
            mock.patch.dict(os.environ, {"PATH": self.path}),
            mock.patch("os.execve") as execve,
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(self.wrapper)["main"](["pr", "view"], tokens=self.tokens)
        execve.assert_not_called()


class DefaultDockerfile(Case):
    def test_default_dockerfile_pins(self):
        text = Path(REPO, "src/broker/worker/Dockerfile").read_text()
        self.assertIn(
            "FROM mcr.microsoft.com/devcontainers/base:bookworm@sha256:"
            "9a5942e64b35c5497c591b9bf177f4a3002d43440bf0a5e1a14fc34667b1a5c1",
            text,
        )
        for needle in (
            "NODE_VERSION=24.22.0",
            "fef00bb4d36e1c50104a9775a10359beee98f162b4104dd12472c75065c6a143",
            "cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008",
            "uv==0.13.1",
            "tmux git gh python3",
            "useradd --create-home --shell /bin/bash agent",
            "SUDO_FORCE_REMOVE=yes apt-get purge -y sudo",
            "rm -rf /etc/sudoers.d",
            "USER agent",
            "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
            "npm install -g @anthropic-ai/claude-code\n",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("SHASUMS256", text)
        self.assertNotIn("@openai/codex", text)


def tarball(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class OpencodeInstall(Case):
    def setUp(self):
        self.ns = script("opencode-install.py")
        version = self.ns["VERSION"]
        self.tarballs = {
            "opencode-ai": tarball(
                {
                    "package/package.json": json.dumps(
                        {"name": "opencode-ai", "version": version}
                    ).encode(),
                    "package/LICENSE": b"MIT",
                    "package/bin/opencode.exe": b"stub",
                }
            )
        }
        self.native = {}
        for name in ("opencode-linux-arm64", "opencode-linux-x64"):
            self.native[name] = self.binary(name, version)
            self.tarballs[name] = tarball(
                {
                    "package/package.json": json.dumps({"name": name, "version": version}).encode(),
                    "package/bin/opencode": self.native[name],
                }
            )
        self.urls, self.runs = [], []
        self.prefix = Path(self.home, "clients")
        self.prefix.mkdir()

    @staticmethod
    def binary(name, version):
        return f"native {name} {version}".encode()

    def version_run(self, argv, **kw):
        """subprocess.run for the version check: answers the version the binary was built with."""
        executable = Path(argv[0])
        self.runs.append((argv, kw, stat.S_IMODE(executable.stat().st_mode)))
        version = executable.read_bytes().split()[-1].decode()
        return SimpleNamespace(returncode=0, stdout=f"{version}\n")

    def opener(self, url, timeout):
        self.urls.append((url, timeout))
        name = url.rsplit("/", 1)[1].removesuffix(f"-{self.ns['VERSION']}.tgz")
        return io.BytesIO(self.tarballs[name])

    def install(self, arch, opener=None):
        sums = {n: hashlib.sha512(data).hexdigest() for n, data in self.tarballs.items()}
        fetch = self.ns["fetch"]
        with (
            mock.patch.dict(self.ns["SHA512"], sums),
            mock.patch("subprocess.run", self.version_run),
        ):
            self.ns["install"](
                arch, self.prefix, fetch=lambda name: fetch(name, opener=opener or self.opener)
            )

    def assert_refused(self, arch="arm64", opener=None):
        with self.assertRaises(SystemExit) as raised:
            self.install(arch, opener)
        self.assertTrue(str(raised.exception.code).startswith("opencode-install: "))
        self.assertEqual(list(self.prefix.iterdir()), [])
        return str(raised.exception.code)

    def test_installs_the_package_with_its_platform_binary(self):
        for arch, name in (("arm64", "opencode-linux-arm64"), ("amd64", "opencode-linux-x64")):
            with self.subTest(arch=arch):
                shutil.rmtree(self.prefix)
                self.prefix.mkdir()
                self.urls.clear()
                self.install(arch)
                root = self.prefix / "lib/node_modules/opencode-ai"
                executable = root / "bin/opencode.exe"
                self.assertEqual(executable.read_bytes(), self.native[name])
                self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
                self.assertEqual((root / "LICENSE").read_bytes(), b"MIT")
                self.assertEqual(
                    json.loads((root / "package.json").read_text())["name"], "opencode-ai"
                )
                link = self.prefix / "bin/opencode"
                self.assertEqual(
                    os.readlink(link), "../lib/node_modules/opencode-ai/bin/opencode.exe"
                )
                self.assertEqual(link.resolve(), executable.resolve())
                version = self.ns["VERSION"]
                self.assertEqual(
                    self.urls,
                    [
                        (f"https://registry.npmjs.org/{n}/-/{n}-{version}.tgz", 300)
                        for n in ("opencode-ai", name)
                    ],
                )

    def test_recorded_hashes_cover_both_platforms_and_the_package(self):
        self.assertEqual(self.ns["VERSION"], "1.19.3")
        self.assertEqual(set(self.ns["SHA512"]), {"opencode-ai", *self.ns["PLATFORMS"].values()})
        self.assertEqual(
            self.ns["PLATFORMS"],
            {"arm64": "opencode-linux-arm64", "amd64": "opencode-linux-x64"},
        )
        for value in self.ns["SHA512"].values():
            self.assertRegex(value, "^[0-9a-f]{128}$")

    def test_a_tarball_that_does_not_match_its_hash_installs_nothing(self):
        for name in ("opencode-ai", "opencode-linux-arm64"):
            with self.subTest(name=name):
                good = self.tarballs[name]

                def tampered(url, timeout, good=good):
                    data = self.opener(url, timeout).read()
                    return io.BytesIO(data + b"x" if data == good else data)

                message = self.assert_refused(opener=tampered)
                self.assertIn(f"{name}@1.19.3 does not match its recorded sha512", message)

    def test_wrong_package_name_or_version_installs_nothing(self):
        for name, manifest in (
            ("opencode-ai", {"name": "opencode-ai", "version": "1.19.2"}),
            ("opencode-linux-arm64", {"name": "opencode-linux-x64", "version": "1.19.3"}),
        ):
            with self.subTest(name=name):
                saved = self.tarballs[name]
                self.tarballs[name] = tarball(
                    {
                        "package/package.json": json.dumps(manifest).encode(),
                        "package/LICENSE": b"MIT",
                        "package/bin/opencode": b"native",
                    }
                )
                self.assertIn(f"{name}: package.json", self.assert_refused())
                self.tarballs[name] = saved

    def test_missing_member_unknown_arch_or_download_failure_installs_nothing(self):
        self.assertIn("architecture 'riscv64'", self.assert_refused("riscv64"))

        def broken(*_, **__):
            raise OSError("no route")

        self.assertIn("downloading opencode-ai@1.19.3 failed", self.assert_refused(opener=broken))

        def truncated(*_, **__):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.side_effect = http.client.IncompleteRead(b"partial")
            return response

        self.assertIn(
            "downloading opencode-ai@1.19.3 failed", self.assert_refused(opener=truncated)
        )
        self.tarballs["opencode-linux-arm64"] = tarball(
            {
                "package/package.json": json.dumps(
                    {"name": "opencode-linux-arm64", "version": "1.19.3"}
                ).encode()
            }
        )
        self.assertIn("no package/bin/opencode", self.assert_refused())

    def test_a_binary_that_does_not_report_the_pinned_version_installs_nothing(self):
        name = "opencode-linux-arm64"
        self.tarballs[name] = tarball(
            {
                "package/package.json": json.dumps({"name": name, "version": "1.19.3"}).encode(),
                "package/bin/opencode": self.binary(name, "1.19.2"),
            }
        )
        self.assertIn("opencode --version gave '1.19.2', want 1.19.3", self.assert_refused())

    def test_the_version_check_runs_in_a_scratch_home_it_removes(self):
        self.install("arm64")
        ((argv, kw, mode),) = self.runs
        scratch = os.path.dirname(argv[0])
        self.assertEqual(argv[1:], ["--version"])
        self.assertEqual(mode, 0o755)
        self.assertEqual(kw["env"]["HOME"], scratch)
        self.assertNotEqual(scratch, self.home)
        self.assertFalse(os.path.exists(scratch))

    def test_main_requires_arch_and_absolute_prefix(self):
        for argv in ([], ["arm64"], ["arm64", "relative"], ["arm64", "/p", "extra"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.ns["main"](argv)

    def test_the_pinned_opencode_still_needs_the_window_floor(self):
        self.assertEqual(
            (self.ns["VERSION"], HOME.min_width),
            ("1.19.3", 40),
            "see HOME in clients/opencode/common.py: keep min_width while the broker's "
            "opencodeMinVersion (src/broker/internal/launcher/versions.go) bundles @opentui/core "
            "older than 0.5.3, then update this pin",
        )


class ImageLayer(Case):
    def test_installs_opencode_and_ripgrep(self):
        text = (ASSETS / "Dockerfile").read_text()
        for needle in (
            "ripgrep",
            "opencode-install.py",
            'python3 -I /opt/ensemble/lib/opencode-install.py "$(dpkg --print-architecture)"'
            " /opt/ensemble/clients",
            "ln -s /opt/ensemble/clients/bin/opencode /opt/ensemble/bin/opencode",
        ):
            self.assertIn(needle, text)

    def test_pins_gh(self):
        text = (ASSETS / "Dockerfile").read_text()
        for needle in (
            "GH_VERSION=2.102.0",
            "bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386",
            "7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484",
            "https://github.com/cli/cli/releases/download/v${GH_VERSION}/",
            "sha256sum -c",
            "/usr/local/bin/gh",
        ):
            self.assertIn(needle, text)


CACHE = ".ensemble-cache"


class WorkspaceCheck(Case):
    """The walk, argv and output over a fake git; real git: workspace_check_integration_test.py."""

    def setUp(self):
        self.check = script("workspace-check")
        self.root = Path(self.home, "work")
        self.calls = []

    def repo(self, rel):
        (self.root / rel / ".git").mkdir(parents=True)

    def run(self, dirty=(), failing=()):
        def run(argv, **kw):
            rel = os.path.relpath(kw["cwd"], self.root)
            self.calls.append((argv, kw))
            if rel in failing:
                raise OSError("no git")
            out = "?? x\n" if rel in dirty and argv[8] == "status" else ""
            return SimpleNamespace(returncode=0, stdout=out)

        return run

    def test_repos_up_to_three_levels_outside_git_and_cache_dirs(self):
        for rel in (
            ".",
            "publish",
            "src/o/a",
            "a/b/c/d",
            ".ensemble-cache/x",
            "src/o/a/.git/modules/m",
        ):
            self.repo(rel)
        (self.root / "src" / "link").symlink_to(self.root / "a")
        self.assertEqual(
            [os.path.relpath(p, self.root) for p in self.check["repos"](str(self.root), CACHE)[0]],
            [".", "publish", "src/o/a"],
        )

    def test_git_runs_guarded_without_optional_locks_and_a_failure_counts_as_dirty(self):
        for rel in ("publish", "src/o/a", "src/o/b"):
            self.repo(rel)
        found = self.check["dirty"](
            str(self.root), CACHE, run=self.run(dirty={"src/o/a"}, failing={"src/o/b"})
        )
        self.assertEqual(found, ["src/o/a", "src/o/b"])
        guard = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "safe.directory=*",
        ]
        publish = [argv for argv, kw in self.calls if kw["cwd"] == str(self.root / "publish")]
        self.assertEqual(
            publish,
            [
                [
                    *guard,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    ".",
                    ":(exclude).ensemble-cache",
                ],
                [*guard, "rev-list", "--all", "--not", "--remotes"],
            ],
        )

    def test_a_dir_it_cannot_list_counts_as_dirty(self):
        self.repo("src/o/a")
        locked = str(self.root / "src" / "o")
        scandir = os.scandir

        def denied(path):
            if path == locked:
                raise PermissionError(13, "Permission denied", path)
            return scandir(path)

        with mock.patch("os.scandir", side_effect=denied):
            found = self.check["dirty"](str(self.root), CACHE, run=self.run())
        self.assertEqual(found, ["src/o"])

    def test_paths_have_control_characters_escaped(self):
        self.repo("src/o/bad\x1b[31m")
        found = self.check["dirty"](
            str(self.root), CACHE, run=self.run(dirty={"src/o/bad\x1b[31m"})
        )
        self.assertEqual(found, ["src/o/bad\\x1b[31m"])

    def test_main_prints_the_dirty_list_and_wants_one_absolute_path(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.check["main"]([str(self.root), CACHE])
        self.assertEqual(json.loads(out.getvalue()), {"dirty": []})
        for argv in (
            [],
            [str(self.root)],
            ["relative", CACHE],
            ["/a", "x/y"],
            ["/a", ".."],
            ["/a", CACHE, "x"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.check["main"](argv)
