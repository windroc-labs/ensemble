"""hosts/docker/broker.py: protocol-1 calls, every shared response fixture's outcome."""

import io
import json
import logging
import os
import tempfile
from unittest import mock

from ensemble.session.engine import daemon as daemons
from ensemble.session.engine.daemon import DAEMON
from ensemble.session.engine.types import ConfigError, Daemon, SessionError, Target
from ensemble.session.hosts.docker import broker
from ensemble.session.hosts.docker.broker import BROKER, Broker, BrokerError
from ensemble.session.hosts.docker.types import Started
from tests.session.case import REPO, Case
from tests.session.unit.hosts.docker.support import (
    BETA,
    HOOK,
    UPDRAFT,
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
LOCAL_ID = "8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6"
KEYS = broker.client_keys(REPO)
CALLS = {
    "version_output": ("version", lambda b: b.version()),
    "target_output": ("target", lambda b: b.target()),
    "build_output": ("build", lambda b: b.build("/opt/ensemble", daemon=LOCAL_ID)),
    "start_output": ("start", lambda b: b.start(MANIFEST, clients=KEYS)),
    "maintain_output": ("maintain", lambda b: b.maintain(CONTAINER, MANIFEST)),
    "close_output": ("close", lambda b: b.close(CONTAINER, daemon=LOCAL_ID)),
    "workspace_rm_output": (
        "workspace-rm",
        lambda b: b.workspace_rm(VOLUME, daemon=LOCAL_ID),
    ),
}
COMMANDS = {
    "version": CALLS["version_output"],
    "target": CALLS["target_output"],
    "build": CALLS["build_output"],
    "start": CALLS["start_output"],
    "maintain": CALLS["maintain_output"],
    "close": CALLS["close_output"],
    "workspace-rm": CALLS["workspace_rm_output"],
    "unknown": CALLS["version_output"],
}


def argv(command):
    return [BROKER, *data(f"{command}-input")["argv"]]


class PluginRootTest(Case):
    def test_plugin_root_from_hook_path(self):
        self.assertEqual(broker.plugin_root(HOOK), REPO)
        self.assertEqual(KEYS, frozenset({"claude", "codex", "opencode"}))
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            with self.assertRaisesRegex(ConfigError, "protocol.schema.json"):
                broker.plugin_root(os.path.join(tmp, "bin", "ensemble-session"))


def handshake(*results):
    """A FakeBroker answering the version handshake before `results`."""
    return FakeBroker(success("version-output"), *results)


class ResponseTest(Case):
    def test_close_and_workspace_rm_name_their_daemon_by_id(self):
        local = FakeBroker(success("close-output"))
        Broker(local).close(CONTAINER, daemon=LOCAL_ID)
        self.assertEqual(local.calls, [argv("close")])
        remote = FakeBroker(success("close-output"))
        Broker(remote).close(CONTAINER, daemon="PC:DAEMON:1")
        self.assertEqual(
            remote.calls, [[BROKER, "close", "--container", CONTAINER, "--daemon", "PC:DAEMON:1"]]
        )
        forced = FakeBroker(success("workspace-rm-output"))
        Broker(forced).workspace_rm(VOLUME, force=True, daemon=LOCAL_ID)
        self.assertEqual(forced.calls, [[BROKER, *data("workspace-rm-input-force")["argv"]]])

    def test_target_lists_each_daemon_with_its_id_or_why_not(self):
        local = Daemon(
            "local",
            "unix:///Users/u/.docker/run/docker.sock",
            remote=False,
            path="/usr/bin:/bin:/usr/sbin:/sbin",
            id=LOCAL_ID,
        )
        remote = Daemon(
            "remote",
            "ssh://u@pc",
            remote=True,
            path="/usr/local/libexec/ensemble/ssh:/usr/bin:/bin:/usr/sbin:/sbin",
            id="PC:DAEMON:1",
        )
        why = "host key unknown: not in ~/.ssh/known_hosts (Host key verification failed.)"
        for name, want in (
            ("target-output", Target("local", (local,))),
            ("target-output-remote", Target("remote", (remote, local))),
            (
                "target-output-remote-unreachable",
                Target("remote", (remote._replace(id=None, unreachable=why), local)),
            ),
        ):
            with self.subTest(name=name):
                fake = handshake(success(name))
                found = Broker(fake).target()
                self.assertEqual(found, want)
                self.assertEqual(fake.calls, [argv("version"), argv("target")])
        self.assertEqual(daemons.daemon_named(found, "remote").unreachable, why)
        self.assertIsNone(daemons.daemon_with_id(found, "PC:DAEMON:1"))
        self.assertEqual(daemons.daemon_with_id(found, LOCAL_ID), local)

    def test_a_target_reply_breaking_its_rules_is_bad(self):
        good = data("target-output-remote")
        remote, local = good["daemons"]
        for change in (
            {"default": "pc"},
            {"daemons": []},
            {"daemons": [remote]},
            {"daemons": [local, local]},
            {"daemons": [local, remote, remote]},
            {"daemons": [local | {"remote": True}]},
            {"daemons": [local | {"endpoint": ""}]},
            {"daemons": [local | {"path": "/usr/bin\n"}]},
            {"daemons": [local | {"id": "../x"}]},
            {"daemons": [local | {"extra": 1}]},
            {"extra": 1},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(SessionError, "^broker: bad response to target") as raised,
            ):
                Broker(handshake((0, json.dumps(good | change), ""))).target()
            self.assertNotIsInstance(raised.exception, BrokerError)

    def test_target_fails_at_the_handshake_of_an_older_broker(self):
        fake = FakeBroker((0, '{"protocol_version": "42"}\n', ""))
        with self.assertRaisesRegex(SessionError, "protocol '42', want '43'"):
            Broker(fake).target()
        self.assertEqual(fake.calls, [argv("version")])

    def test_start_environment_is_a_generic_string_map(self):
        response = data("start-output")
        response["env"] = {"FUTURE_KIND_IDENTITY": "opaque", "GIT_AUTHOR_NAME": "app[bot]"}
        started = Broker(FakeBroker((0, json.dumps(response), ""))).start(MANIFEST, clients=KEYS)
        self.assertEqual(started.env, response["env"])
        for invalid in (None, [], {"IDENTITY": 1}):
            response["env"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(SessionError):
                Broker(FakeBroker((0, json.dumps(response), ""))).start(MANIFEST, clients=KEYS)

    def test_a_warning_sink_failure_keeps_the_success(self):
        def unavailable(_message):
            raise OSError("AUDIT_SINK_SECRET_SENTINEL")

        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("session")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        response = {**data("close-output"), "audit_warning": "audit_write_failed"}
        result = Broker(FakeBroker((0, json.dumps(response), "")), warning=unavailable).close(
            CONTAINER, daemon=LOCAL_ID
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
        for fixture in ("start-output", "maintain-output", "close-output"):
            response = {**data(fixture), "audit_warning": "audit_write_failed"}
            definition = fixture.replace("-", "_")
            _, call = CALLS[definition]
            fake = FakeBroker((0, json.dumps(response), ""))
            result = call(Broker(fake))
            self.assertIsNotNone(result)
            self.assertEqual(len(fake.calls), 1)
        response = {
            **data("error-start-cleanup-failed"),
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
                response = data("error-start-cleanup-failed" if failed else "start-output")
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

    def test_start_response_carrying_client_update_is_rejected(self):
        update = {"client": "codex", "result": "current"}
        for fixture in ("start-output", "start-output-codex"):
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
        # Session never runs status or probe-provider: users and the installer do
        for case in (
            c
            for c in found
            if c["expected"]["stage"] == "response"
            and c["definition"] not in {"status_output", "probe_provider_output"}
        ):
            name, definition, valid = case["name"], case["definition"], case["valid"]
            with self.subTest(name=name):
                if definition == "error":
                    command, call = COMMANDS[data(name)["error"]["command"]]
                    result = failure(name)
                else:
                    command, call = CALLS[definition]
                    result = success(name)
                # target asks for the version first
                fake = handshake(result) if command == "target" else FakeBroker(result)
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
                asked = [argv("version")] if command == "target" else []
                self.assertEqual(fake.calls, [*asked, argv(command)])
                for kwargs in fake.kwargs:
                    self.assertEqual(
                        kwargs,
                        {
                            "stdin": DEVNULL,
                            "env": {"PATH": os.defpath},
                            "timeout": broker.TIMEOUT,
                        },
                    )

    def test_maintain_keeps_each_components_outcome_and_passes_its_options(self):
        good = data("maintain-output")
        fake = FakeBroker((0, json.dumps(good), ""))
        found = Broker(fake).maintain(CONTAINER, MANIFEST, renew=True)
        self.assertEqual(
            fake.calls,
            [
                [
                    BROKER,
                    *("maintain", "--container", CONTAINER, "--manifest", MANIFEST, "--renew"),
                ]
            ],
        )
        self.assertEqual(
            (found.results, found.next_check, found.deadline),
            (good["results"], "2026-10-17T12:01:00Z", good["deadline"]),
        )
        self.assertEqual(found.results[0]["error"]["code"], "refresh_failed")
        self.assertEqual(found.results[1]["expires_at"], "2026-10-17T13:00:00Z")
        plain = FakeBroker(success("maintain-output-idle"))
        static = Broker(plain).maintain(CONTAINER, MANIFEST)
        self.assertEqual(plain.calls, [argv("maintain")])
        self.assertEqual((static.results, static.deadline, static.next_check), ([], None, None))

    def test_maintain_rejects_malformed_replies(self):
        good = data("maintain-output")
        failed, ok = good["results"]
        for change in (
            {"container": "ensemble-other-0123456789abcdef0123456789abcdef"},
            {"next_check": "tomorrow"},
            {"deadline": good["deadline"] | {"action": "stop"}},
            {"deadline": good["deadline"] | {"reason": "two\nlines"}},
            {"deadline": good["deadline"] | {"at": "2026-13-01T00:00:00Z"}},
            {"deadline": {"at": good["deadline"]["at"]}},
            {"results": {}},
            {"results": [ok | {"credential": "GitHub"}]},
            {"results": [ok | {"token": "ghs_x"}]},
            {"results": [ok | {"expires_at": "soon"}]},
            {"results": [failed | {"error": {"code": "refresh_failed"}}]},
            {"results": [failed | {"error": {"message": "No."}}]},
            {"results": [failed | {"error": {"code": "other", "message": "No."}}]},
            {"results": [failed | {"error": failed["error"] | {"message": "a\nforged"}}]},
            {"results": [failed | {"error": failed["error"] | {"cause": "\x07"}}]},
            {"notices": [{"key": "ensemble-broker-bot/x@1:30d", "text": "x"}]},
            {"github": []},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(SessionError, "bad response to maintain"),
            ):
                Broker(FakeBroker((0, json.dumps(good | change), ""))).maintain(CONTAINER, MANIFEST)
        missing = {k: v for k, v in good.items() if k != "deadline"}
        with self.assertRaisesRegex(SessionError, "bad response to maintain"):
            Broker(FakeBroker((0, json.dumps(missing), ""))).maintain(CONTAINER, MANIFEST)

    def test_start_requires_a_valid_deadline(self):
        response = data("start-output-codex")
        started = Broker(FakeBroker((0, json.dumps(response), ""))).start(MANIFEST, clients=KEYS)
        self.assertEqual(started.deadline, response["deadline"])
        for change in (
            {"deadline": {"at": "x", "action": "close", "reason": "r"}},
            {"deadline": 1},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(SessionError, "bad response to start"),
            ):
                Broker(FakeBroker((0, json.dumps(response | change), ""))).start(
                    MANIFEST, clients=KEYS
                )
        without = {k: v for k, v in response.items() if k != "deadline"}
        with self.assertRaisesRegex(SessionError, "bad response to start"):
            Broker(FakeBroker((0, json.dumps(without), ""))).start(MANIFEST, clients=KEYS)

    def test_maintain_errors_and_audit_warning(self):
        warned = []
        response = data("maintain-output-audit-warning")
        found = Broker(FakeBroker((0, json.dumps(response), "")), warning=warned.append).maintain(
            CONTAINER, MANIFEST
        )
        self.assertEqual((found.results, warned), (response["results"], [broker.AUDIT_WARNING]))
        for code, exit_code in (("manifest_rejected", 3), ("internal_error", 70), ("usage", 2)):
            error = {"command": "maintain", "code": code, "message": "No.", "cleanup": "not_needed"}
            fake = FakeBroker((exit_code, "", json.dumps({"exit_code": exit_code, "error": error})))
            with self.subTest(code=code), self.assertRaises(BrokerError) as raised:
                Broker(fake).maintain(CONTAINER, MANIFEST)
            self.assertEqual(raised.exception.code, code)
        # A provider's failure is a result, never the command's error.
        error = {
            "command": "maintain",
            "code": "refresh_failed",
            "message": "No.",
            "cleanup": "not_needed",
        }
        fake = FakeBroker((16, "", json.dumps({"exit_code": 16, "error": error})))
        with self.assertRaisesRegex(SessionError, "bad response to maintain: error fields"):
            Broker(fake).maintain(CONTAINER, MANIFEST)

    def test_parsed_successes(self):
        self.assertEqual(
            Broker(FakeBroker(success("build-cached"))).build("/opt/ensemble", daemon=LOCAL_ID),
            (data("build-cached")["image"], data("build-cached")["digest"]),
        )
        started = Broker(FakeBroker(success("start-output"))).start(MANIFEST, clients=KEYS)
        self.assertEqual(
            started,
            Started(
                container=CONTAINER,
                endpoint="unix:///Users/example/.docker/run/docker.sock",
                daemon="8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6",
                image_digest="sha256:" + "b" * 64,
                clients={"claude": "2.1.0", "codex": "codex-cli 0.100.0", "opencode": "1.19.3"},
                env=data("start-output")["env"],
                deadline=None,
            ),
        )
        self.assertFalse(
            Broker(FakeBroker(success("close-absent"))).close(CONTAINER, daemon=LOCAL_ID)
        )
        self.assertTrue(
            Broker(FakeBroker(success("workspace-rm-output"))).workspace_rm(VOLUME, daemon=LOCAL_ID)
        )
        self.assertFalse(
            Broker(FakeBroker(success("workspace-rm-absent"))).workspace_rm(VOLUME, daemon=LOCAL_ID)
        )
        forced = FakeBroker(success("workspace-rm-output"))
        self.assertTrue(Broker(forced).workspace_rm(VOLUME, force=True, daemon=LOCAL_ID))
        self.assertEqual(forced.calls, [[BROKER, *data("workspace-rm-input-force")["argv"]]])
        other = (0, json.dumps({"volume": "ensemble-ws-other", "removed": True}), "")
        with self.assertRaisesRegex(SessionError, "bad response to workspace-rm"):
            Broker(FakeBroker(other)).workspace_rm(VOLUME, daemon=LOCAL_ID)

    def test_broker_env_adds_only_a_set_ssh_agent_socket(self):
        for sock, env in (
            ("/private/agent.sock", {"PATH": os.defpath, "SSH_AUTH_SOCK": "/private/agent.sock"}),
            ("", {"PATH": os.defpath}),
        ):
            fake = FakeBroker(success("close-absent"))
            with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock, "SSH_AGENT_PID": "1"}):
                Broker(fake).close(CONTAINER, daemon=LOCAL_ID)
            self.assertEqual(fake.kwargs[0]["env"], env)

    def test_a_start_reply_s_endpoint_is_only_checked_as_text(self):
        started = Broker(FakeBroker(success("start-output-remote-endpoint"))).start(
            MANIFEST, clients=KEYS
        )
        self.assertEqual(started.endpoint, "ssh://agent@pc.tail1234.ts.net:2222")
        for endpoint in ("", "ssh://pc\n", "unix:///x\x1b"):
            reply = {**data("start-output"), "endpoint": endpoint}
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaisesRegex(SessionError, "bad response to start"),
            ):
                Broker(FakeBroker((0, json.dumps(reply), ""))).start(MANIFEST, clients=KEYS)

    def test_daemon_pattern_is_the_schemas(self):
        with open(os.path.join(REPO, broker.SCHEMA)) as f:
            pattern = json.load(f)["$defs"]["daemon"]["pattern"]
        self.assertEqual(f"^{DAEMON.pattern}$", pattern)
        started = Broker(FakeBroker(success("start-output-old-style-daemon"))).start(
            MANIFEST, clients=KEYS
        )
        self.assertEqual(started.daemon, data("start-output-old-style-daemon")["daemon"])

    def test_a_start_reply_without_a_usable_daemon_is_refused(self):
        for case in (
            "start-output-missing-daemon",
            "start-output-daemon-path",
            "start-output-daemon-empty",
        ):
            with (
                self.subTest(case=case),
                self.assertRaisesRegex(SessionError, "bad response to start"),
            ):
                Broker(FakeBroker((0, json.dumps(data(case)), ""))).start(MANIFEST, clients=KEYS)
        reply = {**data("start-output"), "daemon": "a" * 129}
        with self.assertRaisesRegex(SessionError, "bad response to start: fields"):
            Broker(FakeBroker((0, json.dumps(reply), ""))).start(MANIFEST, clients=KEYS)

    def test_build_passes_only_the_package_and_its_daemon(self):
        fake = FakeBroker(success("build-cached"))
        Broker(fake).build("/opt/ensemble", daemon=LOCAL_ID)
        self.assertEqual(fake.calls, [[BROKER, *data("build-input")["argv"]]])

    def test_build_passes_plugins(self):
        fake = FakeBroker(success("build-cached"))
        Broker(fake).build(
            "/opt/ensemble",
            daemon=LOCAL_ID,
            plugins=(UPDRAFT, BETA),
        )
        argv = data("build-input-plugins")["argv"]
        self.assertEqual(argv[-2:], ["--max-age", "604800"])
        self.assertEqual(fake.calls, [[BROKER, *argv[:-2]]])

    def test_build_progress_before_the_error_object_is_not_the_error(self):
        progress = "#5 [2/3] RUN apt-get purge -y sudo\n#5 ERROR: process exited 1\n"
        rc, _, err = failure("error-build_failed")
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", progress + err))).build("/opt/ensemble", daemon=LOCAL_ID)
        self.assertEqual(raised.exception.code, "build_failed")
        found = Broker(FakeBroker((0, text("build-cached"), progress))).build(
            "/opt/ensemble", daemon=LOCAL_ID
        )
        self.assertEqual(found[0], data("build-cached")["image"])
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", "#7 partial\n" + err))).build(
                "/opt/ensemble", daemon=LOCAL_ID
            )
        self.assertEqual(raised.exception.code, "build_failed")

    def test_only_build_may_put_progress_before_the_error_object(self):
        rc, _, err = failure("error-login_required")
        with self.assertRaisesRegex(SessionError, "bad response to start") as raised:
            Broker(FakeBroker((rc, "", "progress\n" + err))).start(MANIFEST, clients=KEYS)
        self.assertNotIsInstance(raised.exception, BrokerError)

    def test_mismatched_exit_is_transport_failure(self):
        for result in (
            failure("error-login_required", rc=9),
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
            (0, '{"protocol_version": "30"}\n', ""),
            FileNotFoundError(2, "No such file", BROKER),
            PermissionError(13, "Permission denied", BROKER),
        ):
            with self.subTest(result=result):
                fake = FakeBroker(result)
                with self.assertRaisesRegex(SessionError, "broker/install.sh .*retrust") as raised:
                    Broker(fake).version()
                self.assertIn(BROKER, str(raised.exception))
                self.assertEqual(fake.calls, [argv("version")])

    def test_a_client_key_set_other_than_the_schemas_is_bad(self):
        for keys in (frozenset({"claude"}), frozenset({"claude", "codex", "other"})):
            with self.subTest(keys=keys), self.assertRaisesRegex(SessionError, "bad response"):
                Broker(FakeBroker(success("start-output"))).start(MANIFEST, clients=keys)


