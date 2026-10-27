"""The live Docker gate's checks, fed recorded shapes of the audit log, docker inspect, results and PRs."""

import io
import json
import os
import re
import types
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from tests.session import live_docker as gate
from tests.session.case import Case

G1, G2 = "a" * 32, "b" * 32
FAKE = "ghs_" + "x" * 36
CWD = "/Users/u/work/live"
CLAUDE_DIR = "/Users/u/.ensemble/docker-state/live/claude"
DOCS = os.path.join(os.path.dirname(gate.__file__), "..", "..", "docs", "architecture.md")


def token(result, account="acme", installation=1, **extra):
    return {
        "kind": "github-installation-token",
        "result": result,
        "account": account,
        "installation": installation,
        "expires_at": "2026-10-18T13:00:00Z",
        **extra,
    }


def line(event, generation=G1, *credentials, time="2026-10-18T12:00:00.000000001Z"):
    return {
        "time": time,
        "command": event,
        "event": event,
        "session": "live",
        "generation": generation,
        "container": f"ensemble-live-{generation}",
        "credentials": list(credentials),
    }


def withdrawn(kind="claude-setup-token"):
    return {"kind": kind, "result": "withdrawn"}


NETWORK = "egress-workers"


def inspect(**changes):
    found = {
        "Config": {"User": "agent", "Env": [f"GH_TOKEN={FAKE}"]},
        "HostConfig": {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            "NetworkMode": NETWORK,
            "PidMode": "",
            "IpcMode": "private",
        },
        "NetworkSettings": {"Networks": {NETWORK: {}}},
        "Mounts": [
            {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD},
            {"Type": "bind", "Source": CLAUDE_DIR, "Destination": CLAUDE_DIR},
            {"Type": "tmpfs", "Destination": "/run/ensemble/secrets"},
        ],
    }
    for key, value in changes.items():
        part, _, field = key.partition("__")
        if field:
            found[part][field] = value
        else:
            found[part] = value
    return found


def view(*, login="app/windroc-bot", draft=True, created="2026-10-18T12:10:00Z"):
    return {"author": {"login": login}, "isDraft": draft, "createdAt": created, "number": 7}


class AuditLinesTest(Case):
    def test_skips_blank_and_malformed_lines(self):
        text = "\n".join([json.dumps(line("start")), "", "{oops", "[1]", json.dumps(line("close"))])
        self.assertEqual([x["event"] for x in gate.audit_lines(text)], ["start", "close"])


class ProxyBaselineTest(Case):
    def setUp(self):
        self.egress = {
            "Config": {
                "Labels": {
                    "windroc.egress.contract": "1",
                    "windroc.egress.networks": "mini-op,workers",
                    "windroc.egress.network.workers.name": "egress-workers",
                    "windroc.egress.network.workers.proxy": "http://egress-proxy-workers:4750",
                }
            },
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Networks": {"egress-workers": {}}},
        }
        self.network = {"Name": "egress-workers", "Internal": True}
        proxy = "http://egress-proxy-workers:4750"
        self.worker = {
            "Config": {
                "Env": [
                    *(
                        f"{k}={proxy}"
                        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
                    ),
                    "NO_PROXY=localhost,127.0.0.1,::1",
                    "no_proxy=localhost,127.0.0.1,::1",
                ]
            }
        }

    def check(self):
        return gate.proxy_baseline(self.egress, self.network, self.worker)

    def test_valid_contract_and_environment(self):
        self.assertEqual(self.check(), [])

    def test_starting_and_stopped_are_rejected(self):
        for state in ({"Running": False}, {"Running": True, "Health": {"Status": "starting"}}):
            self.egress["State"] = state
            self.assertTrue(self.check())

    def test_each_missing_label_is_rejected(self):
        labels = self.egress["Config"]["Labels"]
        for key in list(labels):
            value = labels.pop(key)
            self.assertTrue(self.check(), key)
            labels[key] = value

    def test_network_must_be_internal_and_attached(self):
        self.network["Internal"] = False
        self.assertTrue(self.check())
        self.network["Internal"] = True
        self.egress["NetworkSettings"]["Networks"] = {}
        self.assertTrue(self.check())

    def test_missing_proxy_variable_is_rejected(self):
        self.worker["Config"]["Env"].pop(0)
        self.assertEqual(self.check(), ["HTTP_PROXY differs from proxy label"])

    def test_host_bypass_is_rejected_independently_for_each_case(self):
        valid = self.worker["Config"]["Env"][:]
        for key in ("NO_PROXY", "no_proxy"):
            with self.subTest(key=key):
                self.worker["Config"]["Env"] = [
                    f"{key}=localhost,127.0.0.1,::1,host.docker.internal"
                    if item.startswith(key + "=")
                    else item
                    for item in valid
                ]
                self.assertEqual(self.check(), [f"{key} must bypass only container loopback"])

    def test_probe_requires_proxy_decision_and_real_curl(self):
        for kind, status, code in (
            ("public", "200 200", 0),
            ("denied", "407 000", 56),
            ("direct", "000 000", 7),
        ):
            self.assertEqual(gate.probe_problems(kind, status, code), [])
        for kind, status, code in (
            ("public", "000 000", 0),
            ("denied", "000 000", 7),
            ("denied", "200 200", 0),
            ("direct", "000 000", 125),
            ("direct", "000 200", 0),
        ):
            self.assertTrue(gate.probe_problems(kind, status, code))


