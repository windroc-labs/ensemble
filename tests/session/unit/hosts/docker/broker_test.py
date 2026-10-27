"""hosts/docker/broker.py: protocol-1 calls, every shared response fixture's outcome, the manifest."""

import io
import json
import logging
import os
import stat
import tempfile
from unittest import mock

from ensemble import session
from ensemble.session.engine.types import ConfigError, SessionError
from ensemble.session.hosts.docker import broker
from ensemble.session.hosts.docker.broker import BROKER, Broker, BrokerError
from ensemble.session.hosts.docker.types import Started
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
    "maintain_output": ("maintain", lambda b: b.maintain(CONTAINER, MANIFEST, root=REPO)),
    "close_output": ("close", lambda b: b.close(CONTAINER)),
    "workspace_rm_output": ("workspace-rm", lambda b: b.workspace_rm(VOLUME)),
}
COMMANDS = {
    "probe-provider": CALLS["probe_provider_output"],
    "version": CALLS["version_output"],
    "build": CALLS["build_output"],
    "start": CALLS["start_output"],
    "maintain": CALLS["maintain_output"],
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
        for fixture in ("start-output.json", "maintain-output.json", "close-output.json"):
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

    def test_maintain_notices_are_strict_and_pass_delivered_keys(self):
        good = data("maintain-output-static.json")
        fake = FakeBroker((0, json.dumps(good), ""))
        found = Broker(fake).maintain(
            CONTAINER,
            MANIFEST,
            delivered=("ensemble-broker-env/db@2:7d", "ensemble-broker-claude/w@1:rotated"),
            root=REPO,
        )
        self.assertEqual((found.notices, found.next_check), (good["notices"], good["next_check"]))
        self.assertEqual(
            fake.calls[0][1:],
            [
                *("maintain", "--container", CONTAINER, "--manifest", MANIFEST),
                *(
                    "--delivered",
                    "ensemble-broker-env/db@2:7d",
                    "--delivered",
                    "ensemble-broker-claude/w@1:rotated",
                ),
            ],
        )
        notice = good["notices"][0]
        for change in (
            {"next_check": "2026-02-30T00:00:00Z"},
            {"next_check": "soon"},
            {"container": "wrong"},
            {"token": "secret"},
            {"notices": {}},
            {"notices": [notice | {"token": "secret"}]},
            {"notices": [{"key": notice["key"]}]},
            {"notices": [notice | {"key": "other/x@1:30d"}]},
            {"notices": [notice | {"key": "ensemble-broker-bot/x@1:30d"}]},
            {"notices": [notice | {"key": "ensemble-other/x@1:30d"}]},
            {"notices": [notice | {"key": 1}]},
            {"notices": [notice | {"key": "ensemble-broker-env/db@1:2d"}]},
            {"notices": [notice | {"text": "two\nlines"}]},
            {"notices": [notice | {"text": ""}]},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(SessionError, "bad response"):
                Broker(FakeBroker((0, json.dumps(good | change), ""))).maintain(
                    CONTAINER, MANIFEST, root=REPO
                )
        for key in (
            "ensemble-broker-claude/default@1:30d",
            "ensemble-broker-opencode/strata@2:rotated",
            "ensemble-broker-env/db@3:expired",
        ):
            response = good | {"notices": [notice | {"key": key}]}
            found = Broker(FakeBroker((0, json.dumps(response), ""))).maintain(
                CONTAINER, MANIFEST, root=REPO
            )
            self.assertEqual(found.notices[0]["key"], key)
        # An empty list reads no schema.
        quiet = good | {"notices": [], "next_check": None}
        empty = Broker(FakeBroker((0, json.dumps(quiet), ""))).maintain(
            CONTAINER, MANIFEST, root="/nonexistent"
        )
        self.assertEqual((empty.notices, empty.next_check), ([], None))

    def test_maintain_keeps_each_components_outcome_and_passes_its_options(self):
        good = data("maintain-output.json")
        fake = FakeBroker((0, json.dumps(good), ""))
        found = Broker(fake).maintain(
            CONTAINER, MANIFEST, ["ensemble-broker-env/db@2:7d"], renew=True, root=REPO
        )
        self.assertEqual(
            fake.calls,
            [
                [
                    BROKER,
                    *("maintain", "--container", CONTAINER, "--manifest", MANIFEST, "--renew"),
                    *("--delivered", "ensemble-broker-env/db@2:7d"),
                ]
            ],
        )
        self.assertEqual(
            (found.results, found.notices, found.next_check, found.deadline),
            (good["results"], [], "2026-10-17T12:01:00Z", good["deadline"]),
        )
        self.assertEqual(found.results[0]["error"]["code"], "refresh_failed")
        self.assertEqual(found.results[1]["expires_at"], "2026-10-17T13:00:00Z")
        plain = FakeBroker(success("maintain-output-static.json"))
        static = Broker(plain).maintain(CONTAINER, MANIFEST, root=REPO)
        self.assertEqual(plain.calls, [argv("maintain")])
        self.assertEqual((static.results, static.deadline, len(static.notices)), ([], None, 1))

    def test_maintain_rejects_malformed_replies(self):
        good = data("maintain-output.json")
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
                Broker(FakeBroker((0, json.dumps(good | change), ""))).maintain(
                    CONTAINER, MANIFEST, root=REPO
                )
        missing = {k: v for k, v in good.items() if k != "deadline"}
        with self.assertRaisesRegex(SessionError, "bad response to maintain"):
            Broker(FakeBroker((0, json.dumps(missing), ""))).maintain(
                CONTAINER, MANIFEST, root=REPO
            )

    def test_start_requires_a_valid_deadline(self):
        response = data("start-output-codex.json")
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

    def test_notice_keys_are_the_schemas(self):
        with open(os.path.join(REPO, broker.SCHEMA), encoding="utf-8") as f:
            schema = json.load(f)["$defs"]["notice_key"]["pattern"]
        self.assertEqual(broker.notice_key(REPO).pattern, schema)
        self.assertEqual(broker.hook_root(HOOK), REPO)

    def test_an_unreadable_notice_key_is_a_session_error(self):
        response = data("maintain-output-static.json")
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, broker.SCHEMA)
            os.makedirs(os.path.dirname(path))
            for text in (
                '{"$defs": {}}',
                "not json",
                '{"$defs": {"notice_key": {"pattern": "("}}}',
            ):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                fake = FakeBroker((0, json.dumps(response), ""))
                with self.subTest(text=text), self.assertRaises(SessionError) as raised:
                    Broker(fake).maintain(CONTAINER, MANIFEST, root=root)
                self.assertNotIsInstance(raised.exception, BrokerError)
                self.assertIn(broker.SCHEMA, str(raised.exception))
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            for read in (broker.notice_key, broker.client_keys):
                with self.subTest(read=read), self.assertRaisesRegex(ConfigError, "unreadable"):
                    read(root)

    def test_maintain_errors_and_audit_warning(self):
        warned = []
        response = data("maintain-output-audit-warning.json")
        found = Broker(FakeBroker((0, json.dumps(response), "")), warning=warned.append).maintain(
            CONTAINER, MANIFEST, root=REPO
        )
        self.assertEqual((len(found.notices), warned), (1, [broker.AUDIT_WARNING]))
        for code, exit_code in (("manifest_rejected", 3), ("internal_error", 70), ("usage", 2)):
            error = {"command": "maintain", "code": code, "message": "No.", "cleanup": "not_needed"}
            fake = FakeBroker((exit_code, "", json.dumps({"exit_code": exit_code, "error": error})))
            with self.subTest(code=code), self.assertRaises(BrokerError) as raised:
                Broker(fake).maintain(CONTAINER, MANIFEST, root=REPO)
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
            Broker(fake).maintain(CONTAINER, MANIFEST, root=REPO)

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
                github_user_id=343764394,
                deadline=None,
            ),
        )
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

    def test_build_passes_only_the_package(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build("/opt/ensemble")
        self.assertEqual(fake.calls, [[BROKER, *data("build-input.json")["argv"]]])
        self.assertFalse(hasattr(broker, "REFRESH"))

    def test_build_passes_plugins(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build(
            "/opt/ensemble",
            plugins=(
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
        )
        argv = data("build-input-plugins.json")["argv"]
        self.assertEqual(argv[-2:], ["--max-age", "604800"])
        self.assertEqual(fake.calls, [[BROKER, *argv[:-2]]])

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
            (0, '{"protocol_version": "30"}\n', ""),
            FileNotFoundError(2, "No such file", BROKER),
            PermissionError(13, "Permission denied", BROKER),
        ):
            with self.subTest(result=result):
                fake = FakeBroker(result)
                with self.assertRaisesRegex(SessionError, "#docker-install") as raised:
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


class LockTest(Case):
    def test_busy_error_keeps_its_holder(self):
        for name in ("error-busy.json", "error-busy-starting.json", "error-busy-removed.json"):
            with self.subTest(name), self.assertRaises(BrokerError) as raised:
                Broker(FakeBroker(failure(name))).start(MANIFEST, clients=KEYS)
            error = data(name)["error"]
            self.assertEqual((raised.exception.exit_code, raised.exception.code), (75, "busy"))
            self.assertEqual(raised.exception.holder, error["holder"])
            self.assertEqual(raised.exception.cleanup, error["cleanup"])

    def test_holder_only_with_busy(self):
        for name, call in (
            ("error-busy-missing-holder.json", lambda b: b.start(MANIFEST, clients=KEYS)),
            ("error-nonbusy-holder.json", lambda b: b.start(MANIFEST, clients=KEYS)),
        ):
            with self.subTest(name), self.assertRaisesRegex(SessionError, "bad response") as raised:
                call(Broker(FakeBroker(failure(name))))
            self.assertNotIsInstance(raised.exception, BrokerError)
        bad = data("error-busy.json")
        for holder in ({"container": "c"}, {"container": "c", "owner": "o\x1b"}, "c"):
            with self.subTest(holder), self.assertRaisesRegex(SessionError, "bad response"):
                error = {**bad, "error": {**bad["error"], "holder": holder}}
                Broker(FakeBroker((75, "", json.dumps(error)))).start(MANIFEST, clients=KEYS)

    def test_build_argv_order(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build(
            "/opt/ensemble",
            plugins=("beta@owner/market#" + "b" * 40,),
            base_image="dummy-env:test",
            image_plugins=("dummy-plugin", "other.plugin"),
        )
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-base-image.json")["argv"][:-2]]])
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build(
            "/opt/ensemble",
            plugins=("beta@owner/market#" + "b" * 40,),
            image_plugins=("dummy-plugin", "other.plugin"),
        )
        self.assertEqual(
            fake.calls, [[BROKER, *data("build-input-image-plugins.json")["argv"][:-2]]]
        )


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
            github="ensemble-broker-bot",
            permissions={"contents": "write", "statuses": "read"},
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f)["github"],
                {
                    "app": "ensemble-broker-bot",
                    "permissions": {"contents": "write", "statuses": "read"},
                },
            )
        broker.write_manifest(
            path,
            name="w",
            generation="0" * 32,
            image="ensemble-worker:" + "a" * 64,
            cwd="/work",
            workspace="ensemble-ws-w",
            home=("x_dir", "/home/x"),
            hook_path="/plugin/bin/ensemble-session",
            github="ensemble-broker-bot",
            permissions={"contents": "write"},
            repo="o/out",
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f)["github"],
                {
                    "app": "ensemble-broker-bot",
                    "permissions": {"contents": "write"},
                    "repo": "o/out",
                },
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

    def test_an_environment_reaches_the_manifest_only_when_set(self):
        path = os.path.join(self.home, "state", "g.json")
        base = {
            "name": "w",
            "generation": "0" * 32,
            "image": "ensemble-worker:" + "a" * 64,
            "cwd": "/work",
            "workspace": "ensemble-ws-w",
            "home": ("claude_dir", "/home/claude"),
            "hook_path": "/plugin/bin/ensemble-session",
            "github": "",
        }
        secret = {"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}
        broker.write_manifest(
            path,
            **base,
            gateway="mini-op",
            secrets=(secret,),
            lock="dummy-lock",
            lock_owner="w",
            prefetch={"repo": "o/r", "ref": "main"},
        )
        with open(path) as f:
            manifest = json.load(f)
        self.assertEqual(
            {
                key: manifest[key]
                for key in ("gateway", "secrets", "lock", "lock_owner", "prefetch")
            },
            {
                "gateway": "mini-op",
                "secrets": [secret],
                "lock": "dummy-lock",
                "lock_owner": "w",
                "prefetch": {"repo": "o/r", "ref": "main"},
            },
        )
        broker.write_manifest(path, **base)
        with open(path) as f:
            manifest = json.load(f)
        self.assertFalse({"gateway", "secrets", "lock", "lock_owner", "prefetch"} & set(manifest))


class ManifestCredentialsTest(Case):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.home, "state", "g.json")
        fixture = data("manifest-credentials.json")
        self.base = {
            "name": fixture["name"],
            "generation": fixture["generation"],
            "image": fixture["image"],
            "cwd": fixture["cwd"],
            "workspace": fixture["workspace"],
            "home": ("claude_dir", fixture["claude_dir"]),
            "hook_path": fixture["hook_path"],
            "github": "ensemble-broker-bot",
        }

    def written(self, **kw):
        broker.write_manifest(self.path, **{**self.base, **kw})
        with open(self.path) as f:
            return json.load(f)

    def test_the_request_is_written_in_the_manifest_form(self):
        request = session.CredentialRequest.from_fields(data("credential-request-all.json"))
        fixture = data("manifest-credentials.json")
        found = self.written(credentials=request)
        self.assertEqual(found["credentials"], fixture["credentials"])
        self.assertEqual(found, {**fixture, "labels": {}})
        self.assertEqual(
            broker.manifest_credentials(request, "ensemble-broker-bot"), fixture["credentials"]
        )
        self.assertEqual(broker.manifest_credentials(session.CredentialRequest(), ""), {})

    def test_github_grants_need_the_app(self):
        request = session.CredentialRequest.from_fields(
            data("credential-request-github-default.json")
        )
        with self.assertRaisesRegex(
            ConfigError, r"\Acredentials github: needs \[hosts.docker.github\] app\Z"
        ):
            self.written(github="", credentials=request)
        with self.assertRaisesRegex(ConfigError, "credentials github"):
            broker.manifest_credentials(request, "")
        self.assertFalse(os.path.exists(self.path))

    def test_a_linear_request_keeps_the_legacy_github(self):
        request = session.CredentialRequest(linear={"role": "engineer"})
        found = self.written(credentials=request, permissions={"contents": "read"})
        self.assertEqual(found["credentials"], {"linear": {"role": "engineer"}})
        self.assertEqual(
            found["github"], {"app": "ensemble-broker-bot", "permissions": {"contents": "read"}}
        )
        found = self.written(github="", credentials=request)
        self.assertIsNone(found["github"])

    def test_one_source_per_kind(self):
        github = session.CredentialRequest.from_fields(
            data("credential-request-github-default.json")
        )
        env = session.CredentialRequest(
            env=({"item": "ensemble-broker-env/dummy", "file": "dummy.txt"},)
        )
        secret = {"item": "ensemble-broker-env/other", "file": "other.txt"}
        for kw in (
            {"credentials": github, "permissions": {"contents": "read"}},
            {"credentials": github, "permissions": {}},
            {"credentials": github, "repo": "o/r"},
            {"credentials": env, "secrets": (secret,)},
        ):
            with self.subTest(kw), self.assertRaises(ConfigError):
                self.written(**kw)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.written(credentials=env)["credentials"], env.persisted())
        self.assertEqual(
            self.written(
                credentials=session.CredentialRequest(linear={"role": "pm"}), secrets=(secret,)
            )["secrets"],
            [secret],
        )

    def test_no_request_writes_no_credentials(self):
        self.assertNotIn("credentials", self.written())
        self.assertEqual(self.written(credentials=session.CredentialRequest())["credentials"], {})


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