class LockTest(Case):
    def test_busy_error_keeps_its_holder(self):
        for name in ("error-busy", "error-busy-starting", "error-busy-removed"):
            with self.subTest(name), self.assertRaises(BrokerError) as raised:
                Broker(FakeBroker(failure(name))).start(MANIFEST, clients=KEYS)
            error = data(name)["error"]
            self.assertEqual((raised.exception.exit_code, raised.exception.code), (75, "busy"))
            self.assertEqual(raised.exception.holder, error["holder"])
            self.assertEqual(raised.exception.cleanup, error["cleanup"])

    def test_holder_only_with_busy(self):
        for name, call in (
            ("error-busy-missing-holder", lambda b: b.start(MANIFEST, clients=KEYS)),
            ("error-nonbusy-holder", lambda b: b.start(MANIFEST, clients=KEYS)),
        ):
            with self.subTest(name), self.assertRaisesRegex(SessionError, "bad response") as raised:
                call(Broker(FakeBroker(failure(name))))
            self.assertNotIsInstance(raised.exception, BrokerError)
        bad = data("error-busy")
        for holder in ({"container": "c"}, {"container": "c", "owner": "o\x1b"}, "c"):
            with self.subTest(holder), self.assertRaisesRegex(SessionError, "bad response"):
                error = {**bad, "error": {**bad["error"], "holder": holder}}
                Broker(FakeBroker((75, "", json.dumps(error)))).start(MANIFEST, clients=KEYS)

    def test_build_argv_order(self):
        fake = FakeBroker(success("build-cached"))
        Broker(fake).build(
            "/opt/ensemble",
            daemon=LOCAL_ID,
            plugins=(BETA,),
            base_image="dummy-env:test",
            image_plugins=("dummy-plugin", "other.plugin"),
        )
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-base-image")["argv"][:-2]]])
        fake = FakeBroker(success("build-cached"))
        Broker(fake).build(
            "/opt/ensemble",
            daemon=LOCAL_ID,
            plugins=(BETA,),
            image_plugins=("dummy-plugin", "other.plugin"),
        )
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-image-plugins")["argv"][:-2]]])


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