class NetworkProbeTest(Case):
    def setUp(self):
        self.worker = gate.Worker(
            gate.Args("worker", "live", client="claude", cwd=CWD), "mgr", "docker"
        )
        self.proxy = "http://egress-proxy-workers:4750"
        self.route = ("  interface: en0\n", "")
        self.lan = ("192.168.1.20\n", "")

    def test_host_controls_and_every_worker_probe(self):
        with (
            mock.patch.object(
                gate, "call", side_effect=[self.route, self.lan, ("", ""), ("", "")]
            ) as call,
            mock.patch.object(
                gate, "listener", side_effect=[nullcontext(32101), nullcontext(32102)]
            ) as listen,
            mock.patch.object(self.worker, "probe") as probe,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.worker.network("container", self.proxy)
        self.assertEqual(listen.call_args_list, [mock.call("127.0.0.1"), mock.call("192.168.1.20")])
        self.assertEqual(
            probe.call_args_list,
            [
                mock.call("container", self.proxy, "https://example.com", "public"),
                mock.call("container", "", "https://1.1.1.1", "direct"),
                mock.call("container", self.proxy, "http://host.docker.internal:32101/", "denied"),
                mock.call("container", "", "http://host.docker.internal:32101/", "direct"),
                mock.call("container", self.proxy, "http://192.168.1.20:32102/", "denied"),
                mock.call("container", "", "http://192.168.1.20:32102/", "direct"),
            ],
        )
        self.assertEqual(
            call.call_args_list[:2],
            [
                mock.call(["route", "-n", "get", "default"]),
                mock.call(["ipconfig", "getifaddr", "en0"]),
            ],
        )
        for index, host, port in ((2, "127.0.0.1", 32101), (3, "192.168.1.20", 32102)):
            self.assertEqual(
                call.call_args_list[index].args[0],
                [
                    "curl",
                    "--noproxy",
                    "*",
                    "-fsS",
                    "-o",
                    "/dev/null",
                    "--max-time",
                    "5",
                    f"http://{host}:{port}/",
                ],
            )

    def test_lan_control_needs_one_default_interface_and_a_private_ipv4(self):
        two = ("  interface: en0\n  interface: en1\n", "")
        cases = [
            ([("", "")], "no unique default interface"),
            ([two], "no unique default interface"),
            *(
                ([self.route, (address, "")], "private LAN IPv4")
                for address in ("8.8.8.8\n", "127.0.0.1\n", "fd00::1\n", "\n")
            ),
        ]
        for replies, message in cases:
            with (
                self.subTest(replies=replies),
                mock.patch.object(gate, "call", side_effect=replies),
                mock.patch.object(gate, "listener") as listen,
                mock.patch.object(self.worker, "probe"),
                self.assertRaisesRegex(SystemExit, message),
            ):
                self.worker.network("container", self.proxy)
            listen.assert_not_called()

    def test_unreachable_host_control_cannot_pass_denial(self):
        with (
            mock.patch.object(
                gate, "call", side_effect=[self.route, self.lan, SystemExit("host control failed")]
            ),
            mock.patch.object(gate, "listener", return_value=nullcontext(32101)),
            mock.patch.object(self.worker, "probe") as probe,
            self.assertRaisesRegex(SystemExit, "host control failed"),
        ):
            self.worker.network("container", self.proxy)
        self.assertEqual(probe.call_count, 2)

    def test_curl_uses_supplied_proxy_and_propagates_a_failed_denial(self):
        response = types.SimpleNamespace(stdout="407 000", returncode=56)
        with (
            mock.patch.object(gate.subprocess, "run", return_value=response) as run,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.worker.probe(
                "container", self.proxy, "http://host.docker.internal:32101/", "denied"
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["docker", "exec", "container", "curl"])
        self.assertEqual(
            argv[-6:],
            [
                "--noproxy",
                "",
                "--proxytunnel",
                "--proxy",
                self.proxy,
                "http://host.docker.internal:32101/",
            ],
        )
        self.assertEqual(argv[argv.index("-o") + 1], "/dev/null")
        response.stdout, response.returncode = "000 000", 7
        with (
            mock.patch.object(gate.subprocess, "run", return_value=response),
            self.assertRaisesRegex(SystemExit, "FAIL network denied"),
        ):
            self.worker.probe(
                "container", self.proxy, "http://host.docker.internal:32101/", "denied"
            )

    def test_direct_probe_explicitly_bypasses_all_proxies(self):
        response = types.SimpleNamespace(stdout="000 000", returncode=28)
        with (
            mock.patch.object(gate.subprocess, "run", return_value=response) as run,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.worker.probe("container", "", "https://1.1.1.1", "direct")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["--noproxy", "*", "https://1.1.1.1"])
        self.assertNotIn("--proxy", argv)

    def test_listener_releases_server_and_thread_on_failure(self):
        with (
            mock.patch.object(gate, "HTTPServer") as server_type,
            mock.patch.object(gate.threading, "Thread") as thread_type,
        ):
            server = server_type.return_value.__enter__.return_value
            server.server_port = 32101
            with (
                self.assertRaisesRegex(RuntimeError, "consumer failed"),
                gate.listener("127.0.0.1") as port,
            ):
                self.assertEqual(port, 32101)
                raise RuntimeError("consumer failed")
            server.shutdown.assert_called_once_with()
            thread_type.return_value.start.assert_called_once_with()
            thread_type.return_value.join.assert_called_once_with()


class ReclaimedTest(Case):
    def test_revoked_and_withdrawn(self):
        lines = [
            line("start", G1, token("issued")),
            line("close", G1, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_opencode_has_no_client_credential(self):
        lines = [line("close", G1, token("revoked"))]
        self.assertEqual(gate.reclaimed(lines, G1, "opencode"), [])
        self.assertEqual(len(gate.reclaimed([line("close", G1)], G1, "opencode")), 1)

    def test_codex_withdrawal(self):
        lines = [line("close", G1, token("revoked"), withdrawn("codex-access-token"))]
        self.assertEqual(gate.reclaimed(lines, G1, "codex"), [])
        self.assertEqual(len(gate.reclaimed(lines, G1, "claude")), 1)

    def test_pending_names_its_reason(self):
        lines = [line("close", G1, token("pending", reason="network down"), withdrawn())]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("pending", problem)
        self.assertIn("network down", problem)

    def test_expired_unrevoked(self):
        lines = [
            line("close", G1, token("pending", reason="offline"), withdrawn()),
            line("revocation-retry", G1, token("expired-unrevoked")),
        ]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("expired-unrevoked", problem)

    def test_pending_then_revoked_by_retry(self):
        lines = [
            line("close", G1, token("pending", reason="offline"), withdrawn()),
            line("revocation-retry", G1, token("revoked")),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_every_installation_counts(self):
        lines = [line("close", G1, token("revoked"), token("pending", "other", 2), withdrawn())]
        (problem,) = gate.reclaimed(lines, G1, "claude")
        self.assertIn("other", problem)

    def test_no_close_line(self):
        (problem,) = gate.reclaimed([line("start", G1, token("issued"))], G1, "claude")
        self.assertIn("no close", problem)

    def test_no_github_token(self):
        (problem,) = gate.reclaimed([line("close", G1, withdrawn())], G1, "claude")
        self.assertIn("github-installation-token", problem)

    def test_no_withdrawal(self):
        (problem,) = gate.reclaimed([line("close", G1, token("revoked"))], G1, "claude")
        self.assertIn("withdrawn", problem)

    def test_other_generation_ignored(self):
        lines = [
            line("close", G2, token("pending", reason="offline")),
            line("close", G1, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G1, "claude"), [])

    def test_role_generation(self):
        lines = [
            line("start", G2, token("issued")),
            line("refresh", G2, token("renewed")),
            line("close", G2, token("revoked"), withdrawn()),
        ]
        self.assertEqual(gate.reclaimed(lines, G2, "claude"), [])


class RemovedTokensTest(Case):
    def test_revoked_later(self):
        lines = [line("start", G1, token("issued")), line("close", G1, token("revoked"))]
        self.assertEqual(gate.removed_tokens(lines, G1), f"removed {G1}: github tokens revoked 1/1")

    def test_never_revoked(self):
        lines = [line("start", G1, token("issued"), token("issued", "other", 2))]
        self.assertEqual(
            gate.removed_tokens(lines, G1),
            f"removed {G1}: github tokens not revoked (revoked 0/2), expires 2026-10-18T13:00:00Z",
        )


class BaselineTest(Case):
    def check(self, found, plan=(CLAUDE_DIR,)):
        return gate.baseline(found, "live", CWD, list(plan), NETWORK)

    def test_compliant_claude(self):
        self.assertEqual(self.check(inspect()), [])

    def test_compliant_codex(self):
        sessions = "/Users/u/.ensemble/docker-state/live/codex/sessions"
        mounts = [
            {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD},
            {"Type": "bind", "Source": sessions, "Destination": sessions},
        ]
        self.assertEqual(self.check(inspect(Mounts=mounts), (sessions,)), [])

    def test_docker_desktop_shared_folder_source(self):
        volume = {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD}
        bind = {"Type": "bind", "Source": "/host_mnt" + CLAUDE_DIR, "Destination": CLAUDE_DIR}
        self.assertEqual(self.check(inspect(Mounts=[volume, bind])), [])
        inside = {"Type": "bind", "Source": "/host_mnt" + CWD + "/src", "Destination": "/w"}
        problems = gate.baseline(
            inspect(Mounts=[volume, inside]), "live", CWD, [CWD + "/src"], NETWORK
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cwd", problems[0])

    def test_no_extra_hosts(self):
        for hosts in (None, []):
            with self.subTest(hosts=hosts):
                self.assertEqual(self.check(inspect(HostConfig__ExtraHosts=hosts)), [])

    def test_no_new_privileges_with_value(self):
        self.assertEqual(
            self.check(inspect(HostConfig__SecurityOpt=["no-new-privileges:true"])), []
        )

    def test_each_violation_alone(self):
        volume = {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD}
        bind = {"Type": "bind", "Source": CLAUDE_DIR, "Destination": CLAUDE_DIR}
        cases = {
            "cap": inspect(HostConfig__CapDrop=["NET_RAW"]),
            "no-new-privileges": inspect(HostConfig__SecurityOpt=[]),
            "user": inspect(Config__User="root"),
            "privileged": inspect(HostConfig__Privileged=True),
            "network": inspect(HostConfig__NetworkMode="host"),
            "network none": inspect(HostConfig__NetworkMode="none"),
            "network bridge": inspect(HostConfig__NetworkMode="bridge"),
            "second network": inspect(NetworkSettings={"Networks": {NETWORK: {}, "bridge": {}}}),
            "extra hosts": inspect(HostConfig__ExtraHosts=["host.docker.internal:host-gateway"]),
            "pid": inspect(HostConfig__PidMode="host"),
            "ipc": inspect(HostConfig__IpcMode="host"),
            "extra volume": inspect(
                Mounts=[volume, bind, {"Type": "volume", "Name": "x", "Destination": "/x"}]
            ),
            "wrong volume target": inspect(Mounts=[{**volume, "Destination": "/elsewhere"}, bind]),
            "bind not in plan": inspect(
                Mounts=[volume, bind, {"Type": "bind", "Source": "/opt/x", "Destination": "/opt/x"}]
            ),
        }
        for label, found in cases.items():
            with self.subTest(label):
                self.assertEqual(len(self.check(found)), 1, self.check(found))

    def test_cwd_related_binds(self):
        volume = {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD}
        for source in (CWD, "/Users/u/work", CWD + "/src"):
            with self.subTest(source):
                found = inspect(
                    Mounts=[volume, {"Type": "bind", "Source": source, "Destination": "/w"}]
                )
                problems = gate.baseline(found, "live", CWD, [source], NETWORK)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("cwd", problems[0])

    def test_docker_socket(self):
        volume = {"Type": "volume", "Name": "ensemble-ws-live", "Destination": CWD}
        sock = "/var/run/docker.sock"
        found = inspect(Mounts=[volume, {"Type": "bind", "Source": sock, "Destination": sock}])
        problems = gate.baseline(found, "live", CWD, [sock], NETWORK)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("docker.sock", problems[0])


class EgressProxyTest(Case):
    PROXY = "e" * 64

    def network(self, listing, labels=None):
        found = [{"Config": {"Labels": labels or {}}}]
        replies = iter([(listing, ""), (json.dumps(found), "")])
        with mock.patch.object(gate, "call", side_effect=lambda _argv: next(replies)) as call:
            return gate.egress_proxy("docker")[1], call

    def test_the_proxy_workers_network(self):
        labels = {"windroc.egress.network.workers.name": NETWORK}
        found, call = self.network(self.PROXY + "\n", labels)
        self.assertEqual(found, NETWORK)
        self.assertEqual(
            call.call_args_list[0].args[0],
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "label=windroc.egress.contract",
                "--format",
                "{{.ID}}",
            ],
        )

    def test_no_single_proxy_or_label_fails_naming_the_proxy(self):
        for listing, labels in (
            ("", None),
            (self.PROXY + "\n" + "c" * 64 + "\n", None),
            (self.PROXY + "\n", {}),
        ):
            with (
                self.subTest(listing=listing, labels=labels),
                self.assertRaisesRegex(SystemExit, "egress proxy"),
            ):
                self.network(listing, labels)


class DrainedTest(Case):
    def drained(self, backlog):
        lines = iter(backlog)

        def run(*argv, timeout):
            try:
                return next(lines)
            except StopIteration:
                raise gate.subprocess.TimeoutExpired(argv, timeout) from None

        with mock.patch.object(gate, "run", run):
            return gate.drained(("ensemble-crew", "next-event"), "1540")

    def test_skips_the_backlog(self):
        self.assertEqual(self.drained(["1541 10:00:00 a done", "1542 10:00:01 b done"]), "1542")

    def test_no_backlog(self):
        self.assertEqual(self.drained([]), "1540")


class ZeroCountsTest(Case):
    def test_counts(self):
        self.assertTrue(gate.zero_counts("0\n0\n"))
        self.assertTrue(gate.zero_counts("      0\n\n0\n"))
        self.assertFalse(gate.zero_counts("0\n12\n"))
        self.assertFalse(gate.zero_counts(""))


class TimingTest(Case):
    PHASES = {"checks": 0.1, "image": 0.5, "start": 4.0, "client": 1.0}
    STDERR = (
        "docker timing live: preflight 2.40 s\n"
        "ensemble-broker: start preflight 0.01 s\n"
        f"ensemble-broker: start credentials 0.10 s {FAKE}\n"
    )

    def result(self, **extra):
        return {"phases": self.PHASES, "launched_at": "2026-10-18T14:00:00.000+02:00", **extra}

    def hooks(self, *stamps):
        return "".join(
            json.dumps({"ts": ts, "label": "Stop", "payload": {}}) + "\n" for ts in stamps
        )

    def test_claude_terminal(self):
        hooks = self.hooks("2026-10-18T13:59:59.000+02:00", "2026-10-18T14:00:02.500+02:00")
        self.assertEqual(
            gate.timing(self.result(), hooks, self.STDERR, "live"),
            "checks 0.1 s | image 0.5 s | start 4.0 s | client 1.0 s"
            " | client ready 2.5 s | preflight 2.4 s",
        )

    def test_headless_has_no_launch(self):
        result = {"phases": {"checks": 0.1, "image": 0.2, "start": 3.0, "client": 0.4}}
        self.assertIn("client ready n/a", gate.timing(result, "", "", "live"))

    def test_no_hook_after_launch(self):
        hooks = self.hooks("2026-10-18T13:59:00.000+02:00")
        self.assertIn("client ready n/a", gate.timing(self.result(), hooks, "", "live"))

    def test_stderr_missing(self):
        self.assertIn("preflight n/a", gate.timing(self.result(), "", "", "live"))


class RefreshedAtTest(Case):
    def test_first_renewal_of_that_container(self):
        lines = [
            line("refresh", G2, token("renewed"), time="2026-10-18T12:01:00Z"),
            line("refresh", G1, token("pending", reason="x"), time="2026-10-18T12:02:00Z"),
            line("refresh", G1, token("renewed"), time="2026-10-18T12:03:00Z"),
        ]
        self.assertEqual(gate.refreshed_at(lines, f"ensemble-live-{G1}"), "2026-10-18T12:03:00Z")
        self.assertIsNone(gate.refreshed_at(lines, "ensemble-live-" + "c" * 32))


class NewGenerationTest(Case):
    def test_one_new(self):
        self.assertEqual(gate.new_generation({G1}, {G1, G2}), G2)

    def test_none_or_two(self):
        for after in ({G1}, {G1, G2, "c" * 32}):
            with self.subTest(after=len(after)), self.assertRaises(SystemExit) as raised:
                gate.new_generation({G1}, after, step="restart")
            self.assertIn("restart: want one new generation", str(raised.exception))


class InstallStepsTest(Case):
    PHASES = {"checks": 0.1, "image": 0.5, "start": 4.0, "client": 1.0}
    BROKER = "".join(
        f"ensemble-broker: start {p} 0.10 s\n"
        for p in (
            "preflight",
            "image",
            "checks",
            "create",
            "bootstrap",
            "clients",
            "credentials",
            "github",
            "total",
        )
    )

    def test_clean(self):
        self.assertEqual(gate.install_steps({"phases": self.PHASES}, self.BROKER), [])

    def test_build_phase(self):
        (problem,) = gate.install_steps({"phases": {**self.PHASES, "build": 9.0}}, "")
        self.assertIn("build", problem)

    def test_client_update_is_an_install_step(self):
        stderr = self.BROKER + "ensemble-broker: start client_update 2.00 s\n"
        (problem,) = gate.install_steps({"phases": self.PHASES}, stderr)
        self.assertIn("client_update", problem)

    def test_unknown_broker_phase(self):
        stderr = self.BROKER + "ensemble-broker: start install 2.00 s\n"
        (problem,) = gate.install_steps({"phases": self.PHASES}, stderr)
        self.assertIn("install", problem)


class OpenedDraftTest(Case):
    def test_rules(self):
        self.assertTrue(gate.opened_draft([], is_draft=True))
        self.assertTrue(
            gate.opened_draft(["labeled", "ready_for_review", "convert_to_draft"], is_draft=False)
        )
        self.assertFalse(gate.opened_draft(["convert_to_draft"], is_draft=True))
        self.assertFalse(gate.opened_draft(["labeled"], is_draft=False))


class BotPrTest(Case):
    REFRESHED = "2026-10-18T12:05:00.123456789Z"

    def test_bot_draft_after_refresh(self):
        self.assertEqual(gate.bot_pr(view(), [], self.REFRESHED, need_refresh=True), [])

    def test_human_author(self):
        (problem,) = gate.bot_pr(view(login="someone"), [], self.REFRESHED, need_refresh=True)
        self.assertIn("someone", problem)

    def test_never_draft(self):
        (problem,) = gate.bot_pr(view(draft=False), [], self.REFRESHED, need_refresh=True)
        self.assertIn("draft", problem)

    def test_created_before_refresh(self):
        early = view(created="2026-10-18T12:00:00Z")
        (problem,) = gate.bot_pr(early, [], self.REFRESHED, need_refresh=True)
        self.assertIn("before", problem)

    def test_no_refresh(self):
        (problem,) = gate.bot_pr(view(), [], None, need_refresh=True)
        self.assertIn("no token refresh recorded", problem)

    def test_without_refresh(self):
        self.assertEqual(gate.bot_pr(view(), ["ready_for_review"], None, need_refresh=False), [])


class PendingExceptTest(Case):
    def test_rules(self):
        removed = f"ensemble-live-{G1}"
        entry = {"container": removed, "account": "acme", "expires_at": "2026-10-18T13:00:00Z"}
        self.assertEqual(gate.pending_except({"revocations": []}, removed), [])
        self.assertEqual(gate.pending_except({"revocations": [entry]}, removed), [])
        other = {**entry, "container": f"ensemble-live-{G2}"}
        (problem,) = gate.pending_except({"revocations": [entry, other]}, removed)
        self.assertIn(G2, problem)
        self.assertEqual(len(gate.pending_except({"revocations": [entry]}, None)), 1)


class UnreachableDocTest(Case):
    def test_gate_runs_the_documented_commands(self):
        with open(DOCS, encoding="utf-8") as f:
            text = f.read()
        section = text.split("### What a container cannot reach\n", 1)[1].split("\n#", 1)[0]
        documented = {}
        for row in section.splitlines():
            found = re.match(r"\| (.+?) \| `docker exec \"\$C\" sh -c '(.+)'` \|", row)
            if found:
                documented[found[1]] = found[2].replace("\\|", "|")
        self.assertEqual(documented, gate.UNREACHABLE)


class NoSecretTest(Case):
    def test_outputs_never_echo_values(self):
        secret = token("pending", reason="offline", note=FAKE, token=FAKE)
        lines = [line("close", G1, secret, withdrawn()), {**line("refresh", G1), "error": FAKE}]
        outputs = [
            *gate.reclaimed(lines, G1, "claude"),
            gate.removed_tokens(lines, G1),
            *gate.baseline(inspect(HostConfig__CapDrop=[]), "live", CWD, [CLAUDE_DIR], NETWORK),
            gate.timing({"phases": {}}, "", TimingTest.STDERR, "live"),
            *gate.install_steps({"phases": {}}, TimingTest.STDERR),
        ]
        for text in outputs:
            self.assertNotIn(FAKE, text)


class ParseTest(Case):
    def test_bad_forms(self):
        for argv in (
            [],
            ["start"],
            ["worker", CWD],
            ["worker", "--client", "gemini", CWD],
            ["worker", "--client", "claude", "relative"],
            ["worker", "--client", "claude", CWD, "a", "b"],
            ["worker", "--client"],
            ["worker", "--client", "claude", "--repo", "o/n", CWD],
            ["role"],
            ["role", "--repo", "not-a-repo"],
            ["role", "--repo", "o/n", "--timeout", "0"],
            ["role", "--repo", "o/n", "--timeout", "soon"],
            ["pr"],
            ["pr", "https://example.com/o/n/pull/1"],
            ["pr", "https://github.com/o/n/pull/1", "--client", "claude"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(gate.parse(argv))

    def test_good_forms(self):
        self.assertEqual(
            gate.parse(["worker", "--client", "codex", CWD]),
            gate.Args("worker", "docker-live-codex", client="codex", cwd=CWD),
        )
        self.assertEqual(
            gate.parse(["worker", "--client", "opencode", CWD]),
            gate.Args("worker", "docker-live-opencode", client="opencode", cwd=CWD),
        )
        self.assertEqual(
            gate.parse(["worker", CWD, "--client", "claude", "w1"]),
            gate.Args("worker", "w1", client="claude", cwd=CWD),
        )
        self.assertEqual(
            gate.parse(["role", "--repo", "o/n"]),
            gate.Args("role", "docker-live-role", repo="o/n", timeout=7200.0),
        )
        self.assertEqual(
            gate.parse(["role", "--repo", "o/n", "--timeout", "60", "r1"]),
            gate.Args("role", "r1", repo="o/n", timeout=60.0),
        )
        url = "https://github.com/o/n/pull/12"
        self.assertEqual(gate.parse(["pr", url]), gate.Args("pr", "", url=url))

    def test_main_prints_usage(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(gate.main([]), 2)
        self.assertIn("live_docker.py worker", err.getvalue())


class Docker:
    """A fake subprocess.run: records each argv; `fail` names commands that exit 1."""

    def __init__(self, listed=(), fail=()):
        self.calls, self.listed, self.fail = [], list(listed), set(fail)

    def __call__(self, argv, **_):
        self.calls.append(list(argv))
        command = (
            " ".join(argv[1:3]) if os.path.basename(argv[0]).startswith("ensemble") else argv[1]
        )
        out = "\n".join(self.listed) if argv[1:2] == ["ps"] else ""
        failed = command in self.fail or argv[-1] in self.fail
        return types.SimpleNamespace(returncode=int(failed), stdout=out, stderr="boom")

    def ran(self, program, *words):
        return [
            c
            for c in self.calls
            if os.path.basename(c[0]) == program and all(w in c for w in words)
        ]


class OwnContainersTest(Case):
    def test_literal_name(self):
        own, other = f"ensemble-live-{G1}", f"ensemble-live-x-{G2}"
        names = {own, other, f"ensemble-other-{G1}", "ensemble-live-123", f"ensemble-aXb-{G1}"}
        self.assertEqual(gate.own_containers(names, "live"), [own])
        self.assertEqual(gate.own_containers(names, "a.b"), [])
        self.assertEqual(gate.own_containers(names, ".+"), [])


class CleanupTest(Case):
    def worker(self):
        w = gate.Worker(gate.Args("worker", "live", client="claude", cwd=CWD), "mgr", "/bin/docker")
        w.open = True
        return w

    def test_failed_final_close_then_cleanup(self):
        leftover = f"ensemble-live-{G1}"
        fake = Docker(listed=[leftover, f"ensemble-live-x-{G2}"], fail={"close --manager"})
        w = self.worker()
        with (
            mock.patch("subprocess.run", fake),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            with self.assertRaises(SystemExit):
                w.close(gate.Start("final", G1, ""), final=True)
            w.cleanup()
        programs = [os.path.basename(c[0]) + " " + c[1] for c in fake.calls]
        self.assertEqual(
            programs,
            ["ensemble-crew close"] * 2 + ["ensemble-session close", "docker ps", "docker rm"],
        )
        self.assertTrue(fake.ran("docker", "ps", "--filter", "label=ensemble.session=live"))
        self.assertEqual(fake.ran("docker", "rm"), [["/bin/docker", "rm", "--force", leftover]])
        self.assertIn(f"cleanup: removed {leftover}", err.getvalue())

    def test_crew_close_succeeds(self):
        fake = Docker()
        with mock.patch("subprocess.run", fake):
            self.worker().cleanup()
        self.assertEqual(len(fake.ran("ensemble-crew", "close")), 1)
        self.assertEqual(fake.ran("ensemble-session"), [])
        self.assertEqual(fake.ran("docker", "rm"), [])

    def test_nothing_to_close(self):
        fake = Docker()
        w = self.worker()
        w.open = w.listed = False
        with mock.patch("subprocess.run", fake):
            w.cleanup()
        self.assertEqual([os.path.basename(c[0]) for c in fake.calls], ["docker"])

    def test_one_failed_rm_does_not_stop_the_next(self):
        first, second = f"ensemble-live-{G1}", f"ensemble-live-{G2}"
        fake = Docker(listed=[first, second], fail={first})
        with (
            mock.patch("subprocess.run", fake),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.worker().cleanup()
        self.assertEqual([c[-1] for c in fake.ran("docker", "rm")], [first, second])
        self.assertIn(f"cleanup: removed {second}", err.getvalue())
        self.assertNotIn(f"cleanup: removed {first}", err.getvalue())

    def test_failures_are_printed_not_raised(self):
        fake = Docker(fail={"close --manager", "close live", "ps"})
        with (
            mock.patch("subprocess.run", fake),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.worker().cleanup()
        self.assertEqual(err.getvalue().count("cleanup: "), 3)


class Proc:
    """A fake Popen: running until `ends` polls have passed."""

    def __init__(self, ends=None, returncode=3):
        self.polls, self.ends, self.code = 0, ends, returncode
        self.returncode = None

    def poll(self):
        self.polls += 1
        if self.ends is not None and self.polls > self.ends:
            self.returncode = self.code
        return self.returncode


class Broker:
    """A fake Broker: `fails` maintains fail, alternately raising and reporting a provider error;
    then they renew.
    """

    def __init__(self, fails):
        self.fails, self.calls = fails, []

    def maintain(self, container, manifest, delivered=(), *, renew=False, root):
        self.calls.append((container, manifest, renew, root))
        if self.fails:
            self.fails -= 1
            if self.fails % 2:
                raise gate.SessionError("not yet")
            error = {"code": "refresh_failed", "message": "not yet"}
            return SimpleNamespace(results=[{"credential": "github", "error": error}])
        return SimpleNamespace(results=[{"credential": "github", "error": None}])


class RoleRunTest(Case):
    def plan(self, session, generation):
        folder = os.path.join(self.ensemble, "docker-state", session)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{generation}.plan.json"), "w", encoding="utf-8") as f:
            f.write("{}")

    def test_sessions(self):
        run = gate.RoleRun("role", "/bin/docker")
        self.assertEqual(run.sessions(), set())
        self.plan("role", G1)
        self.plan("role-drive", G2)
        self.plan("roles", G2)
        self.assertEqual(run.sessions(), {("role", G1), ("role-drive", G2)})

    def test_generation_appears_after_polls(self):
        run, polls = gate.RoleRun("role", "/bin/docker"), []

        def sleep(_):
            polls.append(1)
            if len(polls) == 2:
                self.plan("role", G1)

        with mock.patch("time.sleep", sleep):
            self.assertEqual(run.generation(set(), Proc()), ("role", G1))
        self.assertEqual(len(polls), 2)

    def test_generation_run_exits_first(self):
        with mock.patch("time.sleep"), self.assertRaises(SystemExit) as raised:
            gate.RoleRun("role", "/bin/docker").generation(set(), Proc(ends=1))
        self.assertEqual(str(raised.exception), "the run exited 3 before its container started")

    def test_two_new_generations(self):
        self.plan("role", G1)
        self.plan("role", G2)
        with self.assertRaises(SystemExit) as raised:
            gate.RoleRun("role", "/bin/docker").generation(set(), Proc())
        self.assertEqual(str(raised.exception), "want one new role generation, got 2")

    def test_running(self):
        container = f"ensemble-role-{G1}"
        listings = iter([[], [container]])
        fake = Docker()

        def run(argv, **kwargs):
            fake.listed = next(listings)
            return fake(argv, **kwargs)

        with mock.patch("subprocess.run", run), mock.patch("time.sleep") as sleep:
            gate.RoleRun("role", "/bin/docker").running(container, Proc())
        self.assertEqual(sleep.call_count, 1)
        self.assertNotIn("--all", fake.calls[0])

    def test_running_run_exits_first(self):
        container = f"ensemble-role-{G1}"
        with (
            mock.patch("subprocess.run", Docker()),
            mock.patch("time.sleep"),
            self.assertRaises(SystemExit) as raised,
        ):
            gate.RoleRun("role", "/bin/docker").running(container, Proc(ends=0))
        self.assertEqual(str(raised.exception), f"the run exited 3 before {container} ran")

    def test_refresh_retries(self):
        broker = Broker(fails=2)
        with (
            mock.patch("time.sleep") as sleep,
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            gate.RoleRun("role", "/bin/docker", broker=broker).renew("c", "m.json")
        self.assertEqual(broker.calls, [("c", "m.json", True, gate.REPO)] * 3)
        self.assertEqual([c.args for c in sleep.call_args_list], [(gate.REFRESH_RETRY,)] * 2)
        self.assertIn("PASS token refresh of c", out.getvalue())

    def test_refresh_gives_up_after_the_wait(self):
        clock = iter([0.0, 50.0, gate.REFRESH_WAIT + 1.0])
        with (
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", lambda: next(clock)),
            self.assertRaises(SystemExit) as raised,
        ):
            gate.RoleRun("role", "/bin/docker", broker=Broker(fails=99)).renew("c", "m.json")
        self.assertEqual(str(raised.exception), "FAIL token refresh of c: not yet")


LSOF = (
    "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
    "ollama  12345    u    3u  IPv4 0x1234      0t0  TCP {address}:11434 (LISTEN)\n"
)


class LoopbackOnlyTest(Case):
    def test_only_127_0_0_1_passes(self):
        self.assertEqual(gate.loopback_only(LSOF.format(address="127.0.0.1"), 11434), [])

    def test_any_other_listener_fails(self):
        for address in ("*", "0.0.0.0", "192.168.1.2", "[::1]"):
            with self.subTest(address=address):
                (problem,) = gate.loopback_only(LSOF.format(address=address), 11434)
                self.assertIn(f"{address}:11434", problem)

    def test_no_listener_fails(self):
        (problem,) = gate.loopback_only("", 11434)
        self.assertIn("no listener on TCP port 11434", problem)


SECRETS = "/run/ensemble/secrets"


class HeldSecretsTest(Case):
    def listing(self, *paths):
        return "\n".join(f"{SECRETS}/{p}" for p in paths) + "\n"

    def test_only_github_without_providers_passes(self):
        found = self.listing("github", "github/acme")
        self.assertEqual(gate.held_secrets(found, []), [])

    def test_github_and_each_listed_key_pass(self):
        found = self.listing(
            "github", "github/acme", "opencode", "opencode/lab.key", "opencode/strata.key"
        )
        self.assertEqual(gate.held_secrets(found, ["strata", "lab"]), [])

    def test_anything_else_fails(self):
        for paths, providers, problem in (
            (("github", "opencode"), ["strata"], "no opencode/strata.key"),
            (
                ("github", "opencode", "opencode/strata.key", "opencode/lab.key"),
                ["strata"],
                "opencode/lab.key",
            ),
            (("github", "claude"), [], f"{SECRETS}/claude"),
            (("github", "opencode"), [], f"{SECRETS}/opencode"),
            (("opencode", "opencode/strata.key"), ["strata"], "no github"),
        ):
            with self.subTest(paths=paths):
                (found,) = gate.held_secrets(self.listing(*paths), providers)
                self.assertIn(problem, found)


def hook(ts, session="ses_one", label="session.idle"):
    return json.dumps({"ts": ts, "label": label, "payload": {"session_id": session}})


class SessionIdsTest(Case):
    def test_ids_after_the_launch_in_first_seen_order(self):
        hooks = "\n".join(
            (
                hook("2026-10-18T12:00:00.000+00:00", "ses_old"),
                hook("2026-10-18T12:01:00.000+00:00", "ses_new"),
                json.dumps({"ts": "2026-10-18T12:01:01.000+00:00", "label": "x", "payload": {}}),
                hook("2026-10-18T12:01:02.000+00:00", "ses_new"),
                "not json",
            )
        )
        self.assertEqual(gate.session_ids(hooks, "2026-10-18T12:00:30.000+00:00"), ["ses_new"])

    def test_one_session(self):
        self.assertEqual(gate.one_session(["ses_a"], None), ("ses_a", []))
        self.assertEqual(gate.one_session(["ses_a"], "ses_a"), ("ses_a", []))
        for ids, before, words in (
            ([], None, "no OpenCode session id in the hook journal"),
            (["ses_a", "ses_b"], None, "several OpenCode sessions"),
            (["ses_b"], "ses_a", "OpenCode session ses_b, want ses_a"),
        ):
            with self.subTest(ids=ids, before=before):
                (problem,) = gate.one_session(ids, before)[1]
                self.assertIn(words, problem)


if __name__ == "__main__":
    unittest.main()
