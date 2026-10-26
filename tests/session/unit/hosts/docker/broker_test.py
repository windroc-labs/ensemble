"""hosts/docker/broker.py: protocol-1 calls, every shared response fixture's outcome, the manifest."""

import io
import json
import logging
import os
import stat
import tempfile
from unittest import mock

from ensemble.session.engine.types import ConfigError, SessionError
from ensemble.session.hosts.docker import broker
from ensemble.session.hosts.docker.broker import BROKER, Broker, BrokerError
from ensemble.session.hosts.docker.types import Refreshed, Started
from tests.session.case import REPO, Case
from tests.session.unit.hosts.docker.support import (
    HOOK,
    FakeBroker,
    FakePopen,
    FakeProc,
    cases,
    data,
    failure,
    success,
    text,
)
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

MANIFEST = "/Users/example/.ensemble/docker-state/worker_1/0123456789abcdef0123456789abcdef.json"
CONTAINER = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
VOLUME = "ensemble-ws-worker_1"
KEYS = broker.client_keys(REPO)
CALLS = {
    "probe_provider_output": (
        "probe-provider",
        lambda b: b.probe_provider("strata", "https://strata.test/v1"),
    ),
    "version_output": ("version", lambda b: b.version()),
    "build_output": ("build", lambda b: b.build("/opt/ensemble")),
    "start_output": ("start", lambda b: b.start(MANIFEST, clients=KEYS)),
    "refresh_output": ("refresh", lambda b: b.refresh(CONTAINER, MANIFEST)),
    "close_output": ("close", lambda b: b.close(CONTAINER)),
    "workspace_rm_output": ("workspace-rm", lambda b: b.workspace_rm(VOLUME)),
}
COMMANDS = {
    "probe-provider": CALLS["probe_provider_output"],
    "version": CALLS["version_output"],
    "build": CALLS["build_output"],
    "start": CALLS["start_output"],
    "refresh": CALLS["refresh_output"],
    "close": CALLS["close_output"],
    "workspace-rm": CALLS["workspace_rm_output"],
    "unknown": CALLS["version_output"],
}


def argv(command):
    return [BROKER, *data(f"{command}-input.json")["argv"]]


class PluginRootTest(Case):
    def test_plugin_root_from_hook_path(self):
        self.assertEqual(broker.plugin_root(HOOK), REPO)
        self.assertEqual(KEYS, frozenset({"claude", "codex", "opencode"}))
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            with self.assertRaisesRegex(ConfigError, "protocol.schema.json"):
                broker.plugin_root(os.path.join(tmp, "bin", "ensemble-session"))


class ResponseTest(Case):
    def test_a_warning_sink_failure_keeps_the_success(self):
        def unavailable(_message):
            raise OSError("AUDIT_SINK_SECRET_SENTINEL")

        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("session")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        response = {**data("close-output.json"), "audit_warning": "audit_write_failed"}
        result = Broker(FakeBroker((0, json.dumps(response), "")), warning=unavailable).close(
            CONTAINER
        )
        self.assertIs(result, response["removed"])
        self.assertIn("audit_write_failed", output.getvalue())
        self.assertNotIn("AUDIT_SINK_SECRET_SENTINEL", output.getvalue())

    def test_audit_warning_preserves_success_and_failure(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("session")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        for fixture in ("start-output.json", "refresh-output.json", "close-output.json"):
            response = {**data(fixture), "audit_warning": "audit_write_failed"}
            definition = fixture.removesuffix(".json").replace("-", "_")
            _, call = CALLS[definition]
            fake = FakeBroker((0, json.dumps(response), ""))
            result = call(Broker(fake))
            self.assertIsNotNone(result)
            self.assertEqual(len(fake.calls), 1)
        response = {
            **data("error-start-cleanup-failed.json"),
            "audit_warning": "audit_write_failed",
        }
        fake = FakeBroker((response["exit_code"], "", json.dumps(response)))
        with self.assertRaises(BrokerError) as caught:
            Broker(fake).start(MANIFEST, clients=KEYS)
        expected = response["error"]
        self.assertEqual(caught.exception.code, expected["code"])
        self.assertEqual(caught.exception.cleanup, expected["cleanup"])
        self.assertEqual(caught.exception.container, expected.get("container"))
        self.assertEqual(caught.exception.cause, expected.get("cause"))
        self.assertEqual(output.getvalue().count("audit_write_failed"), 4)

    def test_invalid_audit_warning_is_rejected_without_leaking_its_value(self):
        for value in (None, False, [], {}, "SECRET_AUDIT_SENTINEL"):
            for failed in (False, True):
                response = data(
                    "error-start-cleanup-failed.json" if failed else "start-output.json"
                )
                response["audit_warning"] = value
                wire = json.dumps(response)
                fake = FakeBroker((response["exit_code"], "", wire) if failed else (0, wire, ""))
                with (
                    self.subTest(value=value, failed=failed),
                    self.assertRaises(SessionError) as caught,
                ):
                    Broker(fake).start(MANIFEST, clients=KEYS)
                self.assertNotIsInstance(caught.exception, BrokerError)
                self.assertNotIn("SECRET_AUDIT_SENTINEL", str(caught.exception))

    def test_provider_probe_response_is_only_a_boolean(self):
        for value in (True, False):
            runner = FakeBroker((0, json.dumps({"available": value}), ""))
            self.assertIs(Broker(runner).probe_provider("strata", "https://strata.test/v1"), value)
        for response in ({}, {"available": 1}, {"available": True, "key": "secret"}):
            with self.subTest(response=response), self.assertRaises(SessionError):
                Broker(FakeBroker((0, json.dumps(response), ""))).probe_provider(
                    "strata", "https://strata.test/v1"
                )

    def test_start_response_carrying_client_update_is_rejected(self):
        update = {"client": "codex", "result": "current"}
        for fixture in ("start-output.json", "start-output-codex.json"):
            response = data(fixture)
            started = Broker(FakeBroker((0, json.dumps(response), ""))).start(
                MANIFEST, clients=KEYS
            )
            self.assertFalse(hasattr(started, "client_update"))
            for value in (update, None):
                with (
                    self.subTest(fixture=fixture, value=value),
                    self.assertRaisesRegex(SessionError, "^broker: bad response to start: fields$"),
                ):
                    Broker(
                        FakeBroker((0, json.dumps({**response, "client_update": value}), ""))
                    ).start(MANIFEST, clients=KEYS)

    def test_every_response_fixture_has_its_outcome(self):
        found = cases()
        self.assertTrue(any(c["definition"] == "error" for c in found))
        # Session never runs status: users do
        for case in (
            c
            for c in found
            if c["expected"]["stage"] == "response" and c["definition"] != "status_output"
        ):
            name, definition, valid = case["file"], case["definition"], case["valid"]
            with self.subTest(name=name):
                if definition == "error":
                    command, call = COMMANDS[data(name)["error"]["command"]]
                    fake = FakeBroker(failure(name))
                else:
                    command, call = CALLS[definition]
                    fake = FakeBroker(success(name))
                if definition == "error" and valid:
                    with self.assertRaises(BrokerError) as raised:
                        call(Broker(fake))
                    error = data(name)
                    self.assertEqual(
                        (raised.exception.exit_code, raised.exception.code),
                        (error["exit_code"], error["error"]["code"]),
                    )
                    self.assertEqual(raised.exception.cleanup, error["error"]["cleanup"])
                    self.assertEqual(raised.exception.container, error["error"].get("container"))
                    self.assertIn(error["error"]["message"], str(raised.exception))
                elif valid:
                    call(Broker(fake))
                else:
                    with self.assertRaisesRegex(SessionError, "broker: bad response") as raised:
                        call(Broker(fake))
                    self.assertNotIsInstance(raised.exception, BrokerError)
                self.assertEqual(fake.calls, [argv(command)])
                self.assertEqual(
                    fake.kwargs[0],
                    {
                        "stdin": DEVNULL,
                        "env": {"PATH": os.defpath},
                        "timeout": broker.TIMEOUT,
                    },
                )

    def test_refresh_requires_public_deadline_and_rejects_secrets(self):
        good = {
            "container": CONTAINER,
            "github": [],
            "credential_expires_at": "2026-10-18T12:00:00Z",
        }
        result = Broker(FakeBroker((0, json.dumps(good), ""))).refresh(CONTAINER, MANIFEST)
        self.assertEqual(result.credential_expires_at, good["credential_expires_at"])
        self.assertEqual(result.github, [])
        for change in (
            {"credential_expires_at": "2026-02-30T00:00:00Z"},
            {"credential_expires_at": "access-token"},
            {"access_token": "secret"},
            {"container": "wrong"},
        ):
            with self.subTest(change=change), self.assertRaises(SessionError):
                Broker(FakeBroker((0, json.dumps(good | change), ""))).refresh(CONTAINER, MANIFEST)
        with self.assertRaises(SessionError):
            Broker(FakeBroker((0, json.dumps({"container": CONTAINER, "github": []}), ""))).refresh(
                CONTAINER, MANIFEST
            )

    def test_refresh_failure_keeps_the_brokers_message_and_cause(self):
        message = "Writing Codex credentials into the container failed"
        cause = "bootstrap: cannot write managed Codex credentials: No space left on device"

        def fails(code, exit_code, **extra):
            error = {
                "command": "refresh",
                "code": code,
                "message": message,
                "cleanup": "not_needed",
            }
            return exit_code, "", json.dumps({"exit_code": exit_code, "error": error | extra})

        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker(fails("refresh_failed", 16, cause=cause))).refresh(
                CONTAINER, MANIFEST
            )
        self.assertEqual(
            (raised.exception.code, raised.exception.cause, str(raised.exception)),
            ("refresh_failed", cause, f"broker: {message} ({cause})"),
        )
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker(fails("refresh_failed", 16))).refresh(CONTAINER, MANIFEST)
        self.assertEqual(
            (raised.exception.cause, str(raised.exception)), (None, f"broker: {message}")
        )
        with self.assertRaisesRegex(SessionError, "bad response to refresh: error fields"):
            Broker(FakeBroker(fails("start_failed", 9, cause=cause))).refresh(CONTAINER, MANIFEST)
        with self.assertRaisesRegex(SessionError, "bad response to refresh: error fields"):
            Broker(FakeBroker(fails("egress_unavailable", 17))).refresh(CONTAINER, MANIFEST)

    def test_parsed_successes(self):
        self.assertEqual(
            Broker(FakeBroker(success("build-cached.json"))).build("/opt/ensemble"),
            (data("build-cached.json")["image"], data("build-cached.json")["digest"]),
        )
        started = Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=KEYS)
        self.assertEqual(
            started,
            Started(
                container=CONTAINER,
                endpoint="unix:///Users/example/.docker/run/docker.sock",
                image_digest="sha256:" + "b" * 64,
                clients={"claude": "2.1.0", "codex": "codex-cli 0.100.0", "opencode": "1.19.3"},
                github=[{"account": "acme", "expires_at": "2026-10-17T13:00:00Z"}],
                github_user_id=343764394,
                credential_expires_at=None,
            ),
        )
        self.assertEqual(
            Broker(FakeBroker(success("refresh-output.json"))).refresh(CONTAINER, MANIFEST),
            Refreshed([{"account": "acme", "expires_at": "2026-10-17T13:00:00Z"}], None),
        )
        for bad in (
            {"container": "ensemble-other-" + "0" * 32, "github": []},
            {
                "container": CONTAINER,
                "github": [{"account": "../x", "expires_at": "2026-10-17T13:00:00Z"}],
            },
            {"container": CONTAINER, "github": [{"account": "acme"}]},
            {"container": CONTAINER, "github": {}},
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(SessionError, "bad response to refresh"),
            ):
                Broker(FakeBroker((0, json.dumps(bad), ""))).refresh(CONTAINER, MANIFEST)
        self.assertFalse(Broker(FakeBroker(success("close-absent.json"))).close(CONTAINER))
        self.assertTrue(
            Broker(FakeBroker(success("workspace-rm-output.json"))).workspace_rm(VOLUME)
        )
        self.assertFalse(
            Broker(FakeBroker(success("workspace-rm-absent.json"))).workspace_rm(VOLUME)
        )
        other = (0, json.dumps({"volume": "ensemble-ws-other", "removed": True}), "")
        with self.assertRaisesRegex(SessionError, "bad response to workspace-rm"):
            Broker(FakeBroker(other)).workspace_rm(VOLUME)

    def test_start_takes_a_null_or_positive_integer_github_user_id(self):
        good = data("start-output.json")
        for value in (None, 1, 343764394):
            with self.subTest(value=value):
                found = (0, json.dumps(good | {"github_user_id": value}), "")
                started = Broker(FakeBroker(found)).start(MANIFEST, clients=KEYS)
                self.assertEqual(started.github_user_id, value)
        missing = {k: v for k, v in good.items() if k != "github_user_id"}
        for bad in (missing, *(good | {"github_user_id": v} for v in (0, -1, "1", True, 1.5))):
            with (
                self.subTest(bad=bad.get("github_user_id", "missing")),
                self.assertRaisesRegex(SessionError, "bad response to start: fields"),
            ):
                Broker(FakeBroker((0, json.dumps(bad), ""))).start(MANIFEST, clients=KEYS)

    def test_build_passes_a_max_age(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build("/opt/ensemble", max_age=broker.REFRESH)
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-max-age.json")["argv"]]])
        self.assertEqual(broker.REFRESH, 7 * 24 * 3600)

    def test_build_passes_plugins_before_the_max_age(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build(
            "/opt/ensemble",
            plugins=(
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
            max_age=604800,
        )
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-plugins.json")["argv"]]])

    def test_build_progress_before_the_error_object_is_not_the_error(self):
        progress = "#5 [2/3] RUN apt-get purge -y sudo\n#5 ERROR: process exited 1\n"
        rc, _, err = failure("error-build_failed.json")
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", progress + err))).build("/opt/ensemble")
        self.assertEqual(raised.exception.code, "build_failed")
        found = Broker(FakeBroker((0, text("build-cached.json"), progress))).build("/opt/ensemble")
        self.assertEqual(found[0], data("build-cached.json")["image"])
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", "#7 partial\n" + err))).build("/opt/ensemble")
        self.assertEqual(raised.exception.code, "build_failed")

    def test_only_build_may_put_progress_before_the_error_object(self):
        rc, _, err = failure("error-login_required.json")
        with self.assertRaisesRegex(SessionError, "bad response to start") as raised:
            Broker(FakeBroker((rc, "", "progress\n" + err))).start(MANIFEST, clients=KEYS)
        self.assertNotIsInstance(raised.exception, BrokerError)

    def test_mismatched_exit_is_transport_failure(self):
        for result in (
            failure("error-login_required.json", rc=9),
            (0, "", ""),
            (0, "not json\n", ""),
            (0, '{"protocol_version": "2", "protocol_version": "2"}\n', ""),
            (0, '{"protocol_version": NaN}\n', ""),
            (0, '{"protocol_version": "2"} {}\n', ""),
            (5, "", "not json\n"),
            (5, "", '{"exit_code": true, "error": {}}\n'),
            (
                5,
                "",
                '{"exit_code": 5, "error": {"command": [], "code": [], "message": "m",'
                ' "cleanup": []}}\n',
            ),
            TimeoutExpired(BROKER, broker.TIMEOUT),
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(SessionError, "broker: ") as raised:
                    Broker(FakeBroker(result)).version()
                self.assertNotIsInstance(raised.exception, BrokerError)

    def test_wrong_protocol_or_missing_broker_fails_before_build(self):
        for result in (
            (0, '{"protocol_version": "1"}\n', ""),
            (0, '{"protocol_version": "2"}\n', ""),
            FileNotFoundError(2, "No such file", BROKER),
            PermissionError(13, "Permission denied", BROKER),
        ):
            with self.subTest(result=result):
                fake = FakeBroker(result)
                with self.assertRaisesRegex(SessionError, "docs/docker-host.md") as raised:
                    Broker(fake).version()
                self.assertIn(BROKER, str(raised.exception))
                self.assertEqual(fake.calls, [argv("version")])

    def test_a_protocol_2_update_field_is_bad(self):
        started = {**data("start-output.json"), "update": {"status": "updated"}}
        result = (0, json.dumps(started), "")
        with self.assertRaisesRegex(SessionError, "bad response"):
            Broker(FakeBroker(result)).start(MANIFEST, clients=KEYS)

    def test_a_client_key_set_other_than_the_schemas_is_bad(self):
        for keys in (frozenset({"claude"}), frozenset({"claude", "codex", "other"})):
            with self.subTest(keys=keys), self.assertRaisesRegex(SessionError, "bad response"):
                Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=keys)


class ManifestTest(Case):
    def test_writes_the_protocol_manifest_owner_only(self):
        path = os.path.join(self.home, "state", "g.json")
        broker.write_manifest(
            path,
            name="w",
            generation="0" * 32,
            image="ensemble-worker:" + "a" * 64,
            cwd="/work",
            workspace="ensemble-ws-w",
            home=("x_dir", "/home/x"),
            hook_path="/plugin/bin/ensemble-session",
            github="",
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": "0" * 32,
                    "image": "ensemble-worker:" + "a" * 64,
                    "cwd": "/work",
                    "workspace": "ensemble-ws-w",
                    "client": "x",
                    "x_dir": "/home/x",
                    "hook_path": "/plugin/bin/ensemble-session",
                    "github": None,
                    "labels": {},
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        broker.write_manifest(
            path,
            name="w",
            generation="0" * 32,
            image="ensemble-worker:" + "a" * 64,
            cwd="/work",
            workspace="ensemble-ws-w",
            home=("x_dir", "/home/x"),
            hook_path="/plugin/bin/ensemble-session",
            github="ensemble-bot",
            permissions={"contents": "write", "statuses": "read"},
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f)["github"],
                {"app": "ensemble-bot", "permissions": {"contents": "write", "statuses": "read"}},
            )

    def test_an_opencode_manifest_lists_its_providers(self):
        path = os.path.join(self.home, "state", "g.json")
        for providers in (("lab", "strata"), ()):
            broker.write_manifest(
                path,
                name="w",
                generation="0" * 32,
                image="ensemble-worker:" + "a" * 64,
                cwd="/work",
                workspace="ensemble-ws-w",
                home=("opencode_dir", "/home/opencode"),
                hook_path="/plugin/bin/ensemble-session",
                github="",
                providers=providers,
            )
            with open(path) as f:
                manifest = json.load(f)
            if providers:
                self.assertEqual(manifest["opencode_providers"], ["lab", "strata"])
            else:
                self.assertNotIn("opencode_providers", manifest)

    def test_a_claude_manifest_names_its_selected_account(self):
        path = os.path.join(self.home, "state", "g.json")
        for account in ("work", ""):
            broker.write_manifest(
                path,
                name="w",
                generation="0" * 32,
                image="ensemble-worker:" + "a" * 64,
                cwd="/work",
                workspace="ensemble-ws-w",
                home=("claude_dir", "/home/claude"),
                hook_path="/plugin/bin/ensemble-session",
                github="",
                account=account,
            )
            with open(path) as f:
                manifest = json.load(f)
            if account:
                self.assertEqual(manifest["claude_account"], "work")
            else:
                self.assertNotIn("claude_account", manifest)


class StreamTest(Case):
    def run_stream(self, out, err, code, **kw):
        popen, shown = FakePopen((out, err, code)), io.StringIO()
        with mock.patch("subprocess.Popen", popen), mock.patch("sys.stderr", shown):
            res = broker._stream(["b"], stdin=DEVNULL, env={"PATH": "/bin"}, timeout=30, **kw)
        return popen, shown.getvalue(), res

    def test_stdout_stays_the_json_and_progress_is_shown(self):
        popen, shown, res = self.run_stream(b'{"ok": 1}\n', b"#1 load\n#2 done\n", 0)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr), (0, '{"ok": 1}\n', "#1 load\n#2 done\n")
        )
        self.assertEqual(shown, "#1 load\n#2 done\n")
        ((argv, kw, given),) = popen.calls
        self.assertEqual(
            (argv, kw["env"], kw["stdout"], kw["stderr"], given),
            (["b"], {"PATH": "/bin"}, PIPE, PIPE, DEVNULL),
        )

    def test_a_failing_runs_error_object_is_not_shown(self):
        _, shown, res = self.run_stream(b"", b'#1 load\n{"exit_code": 8}\n', 8)
        self.assertEqual((res.returncode, res.stderr), (8, '#1 load\n{"exit_code": 8}\n'))
        self.assertEqual(shown, "#1 load\n")

    def test_build_notes_after_progress_are_shown(self):
        notes = b"#7 partial\nensemble-broker: removed ensemble-user:" + b"1" * 64 + b"\n"
        _, shown, res = self.run_stream(b'{"ok": 1}\n', notes, 0)
        self.assertEqual((res.stdout, shown), ('{"ok": 1}\n', notes.decode()))

    def test_a_broker_reaped_just_after_the_deadline_is_not_a_timeout(self):
        class Late(FakeProc):
            def wait(self, timeout=None):
                if not timeout:
                    raise TimeoutExpired(self.args, 0)
                return self.code

        with mock.patch("subprocess.Popen", return_value=Late(["b"], b"{}\n", b"", 0)):
            res = broker._stream(["b"], stdin=DEVNULL, env={}, timeout=0)
        self.assertEqual((res.returncode, res.stdout), (0, "{}\n"))

    def test_bad_utf8_is_a_decode_error_only_on_stdout(self):
        with self.assertRaises(UnicodeDecodeError):
            self.run_stream(b"\xff", b"", 0)
        _, shown, res = self.run_stream(b"{}\n", b"#3 caf\xe9\n", 0)
        self.assertEqual((res.stderr, shown), ("#3 caf\ufffd\n", "#3 caf\ufffd\n"))

    def test_a_silent_step_shows_its_header_then_times_out_and_is_killed(self):
        proc, shown = FakeProc(["b"], err=b"#5 [2/3] RUN make\n", hang=True), io.StringIO()
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("sys.stderr", shown),
            self.assertRaises(TimeoutExpired),
        ):
            broker._stream(["b"], stdin=DEVNULL, env={}, timeout=0.1)
        self.assertTrue(proc.killed)
        self.assertEqual(shown.getvalue(), "#5 [2/3] RUN make\n")
