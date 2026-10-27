"""hosts/docker/host.py: DockerHost's config, start order and rollback, container commands, the Host
record, listing, close and show, over a fake docker CLI, a fake broker and a recorded host tmux.
"""

import contextlib
import fcntl
import io
import json
import logging
import os
import re
import runpy
import shlex
import shutil
import signal
import stat
import sys
import threading
import time
import tomllib
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest import mock

from ensemble.session import Viewer
from ensemble.session.engine import tmux_cli, worker
from ensemble.session.engine.config import read_config
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import (
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    EnvironmentSpec,
    IdleTimeout,
    KeyPress,
    Launch,
    Liveness,
    Paste,
    Permission,
    SessionBusy,
    SessionError,
    StatusState,
    Unanswered,
)
from ensemble.session.engine.worker import Worker, WorkerState
from ensemble.session.harness import guard
from ensemble.session.hosts.base import Host, tui_lock
from ensemble.session.hosts.docker import host as docker_host
from ensemble.session.hosts.docker import worker as worker_module
from ensemble.session.hosts.docker.broker import (
    BROKER,
    RELAY,
    Broker,
    BrokerError,
    client_keys,
)
from ensemble.session.hosts.docker.daemon import CLEARED, RUN_TIMEOUT, Overflow
from ensemble.session.hosts.docker.host import (
    KEPT_RETRY,
    PROMPT_STAGE,
    DockerHost,
    DockerTerminal,
)
from ensemble.session.hosts.docker.relay import SOCKET
from ensemble.session.hosts.docker.types import Maintained, Started
from ensemble.session.hosts.types import CommandView
from ensemble.session.viewer import display as displays
from ensemble.utils import Record, inode_lock, parse_time
from tests.session.case import REPO, Case
from tests.session.unit.hosts.docker.support import FakeBroker as BrokerRunner
from tests.session.unit.hosts.docker.support import data
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

DOCKER = "/usr/local/bin/docker"
# The packaged config's GitHub token permissions.
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "checks": "read",
    "actions": "read",
    "statuses": "read",
}
GEN = "0123456789abcdef0123456789abcdef"
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
NEW = "f" * 32
CONTAINER = f"ensemble-w-{GEN}"
PROCESS = "4242 $3 %7 4343"
PYTHON = "/opt/ensemble/venv/bin/python"
STAGED = "/run/ensemble/launch-x1"
IMAGE = "ensemble-worker:" + "a" * 64
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
DIGEST = "sha256:" + "c" * 64
HOME = ClientHome("agent_dir", "agent", "AGENT_HOME", "agent-token")
RECORD = Record(
    1,
    "agent",
    "w",
    (),
    "s1",
    None,
    ("ensemble-session",),
    ("ensemble-session", "resume", "w"),
    None,
    None,
)


def pane(dead="0  "):
    """display-message's answer for the pane PROCESS: its id, its DEAD fields, or both."""

    def reply(cmd):
        fmt = cmd[-1]
        if "\t" in fmt:
            return done(0, f"{PROCESS}\t{dead}\n")
        return done(0, f"{dead}\n" if fmt.startswith("#{pane_dead}") else f"{PROCESS}\n")

    return reply


def done(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeDocker:
    """The docker CLI: `ps` prints `ps`, `volume ls` prints `volumes`; an exec answers from `replies`, {command prefix: (rc, out,
    err), an exception or a callable of the command}, the first matching prefix; else success, a
    launch's stage printing STAGED and keeping its handover in `staged`.
    """

    def __init__(self, ep, order):
        self.ep, self.order, self.ps, self.replies, self.inputs = ep, order, [], {}, []
        self.volumes, self.staged = [], []

    def __call__(self, argv, env, *, input=None, timeout):
        assert argv[:3] == [DOCKER, "--host", self.ep], argv
        assert not set(CLEARED) & env.keys() and timeout <= RUN_TIMEOUT
        self.order.append(argv[3:])
        self.inputs.append(input)
        if argv[3] in ("ps", "volume"):
            rows = self.ps if argv[3] == "ps" else self.volumes
            return done(0, "".join(f"{line}\n" for line in rows))
        cmd = argv[6:]
        for prefix, reply in self.replies.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if isinstance(reply, BaseException):
                    raise reply
                return reply(cmd) if callable(reply) else done(*reply)
        if cmd[:3] == [PYTHON, "-I", "-c"] and cmd[3:] == [tmux_cli.STAGE, "/run/ensemble"]:
            self.staged.append(json.loads(input))
            return done(0, STAGED + "\n")
        return done()

    def execs(self):
        return [call[3:] for call in self.order if isinstance(call, list) and call[0] == "exec"]


class FakeBroker:
    """`failing`: {command: the error it raises}."""

    def __init__(self, order, started):
        self.order, self.started, self.error, self.closed = order, started, None, []
        self.failing, self.refreshed = {}, threading.Event()

    def version(self):
        self.order.append("version")
        if "version" in self.failing:
            raise self.failing["version"]

    def probe_provider(self, provider, base_url):
        self.order.append(("probe", provider, base_url))
        if "probe-provider" in self.failing:
            raise self.failing["probe-provider"]
        return True

    def build(self, package, *, plugins=(), **environment):
        self.order.append(("build", package))
        self.plugins, self.built = plugins, environment
        if "build" in self.failing:
            raise self.failing["build"]
        return IMAGE, DIGEST

    def start(self, manifest, *, clients):
        self.order.append(("start", manifest, clients))
        if self.error:
            raise self.error
        return self.started

    def maintain(self, container, manifest, delivered=(), *, root):
        self.order.append(("maintain", container, manifest))
        self.refreshed.set()
        if "maintain" in self.failing:
            raise self.failing["maintain"]
        return Maintained([], [], None, self.started.deadline)

    def close(self, container):
        self.order.append(("close", container))
        self.closed.append(container)
        return True

    def workspace_rm(self, volume):
        self.order.append(("workspace_rm", volume))
        if "workspace_rm" in self.failing:
            raise self.failing["workspace_rm"]
        return True


BOT = "ensemble-bot[bot]"
BOT_EMAIL = "343764394+ensemble-bot[bot]@users.noreply.github.com"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": BOT,
    "GIT_AUTHOR_EMAIL": BOT_EMAIL,
    "GIT_COMMITTER_NAME": BOT,
    "GIT_COMMITTER_EMAIL": BOT_EMAIL,
}


class Base(Case):
    def setUp(self):
        self.account = os.path.realpath(self.home)
        patcher = mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=self.account))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ep = f"unix://{self.account}/.docker/run/docker.sock"
        os.makedirs(os.path.dirname(self.ep.removeprefix("unix://")))
        open(self.ep.removeprefix("unix://"), "w").close()
        self.cwd = os.path.join(self.account, "src", "repo")
        os.makedirs(self.cwd)
        self.order = []
        self.docker = FakeDocker(self.ep, self.order)
        started = Started(
            CONTAINER,
            self.ep,
            "sha256:" + "b" * 64,
            {},
            github_user_id=None,
            deadline=None,
        )
        self.broker = FakeBroker(self.order, started)
        self.host = DockerHost(
            "docker",
            {"docker": DOCKER},
            run=self.docker,
            broker=self.broker,
        )
        self.state = os.path.join(self.home, ".ensemble", "docker-state", "w")
        self.spawned, self.inherit = [], False
        self.tui_session("w")
        popen = mock.patch("subprocess.Popen", side_effect=self.spawn)
        self.spawn_mock = popen.start()
        self.addCleanup(popen.stop)

    def tui_session(self, name, gen=GEN):
        """The tui mode's session.json for `name`, as start writes it before the Host runs."""
        path = os.path.join(self.home, ".ensemble", "tui", name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "session.json"), "w") as f:
            json.dump({"generation": gen}, f)

    def spawn(self, argv, **kw):
        """A supervisor that holds the lock fd it inherits while `inherit` is set."""
        held = [os.dup(fd) for fd in kw.get("pass_fds", ())] if self.inherit else []
        for fd in held:
            self.addCleanup(_close, fd)
        proc = mock.Mock(pid=4321 + len(self.spawned))
        proc.terminate.side_effect = lambda: [_close(fd) for fd in held]
        self.spawned.append((argv, kw, held))
        return proc

    def record(self, **changes):
        found = {
            "generation": GEN,
            "sid": "s1",
            "client": "agent",
            "display": "lead/below",
            "record": None,
            "process": PROCESS,
            **changes,
        }
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "host.json"), "w") as f:
            json.dump(found, f)

    def host_record(self):
        with open(os.path.join(self.state, "host.json")) as f:
            return json.load(f)

    def listed(self, *names, state="running", gen=GEN):
        self.docker.ps = [f"ensemble-{name}-{gen}\t{name}\t{gen}\t{state}" for name in names]

    def live_supervisor(self):
        """supervisor.lock held, as by a running supervisor, until _close(fd) or cleanup."""
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(_close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def terminal(self):
        """w attached while its supervisor runs: no restart."""
        self.listed("w")
        self.record()
        fd = self.live_supervisor()
        try:
            return self.host.attach("w")
        finally:
            _close(fd)


def _close(fd):
    with contextlib.suppress(OSError):
        os.close(fd)


def _free(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


class ConfigTest(Base):
    def test_capacity_namespace_uses_the_fixed_endpoint_across_aliases(self):
        alias = DockerHost(
            "alias", {"docker": "/other/docker"}, run=self.docker, broker=self.broker
        )
        self.assertEqual(self.host._capacity_namespace(), f"docker:{self.ep}")
        self.assertEqual(alias._capacity_namespace(), self.host._capacity_namespace())

    def test_provider_usage_rejects_broker_18_before_probing_credentials(self):
        request = SimpleNamespace(provider="strata", base_url="https://strata.test/v1")
        locked = data("error-login_required.json")
        locked["error"].update(
            command="probe-provider", message="secret-store ensemble-opencode: keychain locked"
        )
        runner = BrokerRunner(
            (0, '{"protocol_version":"18"}\n', ""),
            (5, "", json.dumps(locked)),
        )
        self.host.broker = Broker(runner)
        with self.assertRaisesRegex(SessionError, "protocol '18', want '29'") as caught:
            self.host._provider_usage(request)
        self.assertIn(
            "install or upgrade it: see docs/docker-host.md#install", str(caught.exception)
        )
        self.assertEqual(runner.calls, [[BROKER, "version"]])
        self.assertEqual(len(runner.results), 1)

    def test_provider_usage_checks_protocol_and_uses_the_broker(self):
        request = SimpleNamespace(provider="strata", base_url="https://strata.test/v1")
        self.assertTrue(self.host._provider_usage(request))
        self.assertEqual(self.order, ["version", ("probe", "strata", "https://strata.test/v1")])
        self.broker.failing["version"] = SessionError("upgrade broker")
        with self.assertRaisesRegex(SessionError, "upgrade broker"):
            self.host._provider_usage(request)

    def test_provider_usage_preserves_host_credential_failures(self):
        request = SimpleNamespace(provider="strata", base_url="https://strata.test/v1")
        for exit_code, code, message in (
            (11, "keychain_failed", "secret-store ensemble-strata: keychain locked"),
            (11, "keychain_failed", "secret-store ensemble-strata: credentials unreadable"),
        ):
            with self.subTest(code=code):
                error = BrokerError(exit_code, code, message, "not_needed", None)
                self.broker.failing["probe-provider"] = error
                with self.assertRaises(SessionError) as caught:
                    self.host._provider_usage(request)
                self.assertIs(caught.exception, error)

    def test_provider_usage_reports_missing_or_invalid_key_as_client_configuration(self):
        request = SimpleNamespace(provider="strata", base_url="https://strata.test/v1")
        for message in ("No API key in ensemble-opencode", "Invalid API key in ensemble-opencode"):
            with self.subTest(message=message):
                error = BrokerError(5, "login_required", message, "not_needed", None)
                self.broker.failing["probe-provider"] = error
                with self.assertRaisesRegex(ConfigError, message):
                    self.host._provider_usage(request)

    def test_the_packaged_table_parses_and_tmux_stays_the_default(self):
        with open(os.path.join(REPO, "src/ensemble/session/config.toml"), "rb") as f:
            table = tomllib.load(f)["hosts"]
        self.assertEqual(
            table["docker"],
            {
                "implementation": "ensemble.session.hosts.docker.host:DockerHost",
                "docker": "/usr/local/bin/docker",
                "plugins": [
                    "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43"
                ],
                "isolated": True,
                "github": {"app": "", "permissions": PERMISSIONS},
            },
        )
        self.assertEqual(Host.parse_hosts(table).default, "tmux")
        settings = {k: v for k, v in table["docker"].items() if k != "implementation"}
        host = DockerHost("docker", settings)
        self.assertEqual(
            (host.docker, host.github, host.github_permissions, host.plugins, host._isolated),
            (
                "/usr/local/bin/docker",
                "",
                PERMISSIONS,
                (
                    "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                ),
                True,
            ),
        )
        bare = DockerHost("docker", {"docker": DOCKER})
        self.assertEqual(
            (bare.github, bare.github_permissions, bare.plugins, bare._isolated),
            ("", {}, (), False),
        )
        self.assertTrue(DockerHost("docker", {"docker": DOCKER, "isolated": True})._isolated)
        named = DockerHost(
            "docker",
            {
                "docker": DOCKER,
                "plugins": [
                    "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                    "b.c@owner/market_1-x#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ],
            },
        )
        self.assertEqual(
            named.plugins,
            (
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "b.c@owner/market_1-x#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ),
        )

    def test_a_local_permissions_table_overrides_the_packaged_one_key_by_key(self):
        local = os.path.join(self.home, "config.local.toml")
        with open(local, "w") as f:
            f.write(
                '[session.hosts.docker.github]\napp = "ensemble-bot"\n'
                '[session.hosts.docker.github.permissions]\nstatuses = "write"\nissues = "write"\n'
            )
        settings = read_config(local=local)["hosts"]["docker"]
        del settings["implementation"]
        host = DockerHost("docker", settings)
        self.assertEqual(host.github, "ensemble-bot")
        self.assertEqual(
            host.github_permissions, {**PERMISSIONS, "statuses": "write", "issues": "write"}
        )

    def test_bad_settings_name_their_key(self):
        for settings, key in (
            ({"docker": DOCKER, "other": 1}, r"^unknown key 'other' in \[hosts.docker\]$"),
            ({"docker": "docker"}, "docker"),
            (
                {"docker": DOCKER, "mount_roots": ["~/src"]},
                r"^unknown key 'mount_roots' in \[hosts.docker\]; delete it: workspaces are volumes$",
            ),
            (
                {"docker": DOCKER, "github": "ensemble-bot"},
                r"^\[hosts.docker\] github: want a table$",
            ),
            (
                {"docker": DOCKER, "github": {"app": "a", "x": 1}},
                r"^unknown key 'x' in \[hosts.docker.github\]$",
            ),
            (
                {"docker": DOCKER, "github": {"app": 1}},
                r"^\[hosts.docker.github\] app: want a string$",
            ),
            ({"docker": DOCKER, "github": {}}, r"^\[hosts.docker.github\] app: want a string$"),
            *(
                (
                    {"docker": DOCKER, "github": {"app": "a", "permissions": permissions}},
                    r"^\[hosts.docker.github\] permissions: want a table of permission = level strings$",
                )
                for permissions in ("contents", ["contents"], {"contents": 1}, {"contents": None})
            ),
            (
                {"docker": DOCKER, "github": {"app": "a", "permissions": {}}},
                r"^\[hosts.docker.github\] permissions: want at least one with app set$",
            ),
            (
                {"docker": DOCKER, "github": {"app": "a"}},
                r"^\[hosts.docker.github\] permissions: want at least one with app set$",
            ),
            *(
                (
                    {"docker": DOCKER, "plugins": plugins},
                    r"^\[hosts.docker\] plugins: want unique name@owner/repo#40-hex-commit sources, optionally ::owner/repo#40-hex-commit$",
                )
                for plugins in (
                    "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                    ["a", "a"],
                    [1],
                    [["a"]],
                    [""],
                    ["../a"],
                    ["updraft"],
                    ["x@owner/repo"],
                    ["x@owner/repo#main"],
                    ["x@owner/repo#" + "a" * 39],
                    ["x@owner/repo#" + "A" * 40],
                    ["x@owner/repo#" + "a" * 40 + "::"],
                    ["x@owner/repo#" + "a" * 40 + "::other/catalogue#main"],
                    ["x@owner/repo#" + "a" * 40 + "::other/catalogue#" + "b" * 39],
                    ["x@owner/repo#" + "a" * 40 + "::../catalogue#" + "b" * 40],
                    ["a@"],
                    ["a@b@c"],
                    ["-a"],
                )
            ),
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(ConfigError, key):
                DockerHost("docker", settings)


class ClientEnvTest(Base):
    def test_a_client_without_home_is_refused(self):
        for client, named in (("other", "client other"), ("", r"client \(unnamed\)")):
            with (
                self.subTest(client=client),
                self.assertRaisesRegex(
                    ConfigError, rf"{named}: not supported on the Docker Host yet"
                ),
            ):
                self.host._client_env("w", client, None, resume=False)

    def test_resume_gets_the_same_env_and_touches_no_file(self):
        self.assertEqual(
            self.host._client_env("w", "agent", HOME, resume=True),
            {"AGENT_HOME": os.path.join(self.home, ".ensemble", "docker-state", "w", "agent")},
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(self.host._inherit_env)

    def test_resume_argv_names_the_session(self):
        self.assertEqual(self.host._resume_argv("w"), ("ensemble-session", "resume", "w"))
        self.assertIsNone(Host._resume_argv(self.host, "w"))

    def test_client_context_is_host_docker_internal_with_container_paths(self):
        container = Container(
            "/opt/ensemble/bin",
            "/opt/ensemble/venv/bin/python",
            "/opt/ensemble/src",
            "/run/ensemble",
        )
        for isolated, permission in ((True, Permission.YOLO), (False, Permission.AUTO)):
            with self.subTest(isolated=isolated):
                host = DockerHost("docker", {"docker": DOCKER, "isolated": isolated})
                self.assertEqual(
                    host._client_context(),
                    ClientContext("host.docker.internal", permission, container),
                )

    def test_the_pythonpath_starts_at_the_images_session_src(self):
        # Clients find their packaged files (Container.source) there.
        container = self.host._client_context().container
        assert container is not None
        self.assertEqual(container.source, "/opt/ensemble/src")

    def test_client_env_points_at_the_home(self):
        self.assertEqual(
            self.host._client_env("w", "agent", HOME, resume=False),
            {"AGENT_HOME": os.path.join(self.home, ".ensemble", "docker-state", "w", "agent")},
        )


class StartBase(Base):
    """A terminal start's metadata, launch and supervisor spawn."""

    METADATA = {
        "sid": "s1",
        "client": "agent",
        "generation": GEN,
        "home_field": "agent_dir",
        "home_dir": "agent",
        "resume": "0",
        "credential": "agent-token",
    }

    def setUp(self):
        super().setUp()
        self.home_dir = os.path.join(self.state, "agent")
        self.docker.replies = {
            ("tmux", "start-server"): (0, PROCESS + "\n", ""),
            ("tmux", "display-message"): pane(),
        }
        self.supervisor = mock.Mock(pid=4321)
        popen = mock.patch("subprocess.Popen", side_effect=self.popen)
        self.popen_mock = popen.start()
        self.addCleanup(popen.stop)
        self.launch = Launch(
            ("agent", "--flag"),
            cwd=self.cwd,
            env={"PATH": "/host/bin", "AGENT_HOME": self.home_dir, "K": "v"},
        )

    def popen(self, argv, **kw):
        self.order.append("supervisor")
        self.spawned_lock_held = not _free(os.path.join(self.state, "supervisor.lock"))
        self.spawned_record = self.host_record()
        self.spawned_deadline = os.path.exists(os.path.join(self.state, f"{GEN}.deadline.json"))
        return self.supervisor

    def start(self, **metadata):
        return self.host.terminal(
            "w",
            self.launch,
            display="lead/below",
            events=os.path.join(self.home, "events"),
            record=RECORD,
            metadata={k: v for k, v in {**self.METADATA, **metadata}.items() if v is not None},
        )

    def history(self):
        """A native transcript in the home, which the launch names."""
        transcript = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w") as f:
            f.write("{}\n")
        self.launch = replace(self.launch, transcript=transcript)
        return transcript

    def refused_untouched(self, error, pattern):
        """start(resume="1") fails with `pattern`, before any broker call, file or supervisor."""
        self.order.clear()
        with self.assertRaisesRegex(error, pattern):
            self.start(resume="1")
        self.assertNotIn("version", self.order)
        self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.json")))
        self.popen_mock.assert_not_called()


class StartTest(StartBase):
    def test_runtime_staging_errors_are_reported_without_container_output(self):
        files = json.dumps({"/run/ensemble/agent-store/" + "a" * 64 + ".json": "{}"})
        for result in (
            done(1, "private", "private"),
            OSError("unavailable"),
            TimeoutExpired("exec", 1),
        ):
            with self.subTest(result=type(result).__name__):
                with (
                    mock.patch.object(
                        self.host,
                        "run",
                        **(
                            {"side_effect": result}
                            if isinstance(result, Exception)
                            else {"return_value": result}
                        ),
                    ),
                    self.assertRaises(SessionError) as raised,
                ):
                    self.host._stage_files(CONTAINER, {"runtime_files": files})
                self.assertNotIn("private", str(raised.exception))

    def test_runtime_projection_and_initial_expiry_are_staged_before_client(self):
        expiry = {"at": "2026-10-18T12:00:00Z", "action": "close", "reason": "r"}
        self.broker.started = replace(self.broker.started, deadline=expiry)
        files = json.dumps({"/run/ensemble/agent-store/" + "a" * 64 + ".json": "{}"})
        self.start(runtime_files=files, home_persistent='["sessions"]')
        self.assertTrue(os.path.isdir(os.path.join(self.home_dir, "sessions")))
        self.assertIn(files, self.docker.inputs)
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as f:
            self.assertEqual(json.load(f), expiry)
        with open(os.path.join(self.state, f"{GEN}.plan.json")) as f:
            self.assertEqual(
                json.load(f)["mounts"], [{"path": self.home_dir + "/sessions", "rw": True}]
            )

    def test_a_prompt_file_is_staged_in_the_container_before_the_tmux_launch(self):
        path, text = "/run/ensemble/prompt/prompt.md", "雪\n" * 50_000
        self.start(prompt_file=path, prompt=text)
        calls = [call for call in self.order if isinstance(call, list)]
        staged = [i for i, call in enumerate(calls) if PROMPT_STAGE in call]
        launched = [i for i, call in enumerate(calls) if tmux_cli.RELAUNCH in call]
        self.assertEqual(len(staged), 1)
        self.assertLess(staged[0], launched[0])
        self.assertEqual(
            calls[staged[0]],
            ["exec", "-i", "--user", "agent", CONTAINER, PYTHON, "-I", "-c", PROMPT_STAGE, path],
        )
        self.assertEqual(self.docker.inputs[staged[0]], text)

    def test_no_prompt_file_stages_nothing(self):
        self.start()
        self.assertFalse(any(PROMPT_STAGE in call for call in self.order if isinstance(call, list)))

    def test_prompt_staging_errors_name_the_step(self):
        metadata = {"prompt_file": "/run/ensemble/prompt/prompt.md", "prompt": "private"}
        for result, want in (
            (done(1, "", ""), "^staging prompt file: exit status 1$"),
            (
                done(1, "", "Traceback (most recent call last):\nOSError: [Errno 28] No space\n"),
                r"^staging prompt file: OSError: \[Errno 28\] No space$",
            ),
            (OSError("unavailable"), "^staging prompt file: unavailable$"),
            (TimeoutExpired("exec", 1), "^staging prompt file: "),
        ):
            with (
                self.subTest(result=type(result).__name__),
                mock.patch.object(
                    self.host,
                    "run",
                    **(
                        {"side_effect": result}
                        if isinstance(result, BaseException)
                        else {"return_value": result}
                    ),
                ),
                self.assertRaisesRegex(SessionError, want) as raised,
            ):
                self.host._stage_prompt(CONTAINER, metadata)
            self.assertNotIn("private", str(raised.exception))

    def test_a_failed_prompt_stage_rolls_the_start_back(self):
        with (
            mock.patch.object(self.host, "_stage_prompt", side_effect=SessionError("no room")),
            self.assertRaisesRegex(SessionError, "no room"),
        ):
            self.start(prompt_file="/run/ensemble/prompt/prompt.md", prompt="x")
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_staging_failure_rolls_back_the_container(self):
        with (
            mock.patch.object(
                self.host, "_stage_files", side_effect=SessionError("projection failed")
            ),
            self.assertRaisesRegex(SessionError, "projection failed"),
        ):
            self.start()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_the_build_holds_the_whitelisted_plugins(self):
        self.host.plugins = (
            "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
            "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.start()
        self.assertEqual(
            self.broker.plugins,
            (
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
        )

    def test_start_order(self):
        os.makedirs(self.home_dir)
        with open(os.path.join(self.home_dir, "old"), "w"):
            pass
        process = self.start()
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertEqual(
            [step if isinstance(step, str | tuple) else step[:5] for step in self.order],
            [
                ["ps", "-a", "--filter", "label=ensemble.session", "--format"],
                ["ps", "-a", "--filter", "label=ensemble.session", "--format"],
                "version",
                ("build", REPO),
                ["volume", "ls", "--filter", "label=ensemble.kind", "--format"],
                ("start", manifest, client_keys(REPO)),
                ["exec", "-d", CONTAINER, RELAY, "serve"],
                ["exec", "-i", CONTAINER, "test", "-S"],
                "supervisor",
                ["exec", "-i", CONTAINER, PYTHON, "-I"],
                ["exec", "-i", CONTAINER, "tmux", "start-server"],
                ["exec", "-i", CONTAINER, "tmux", "display-message"],
            ],
        )
        self.assertEqual(os.listdir(self.home_dir), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.home_dir).st_mode), 0o700)
        with open(manifest) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": GEN,
                    "image": IMAGE,
                    "cwd": self.cwd,
                    "workspace": "ensemble-ws-w",
                    "client": "agent",
                    "agent_dir": self.home_dir,
                    "hook_path": os.path.join(REPO, "bin", "ensemble-session"),
                    "github": None,
                    "labels": {},
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(manifest).st_mode), 0o600)
        self.assertEqual(
            self.host_record(),
            {
                "generation": GEN,
                "sid": "s1",
                "client": "agent",
                "display": "lead/below",
                "record": RECORD.to_line(),
                "events": os.path.join(self.home, "events"),
                "environment": None,
                "process": PROCESS,
            },
        )
        self.assertIsInstance(process, DockerTerminal)
        self.assertEqual((process.identity, process.token), (PROCESS, None))

    def test_start_takes_the_lock_before_saving_the_generation_and_spawns_on_it(self):
        self.start()
        self.assertTrue(self.spawned_lock_held)
        self.assertEqual(self.spawned_record["generation"], GEN)
        with open(os.path.join(self.state, "supervisor.lock")) as f:
            self.assertEqual(f.read(), GEN)
        with open(os.path.join(self.state, "supervisor.pid")) as f:
            self.assertEqual(f.read(), "4321")
        argv = self.popen_mock.call_args.args[0]
        lock = int(argv[11])
        self.assertEqual(self.popen_mock.call_args.kwargs["pass_fds"], (lock,))
        self.assertTrue(_free(os.path.join(self.state, "supervisor.lock")))

    def test_start_rolls_back_when_relay_serve_fails_the_socket_never_appears_or_the_lock_is_held(
        self,
    ):
        for case, replies in (
            ("serve fails", {(RELAY, "serve"): (1, "", "no relay")}),
            ("serve times out", {(RELAY, "serve"): TimeoutExpired("docker", 10)}),
            ("no socket", {("test",): (1, "", "")}),
        ):
            with (
                self.subTest(case=case),
                mock.patch("time.sleep"),
                mock.patch("time.monotonic", side_effect=range(0, 10000, 5)),
            ):
                self.docker.replies = replies
                self.order.clear()
                with self.assertRaisesRegex(SessionError, "docker session w: "):
                    self.start()
                self.assertNotIn("supervisor", self.order)
                self.assertEqual(self.order[-1], ("close", CONTAINER))
        self.docker.replies = {}
        fd = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.order.clear()
        with (
            mock.patch("os.kill"),
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", side_effect=range(0, 10000, 10)),
            self.assertRaisesRegex(SessionError, "still holds"),
        ):
            self.start()
        self.assertNotIn("supervisor", self.order)
        self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_the_container_launch_and_the_supervisor(self):
        self.start()
        self.assertEqual(self.docker.execs()[0], [RELAY, "serve", GEN])
        self.assertEqual(self.docker.execs()[1], ["test", "-S", SOCKET])
        self.assertEqual(SOCKET, "/run/ensemble/relay.sock")
        stage = self.docker.execs()[2]
        self.assertEqual(stage[:3], [PYTHON, "-I", "-c"])
        self.assertEqual(stage[3:], [tmux_cli.STAGE, "/run/ensemble"])
        self.assertEqual(
            self.docker.staged,
            [
                {
                    "argv": ["with-secrets", "--client", "agent", "agent", "--flag"],
                    "env": {"AGENT_HOME": self.home_dir, "K": "v"},
                }
            ],
        )
        new = self.docker.execs()[3]
        self.assertEqual(
            new[new.index("--") : new.index(";", new.index("--"))],
            ["--", PYTHON, "-I", "-c", tmux_cli.RELAUNCH, STAGED],
        )
        self.assertEqual(new[new.index("-c") + 1], self.cwd)
        self.assertFalse(any("/host/bin" in arg or "AGENT_HOME" in arg for arg in new))
        for tag in ("@kind docker", f"@generation {GEN}", "@sid s1"):
            self.assertIn(tag, " ".join(new))

    def test_the_container_tmux_forwards_a_panes_clipboard_copy_to_the_attached_terminal(self):
        self.start()
        (new,) = [call for call in self.docker.execs() if "new-session" in call]
        for option in (
            "; set-option -s set-clipboard on ;",
            "; set-option -qp -t =w: allow-passthrough on ;",
        ):
            self.assertIn(option, " ".join(new))
        self.assertIn("set-option @state dead ; wait-for -S ensemble-dead", new)
        self.assertNotIn("@record", " ".join(new))
        argv = self.popen_mock.call_args.args[0]
        self.assertEqual(argv[1:5], ["-P", "-s", "-m", "ensemble.session.hosts.docker.supervisor"])
        self.assertEqual(
            argv[5:11],
            [
                "w",
                self.state,
                os.path.join(self.home, ".ensemble", "tui", "w"),
                os.path.join(self.home, "events"),
                os.path.join(REPO, "bin", "ensemble-session"),
                GEN,
            ],
        )
        relay, wait, check = (json.loads(arg) for arg in argv[12:])
        prefix = [DOCKER, "--host", self.ep, "exec"]
        self.assertEqual(relay, [*prefix, "-i", CONTAINER, RELAY, "connect"])
        self.assertEqual(RELAY, "/opt/ensemble/bin/relay")
        self.assertEqual(wait, [*prefix, CONTAINER, "tmux", "wait-for", "ensemble-dead"])
        self.assertEqual(
            check,
            [*prefix, CONTAINER, "tmux", "display-message", "-p", "-t", "=w:", "#{pane_dead}"],
        )
        kw = self.popen_mock.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertFalse(set(CLEARED) & kw["env"].keys())

    def test_scroll_settings_reach_container_before_its_first_pane_without_rebinding_mouse(self):
        self.start()
        (new,) = [call for call in self.docker.execs() if "new-session" in call]
        self.assertEqual(
            new[: new.index("new-session")],
            [
                "tmux",
                "start-server",
                ";",
                "set-option",
                "-g",
                "history-limit",
                "50000",
                ";",
                "set-option",
                "-g",
                "mouse",
                "on",
                ";",
            ],
        )
        self.assertNotIn("bind-key", new)
        self.assertNotIn("unbind-key", new)

    def test_a_min_width_reaches_the_container_tmux(self):
        for case, width, floored in (("opencode", "40", True), ("claude", None, False)):
            with self.subTest(case=case):
                self.order.clear()
                self.start(home_min_width=width)
                (new,) = [call for call in self.docker.execs() if "new-session" in call]
                self.assertEqual("window-size" in new, floored)
                self.assertEqual("client-resized" in new, floored)
                if floored:
                    self.assertGreaterEqual(int(new[new.index("-x") + 1]), 40)

    def test_a_bad_min_width_is_refused_before_a_container(self):
        for width in ("4x", "-1", ""):
            with self.subTest(width=width):
                self.order.clear()
                with self.assertRaisesRegex(
                    SessionError, f"docker session w: bad min width {width!r}"
                ):
                    self.start(home_min_width=width)
                self.assertFalse(
                    [o for o in self.order if isinstance(o, tuple) and o[0] == "start"]
                )
                self.popen_mock.assert_not_called()

    def test_a_long_prompt_is_staged_whole_off_tmux_s_command_line_on_start_resume_and_codex(self):
        """Restart and resume both start with resume=1; a Claude and a Codex client differ only in
        their with-secrets client and home."""
        prompt = "do the task ; $(echo x)\n" * 3000
        rollout = os.path.join(self.state, "codex", "sessions", "rollout.jsonl")
        codex = {
            "client": "codex",
            "home_field": "codex_dir",
            "home_dir": "codex",
            "credential": "codex-access-token",
            "home_persistent": '["sessions"]',
        }
        for case, client, metadata in (
            ("start", "agent", {}),
            ("resume", "agent", {"resume": "1"}),
            ("codex start", "codex", codex),
            ("codex resume", "codex", {**codex, "resume": "1"}),
        ):
            with self.subTest(case=case):
                transcript = self.history() if case == "resume" else rollout
                if case == "codex resume":
                    os.makedirs(os.path.dirname(rollout), exist_ok=True)
                    with open(rollout, "w") as f:
                        f.write("{}\n")
                self.launch = replace(
                    self.launch, argv=("agent", "--flag", prompt), transcript=transcript
                )
                self.docker.staged.clear()
                self.order.clear()
                self.start(**metadata)
                (new,) = [call for call in self.docker.execs() if "new-session" in call]
                self.assertLess(len("\0".join(new).encode()), 4096)
                self.assertEqual(
                    new[new.index("--") : new.index(";", new.index("--"))],
                    ["--", PYTHON, "-I", "-c", tmux_cli.RELAUNCH, STAGED],
                )
                (staged,) = self.docker.staged
                self.assertEqual(
                    staged["argv"], ["with-secrets", "--client", client, "agent", "--flag", prompt]
                )
                self.assertNotIn("PATH", staged["env"])

    def test_a_fresh_home_gets_the_clients_files(self):
        self.start(home_files=json.dumps({"settings.json": '{"model": "opus"}'}))
        self.assertEqual(os.listdir(self.home_dir), ["settings.json"])
        with open(os.path.join(self.home_dir, "settings.json")) as f:
            self.assertEqual(f.read(), '{"model": "opus"}')

    CODEX = {
        "client": "codex",
        "home_field": "codex_dir",
        "home_dir": "codex",
        "credential": "codex-access-token",
        "home_persistent": '["sessions"]',
    }

    def used(self, home, *kept):
        """A home a closed session of the name left: an old file and tree in it and each `kept`
        dir; a check that each of these dirs is still the same one (an open fd pins its inode).
        """
        dirs = [home, *(os.path.join(home, d) for d in kept)]
        fds = []
        for path in dirs:
            os.makedirs(os.path.join(path, "old", "deep"), exist_ok=True)
            with open(os.path.join(path, "old.jsonl"), "w") as f:
                f.write("{}\n")
            fds.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
            self.addCleanup(os.close, fds[-1])
        return lambda: [
            os.path.samestat(os.fstat(fd), os.lstat(p)) for fd, p in zip(fds, dirs, strict=True)
        ]

    def test_a_reused_codex_name_starts_with_its_bind_source_emptied_in_place(self):
        """Docker Desktop can report a bind source recreated at a path it saw before as missing."""
        home = os.path.join(self.state, "codex")
        sessions = os.path.join(home, "sessions")
        for run in range(3):
            with self.subTest(run=run):
                same = self.used(home, "sessions")
                self.order.clear()
                self.start(**self.CODEX, home_files=json.dumps({"config.toml": "x"}))
                self.assertEqual(same(), [True, True])
                self.assertEqual(sorted(os.listdir(home)), ["config.toml", "sessions"])
                self.assertEqual(os.listdir(sessions), [])
                self.assertEqual(stat.S_IMODE(os.lstat(sessions).st_mode), 0o700)
                self.assertIn(
                    ("start", os.path.join(self.state, f"{GEN}.json")),
                    [o[:2] for o in self.order if isinstance(o, tuple)],
                )

    def test_a_reused_name_keeps_a_whole_bound_home_and_empties_it(self):
        same = self.used(self.home_dir)
        self.start(home_files=json.dumps({"settings.json": "{}"}))
        self.assertEqual(same(), [True])
        self.assertEqual(os.listdir(self.home_dir), ["settings.json"])

    def test_a_fresh_start_replaces_links_and_files_in_the_home_never_following_them(self):
        home = os.path.join(self.state, "codex")
        sessions = os.path.join(home, "sessions")
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("x")
        for case in ("home link", "sessions link", "sessions file", "links in sessions"):
            with self.subTest(case=case):
                if case == "home link":
                    os.makedirs(self.state, exist_ok=True)
                    os.symlink(outside, home)
                else:
                    os.makedirs(home, exist_ok=True)
                    if case == "sessions link":
                        os.symlink(outside, sessions)
                    elif case == "sessions file":
                        open(sessions, "w").close()
                    else:
                        os.makedirs(sessions)
                        os.symlink(outside, os.path.join(sessions, "dir"))
                        os.symlink(os.path.join(outside, "keep"), os.path.join(sessions, "file"))
                self.start(**self.CODEX)
                self.assertFalse(os.path.islink(home) or os.path.islink(sessions))
                self.assertEqual(os.listdir(home), ["sessions"])
                self.assertEqual(os.listdir(sessions), [])
                self.assertEqual(os.listdir(outside), ["keep"])
                shutil.rmtree(home)

    def test_a_home_it_cannot_empty_fails_the_start_before_the_broker(self):
        for target in ("shutil.rmtree", "os.unlink"):
            with self.subTest(target=target):
                self.used(self.home_dir)
                self.order.clear()
                with (
                    mock.patch(target, side_effect=PermissionError(13, "Permission denied")),
                    self.assertRaisesRegex(
                        SessionError,
                        f"^docker session w: cannot reset {re.escape(self.home_dir)}: Permission denied$",
                    ),
                ):
                    self.start()
                self.assertFalse(
                    [o for o in self.order if isinstance(o, tuple) and o[0] == "start"]
                )

    def test_the_manifest_lists_the_clients_providers(self):
        self.start(home_providers='["lab", "strata"]')
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(json.load(f)["agent_providers"], ["lab", "strata"])

    def test_the_manifest_names_the_selected_account(self):
        self.start(home_account="work")
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(json.load(f)["agent_account"], "work")

    def test_no_providers_or_account_leave_the_manifest_without_them(self):
        self.start()
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            manifest = json.load(f)
        self.assertNotIn("agent_providers", manifest)
        self.assertNotIn("agent_account", manifest)

    def test_codex_resume_after_host_login_preserves_native_history(self):
        home = os.path.join(self.state, "codex")
        transcript = os.path.join(home, "sessions", "rollout.jsonl")
        os.makedirs(os.path.dirname(transcript))
        native = '{"session_id":"same-native-identity"}\n'
        with open(transcript, "w") as stream:
            stream.write(native)
        self.launch = replace(self.launch, transcript=transcript, env={"CODEX_HOME": home})
        metadata = dict(
            client="codex",
            home_field="codex_dir",
            home_dir="codex",
            credential="codex-access-token",
            home_persistent='["sessions"]',
            resume="1",
        )
        self.broker.error = BrokerError(
            5,
            "login_required",
            "/usr/local/libexec/ensemble/ensemble-broker login codex",
            "not_needed",
            None,
        )
        with self.assertRaisesRegex(SessionError, "login codex"):
            self.start(**metadata)
        self.broker.error = None
        until = {"at": "2100-01-01T00:00:00Z", "action": "close", "reason": "r"}
        self.broker.started = replace(self.broker.started, deadline=until)
        self.start(**metadata)
        with open(transcript) as stream:
            self.assertEqual(stream.read(), native)
        self.assertEqual(self.host_record()["sid"], "s1")
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as stream:
            self.assertEqual(json.load(stream), until)

    def test_resume_keeps_the_home_and_starts_a_new_container(self):
        transcript = self.history()
        process = self.start(resume="1", home_files=json.dumps({"settings.json": "{}"}))
        self.assertFalse(os.path.exists(os.path.join(self.home_dir, "settings.json")))
        with open(transcript) as f:
            self.assertEqual(f.read(), "{}\n")
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertIn(("start", manifest, client_keys(REPO)), self.order)
        self.assertNotIn(("workspace_rm", "ensemble-ws-w"), self.order)
        with open(manifest) as f:
            found = json.load(f)
        self.assertEqual((found["cwd"], found["agent_dir"]), (self.cwd, self.home_dir))
        self.assertIsInstance(process, DockerTerminal)

    def test_a_new_start_keeps_its_volume_at_the_same_cwd(self):
        for volumes in (
            [],
            [f"ensemble-ws-w\t{self.cwd}"],
            [f"ensemble-ws-other\t{self.account}/x"],
        ):
            with self.subTest(volumes=volumes):
                self.docker.volumes = volumes
                self.start()
                self.assertNotIn("workspace_rm", {step[0] for step in self.order})
                self.order.clear()

    def test_a_new_start_at_another_cwd_removes_the_old_volume_first(self):
        old = os.path.join(self.account, "src", "old")
        self.docker.volumes = [f"ensemble-ws-w\t{old}"]
        self.start()
        self.assertLess(
            self.order.index(("workspace_rm", "ensemble-ws-w")),
            [step[0] for step in self.order].index("start"),
        )

    def test_a_new_start_whose_old_volume_cannot_be_removed_keeps_the_home_and_starts_nothing(
        self,
    ):
        old = os.path.join(self.account, "src", "old")
        self.docker.volumes = [f"ensemble-ws-w\t{old}"]
        os.makedirs(self.home_dir)
        with open(os.path.join(self.home_dir, "keep"), "w"):
            pass
        dirty = (
            "Workspace ensemble-ws-w has uncommitted or unpushed work in: src/o/a. Delete it"
            " anyway with ensemble-broker workspace-rm --volume ensemble-ws-w --force."
        )
        for error, raised, pattern in (
            (
                BrokerError(12, "workspace_dirty", dirty, "not_needed", None),
                ConfigError,
                re.escape(f"docker session w: {dirty}; or start it in {old} again"),
            ),
            (
                BrokerError(14, "workspace_failed", "check failed", "not_needed", None),
                BrokerError,
                "check failed",
            ),
            (
                BrokerError(7, "docker_unreachable", "no daemon", "not_needed", None),
                BrokerError,
                "no daemon",
            ),
            (SessionError("broker: workspace-rm timed out"), SessionError, "timed out"),
        ):
            with self.subTest(error=error):
                self.order.clear()
                self.broker.failing = {"workspace_rm": error}
                with self.assertRaisesRegex(raised, pattern):
                    self.start()
                self.assertEqual(self.order[-1], ("workspace_rm", "ensemble-ws-w"))
                self.assertEqual(os.listdir(self.home_dir), ["keep"])
                self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.json")))
                self.popen_mock.assert_not_called()

    def test_resume_without_native_history_fails_before_the_broker(self):
        missing = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        outside = os.path.join(self.cwd, "s1.jsonl")
        with open(outside, "w") as f:
            f.write("{}\n")
        for case, transcript, keep in (
            ("no home", missing, False),
            ("no transcript", missing, True),
            ("outside the home", outside, True),
            ("none named", "", True),
        ):
            with self.subTest(case=case):
                if keep:
                    os.makedirs(self.home_dir, exist_ok=True)
                    with open(os.path.join(self.home_dir, "keep"), "w"):
                        pass
                self.launch = replace(self.launch, transcript=transcript)
                self.refused_untouched(
                    ConfigError, f"docker session w: no native history to resume in {self.home_dir}"
                )
                if keep:
                    self.assertEqual(os.listdir(self.home_dir), ["keep"])

    def test_resume_through_a_link_fails_before_the_broker(self):
        secret = os.path.join(self.account, "secret.jsonl")
        with open(secret, "w") as f:
            f.write("host only\n")
        transcript = self.history()
        os.remove(transcript)
        os.symlink(secret, transcript)
        self.refused_untouched(ConfigError, "no native history")
        project = os.path.dirname(transcript)
        os.remove(transcript)
        os.rmdir(project)
        elsewhere = os.path.join(self.account, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(secret, os.path.join(elsewhere, "s1.jsonl"))
        os.symlink(elsewhere, project)
        self.refused_untouched(ConfigError, "no native history")

    def test_a_missing_or_bad_resume_flag_is_refused_untouched(self):
        os.makedirs(self.home_dir)
        with open(os.path.join(self.home_dir, "keep"), "w"):
            pass
        for flag in (None, "", "yes", "2"):
            with self.subTest(flag=flag):
                self.order.clear()
                with self.assertRaisesRegex(SessionError, "docker session w: bad resume flag"):
                    self.start(resume=flag)
                self.assertEqual(self.order, [])
                self.assertEqual(os.listdir(self.home_dir), ["keep"])

    def test_a_github_app_reaches_the_manifest_the_env_and_the_expiry_file(self):
        self.host.github, self.host.github_permissions = "ensemble-bot", PERMISSIONS
        self.broker.started = replace(self.broker.started, github_user_id=343764394)
        self.start()
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(
                json.load(f)["github"], {"app": "ensemble-bot", "permissions": PERMISSIONS}
            )
        env = self.docker.staged[0]["env"]
        self.assertEqual(env["ENSEMBLE_PR_AUTHOR"], "ensemble-bot[bot]")
        self.assertEqual({key: env.get(key) for key in GIT_IDENTITY}, GIT_IDENTITY)
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as f:
            self.assertIsNone(json.load(f))
        self.assertTrue(self.spawned_deadline)

    def test_no_github_app_writes_null_and_no_author(self):
        self.start()
        env = self.docker.staged[0]["env"]
        self.assertNotIn("ENSEMBLE_PR_AUTHOR", env)
        self.assertFalse(set(GIT_IDENTITY) & set(env))
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as f:
            self.assertIsNone(json.load(f))
        for old in ("credential", "github"):
            self.assertFalse(os.path.lexists(os.path.join(self.state, f"{GEN}.{old}.json")))

    def test_each_start_writes_its_generation_result(self):
        self.broker.started = Started(
            CONTAINER,
            self.ep,
            "sha256:" + "b" * 64,
            {"claude": "2.1.0"},
            github_user_id=None,
            deadline=None,
        )
        result = os.path.join(self.state, f"{GEN}.result.json")
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                    os.remove(result)
                self.start(resume=resume)
                with open(result) as f:
                    found = json.load(f)
                began, ended = found.pop("started_at"), found.pop("ended_at")
                self.assertLessEqual(parse_time(began), parse_time(ended))
                found.pop("phases"), found.pop("launched_at")
                self.assertEqual(
                    found,
                    {
                        "container": CONTAINER,
                        "image": IMAGE,
                        "image_digest": "sha256:" + "b" * 64,
                        "client_versions": {"claude": "2.1.0"},
                    },
                )
                self.assertFalse(result.startswith(self.home_dir + os.sep))

    def test_generation_versions_survive_later_client_setup_failure(self):
        with (
            mock.patch.object(self.host, "_stage_files", side_effect=SessionError("setup failed")),
            self.assertRaisesRegex(SessionError, "setup failed"),
        ):
            self.start()
        with open(os.path.join(self.state, f"{GEN}.result.json")) as source:
            result = json.load(source)
        self.assertEqual(result["image_digest"], self.broker.started.image_digest)

    def test_saved_result_has_no_client_update(self):
        self.assertNotIn("client_update", {f.name for f in fields(Started)})
        self.start()
        with open(os.path.join(self.state, f"{GEN}.result.json")) as source:
            result = json.load(source)
        self.assertEqual(
            set(result),
            {
                "container",
                "image",
                "image_digest",
                "client_versions",
                "started_at",
                "ended_at",
                "phases",
                "launched_at",
            },
        )

    def test_each_start_records_and_reports_its_phase_times(self):
        result = os.path.join(self.state, f"{GEN}.result.json")
        phases = ["checks", "image", "start", "relay", "tmux", "client"]
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    self.start(resume=resume)
                with open(result) as f:
                    found = json.load(f)
                self.assertEqual(list(found["phases"]), phases)
                for seconds in found["phases"].values():
                    self.assertIsInstance(seconds, float)
                    self.assertGreaterEqual(seconds, 0)
                launched = found["launched_at"]
                self.assertRegex(launched, r"T\d\d:\d\d:\d\d\.\d{3}[+-]")
                self.assertLessEqual(
                    parse_time(found["ended_at"]), parse_time(launched).replace(microsecond=0)
                )
                timed = [
                    re.fullmatch(rf"docker timing w {GEN}: ([a-z]+) (\d+\.\d\d) s", line)
                    for line in stderr.getvalue().splitlines()
                    if line.startswith("docker timing")
                ]
                self.assertEqual(
                    [(m[1], float(m[2])) for m in timed], list(found["phases"].items())
                )

    def test_each_start_freezes_its_plan_before_the_broker_start(self):
        self.launch = replace(self.launch, env={**self.launch.env, "TOKEN": "sk-secret"})
        plan = os.path.join(self.state, f"{GEN}.plan.json")
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                    os.remove(plan)
                self.broker.error = BrokerError(5, "login_required", "log in", "not_needed", None)
                with (
                    mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    self.assertRaises(BrokerError),
                ):
                    self.start(resume=resume)
                with open(plan) as f:
                    text = f.read()
                self.assertEqual(
                    json.loads(text),
                    {
                        "name": "w",
                        "generation": GEN,
                        "image": IMAGE,
                        "image_digest": DIGEST,
                        "workspace": {"volume": "ensemble-ws-w", "path": self.cwd},
                        "mounts": [{"path": self.home_dir, "rw": True}],
                        "credential": "agent-token",
                    },
                )
                head = f"docker plan w {GEN}:"
                self.assertEqual(
                    stderr.getvalue().splitlines(),
                    [
                        f"{head} image {IMAGE} {DIGEST}",
                        f"{head} workspace ensemble-ws-w at {self.cwd} rw",
                        f"{head} mounts {self.home_dir} rw",
                        f"{head} credential agent-token",
                    ],
                )
                for absent in ("sk-secret", "--flag", "approval"):
                    self.assertNotIn(absent, text + stderr.getvalue())
                self.assertFalse(plan.startswith(self.home_dir + os.sep))

    def test_a_plan_write_failure_starts_nothing(self):
        os.makedirs(os.path.join(self.state, f"{GEN}.plan.json", "taken"))
        with self.assertRaisesRegex(SessionError, "docker session w: plan: "):
            self.start()
        self.assertFalse(any(isinstance(step, tuple) and step[0] == "start" for step in self.order))

    def test_bad_plan_metadata_is_refused_untouched(self):
        for changes, pattern in (
            ({"credential": None}, "no client home"),
            ({"credential": ""}, "no client home"),
        ):
            with self.subTest(changes=changes):
                self.order.clear()
                with self.assertRaisesRegex(SessionError, f"docker session w: {pattern}"):
                    self.start(**changes)
                self.assertEqual(self.order, [])

    def test_a_result_write_failure_rolls_back(self):
        os.makedirs(os.path.join(self.state, f"{GEN}.result.json", "taken"))
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                self.supervisor.reset_mock()
                with self.assertRaisesRegex(SessionError, "docker session w: "):
                    self.start(resume=resume)
                self.supervisor.terminate.assert_not_called()
                self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_final_result_write_failure_terminates_supervisor(self):
        replace_file = os.replace

        def fail_final_result(source, destination):
            if str(destination).endswith(".result.json") and os.path.exists(destination):
                raise OSError("result write failed")
            return replace_file(source, destination)

        with (
            mock.patch("os.replace", side_effect=fail_final_result),
            self.assertRaisesRegex(SessionError, "result write failed"),
        ):
            self.start()
        self.supervisor.terminate.assert_called_once()
        self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_existing_container_refuses_start(self):
        self.docker.ps = ["ensemble-w-other\tw\tother\trunning"]
        with self.assertRaisesRegex(SessionError, "a container for it exists"):
            self.start()
        self.assertEqual({step[0] for step in self.order}, {"ps"})

    def test_a_role_runs_cwd_starts_as_any_other(self):
        cwd = os.path.join(self.account, ".ensemble", "adhoc", "x", "agent")
        os.makedirs(cwd)
        self.launch = replace(self.launch, cwd=cwd)
        self.start()
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            found = json.load(f)
        self.assertEqual((found["cwd"], found["workspace"]), (cwd, "ensemble-ws-w"))

    def test_failure_after_start_rolls_back(self):
        for replies in (
            {("tmux", "start-server"): (1, "", "boom")},
            {("test",): (1, "", "")},
            {
                ("tmux", "start-server"): (0, PROCESS, ""),
                ("tmux", "display-message"): (0, "1 1 \n", ""),
            },
        ):
            with self.subTest(replies=replies), mock.patch("time.sleep"):
                self.docker.replies = replies
                self.order.clear()
                with (
                    mock.patch("time.monotonic", side_effect=range(0, 10000, 5)),
                    self.assertRaisesRegex(SessionError, "docker session w: "),
                ):
                    self.start()
                self.supervisor.terminate.assert_called()
                self.assertEqual(self.order[-1], ("close", CONTAINER))

    def exited(self, screen):
        """start's SessionError with a client that exits 3 at once, its pane showing `screen`."""
        self.docker.replies = {
            ("tmux", "start-server"): (0, PROCESS + "\n", ""),
            ("tmux", "display-message"): pane("1 3 "),
            ("tmux", "capture-pane"): (0, screen, ""),
        }
        self.order.clear()
        with (
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", side_effect=range(0, 10000, 5)),
            self.assertRaises(SessionError) as raised,
        ):
            self.start()
        self.assertEqual(self.order[-1], ("close", CONTAINER))
        return str(raised.exception)

    def test_a_client_that_exits_before_ready_reports_its_code_and_tail(self):
        error = self.exited("OpenAI Codex\n\n  account/read failed during TUI bootstrap\n\n")
        self.assertTrue(
            error.startswith(
                f"docker session w: {CONTAINER}: the client exited 3: "
                "OpenAI Codex | account/read failed during TUI bootstrap"
            ),
            error,
        )
        captured = [i for i, call in enumerate(self.order) if "capture-pane" in call]
        self.assertLess(captured[0], self.order.index(("close", CONTAINER)))

    def test_a_dead_client_without_a_tail_reports_its_code(self):
        error = self.exited("\n\n")
        self.assertTrue(
            error.startswith(f"docker session w: {CONTAINER}: the client exited 3"), error
        )
        self.assertNotIn("exited 3:", error)

    def test_a_bot_user_id_that_does_not_match_the_app_rolls_back(self):
        for github, user_id in (("ensemble-bot", None), ("", 343764394)):
            with self.subTest(github=github, user_id=user_id):
                self.host.github = github
                self.broker.started = replace(self.broker.started, github_user_id=user_id)
                with self.assertRaisesRegex(
                    SessionError, "broker: bad response to start: github_user_id"
                ):
                    self.start()
                self.assertEqual(self.order[-1], ("close", CONTAINER))
                self.assertNotIn("supervisor", self.order)

    def test_a_foreign_container_or_endpoint_rolls_back(self):
        for changes in (
            {"container": f"ensemble-x-{GEN}"},
            {"endpoint": "unix:///x/.docker/run/docker.sock"},
        ):
            with self.subTest(changes=changes):
                self.broker.started = Started(**{**vars(self.broker.started), **changes})
                with self.assertRaisesRegex(SessionError, "broker: started"):
                    self.start()
                self.assertEqual(self.order[-1], ("close", CONTAINER))
                self.assertNotIn("supervisor", self.order)

    def test_cleanup_failed_retries_close(self):
        self.broker.error = BrokerError(9, "start_failed", "no", "failed", CONTAINER)
        with self.assertRaises(BrokerError):
            self.start()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_start_failing_in_transport_closes_the_container(self):
        for error in (
            SessionError("broker: start timed out after 1800 s"),
            SessionError("broker: bad response to start: fields"),
        ):
            with self.subTest(error=error):
                self.broker.error, self.broker.closed = error, []
                with self.assertRaisesRegex(SessionError, f"^docker session w: {error}$"):
                    self.start()
                self.assertEqual(self.broker.closed, [CONTAINER])
                self.popen_mock.assert_not_called()

    def caught(self):
        """The STOPS delivered to a recorder in place of their handlers, until the test ends."""
        delivered = []
        for sig in STOPS:
            self.addCleanup(
                signal.signal, sig, signal.signal(sig, lambda n, _: delivered.append(n))
            )
        return delivered

    def signalled_at(self, step, signum):
        """`signum` sent to this thread as `step` (a broker method or a docker exec prefix) runs."""

        def stop():
            signal.pthread_kill(threading.get_ident(), signum)

        if isinstance(step, tuple):
            self.docker.replies = {
                **self.docker.replies,
                step: lambda cmd: (stop(), pane()(cmd))[1],
            }
        else:
            real = getattr(self.broker, step)
            setattr(self.broker, step, lambda *a, **k: (stop(), real(*a, **k))[1])

    def stopped_at(self, step, signum=signal.SIGTERM):
        """start() with `signum` sent as `step` runs fails, and the signal arrives after it."""
        delivered, mask = self.caught(), signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.signalled_at(step, signum)
        name = signal.Signals(signum).name
        with self.assertRaisesRegex(SessionError, f"^docker session w: stopped by {name}$"):
            self.start()
        self.assertEqual(delivered, [signum])
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, ()), mask)

    def test_a_signal_during_the_broker_start_closes_its_container(self):
        for signum in STOPS:
            with self.subTest(signum=signum):
                self.broker = self.host.broker = FakeBroker(self.order, self.broker.started)
                self.stopped_at("start", signum)
                self.assertEqual(self.broker.closed, [CONTAINER])
                self.popen_mock.assert_not_called()

    def test_a_signal_before_the_broker_start_starts_nothing(self):
        self.stopped_at("build")
        self.assertNotIn("start", [step[0] for step in self.order if isinstance(step, tuple)])
        self.assertEqual(self.broker.closed, [])

    def test_a_signal_while_the_client_starts_stops_the_supervisor_and_closes(self):
        self.stopped_at(("tmux", "display-message"))
        self.supervisor.terminate.assert_called()
        self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_a_start_blocks_no_signal_and_restores_the_handlers(self):
        mask, before = signal.pthread_sigmask(signal.SIG_BLOCK, ()), self.caught()
        handlers = {sig: signal.getsignal(sig) for sig in STOPS}
        masks = []
        real = self.broker.start
        self.broker.start = lambda *a, **k: (
            masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, ())),
            real(*a, **k),
        )[1]
        self.start()
        self.assertEqual(masks, [mask])
        self.assertEqual({sig: signal.getsignal(sig) for sig in STOPS}, handlers)
        self.assertEqual(before, [])

    def test_an_ignored_signal_stays_ignored(self):
        self.addCleanup(signal.signal, signal.SIGHUP, signal.signal(signal.SIGHUP, signal.SIG_IGN))
        self.signalled_at("start", signal.SIGHUP)
        self.start()
        self.assertEqual(self.broker.closed, [])

    def test_login_required_surfaces_message(self):
        self.broker.error = BrokerError(5, "login_required", "log in first", "not_needed", None)
        with self.assertRaisesRegex(SessionError, "broker: log in first"):
            self.start()
        self.assertEqual(self.broker.closed, [])
        self.assertEqual(sum(isinstance(s, tuple) and s[0] == "start" for s in self.order), 1)
        self.popen_mock.assert_not_called()

    def test_no_client_home_is_refused(self):
        with self.assertRaisesRegex(SessionError, "no client home"):
            self.start(home_dir="")

    def test_detach_is_refused(self):
        with self.assertRaisesRegex(SessionError, "only terminal sessions"):
            self.host.detach("w", self.launch)


HOST_SOURCE = "beta@owner/market#" + "b" * 40
ENV_SOURCE = "gamma@owner/market#" + "e" * 40
ENVIRONMENT = EnvironmentSpec(
    image="dummy-env:test",
    gateway="mini-op",
    secrets=({"item": "ensemble-env/dummy", "file": "dummy.txt"},),
    plugins=("dummy-plugin", HOST_SOURCE, ENV_SOURCE),
    lock="dummy-lock",
)
PREFETCHED = replace(ENVIRONMENT, prefetch={"repo": "o/r", "ref": "main"})
HOLDER = {"container": "ensemble-other-" + "d" * 32, "owner": "other"}


def carried(environment):
    return json.dumps(environment.persisted())


class EnvironmentTest(StartBase):
    def manifest(self):
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            return json.load(f)

    def resumable(self, environment):
        """w's host.json from a start in `environment` (absent: a pre-upgrade one), its history."""
        self.record(**({} if environment == "absent" else {"environment": environment}))
        self.history()

    def test_preflight_sends_version_only_without_querying_the_lock(self):
        cwd = os.path.join(self.account, "src", "later")
        launch = Launch(("agent",), cwd=cwd, env={})
        self.host._preflight("w", launch, {**self.METADATA, "environment": carried(ENVIRONMENT)})
        self.assertEqual(self.order, ["version"])
        self.assertFalse(os.path.exists(cwd))

    def test_preflight_defers_a_held_lock_to_start(self):
        self.broker.holder = HOLDER
        self.host._preflight(
            "w", self.launch, {**self.METADATA, "environment": carried(ENVIRONMENT)}
        )
        self.assertEqual(self.order, ["version"])

    def test_preflight_passes_its_own_container_holding_the_lock(self):
        self.broker.holder = {"container": CONTAINER, "owner": "w"}
        self.resumable(ENVIRONMENT.persisted())
        self.host._preflight("w", self.launch, {**self.METADATA, "resume": "1"})
        self.assertEqual(self.order, ["version"])
        for holder in (
            {"container": CONTAINER, "owner": "other"},
            {"container": "ensemble-other-" + GEN, "owner": "w"},
        ):
            self.broker.holder = holder
            with self.subTest(holder):
                self.host._preflight("w", self.launch, {**self.METADATA, "resume": "1"})
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "lock-query" for c in self.order))

    def test_preflight_of_a_resume_in_an_environment_sends_version_only(self):
        self.resumable(ENVIRONMENT.persisted())
        self.host._preflight("w", self.launch, {**self.METADATA, "resume": "1"})
        self.assertEqual(self.order, ["version"])

    def test_another_sids_record_is_not_the_stored_environment(self):
        self.resumable(ENVIRONMENT.persisted())
        self.record(sid="another", environment=ENVIRONMENT.persisted())
        self.start(resume="1")
        self.assertEqual(self.broker.built, {})
        self.assertIsNone(self.host_record()["environment"])

    def test_no_lock_sends_no_lock_fields(self):
        unlocked = carried(replace(ENVIRONMENT, lock=None))
        self.host._preflight("w", self.launch, {**self.METADATA, "environment": unlocked})
        self.assertEqual(self.order, ["version"])
        self.start(environment=unlocked)
        self.assertFalse({"lock", "lock_owner"} & set(self.manifest()))

    def test_build_uses_the_base_image_and_plugins(self):
        self.host.plugins = (HOST_SOURCE,)
        self.start(environment=carried(ENVIRONMENT))
        self.assertEqual(self.broker.plugins, (HOST_SOURCE, ENV_SOURCE))
        self.assertEqual(
            self.broker.built, {"base_image": "dummy-env:test", "image_plugins": ("dummy-plugin",)}
        )

    def test_worker_image_sends_no_base_image(self):
        worker_image = replace(ENVIRONMENT, image=EnvironmentSpec.WORKER, plugins=())
        self.start(environment=carried(worker_image))
        self.assertEqual(self.broker.built, {"base_image": None, "image_plugins": ()})

    def test_manifest_carries_the_environment(self):
        self.host.github, self.host.github_permissions = "ensemble-bot", PERMISSIONS
        self.broker.started = replace(self.broker.started, github_user_id=343764394)
        self.start(environment=carried(ENVIRONMENT))
        manifest = self.manifest()
        self.assertEqual(
            {key: manifest.get(key) for key in ("gateway", "secrets", "lock", "lock_owner")},
            {
                "gateway": "mini-op",
                "secrets": [{"item": "ensemble-env/dummy", "file": "dummy.txt"}],
                "lock": "dummy-lock",
                "lock_owner": "w",
            },
        )
        self.assertNotIn("prefetch", manifest)
        self.assertEqual(manifest["github"]["app"], "ensemble-bot")
        self.assertEqual(self.docker.staged[0]["env"]["ENSEMBLE_PR_AUTHOR"], BOT)

    def test_prefetch_drops_the_github_app(self):
        self.host.github, self.host.github_permissions = "ensemble-bot", PERMISSIONS
        self.start(environment=carried(PREFETCHED))
        manifest = self.manifest()
        self.assertEqual(manifest["prefetch"], {"repo": "o/r", "ref": "main"})
        self.assertIsNone(manifest["github"])
        env = self.docker.staged[0]["env"]
        self.assertNotIn("ENSEMBLE_PR_AUTHOR", env)
        self.assertFalse(set(GIT_IDENTITY) & set(env))

    def test_busy_start_raises_session_busy(self):
        self.broker.error = BrokerError(75, "busy", "Lock held.", "not_needed", None, holder=HOLDER)
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        for call in (
            lambda: self.start(environment=carried(ENVIRONMENT)),
            lambda: self.host.pipe(
                "w", self.launch, metadata={**self.METADATA, "environment": carried(ENVIRONMENT)}
            ),
        ):
            with self.subTest(call), self.assertRaises(SessionBusy) as raised:
                call()
            self.assertEqual(
                (raised.exception.lock, raised.exception.holder),
                ("dummy-lock", f"{HOLDER['container']} (owner other)"),
            )
        self.popen_mock.assert_not_called()

    def test_start_records_the_environment(self):
        self.start(environment=carried(ENVIRONMENT))
        self.assertEqual(self.host_record()["environment"], ENVIRONMENT.persisted())

    def test_resume_with_another_environment_is_refused(self):
        self.resumable(ENVIRONMENT.persisted())
        other = carried(replace(ENVIRONMENT, gateway="workers"))
        self.order.clear()
        with self.assertRaisesRegex(ConfigError, "resume: environment differs"):
            self.start(resume="1", environment=other)
        self.assertEqual(self.order, [])
        with self.assertRaisesRegex(ConfigError, "resume: environment differs"):
            self.host._preflight(
                "w", self.launch, {**self.METADATA, "resume": "1", "environment": other}
            )

    def test_resume_differing_only_in_prefetch_runs_the_stored_one_without_github(self):
        self.host.github, self.host.github_permissions = "ensemble-bot", PERMISSIONS
        self.resumable(PREFETCHED.persisted())
        self.start(resume="1", environment=carried(ENVIRONMENT))
        manifest = self.manifest()
        self.assertEqual(manifest["prefetch"], {"repo": "o/r", "ref": "main"})
        self.assertIsNone(manifest["github"])
        self.assertNotIn("ENSEMBLE_PR_AUTHOR", self.docker.staged[0]["env"])
        self.assertEqual(self.host_record()["environment"], PREFETCHED.persisted())

    def test_pre_upgrade_record_counts_as_none(self):
        self.resumable("absent")
        with self.assertRaisesRegex(ConfigError, "resume: environment differs"):
            self.start(resume="1", environment=carried(ENVIRONMENT))
        self.start(resume="1")
        self.assertEqual(self.broker.built, {})
        self.assertNotIn("gateway", self.manifest())

    def test_resume_without_an_environment_uses_the_stored_one(self):
        self.resumable(ENVIRONMENT.persisted())
        self.host._preflight("w", self.launch, {**self.METADATA, "resume": "1"})
        self.assertEqual(self.order, ["version"])
        self.start(resume="1")
        self.assertEqual(self.broker.built["base_image"], "dummy-env:test")
        self.assertEqual(self.manifest()["lock"], "dummy-lock")
        self.assertEqual(self.host_record()["environment"], ENVIRONMENT.persisted())

    def test_resume_without_stored_or_given_environment_is_phase_one(self):
        self.resumable(None)
        self.start(resume="1")
        self.assertEqual(self.broker.built, {})
        self.assertFalse({"gateway", "secrets", "lock", "prefetch"} & set(self.manifest()))

    def test_resume_resends_the_stored_prefetch(self):
        self.resumable(PREFETCHED.persisted())
        self.start(resume="1")
        self.assertEqual(self.manifest()["prefetch"], {"repo": "o/r", "ref": "main"})

    def test_a_bad_environment_is_a_session_error(self):
        with self.assertRaisesRegex(SessionError, "bad environment"):
            self.start(environment='{"image": "x", "gateway": "workers", "role": "pm"}')
        self.assertEqual(self.broker.closed, [])


class PipeTest(Base):
    METADATA = StartTest.METADATA

    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        self.home_dir = os.path.join(self.state, "agent")
        self.exec = mock.Mock(pid=4321, returncode=None)
        self.exec.stdout, self.exec.stderr = io.StringIO("out\n"), io.StringIO()
        self.exec.wait.return_value = 0
        self.spawn_mock.side_effect = self.popen
        self.launch = Launch(
            ("agent", "-p"),
            cwd=self.cwd,
            env={"PATH": "/host/bin", "AGENT_HOME": self.home_dir, "K": "secret"},
        )

    def popen(self, argv, **kw):
        self.order.append("exec")
        self.popened = (argv, kw)
        self.lock_held = not _free(os.path.join(self.state, "pipe.lock"))
        self.popen_record = self.host_record()
        return self.exec

    def pipe(self, name="w", **metadata):
        return self.host.pipe(
            name,
            self.launch,
            metadata={k: v for k, v in {**self.METADATA, **metadata}.items() if v is not None},
        )

    def test_no_name_or_a_docker_env_key_is_refused_before_docker(self):
        with self.assertRaisesRegex(
            ConfigError, "^a headless session on the Docker Host needs a name$"
        ):
            self.pipe(None)
        for key in (*CLEARED, "DOCKER_CERT_PATH"):
            self.launch = replace(self.launch, env={**self.launch.env, key: "x"})
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    ConfigError, f"^docker session w: env {key} would reconfigure the docker CLI$"
                ),
            ):
                self.pipe()
            self.launch = replace(self.launch, env={"K": "v"})
        self.assertEqual(self.order, [])

    def test_with_secrets_gets_the_implementations_client_not_the_table_name(self):
        piped = self.pipe(client="work")
        argv, _ = self.popened
        start = argv.index("with-secrets")
        self.assertEqual(argv[start : start + 4], ["with-secrets", "--client", "agent", "agent"])
        piped.close()

    def test_pipe_starts_the_container_then_docker_exec_without_tmux(self):
        piped = self.pipe()
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertEqual(
            [
                step if isinstance(step, str | tuple) else step[:5]
                for step in self.order
                if step[0] != "maintain"  # the tick's own thread, at any time
            ],
            [
                ["ps", "-a", "--filter", "label=ensemble.session", "--format"],
                ["ps", "-a", "--filter", "label=ensemble.session", "--format"],
                "version",
                ("build", REPO),
                ["volume", "ls", "--filter", "label=ensemble.kind", "--format"],
                ("start", manifest, client_keys(REPO)),
                "exec",
            ],
        )
        argv, kw = self.popened
        self.assertEqual(
            argv,
            [
                *(DOCKER, "--host", self.ep, "exec", "-i", "--workdir", self.cwd),
                *("--env", "AGENT_HOME", "--env", "K"),
                *(CONTAINER, "with-secrets", "--client", "agent", "agent", "-p"),
            ],
        )
        self.assertEqual((kw["env"]["K"], kw["env"]["AGENT_HOME"]), ("secret", self.home_dir))
        self.assertEqual(kw["env"]["PATH"], os.environ["PATH"])
        self.assertFalse(set(CLEARED) & kw["env"].keys())
        self.assertFalse(set(GIT_IDENTITY) & kw["env"].keys())
        self.assertEqual(
            {k: kw[k] for k in ("stdin", "stdout", "stderr", "text", "errors", "process_group")},
            {
                "stdin": DEVNULL,
                "stdout": PIPE,
                "stderr": PIPE,
                "text": True,
                "errors": "replace",
                "process_group": 0,
            },
        )
        record = {
            "generation": GEN,
            "sid": "s1",
            "client": "agent",
            "mode": "pipe",
            "environment": None,
        }
        self.assertEqual((self.popen_record, self.host_record()), (record, record))
        self.assertTrue(self.lock_held)
        self.assertFalse(_free(os.path.join(self.state, "pipe.lock")))
        self.assertTrue(os.path.exists(os.path.join(self.state, f"{GEN}.result.json")))
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".ensemble", "tui", "w.lock")))
        with piped.stdout as out:
            self.assertEqual(list(out), ["out\n"])
        self.assertEqual((piped.wait(0), self.broker.closed), (0, []))
        piped.close()
        piped.close()
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertTrue(_free(os.path.join(self.state, "pipe.lock")))

    def test_a_prompt_file_is_staged_before_docker_exec(self):
        path = "/run/ensemble/prompt/prompt.md"
        self.pipe(prompt_file=path, prompt="雪" * 9).close()
        calls = [call for call in self.order if isinstance(call, list) or call == "exec"]
        staged = [i for i, call in enumerate(calls) if PROMPT_STAGE in call]
        self.assertEqual(len(staged), 1)
        self.assertLess(staged[0], calls.index("exec"))
        self.assertIn("雪" * 9, self.docker.inputs)

    def test_a_failed_prompt_stage_ends_the_pipe_before_docker_exec(self):
        with (
            mock.patch.object(self.host, "_stage_prompt", side_effect=SessionError("no room")),
            self.assertRaisesRegex(SessionError, "no room"),
        ):
            self.pipe(prompt_file="/run/ensemble/prompt/prompt.md", prompt="x")
        self.assertNotIn("exec", self.order)
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_github_app_reaches_the_piped_client_and_its_tokens_are_refreshed(self):
        self.host.github, self.host.github_permissions = "ensemble-bot", PERMISSIONS
        self.broker.started = replace(self.broker.started, github_user_id=343764394)
        piped = self.pipe()
        argv, kw = self.popened
        self.assertIn("ENSEMBLE_PR_AUTHOR", argv)
        self.assertEqual(kw["env"]["ENSEMBLE_PR_AUTHOR"], "ensemble-bot[bot]")
        for key, value in GIT_IDENTITY.items():
            self.assertIn(key, argv)
            self.assertNotIn(f"{key}={value}", argv)
            self.assertEqual(kw["env"][key], value)
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(
                json.load(f)["github"], {"app": "ensemble-bot", "permissions": PERMISSIONS}
            )
        self.assertTrue(self.broker.refreshed.wait(5))
        piped.close()
        self.assertIn(("maintain", CONTAINER, os.path.join(self.state, f"{GEN}.json")), self.order)

    def test_codex_pipe_uses_its_manifest_history_and_reports_expiry(self):
        home = os.path.join(self.state, "codex")
        self.launch = replace(
            self.launch,
            argv=("/opt/ensemble/bin/codex", "exec", "--json", "hello"),
            env={"CODEX_HOME": home},
        )
        reason = (
            "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login"
            " codex on the host, then resume."
        )
        self.broker.started = replace(
            self.broker.started,
            deadline={"at": "2000-01-01T00:00:00Z", "action": "close", "reason": reason},
        )
        expired = threading.Event()
        original = self.broker.close

        def close(container):
            result = original(container)
            expired.set()
            return result

        self.broker.close = close
        with self.assertLogs("session", "WARNING") as logs:
            piped = self.pipe(
                client="codex",
                home_field="codex_dir",
                home_dir="codex",
                credential="codex-access-token",
                home_persistent='["sessions"]',
            )
            self.assertTrue(expired.wait(5))
        with open(os.path.join(self.state, f"{GEN}.json")) as stream:
            manifest = json.load(stream)
        self.assertEqual(manifest["codex_dir"], home)
        self.assertNotIn("claude_dir", manifest)
        self.assertTrue(os.path.isdir(os.path.join(home, "sessions")))
        self.assertEqual(
            self.popened[0],
            [
                *(DOCKER, "--host", self.ep, "exec", "-i", "--workdir", self.cwd),
                *("--env", "CODEX_HOME"),
                *(CONTAINER, "with-secrets", "--client", "codex"),
                *("/opt/ensemble/bin/codex", "exec", "--json", "hello"),
            ],
        )
        self.assertEqual(
            logs.output,
            [f"WARNING:session:docker session w: client_auth_expired {reason}"],
        )
        self.assertEqual(self.broker.closed, [CONTAINER])
        piped.close()

    def test_piped_tick_logs_partial_result(self):
        response = data("maintain-output.json") | {"container": CONTAINER}
        log, runner = self.logged_to_file(), BrokerRunner((0, json.dumps(response), ""))
        called, wire = threading.Event(), Broker(runner).maintain

        def maintain(*args, **kwargs):
            try:
                return wire(*args, **kwargs)
            finally:
                called.set()

        self.broker.maintain = maintain
        piped = self.pipe()
        self.assertTrue(called.wait(5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "credential_refresh_failed" not in _read(log):
            time.sleep(0.01)
        piped.close()
        self.assertEqual(
            _read(log).splitlines(),
            [
                "docker session w: credential_refresh_failed codex: Renewal unavailable. (status 503)"
            ],
        )
        self.assertEqual(runner.calls[0][1], "maintain")

    def test_piped_tick_log_holds_only_the_brokers_message(self):
        log, called = self.logged_to_file(), threading.Event()
        message = f"Writing a GitHub token into {CONTAINER} failed."
        failed = {"code": "refresh_failed", "message": message}
        renewed = {"credential": "codex", "expires_at": "2100-01-01T00:00:00Z", "error": None}

        def maintain(*_args, **_kwargs):
            called.set()
            results = [{"credential": "github", "expires_at": None, "error": failed}, renewed]
            return Maintained(results, [], None, None)

        self.broker.maintain = maintain
        with mock.patch.dict(os.environ, {"SENTINEL_ENV": "SENTINEL-p1p3d"}):
            piped = self.pipe()
            self.assertTrue(called.wait(5))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "credential_refresh_failed" not in _read(log):
                time.sleep(0.01)
            piped.close()
        text = _read(log)
        self.assertEqual(
            text.splitlines(), [f"docker session w: credential_refresh_failed github: {message}"]
        )
        self.assertNotIn("SENTINEL", text)

    def logged_to_file(self):
        path = os.path.join(self.home, "session.log")
        handler = logging.FileHandler(path)
        logger = logging.getLogger("session")
        logger.addHandler(handler)
        self.addCleanup(handler.close)
        self.addCleanup(logger.removeHandler, handler)
        return path

    def test_a_piped_session_logs_and_acks_its_credential_notice(self):
        log, acked, calls = self.logged_to_file(), threading.Event(), []

        def maintain(_container, _manifest, delivered=(), *, root):
            self.assertEqual(root, REPO)
            calls.append(tuple(delivered))
            if delivered:
                acked.set()
                return Maintained([], [], None, None)
            notice = {"key": "ensemble-env/db@1:7d", "text": "ensemble-env/db expires soon"}
            return Maintained([], [notice], None, None)

        self.broker.maintain = maintain
        piped = self.pipe()
        self.assertTrue(acked.wait(5))
        piped.close()
        with open(log) as stream:
            self.assertIn(
                "docker session w: credential_notice ensemble-env/db expires soon", stream.read()
            )
        self.assertEqual(calls, [(), ("ensemble-env/db@1:7d",)])

    def test_a_piped_maintain_failure_logs_a_fixed_marker(self):
        log, failed = self.logged_to_file(), threading.Event()

        def maintain(*_args, **_kwargs):
            failed.set()
            raise SessionError("broker: bad response to maintain: SENTINEL-headless")

        self.broker.maintain = maintain
        piped = self.pipe()
        self.assertTrue(failed.wait(5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "credential_maintenance_failed" not in _read(log):
            time.sleep(0.01)
        piped.close()
        with open(log) as stream:
            text = stream.read()
        self.assertIn("docker session w: credential_maintenance_failed transport", text)
        self.assertNotIn("SENTINEL", text)

    def test_stop_kills_the_exec_then_closes_and_a_close_failure_only_warns(self):
        piped = self.pipe()
        self.broker.close = mock.Mock(side_effect=SessionError("broker: boom"))
        with mock.patch("os.killpg") as killpg, self.assertLogs("session", "WARNING") as logs:
            piped.stop(ended=False)
        killpg.assert_called_once_with(4321, signal.SIGKILL)
        self.broker.close.assert_called_once_with(CONTAINER)
        self.assertIn("docker session w: broker: boom", logs.output[0])
        self.assertTrue(_free(os.path.join(self.state, "pipe.lock")))

    def test_a_busy_name_or_a_failed_exec_rolls_back(self):
        os.makedirs(os.path.join(self.home, ".ensemble", "tui"), exist_ok=True)
        busy = os.open(
            os.path.join(self.home, ".ensemble", "tui", "w.lock"), os.O_RDWR | os.O_CREAT
        )
        fcntl.flock(busy, fcntl.LOCK_EX)
        with self.assertRaisesRegex(SessionError, "^docker session w: busy$"):
            self.pipe()
        os.close(busy)
        self.assertEqual(self.order, [])
        self.spawn_mock.side_effect = OSError("no docker")
        with self.assertRaisesRegex(SessionError, "^docker session w: no docker$"):
            self.pipe()
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertTrue(_free(os.path.join(self.state, "pipe.lock")))

    def test_resume_keeps_home_and_checks_history(self):
        with self.assertRaisesRegex(ConfigError, "no native history to resume"):
            self.pipe(resume="1")
        transcript = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w") as f:
            f.write("{}\n")
        self.launch = replace(self.launch, transcript=transcript)
        self.pipe(resume="1").close()
        self.assertTrue(os.path.exists(transcript))

    def piped_row(self, held):
        """w's container, a headless generation, its pipe lock held or not."""
        self.listed("w")
        self.record(mode="pipe")
        if held:
            fd = os.open(os.path.join(self.state, "pipe.lock"), os.O_RDWR | os.O_CREAT)
            self.addCleanup(_close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        return None

    def test_a_live_piped_container_is_kept_unlisted_and_an_orphan_closed(self):
        fd = self.piped_row(held=True)
        self.host.sweep("x")
        self.host.sweep("w")
        self.assertEqual(self.broker.closed, [])
        self.assertEqual((self.host.list(), self.host.rows()), ([], []))
        for call in (self.host.attach, self.host.attach_target):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(SessionError, "^no docker session w$"),
            ):
                call("w")
        self.tui_session("w")
        with self.assertRaisesRegex(
            SessionError,
            "^docker session w: a headless run holds it; wait for it to end or stop its run$",
        ):
            StartTest.start(self)
        self.assertEqual(self.broker.closed, [])
        _close(fd)
        self.host.sweep("x")
        self.assertEqual(self.broker.closed, [CONTAINER])


class PreflightTest(Base):
    def validate(self):
        self.host.validate("w", Launch(("agent",), cwd=self.cwd, env={}))

    def test_preflight_reports_its_time(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.validate()
        self.assertRegex(stderr.getvalue(), r"\Adocker timing w: preflight \d+\.\d\d s\n\Z")

    def test_preflight_runs_version_then_build_and_writes_nothing(self):
        self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertFalse(os.path.exists(self.state))

    def test_resume_preflight_builds_with_the_whitelisted_plugins(self):
        self.host.plugins = (
            "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
            "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertEqual(
            self.broker.plugins,
            (
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
        )
        self.assertFalse(os.path.exists(self.state))

    def test_an_argument_over_the_linux_limit_is_refused_before_any_docker_call(self):
        fits, over = "a" * 131_071, "雪" * 43_691  # 131,073 bytes
        self.host.validate("w", Launch(("agent", fits), cwd=self.cwd))
        self.order.clear()
        want = (
            "^docker session w: argument 1 is 131073 bytes, over the 131072-byte limit Linux "
            "exec allows one argument$"
        )
        with self.assertRaisesRegex(ConfigError, want):
            self.host.validate("w", Launch(("agent", over), cwd=self.cwd))
        with self.assertRaisesRegex(ConfigError, want):
            self.host.terminal("w", Launch(("agent", over), cwd=self.cwd), metadata={})
        with self.assertRaisesRegex(ConfigError, want):
            self.host.pipe("w", Launch(("agent", over), cwd=self.cwd), metadata={})
        self.assertEqual(self.order, [])

    def test_a_failed_handshake_stops_before_build(self):
        self.broker.failing["version"] = SessionError("broker: protocol '1', want '2'")
        with self.assertRaisesRegex(SessionError, "protocol '1', want '2'"):
            self.validate()
        self.assertEqual(self.order, ["version"])

    def test_a_failed_build_stops_before_any_file(self):
        self.broker.failing["build"] = BrokerError(
            8, "build_failed", "no image", "not_needed", None
        )
        with self.assertRaisesRegex(SessionError, "broker: no image"):
            self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertFalse(os.path.exists(self.state))

    def test_a_missing_cwd_is_refused_before_the_broker(self):
        launch = Launch(("agent",), cwd=os.path.join(self.account, "src", "gone"), env={})
        for check in (self.host.validate, self.host.terminal, self.host.pipe):
            with (
                self.subTest(check=check.__name__),
                self.assertRaisesRegex(ConfigError, f"Docker cwd {launch.cwd}: no such directory"),
            ):
                check("w", launch)
        self.assertEqual(self.order, [])

    def test_a_session_preflight_accepts_a_cwd_not_yet_made(self):
        cwd = os.path.join(self.account, "src", "later")
        self.host._preflight("w", Launch(("agent",), cwd=cwd, env={}))
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertFalse(os.path.exists(cwd))

    def test_a_session_preflight_refuses_a_cwd_that_is_no_directory(self):
        cwd = os.path.join(self.account, "src", "file")
        open(cwd, "w").close()
        with self.assertRaisesRegex(ConfigError, f"Docker cwd {cwd}: no such directory"):
            self.host._preflight("w", Launch(("agent",), cwd=cwd, env={}))
        self.assertEqual(self.order, [])


class TerminalTest(Base):
    def test_session_commands_run_through_docker_exec(self):
        self.docker.replies = {
            ("tmux", "display-message"): pane(),
            ("tmux", "capture-pane"): (0, "hello\n\n", ""),
        }
        process = self.terminal()
        self.live_supervisor()
        process.send(Paste("hi"))
        process.press([KeyPress("a")])
        self.assertEqual(process.screen(), "hello")
        self.assertEqual(self.host.attach("w").screen(5), "hello")
        execs = self.docker.execs()
        self.assertTrue(all(call[0] == "tmux" for call in execs))
        commands = [call[1] for call in execs]
        for command in ("load-buffer", "copy-mode", "capture-pane"):
            self.assertIn(command, commands)
        self.assertIn("hi", self.docker.inputs)
        self.assertTrue(
            all(c[:3] == ["exec", "-i", CONTAINER] for c in self.order if c[0] == "exec")
        )

    def test_screen_text_reaches_the_host_without_control_characters(self):
        process = self.terminal()
        self.live_supervisor()
        self.docker.replies = {
            ("tmux", "display-message"): pane(),
            ("tmux", "capture-pane"): (0, "a\x1b]0;x\x07b\tc\n\x1b[2Jd\u3000e\u200d\x9b\r\n", ""),
        }
        self.assertEqual(process.screen(), "a]0;xb\tc\n[2Jd\u3000e\u200d")

    def test_screen_of_a_terminal_not_running_is_refused_before_reading(self):
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane("1 3 ")}
        with self.assertRaisesRegex(SessionError, "^session w is not running on host docker$"):
            process.screen()
        self.assertNotIn("capture-pane", [call[1] for call in self.docker.execs()])

    def test_dead_pane_returncode(self):
        process = self.terminal()
        for dead, code in (("1 3 ", 3), ("1  9", -9), ("1  kill", -signal.SIGKILL)):
            with self.subTest(dead=dead):
                self.docker.replies = {("tmux", "display-message"): pane(dead)}
                self.assertFalse(process.alive())
                self.assertEqual(process.returncode(), code)

    def test_missing_container_is_absent(self):
        process = self.terminal()
        for reply in (
            (1, "", f"Error response from daemon: No such container: {CONTAINER}\n"),
            (1, "", f"Error response from daemon: container {CONTAINER} is not running\n"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {(): reply}
                self.assertEqual(process.state(), Liveness.ABSENT)
                self.assertFalse(process.alive())
                self.assertEqual(process.returncode(), -signal.SIGHUP)
                self.assertEqual(process.status().state, StatusState.DEAD)

    def test_a_probe_that_times_out_or_overflows_is_unknown_not_exited(self):
        process = self.terminal()
        for reply in (TimeoutExpired("docker", 10), Overflow("output over 8388608 bytes")):
            with self.subTest(reply=reply):
                self.docker.replies = {(): reply}
                self.assertEqual(process.state(), Liveness.UNKNOWN)
                self.assertIsNone(process.returncode())
                self.assertEqual(process.status().state, StatusState.UNKNOWN)
                with self.assertRaisesRegex(SessionError, f"docker exec in {CONTAINER}: "):
                    process.alive()

    def test_terminal_never_reads_container_control(self):
        self.terminal()
        self.assertEqual(self.docker.execs(), [])

    def test_kill_signals_the_pane_group(self):
        process = self.terminal()
        self.docker.replies = {
            ("tmux", "display-message"): pane("1 0 "),
            ("kill", "-0"): (1, "", "kill: (-4343) - No such process\n"),
        }
        process.kill()
        self.assertEqual(self.docker.execs()[0], ["kill", "-KILL", "--", "-4343"])
        self.assertIn(["kill", "-0", "--", "-4343"], self.docker.execs())

    def test_a_group_gone_or_its_container_gone_or_stopped_has_exited(self):
        process = self.terminal()
        for reply in (
            (1, "", "kill: (-4343) - No such process\n"),
            (1, "", f"Error response from daemon: No such container: {CONTAINER}\n"),
            (1, "", f"Error response from daemon: container {CONTAINER} is not running\n"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {
                    ("tmux", "display-message"): pane("1 0 "),
                    ("kill", "-0"): reply,
                }
                self.assertTrue(process.wait_exit(0))

    def probing(self, screens=None):
        """A terminal on a live pane, at a fake time `now[0]`, and its tmux execs' commands so far."""
        process = self.terminal()
        self.docker.replies = {
            ("tmux", "display-message"): pane(),
            ("tmux", "capture-pane"): screens or (0, "idle\n", ""),
        }
        now = [100.0]
        clock = mock.patch("time.monotonic", lambda: now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.docker.order.clear()
        return process, now, lambda: [c[1] for c in self.docker.execs() if c[0] == "tmux"]

    @staticmethod
    def tick(process):
        """Tui.poll's calls on its terminal in one tick."""
        process.state()
        process.returncode()
        process.screen()
        process.state()

    def test_an_idle_pane_costs_at_most_one_exec_per_second(self):
        process, now, tmux = self.probing()
        counts = []
        for _ in range(120):  # 60 s of HostedSession's 0.5 s ticks
            before = len(tmux())
            self.tick(process)
            counts.append(len(tmux()) - before)
            now[0] += 0.5
        self.assertLessEqual(sum(counts), 60, f"{sum(counts)} tmux execs in 60 s")
        for start in range(12, 109):
            self.assertLessEqual(sum(counts[start : start + 12]), 6, (start, counts))

    def test_one_tick_shares_one_probe_and_one_capture(self):
        process, now, tmux = self.probing()
        for _ in range(3):
            self.tick(process)
            self.assertEqual((process.state(), process.returncode()), (Liveness.LIVE, None))
            now[0] += 0.1
        self.assertEqual(tmux(), ["display-message", "capture-pane"])

    def test_the_probe_interval_backs_off_to_its_cap_and_resets_on_change(self):
        process, now, tmux = self.probing()

        def probes_at(ticks):
            found = []
            for _ in range(ticks):
                before = tmux().count("display-message")
                self.tick(process)
                if tmux().count("display-message") > before:
                    found.append(now[0] - start)
                now[0] += 0.5
            return found

        start = now[0]
        self.assertEqual(probes_at(26), [0, 0.5, 1.5, 3.5, 6.5, 9.5, 12.5])
        probes_at(1)
        self.docker.replies[("tmux", "capture-pane")] = (0, "busy\n", "")
        start = now[0]
        found = probes_at(8)  # seen at the next refresh, then probed every tick
        self.assertLessEqual(found[0], docker_host.PROBE_MAX)
        self.assertEqual(found[1] - found[0], 0.5)
        for act in (lambda: process.send(Paste("hi")), lambda: process.press([KeyPress("a")])):
            probes_at(20)  # backed off to the cap
            act()
            start = now[0]
            self.assertEqual(probes_at(2), [0.5])

    def test_a_probe_that_changes_resets_the_interval(self):
        process, now, tmux = self.probing()
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        self.docker.replies[("tmux", "display-message")] = TimeoutExpired("docker", 10)
        while process.state() != Liveness.UNKNOWN:
            now[0] += 0.5
        self.docker.replies[("tmux", "display-message")] = pane()
        now[0] += 0.5
        self.assertEqual(process.state(), Liveness.LIVE)

    def test_a_dead_pane_seen_by_alive_resets_the_interval_and_is_the_snapshot(self):
        process, now, tmux = self.probing()
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        self.docker.replies[("tmux", "display-message")] = pane("1 3 ")
        self.assertFalse(process.alive())
        self.docker.order.clear()
        self.assertEqual((process.returncode(), process.state()), (3, Liveness.LIVE))
        self.assertEqual(tmux(), [])
        now[0] += docker_host.PROBE_MIN - 0.01
        process.state()
        self.assertEqual(tmux(), [])
        now[0] += 0.01
        process.state()
        self.assertEqual(tmux(), ["display-message"])

    def test_an_unchanged_alive_keeps_the_interval(self):
        process, now, tmux = self.probing()
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        self.assertTrue(process.alive())
        self.docker.order.clear()
        now[0] += docker_host.PROBE_MAX - 0.01
        process.state()
        self.assertEqual(tmux(), [])
        now[0] += 0.01
        process.state()
        self.assertEqual(tmux(), ["display-message"])

    def test_alive_renews_the_snapshot_so_the_next_screen_is_fresh(self):
        process, now, tmux = self.probing()
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        self.docker.replies[("tmux", "capture-pane")] = (0, "Trust this folder?\n", "")
        self.assertEqual(process.screen(), "idle")
        self.docker.order.clear()
        self.assertTrue(process.alive())
        self.assertEqual(process.screen(), "Trust this folder?")
        self.assertEqual(tmux(), ["display-message", "capture-pane"])

    def test_an_exit_during_back_off_is_seen_within_the_cap(self):
        process, now, tmux = self.probing()
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        died = now[0]
        self.docker.replies[("tmux", "display-message")] = pane("1 3 ")
        while process.returncode() is None:
            now[0] += 0.5
        self.assertLessEqual(now[0] - died, docker_host.PROBE_MAX)
        self.docker.replies = {
            (): (1, "", f"Error response from daemon: No such container: {CONTAINER}\n")
        }
        gone = now[0]
        while process.state() != Liveness.ABSENT:
            now[0] += 0.5
        self.assertLessEqual(now[0] - gone, docker_host.PROBE_MAX)
        self.assertEqual(process.returncode(), -signal.SIGHUP)

    def test_a_changing_screen_keeps_the_interval_at_its_minimum(self):
        shown = iter(range(1000))
        process, now, tmux = self.probing(lambda _: done(0, f"working {next(shown)}\n"))
        for _ in range(20):
            self.tick(process)
            now[0] += 0.5
        self.assertEqual(tmux(), ["display-message", "capture-pane"] * 20)

    def test_alive_and_screen_lines_always_probe_fresh(self):
        process, now, tmux = self.probing()
        self.tick(process)
        self.docker.order.clear()
        for _ in range(3):
            self.assertTrue(process.alive())
            self.assertEqual(process.screen(5), "idle")
        self.assertEqual(tmux(), ["display-message", "display-message", "capture-pane"] * 3)

    def test_a_cached_unknown_probe_and_capture_error_keep_todays_errors(self):
        process, now, tmux = self.probing()
        self.docker.replies[("tmux", "display-message")] = TimeoutExpired("docker", 10)
        for _ in range(2):
            self.assertEqual(process.state(), Liveness.UNKNOWN)
            self.assertIsNone(process.returncode())
            with self.assertRaisesRegex(
                Unanswered, f"^tmux session w: .*docker exec in {CONTAINER}: "
            ):
                process.screen()
        self.assertEqual(tmux(), ["display-message"])
        now[0] += 0.5
        self.docker.replies = {
            ("tmux", "display-message"): pane(),
            ("tmux", "capture-pane"): (1, "", "boom"),
        }
        for _ in range(2):
            with self.assertRaisesRegex(SessionError, "^docker session w: tmux: boom$"):
                process.screen()
        self.assertEqual(tmux(), ["display-message", "display-message", "capture-pane"])

    def test_wait_exit_backs_off_to_its_cap_within_the_timeout(self):
        process = self.terminal()
        now, sleeps = [100.0], []

        def sleep(seconds):
            sleeps.append(round(seconds, 6))
            now[0] += seconds

        live = {("tmux", "display-message"): pane(), ("kill", "-0"): (0, "", "")}
        self.docker.replies = live
        with (
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch("time.sleep", sleep),
        ):
            self.assertFalse(process.wait_exit(4))
            self.assertEqual(sleeps, [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0])
            sleeps.clear()
            self.docker.replies = {
                ("tmux", "display-message"): pane("1 0 "),
                ("kill", "-0"): (1, "", "kill: (-4343) - No such process\n"),
            }
            self.assertTrue(process.wait_exit(4))
            self.assertEqual(sleeps, [])

    def test_waits_other_than_wait_exit_keep_a_fixed_poll(self):
        now, sleeps, checks = [0.0], [], iter([False, False, False, True])

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        with (
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch("time.sleep", sleep),
        ):
            self.assertTrue(docker_host._wait(lambda: next(checks), 5))
        self.assertEqual(sleeps, [docker_host.POLL] * 3)

    def test_a_pane_probe_that_cannot_answer_is_unanswered_before_any_kill(self):
        process = self.terminal()
        for reply in (
            TimeoutExpired("docker", 10),
            Overflow("output over 8388608 bytes"),
            (1, "", "server exited unexpectedly"),
        ):
            with self.subTest(reply=reply):
                self.docker.order.clear()
                self.docker.replies = {("tmux", "display-message"): reply}
                with self.assertRaisesRegex(Unanswered, "^tmux session w: "):
                    process.alive()
                with self.assertRaisesRegex(Unanswered, "^tmux session w: "):
                    process.wait_exit(0)
                self.assertNotIn("kill", [call[0] for call in self.docker.execs()])

    def test_a_group_probe_that_cannot_answer_is_unknown_not_exited(self):
        process = self.terminal()
        for reply in (
            TimeoutExpired("docker", 10),
            Overflow("output over 8388608 bytes"),
            (1, "", "kill: (-4343) - Operation not permitted\n"),
            (126, "", ""),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {
                    ("tmux", "display-message"): pane("1 0 "),
                    ("kill", "-0"): reply,
                }
                with self.assertRaisesRegex(Unanswered, "^process group w: "):
                    process.wait_exit(0)
                with self.assertRaisesRegex(Unanswered, "^process group w: "):
                    process.kill()

    def test_record_and_display_ignore_container_options(self):
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        self.assertEqual(
            (process.display(), process.session_identity(), process.record()),
            ("lead/below", ("s1", GEN), None),
        )
        record = RECORD
        process.publish(record)
        self.assertEqual(process.record(), record)
        self.assertNotIn("hidden", self.host_record())
        self.assertFalse(any("show-options" in call for call in self.docker.execs()))
        self.record(generation="f" * 32)
        self.assertEqual(
            (process.display(), process.session_identity(), process.record()),
            (None, (None, None), None),
        )
        with self.assertRaisesRegex(SessionError, "is gone"):
            process.publish(record)

    def test_mark_names_the_host_session(self):
        events = os.path.join(self.home, "events")
        open(events, "w").close()
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        process.mark(StatusState.DONE, events)
        self.assertIn(["tmux", "set-option", "-t", "%7", "@state", "done"], self.docker.execs())
        self.assertFalse(any("#S" in call for call in self.docker.execs()))
        with open(events) as f:
            self.assertRegex(f.read(), r"^\d\d:\d\d:\d\d w done\n$")

    def test_the_viewers_status_attach_is_the_docker_command(self):
        process = self.terminal()
        self.docker.replies = {("tmux", "display-message"): pane()}
        want = f"{DOCKER} --host {self.ep} exec -it {CONTAINER} tmux attach -t '=w'"
        self.assertIsNone(process.status().attach)
        tmux = mock.create_autospec(TmuxCli, instance=True)
        tmux.runner = mock.Mock()
        viewer = Viewer(tmux=tmux)
        self.assertEqual(displays.status(viewer, self.host, process).attach, want)
        self.assertEqual(viewer.attach_command("w", self.host), want)


class CloseTest(Base):
    def hold_lock(self):
        lock = os.path.join(self.state, "supervisor.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with open(os.path.join(self.state, "supervisor.pid"), "w") as f:
            f.write("999")
        return fd

    def test_close_order(self):
        process = self.terminal()
        fd = self.hold_lock()

        def kill(pid, sig):
            self.order.append(("signal", pid, sig))
            os.close(fd)

        with mock.patch("os.kill", side_effect=kill):
            process.close()
        self.assertEqual(
            [step[:4] if isinstance(step, list) else step for step in self.order[1:]],
            [
                ["ps", "-a", "--filter", "label=ensemble.session"],
                ["exec", "-i", CONTAINER, "tmux"],
                ["exec", "-i", CONTAINER, "tmux"],
                ("signal", 999, signal.SIGTERM),
                ("close", CONTAINER),
            ],
        )
        self.assertEqual(self.spawned[-1][0][-2:], [DockerHost.__module__, DOCKER])
        self.assertEqual(self.docker.execs()[0], ["tmux", "has-session", "-t", "=w"])
        self.assertEqual(
            self.docker.execs()[1],
            [
                "tmux",
                "set-hook",
                "-p",
                "-u",
                "-t",
                "%7",
                "pane-died",
                ";",
                "kill-session",
                "-t",
                "=w",
            ],
        )
        self.assertTrue(os.path.isdir(self.state))

    def test_close_succeeds_without_a_host_tmux(self):
        process = self.terminal()
        with mock.patch("os.kill"):
            process.close()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_restart_starts_the_next_generation(self):
        self.listed("w")
        self.record(sid=SID)
        closed = self.broker.close

        def close(container):
            self.docker.ps, self.docker.replies = (
                [],
                {(): (1, "", f"Error response from daemon: No such container: {container}")},
            )
            return closed(container)

        def start(*_, **__):
            self.docker.replies = {}
            self.listed("w", gen=NEW)
            self.record(sid=SID, generation=NEW)
            return session

        self.broker.close = close
        session_cls = mock.Mock()
        session_cls.host.return_value = self.host
        session_cls.start.side_effect = start
        session = mock.Mock(showable=False, opening_pending=None)
        session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        state = WorkerState("w", SID, "agent", self.cwd, "docker", (), {}, None, "lead/below")
        with mock.patch("os.kill"):
            record = Worker(state, session_cls=session_cls, say=lambda _: None).restart()
        self.assertEqual(set(self.broker.closed), {CONTAINER})
        self.assertTrue(session_cls.start.call_args.args[0].resume)
        found = self.host_record()
        self.assertEqual((found["generation"], found["record"]), (NEW, record.to_line()))

    def test_close_when_container_gone(self):
        process = self.terminal()
        self.docker.replies = {
            (): (1, "", f"Error response from daemon: No such container: {CONTAINER}")
        }
        with mock.patch("os.kill") as kill:
            process.close()
        kill.assert_not_called()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_kill_that_cannot_run_still_stops_the_supervisor_and_the_container(self):
        process = self.terminal()
        fd = self.hold_lock()
        self.docker.replies = {("tmux", "set-hook"): FileNotFoundError(2, "no docker")}

        def kill(pid, sig):
            self.order.append(("signal", pid, sig))
            os.close(fd)

        with (
            mock.patch("os.kill", side_effect=kill),
            self.assertRaisesRegex(SessionError, r"^docker session w: kill: tmux: .*no docker"),
        ):
            process.close()
        self.assertIn(("signal", 999, signal.SIGTERM), self.order)
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_a_supervisor_that_keeps_its_lock_fails_close_after_the_rest(self):
        process = self.terminal()
        self.hold_lock()
        with (
            mock.patch("os.kill") as kill,
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", side_effect=range(0, 10000, 10)),
            self.assertRaisesRegex(SessionError, "supervisor 999: still holds"),
        ):
            process.close()
        self.assertEqual(
            [c.args for c in kill.call_args_list], [(999, signal.SIGTERM), (999, signal.SIGKILL)]
        )
        self.assertEqual(self.broker.closed, [CONTAINER])


class RecoverTest(Base):
    def setUp(self):
        super().setUp()
        self.listed("w")
        self.record(events=os.path.join(self.home, "events"))
        self.lock = os.path.join(self.state, "supervisor.lock")
        self.result(IMAGE)

    def result(self, image, digest=DIGEST):
        with open(os.path.join(self.state, f"{GEN}.result.json"), "w") as f:
            json.dump({"container": CONTAINER, "image": image, "image_digest": digest}, f)

    def serves(self):
        return [call for call in self.docker.execs() if call[:2] == [RELAY, "serve"]]

    def test_attach_restarts_daemon_and_supervisor_when_the_lock_is_free(self):
        process = self.host.attach("w")
        self.assertTrue(process.current)
        self.assertEqual(self.serves(), [[RELAY, "serve", GEN]])
        self.assertIn(["exec", "-d", CONTAINER, RELAY, "serve", GEN], self.docker.order)
        ((argv, kw, _),) = self.spawned
        self.assertEqual(json.loads(argv[12])[-2:], [RELAY, "connect"])
        self.assertEqual(argv[10:12], [GEN, str(kw["pass_fds"][0])])
        with open(self.lock) as f:
            self.assertEqual(f.read(), GEN)
        with open(os.path.join(self.state, "supervisor.pid")) as f:
            self.assertEqual(f.read(), "4321")
        self.assertTrue(_free(self.lock))

    def test_recovery_spawns_the_host_hook_and_host_json_events(self):
        self.host.attach("w")
        ((argv, _, _),) = self.spawned
        self.assertEqual(
            argv[5:10],
            [
                "w",
                self.state,
                os.path.join(self.home, ".ensemble", "tui", "w"),
                os.path.join(self.home, "events"),
                os.path.join(REPO, "bin", "ensemble-session"),
            ],
        )

    def test_supervisor_recovery_checks_the_digest_with_the_whitelisted_plugins(self):
        self.host.plugins = (
            "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
            "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.host.attach("w")
        self.assertEqual(
            self.broker.plugins,
            (
                "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43",
                "beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
        )
        self.assertEqual(len(self.spawned), 1)

    def test_attach_with_the_lock_held_starts_nothing(self):
        self.live_supervisor()
        self.host.attach("w")
        self.assertEqual((self.serves(), self.spawned), ([], []))
        self.assertNotIn(("build", REPO), self.order)

    def test_two_attaches_on_a_free_lock_spawn_one_supervisor(self):
        self.inherit = True
        self.host.attach("w")
        self.host.attach("w")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(len(self.serves()), 1)
        self.assertFalse(_free(self.lock))

    def test_close_right_after_a_recovering_attach_stops_the_new_supervisor(self):
        self.inherit = True
        process = self.host.attach("w")

        def kill(pid, sig):
            self.order.append(("signal", pid, sig))
            for fd in self.spawned[0][2]:
                _close(fd)

        with mock.patch("os.kill", side_effect=kill):
            process.close()
        self.assertIn(("signal", 4321, signal.SIGTERM), self.order)
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_attach_on_a_stopped_or_other_generation_container_starts_nothing(self):
        for case in ("stopped", "other generation"):
            with self.subTest(case=case):
                if case == "stopped":
                    self.listed("w", state="exited")
                else:
                    self.listed("w")
                    self.record(generation="f" * 32)
                    self.docker.replies = {("tmux", "display-message"): pane()}
                self.host.attach("w")
                self.assertEqual((self.serves(), self.spawned), ([], []))
        self.listed("w", state="exited")
        self.record()
        (row,) = self.host.rows()
        self.assertEqual(row.state, StatusState.DEAD)

    def test_recover_builds_the_stored_environment(self):
        self.record(events=os.path.join(self.home, "events"), environment=ENVIRONMENT.persisted())
        self.host.attach("w")
        self.assertEqual(self.broker.built["base_image"], "dummy-env:test")
        self.assertEqual(len(self.spawned), 1)

    def test_recover_keeps_worker_after_client_only_rebuild(self):
        self.result(IMAGE, "sha256:" + "d" * 64)
        process = self.host.attach("w")
        self.assertTrue(process.current)
        self.assertEqual(len(self.spawned), 1)
        self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.stale")))

    def test_an_older_image_is_not_recovered_and_closes_without_the_supervisor(self):
        for image in ("ensemble-worker:" + "d" * 64, None):
            with self.subTest(image=image):
                self.host._warned.clear()
                stale = os.path.join(self.state, f"{GEN}.stale")
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(stale)
                if image is None:
                    os.unlink(os.path.join(self.state, f"{GEN}.result.json"))
                else:
                    self.result(image)
                with self.assertLogs("session", "WARNING") as logs:
                    process = self.host.attach("w")
                    builds = self.order.count(("build", REPO))
                    self.host.attach("w")
                self.assertEqual(self.order.count(("build", REPO)), builds)
                with open(stale) as f:
                    self.assertEqual(f.read(), IMAGE)
                self.assertEqual(
                    logs.output,
                    ["WARNING:session:docker session w runs an older image; restart it to upgrade"],
                )
                self.assertFalse(process.current)
                self.assertEqual((self.serves(), self.spawned), ([], []))
                self.assertFalse(os.path.exists(self.lock))
        self.live_supervisor()
        with mock.patch("os.kill") as kill:
            process.close()
        kill.assert_not_called()
        self.assertEqual(self.broker.closed, [CONTAINER])

    def test_recovery_builds_before_taking_the_lock_and_a_lock_error_only_logs(self):
        held = []
        build = self.broker.build
        self.broker.build = lambda package, **kwargs: (
            held.append(os.path.exists(self.lock) and not _free(self.lock))
            or build(package, **kwargs)
        )
        self.host.attach("w")
        self.assertEqual((held, len(self.spawned)), ([False], 1))
        self.spawned.clear()
        os.unlink(self.lock)
        os.mkdir(self.lock)
        with self.assertLogs("session", "WARNING") as logs:
            self.assertTrue(self.host.attach("w").current)
        self.assertIn("no supervisor restart", logs.output[0])
        self.assertEqual((len(self.serves()), self.spawned), (1, []))

    def test_a_failing_recovery_logs_and_close_still_removes_the_container(self):
        for case, replies in (
            ("serve fails", {(RELAY, "serve"): (1, "", "boom")}),
            ("serve times out", {(RELAY, "serve"): TimeoutExpired("docker", 10)}),
        ):
            with self.subTest(case=case):
                self.docker.replies = replies
                with self.assertLogs("session", "WARNING") as logs:
                    process = self.host.attach("w")
                self.assertIn("no supervisor restart", logs.output[0])
                self.assertEqual(self.spawned, [])
                self.assertTrue(_free(self.lock))
        with mock.patch("shutil.which", return_value=None), self.assertLogs("session") as logs:
            self.host.attach("w")
        self.assertEqual(
            logs.output,
            [
                "WARNING:session:docker session w: no supervisor restart:"
                " ensemble-session is not on PATH (its hooks run it)"
            ],
        )
        process.close()
        self.assertEqual(self.broker.closed, [CONTAINER])


class SweepTest(Base):
    OLD = "f" * 32

    def setUp(self):
        super().setUp()
        self.tui = os.path.join(self.home, ".ensemble", "tui")
        self.answers = {}
        self.docker.replies = {("tmux", "has-session"): self.has_session}

    def has_session(self, cmd):
        container = self.docker.order[-1][2]
        found = self.answers.get(container, (0, "", ""))
        if isinstance(found, BaseException):
            raise found
        return done(*found)

    def row(self, name, gen=GEN, state="running"):
        return f"ensemble-{name}-{gen}\t{name}\t{gen}\t{state}"

    def files(self):
        return {
            os.path.relpath(os.path.join(root, name), self.home)
            for root, dirs, names in os.walk(self.home)
            for name in names + dirs
        }

    def test_sweep_removes_leftovers_and_skips_busy_invalid_and_live_ones(self):
        for name, gen in (("b", self.OLD), ("c", GEN), ("d", GEN), ("e", GEN), ("f", GEN)):
            self.tui_session(name, gen)
        busy = os.open(os.path.join(self.tui, "f.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, busy)
        fcntl.flock(busy, fcntl.LOCK_EX)
        self.answers = {
            f"ensemble-c-{GEN}": (1, "", "can't find session: c"),
            f"ensemble-w-{self.OLD}": (1, "", "no server running on /tmp/tmux-1000/default"),
        }
        self.docker.ps = [
            self.row("w", self.OLD),  # own session, older generation
            self.row("a"),  # no session.json
            self.row("b"),  # session.json of another generation
            self.row("c"),  # its tmux has no session c
            self.row("d"),  # live
            self.row("e", state="exited"),  # stopped, still its generation: shows dead
            self.row("f"),  # starting or closing elsewhere
            f"ensemble-g-{GEN}\t\t{GEN}\trunning",
            f"ensemble-..-{GEN}\t../x\t{GEN}\trunning",
            f"ensemble-h-{GEN}\ti\t{GEN}\trunning",
            f"ensemble-j-{GEN.upper()}\tj\t{GEN.upper()}\trunning",
        ]
        self.host.sweep("w")
        self.assertEqual(
            self.broker.closed,
            [
                f"ensemble-w-{self.OLD}",
                f"ensemble-a-{GEN}",
                f"ensemble-b-{GEN}",
                f"ensemble-c-{GEN}",
            ],
        )
        probed = [call[2] for call in self.docker.order if call[:2] == ["exec", "-i"]]
        self.assertEqual(probed, [f"ensemble-c-{GEN}", f"ensemble-d-{GEN}"])
        self.assertEqual(
            sorted(n for n in os.listdir(self.tui) if n.endswith(".lock")),
            ["b.lock", "c.lock", "d.lock", "e.lock", "f.lock"],
        )

    def test_sweep_skips_a_container_whose_tmux_does_not_answer(self):
        self.tui_session("c")
        for answer in (
            TimeoutExpired("docker", 10),
            Overflow("output over"),
            (1, "", f"Error response from daemon: Container ensemble-c-{GEN} is not running"),
            (1, "", "no server running on ensemble-c: timed out"),
            (0, "", ""),
            (2, "", "can't find session: c"),
        ):
            with self.subTest(answer=answer):
                self.answers = {f"ensemble-c-{GEN}": answer}
                self.docker.ps = [self.row("c")]
                self.host.sweep("w")
                self.assertEqual(self.broker.closed, [])

    def test_sweep_creates_nothing_outside_tui_and_drops_orphan_locks(self):
        self.docker.ps = [self.row("a"), self.row("x", state="exited")]
        before = self.files()
        self.host.sweep("w")
        self.assertEqual(self.broker.closed, [f"ensemble-a-{GEN}", f"ensemble-x-{GEN}"])
        self.assertEqual(self.files(), before)

    def test_a_failing_close_or_listing_skips_and_logs(self):
        self.docker.ps = [self.row("a"), self.row("b")]
        closes = iter([SessionError("broker: boom"), True])

        def close(container):
            self.broker.closed.append(container)
            found = next(closes)
            if isinstance(found, BaseException):
                raise found
            return found

        self.broker.close = close
        with self.assertLogs("session", "WARNING") as logs:
            self.host.sweep("w")
        self.assertEqual(self.broker.closed, [f"ensemble-a-{GEN}", f"ensemble-b-{GEN}"])
        self.assertIn("docker sweep a: broker: boom", logs.output[0])
        self.assertEqual(os.listdir(os.path.join(self.home, ".ensemble", "tui")), ["w"])
        self.host.run = mock.Mock(return_value=done(1, "", "Cannot connect"))
        with self.assertLogs("session", "WARNING"):
            self.host.sweep("w")


class ReclaimTest(Base):
    GONE = "ensemble-run-TASK-1-0123abcd"

    def setUp(self):
        super().setUp()
        self.kept = os.path.join(self.home, ".ensemble", "docker-state", ".kept")
        self.docker.volumes = [  # GONE: a protocol 4 run volume, reclaimed as any other
            f"{self.GONE}\t{self.account}/gone/TASK-1",
            f"ensemble-ws-here\t{self.cwd}",
            "ensemble-ws-relative\trelative",
            "ensemble-ws-bare",
        ]

    def removed(self):
        return [
            step[1] for step in self.order if isinstance(step, tuple) and step[0] == "workspace_rm"
        ]

    def marker(self, volume, text="", age=0):
        os.makedirs(self.kept, exist_ok=True)
        path = os.path.join(self.kept, volume)
        with open(path, "w") as f:
            f.write(text)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def test_a_volume_whose_workspace_is_gone_is_removed_and_others_are_left(self):
        self.host.reclaim()
        self.assertEqual(self.removed(), [self.GONE])
        self.assertIn(
            [
                "volume",
                "ls",
                "--filter",
                "label=ensemble.kind",
                "--format",
                '{{.Name}}\t{{.Label "ensemble.workspace"}}',
            ],
            self.order,
        )

    def test_a_sweep_never_reclaims(self):
        self.host.sweep("w")
        self.assertFalse([step for step in self.order if step[0] == "volume"])
        self.assertEqual(self.removed(), [])

    def test_each_reclaim_checks_all_eligible_volumes(self):
        self.docker.volumes.append(f"ensemble-ws-gone\t{self.account}/gone/w")
        self.host.reclaim()
        self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])

    def test_a_kept_volume_does_not_block_later_volumes(self):
        self.docker.volumes.append(f"ensemble-ws-gone\t{self.account}/gone/w")
        for code in ("workspace_dirty", "workspace_failed"):
            with self.subTest(code=code):
                self.order.clear()
                self.broker.failing = {}

                def remove(volume, code=code):
                    self.order.append(("workspace_rm", volume))
                    if volume == self.GONE:
                        raise BrokerError(12, code, "kept", "not_needed", None)
                    return True

                self.broker.workspace_rm = remove
                with self.assertLogs("session", "WARNING"):
                    self.host.reclaim()
                self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])
                os.unlink(os.path.join(self.kept, self.GONE))

    def test_a_failed_batch_warns_once_per_volume(self):
        self.docker.volumes.append(f"ensemble-ws-gone\t{self.account}/gone/w")
        self.broker.failing = {
            "workspace_rm": BrokerError(14, "workspace_failed", "check failed", "not_needed", None)
        }
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim()
        self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])
        self.assertEqual(len(logs.output), 2)
        self.order.clear()
        with self.assertNoLogs("session", "WARNING"):
            self.host.reclaim()
        self.assertEqual(self.removed(), [])

    def test_background_reclaim_detaches_without_listing_or_waiting(self):
        self.spawn_mock.side_effect = None
        with mock.patch("threading.Thread") as thread:
            self.host._spawn_reclaim()
        (argv,), kwargs = self.spawn_mock.call_args
        self.assertEqual(argv[1:], ["-P", "-s", "-m", DockerHost.__module__, DOCKER])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["stdin"], DEVNULL)
        self.assertEqual(kwargs["stdout"], DEVNULL)
        self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "PYTHONPATH"})
        log = os.path.join(os.path.dirname(self.kept), "reclaim.log")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        self.spawn_mock.return_value.wait.assert_not_called()
        thread.assert_called_once()
        self.assertEqual(self.order, [])

    def test_a_background_spawn_failure_warns_and_returns(self):
        self.spawn_mock.side_effect = OSError("cannot spawn")
        with self.assertLogs("session", "WARNING") as logs:
            self.host._spawn_reclaim()
        self.assertIn("docker reclaim: cannot spawn", logs.output[0])

    def test_the_background_entrypoint_skips_without_a_daemon(self):
        module = sys.modules[DockerHost.__module__]
        os.unlink(self.ep.removeprefix("unix://"))
        with mock.patch("sys.argv", [module.__file__, DOCKER]):
            runpy.run_path(module.__file__, run_name="__main__")
        self.assertEqual(self.order, [])
        self.spawn_mock.assert_not_called()

    def test_a_piped_close_schedules_reclaim_after_closing_the_container(self):
        def spawn():
            self.order.append("reclaim")

        with mock.patch.object(self.host, "_spawn_reclaim", side_effect=spawn):
            self.host._close_piped("w", CONTAINER)
        self.assertEqual(self.order, [("close", CONTAINER), "reclaim"])

    def test_unsaved_work_keeps_the_volume_and_warns_once_for_good(self):
        message = f"Workspace {self.GONE} has uncommitted or unpushed work in: publish."
        self.broker.failing = {
            "workspace_rm": BrokerError(12, "workspace_dirty", message, "not_needed", None)
        }
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim()
        self.assertEqual(
            logs.output, [f"WARNING:session:docker workspace {self.GONE} kept: {message}"]
        )
        marker = os.path.join(self.kept, self.GONE)
        self.assertTrue(os.path.isfile(marker))
        old = time.time() - 10 * KEPT_RETRY
        os.utime(marker, (old, old))
        self.order.clear()
        with self.assertNoLogs("session", "WARNING"):
            self.host.reclaim()
        self.assertEqual(self.removed(), [])

    def test_a_failing_check_is_retried_only_after_a_day(self):
        for error in (
            BrokerError(14, "workspace_failed", "check failed", "not_needed", None),
            SessionError("broker: workspace-rm timed out"),
        ):
            with self.subTest(error=error):
                self.broker.failing = {"workspace_rm": error}
                self.order.clear()
                with self.assertLogs("session", "WARNING") as logs:
                    self.host.reclaim()
                self.assertEqual(self.removed(), [self.GONE])
                self.assertIn(f"docker workspace {self.GONE} kept: ", logs.output[0])
                marker = os.path.join(self.kept, self.GONE)
                with open(marker) as f:
                    self.assertEqual(f.read(), "failed")
                self.order.clear()
                with self.assertNoLogs("session", "WARNING"):
                    self.host.reclaim()
                self.assertEqual(self.removed(), [])
                self.marker(self.GONE, "failed", age=KEPT_RETRY + 1)
                self.broker.failing = {}
                self.host.reclaim()
                self.assertEqual(self.removed(), [self.GONE])
                self.assertFalse(os.path.exists(marker))

    def test_a_marker_whose_volume_is_gone_is_deleted(self):
        for name in ("ensemble-run-old-0123abcd", "ensemble-ws-here"):
            self.marker(name)
        self.docker.volumes = self.docker.volumes[1:]
        self.host.reclaim()
        self.assertEqual(sorted(os.listdir(self.kept)), [".lock", "ensemble-ws-here"])
        self.assertEqual(self.removed(), [])

    def test_a_held_lock_or_no_daemon_socket_skips_reclaim(self):
        os.makedirs(self.kept)
        busy = os.open(os.path.join(self.kept, ".lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, busy)
        fcntl.flock(busy, fcntl.LOCK_EX)
        self.host.reclaim()
        self.assertEqual(self.removed(), [])
        fcntl.flock(busy, fcntl.LOCK_UN)
        os.remove(self.ep.removeprefix("unix://"))
        self.order.clear()
        self.host.reclaim()
        self.assertEqual(self.order, [])

    def test_without_the_daemon_or_the_cli_a_kept_marker_stays(self):
        marker = self.marker(self.GONE)
        os.remove(self.ep.removeprefix("unix://"))
        self.host.reclaim()
        open(self.ep.removeprefix("unix://"), "w").close()
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "No such file", DOCKER))
        self.host.reclaim()
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(self.removed(), [])

    def test_a_failed_listing_only_warns(self):
        self.host.run = mock.Mock(return_value=done(1, "", "Cannot connect"))
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim()
        self.assertIn("docker reclaim: docker volume: Cannot connect", logs.output[0])

    def test_no_orphan_creates_no_file(self):
        self.docker.volumes = self.docker.volumes[1:]
        self.host.reclaim()
        self.assertFalse(os.path.exists(self.kept))


class ListTest(Base):
    def test_admission_inventory_distinguishes_missing_docker_from_empty(self):
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "no docker"))
        with self.assertRaisesRegex(SessionError, "inventory unavailable"):
            self.host._capacity_rows()
        os.unlink(self.ep.removeprefix("unix://"))
        self.host.run = self.docker
        with self.assertRaisesRegex(SessionError, "inventory unavailable"):
            self.host._capacity_rows()

    def test_admission_inventory_reads_successful_empty_and_populated_results(self):
        self.assertEqual(self.host._capacity_rows(), [])
        self.listed("w")
        self.assertEqual([row.name for row in self.host._capacity_rows()], ["w"])

    def test_list_and_rows_from_labelled_containers(self):
        self.listed("w", "v")
        self.docker.ps += [
            f"ensemble-x-{GEN}\tnot a name\t{GEN}\trunning",
            f"someone-else\tz\t{GEN}\trunning",
            f"ensemble-y-{GEN.upper()}\ty\t{GEN.upper()}\trunning",
            f"ensemble-u-{GEN}\tu\t{GEN}",
            "bad line",
        ]
        self.record()
        self.assertEqual(self.host.list(), ["v", "w"])
        listing = "w\t0\tforged\tforged\tdone\t\t\tforged\t\n"
        self.docker.replies = {("tmux", "-u", "list-sessions"): (0, listing, "")}
        before = len(self.docker.order)
        rows = {row.name: row for row in self.host.rows()}
        ran = [call[0] for call in self.docker.order[before:] if isinstance(call, list)]
        self.assertEqual(ran.count("ps"), 1)
        self.assertEqual(
            (rows["w"].sid, rows["w"].client, rows["w"].display, rows["w"].state),
            ("s1", "agent", "lead/below", StatusState.DONE),
        )
        self.assertEqual((rows["v"].sid, rows["v"].state), (None, StatusState.DEAD))

    def test_rows_ignore_other_container_sessions_and_bad_labels(self):
        self.terminal()
        for listing in ("x\t0\t\t\t\t\t\t\t\n", "w\t0\t\t\t\t\t\t\t\nw\t0\t\t\t\t\t\t\t\n", ""):
            with self.subTest(listing=listing):
                self.docker.replies = {("tmux", "-u", "list-sessions"): (0, listing, "")}
                (row,) = self.host.rows()
                self.assertEqual((row.name, row.state), ("w", StatusState.DEAD))
        self.docker.replies = {(): (1, "", "Error response from daemon: No such container: x")}
        self.assertEqual(self.host.rows()[0].state, StatusState.DEAD)

    def test_rows_of_an_unanswered_listing_are_unknown(self):
        self.listed("w")
        self.record()
        for reply in (
            TimeoutExpired("docker", 10),
            Overflow("output over 8388608 bytes"),
            (1, "", "server exited unexpectedly"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {("tmux", "-u", "list-sessions"): reply}
                (row,) = self.host.rows()
                self.assertEqual(
                    (row.state, row.last_lines, row.sid, row.client, row.display),
                    (StatusState.UNKNOWN, None, "s1", "agent", "lead/below"),
                )

    def test_a_dead_rows_last_lines_have_no_control_characters(self):
        self.listed("w")
        self.record()
        self.docker.replies = {
            ("tmux", "-u", "list-sessions"): (0, "w\t1\t\t\t\t\t\t\t\n", ""),
            ("tmux", "capture-pane"): (0, "bye\x1b[31m\nend\x07\n", ""),
        }
        (row,) = self.host.rows()
        self.assertEqual((row.state, row.last_lines), (StatusState.DEAD, "bye[31m\nend"))

    def test_docker_ps_failure_raises(self):
        for run in (
            mock.Mock(return_value=done(1, "", "Cannot connect")),
            mock.Mock(side_effect=PermissionError(13, "denied")),
        ):
            with self.subTest(run=run):
                self.host.run = run
                with self.assertRaisesRegex(SessionError, "docker ps"):
                    self.host.list()

    def test_a_docker_ps_that_does_not_answer_leaves_attach_unanswered(self):
        self.listed("w")
        for run in (
            mock.Mock(side_effect=TimeoutExpired("docker", RUN_TIMEOUT)),
            mock.Mock(return_value=done(1, "", "Cannot connect")),
            mock.Mock(side_effect=PermissionError(13, "denied")),
        ):
            with self.subTest(run=run):
                self.host.run = run
                with self.assertRaisesRegex(Unanswered, "^docker ps: "):
                    self.host.attach("w")
                with self.assertRaisesRegex(Unanswered, "^docker ps: "):
                    self.host.list()

    def test_a_client_probe_that_does_not_answer_leaves_attach_unanswered(self):
        self.listed("w")
        for reply in (
            TimeoutExpired("docker", 10),
            Overflow("output over 8388608 bytes"),
            (1, "", "server exited unexpectedly"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {("tmux", "display-message"): reply}
                with self.assertRaisesRegex(Unanswered, "^docker session w: "):
                    self.host.attach("w")

    def test_a_daemon_that_does_not_answer_lists_no_rows_but_fails_list(self):
        self.listed("w")
        self.host.run = mock.Mock(side_effect=TimeoutExpired("docker", RUN_TIMEOUT))
        with self.assertLogs("session", "WARNING") as logs:
            self.assertEqual(self.host.rows(), [])
        self.assertIn("docker ps", logs.output[0])
        with self.assertRaisesRegex(SessionError, "docker ps"):
            self.host.list()

    def test_no_daemon_socket_or_cli_lists_nothing(self):
        self.listed("w")
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "no docker"))
        self.assertEqual(self.host.list(), [])
        os.unlink(self.ep.removeprefix("unix://"))
        self.host.run = self.docker
        self.assertEqual((self.host.list(), self.host.rows(), self.order), ([], [], []))

    def test_attach_unknown_or_without_client(self):
        with self.assertRaisesRegex(SessionError, "no docker session w") as caught:
            self.host.attach("w")
        self.assertNotIsInstance(caught.exception, Unanswered)
        self.listed("w")
        for reply in (
            (1, "", "can't find session: =w"),
            (1, "", f"Error response from daemon: No such container: {CONTAINER}"),
            (1, "", f"Error response from daemon: container {CONTAINER} is not running"),
        ):
            with self.subTest(reply=reply):
                self.docker.replies = {("tmux", "display-message"): reply}
                with self.assertRaisesRegex(SessionError, "runs no client") as caught:
                    self.host.attach("w")
                self.assertNotIsInstance(caught.exception, Unanswered)
        self.docker.replies = {("tmux", "display-message"): (0, PROCESS + "\n", "")}
        self.assertEqual(self.host.attach("w").identity, PROCESS)

    def test_hook_callback(self):
        callback = self.host.hook_callback("w", {"generation": GEN})
        self.assertEqual(callback.module, "ensemble.session.hosts.docker.hook")
        self.assertEqual(
            dict(callback.state),
            {
                "docker": DOCKER,
                "endpoint": self.ep,
                "container": CONTAINER,
                "target": "w",
                "generation": GEN,
            },
        )


class AttachTargetTest(Base):
    def test_attach_target_runs_docker_exec_with_tmux_variables_cleared(self):
        self.terminal()
        argv = [DOCKER, "--host", self.ep, "exec", "-it", CONTAINER, "tmux", "attach", "-t", "=w"]
        cleared = tuple(arg for key in CLEARED for arg in ("-u", key))
        self.assertTrue(DockerHost.attachable)
        self.assertEqual(
            self.host.attach_target("w"),
            CommandView(
                printed=shlex.join(argv[:-1]) + " '=w'", argv=("/usr/bin/env", *cleared, *argv)
            ),
        )
        with self.assertRaisesRegex(SessionError, "^no docker session x$"):
            self.host.attach_target("x")


def _read(path):
    with open(path) as stream:
        return stream.read()


class GuardExec:
    """`docker exec -i` of the guard over real pipes: its start frame at once, its exit frame once
    told to close (or by exit()).
    """

    def __init__(self, argv):
        self.args, self.pid, self.returncode = argv, 7777, None
        read_in, write_in = os.pipe()
        read_out, self._out = os.pipe()
        self.stdin = os.fdopen(write_in, "wb", buffering=0)
        self.stdout = os.fdopen(read_out, "rb", buffering=0)
        self._in = os.fdopen(read_in, "rb", buffering=0)
        self.exited = threading.Event()
        self.write(b"s", {"pid": 1})
        threading.Thread(target=self._serve, daemon=True).start()

    def write(self, kind, value):
        with contextlib.suppress(OSError):
            os.write(self._out, guard.json_frame(kind, value))

    def _serve(self):
        decoder = guard.Decoder()
        while data := self._in.read(65536):
            for kind, _ in decoder.feed(data):
                if kind == b"c":
                    self.exit(0)
        self._in.close()

    def exit(self, code):
        if not self.exited.is_set():
            self.exited.set()
            self.write(b"x", {"returncode": code})
            os.close(self._out)

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0 if self.exited.is_set() else None


class GuardedBase(StartBase):
    OWNER = "/Users/x/.ensemble/harness/" + "a" * 32

    def setUp(self):
        super().setUp()
        self.execs = []
        self.popen_mock.side_effect = self.guard_popen

    def guard_popen(self, argv, **kw):
        if "--hook-socket" in argv:
            proc = GuardExec(argv)
            self.execs.append((argv, kw, proc))
            self.addCleanup(proc.exit, 0)
            return proc
        self.order.append(("popen", argv[:3]))
        return mock.Mock(pid=4321)

    def owned(self, **metadata):
        """An owned start, as its TuiStore makes it holding the tui lock."""
        handle = self.host.worker(
            "w",
            self.launch,
            terminal=True,
            display="lead/below",
            events=os.path.join(self.home, "events"),
            metadata={
                k: v for k, v in {**self.METADATA, "owner": self.OWNER, **metadata}.items() if v
            },
        )
        self.addCleanup(self.close_quietly, handle)
        return handle

    def close_quietly(self, handle):
        with contextlib.suppress(SessionError):
            handle.close()

    def guarded_record(self, gen=GEN):
        self.record(generation=gen, mode="guard")

    def owner_held(self):
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "guard.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(_close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def probe_says(self, word, rc=0):
        self.docker.replies[(PYTHON, "-I", worker_module.GUARD, "--probe")] = (rc, word + "\n", "")


class GuardedStartTest(GuardedBase):
    def test_owned_terminal_execs_the_guard_without_tmux_relay_or_supervisor(self):
        handle = self.owned()
        self.assertIsInstance(handle, worker_module.DockerWorker)
        self.assertTrue(handle.remote)
        [(argv, kw, _)] = self.execs
        run = f"/run/ensemble/guard/{GEN}"
        self.assertEqual(
            argv,
            [
                *(DOCKER, "--host", self.ep, "exec", "-i", "--workdir", self.cwd),
                *("--env", "AGENT_HOME", "--env", "K", "--env", "ENSEMBLE_GUARD_HOOK_SOCKET"),
                *(CONTAINER, PYTHON, "-I", worker_module.GUARD),
                *("--terminal", "--display-socket", f"{run}/display"),
                *("--hook-socket", f"{run}/hook", "--lease", f"{run}/lease", "--"),
                *("with-secrets", "--client", "agent", "agent", "--flag"),
            ],
        )
        self.assertEqual(kw["env"]["ENSEMBLE_GUARD_HOOK_SOCKET"], f"{run}/hook")
        self.assertTrue(kw["close_fds"] and kw["start_new_session"])
        self.assertNotIn("pass_fds", kw)
        execs = self.docker.execs()
        self.assertFalse(any("tmux" in call or RELAY in call for call in execs), execs)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "popen" for c in self.order))
        self.assertEqual(self.host_record()["mode"], "guard")
        self.assertFalse(_free(os.path.join(self.state, "guard.lock")))
        self.assertEqual(handle.session_identity(), ("s1", GEN))
        handle.close()
        self.assertIn(CONTAINER, self.broker.closed)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        self.assertEqual(handle.state(), Liveness.ABSENT)

    def test_unowned_terminal_keeps_the_legacy_path(self):
        self.popen_mock.side_effect = self.popen
        self.start()
        self.assertIn([RELAY, "serve", GEN], self.docker.execs())
        self.assertIn("supervisor", self.order)
        self.assertEqual(self.execs, [])

    def test_the_terminal_path_relies_on_the_tui_lock_its_store_holds(self):
        lock = tui_lock("w")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with inode_lock(lock):
            self.owned()
            with self.assertRaisesRegex(SessionError, "^docker session w: busy$"):
                self.host.worker("w", self.launch, terminal=False, metadata=self.METADATA)

    def test_a_start_failing_after_its_handle_closes_the_owner_lock_once(self):
        class Broken(GuardExec):
            def write(self, kind, value):
                super().write(b"z" if kind == b"s" else kind, value)

        def popen(argv, **_):
            if "--hook-socket" in argv:
                proc = Broken(argv)
                self.addCleanup(proc.exit, 0)
                return proc
            return mock.Mock(pid=4321)

        self.popen_mock.side_effect = popen
        spare = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(_close, spare)
        with self.assertRaisesRegex(SessionError, "^docker session w: "):
            self.owned()
        self.assertIn(CONTAINER, self.broker.closed)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        os.fstat(spare)

    def test_a_held_owner_lock_is_busy_before_any_container(self):
        self.owner_held()
        with self.assertRaises(SessionBusy):
            self.owned()
        self.assertNotIn(("start", os.path.join(self.state, f"{GEN}.json"), ()), self.order)
        self.assertEqual(self.execs, [])

    def test_attach_target_of_a_guarded_row_attaches_the_guard_display(self):
        self.listed("w")
        self.guarded_record()
        target = self.host.attach_target("w")
        argv = [
            *(DOCKER, "--host", self.ep, "exec", "-it", CONTAINER, PYTHON, "-I"),
            *(worker_module.GUARD, "--attach", f"/run/ensemble/guard/{GEN}/display"),
        ]
        self.assertEqual(target.argv[-len(argv) :], tuple(argv))
        self.assertEqual(target.argv[0], "/usr/bin/env")
        self.assertEqual(target.printed, shlex.join(argv))


class WorkerLivenessTest(GuardedBase):
    def test_a_held_owner_lock_is_live_with_no_docker_call(self):
        self.owner_held()
        self.order.clear()
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.LIVE)
        self.assertEqual(self.order, [])

    def test_row_and_probe_answers(self):
        self.assertEqual(self.host.worker_liveness("w", "../x"), Liveness.UNKNOWN)
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.ABSENT)
        self.listed("w", state="exited")
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.ABSENT)
        self.listed("w")
        self.record()
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)
        self.guarded_record()
        for word, rc, want in (
            ("live", 0, Liveness.LIVE),
            ("exited", 0, Liveness.ABSENT),
            ("absent", 0, Liveness.ABSENT),
            ("dead", 0, Liveness.UNKNOWN),
            ("live", 1, Liveness.UNKNOWN),
        ):
            with self.subTest(word=word, rc=rc):
                self.probe_says(word, rc)
                self.assertEqual(self.host.worker_liveness("w", GEN), want)
        self.docker.replies[(PYTHON, "-I", worker_module.GUARD, "--probe")] = TimeoutExpired(
            "docker", 10
        )
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)

    def test_worker_refuses_a_live_or_unknown_earlier_generation_and_reclaims_an_ended_one(self):
        old = "e" * 32
        self.listed("w", gen=old)
        self.guarded_record(old)
        for word in ("live", "dead"):
            with self.subTest(word=word):
                self.probe_says(word)
                with self.assertRaises(SessionBusy):
                    self.owned()
                self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        self.probe_says("exited")
        self.docker.ps = []
        self.listed("w", gen=old)

        def close(container):
            self.broker.closed.append(container)
            self.docker.ps = []
            return True

        self.broker.close = close
        self.owned()
        self.assertEqual(self.broker.closed[0], f"ensemble-w-{old}")
        self.assertEqual(len(self.execs), 1)

    def test_legacy_and_piped_rows_are_left_to_the_container_refusal(self):
        for mode in (None, "pipe"):
            with self.subTest(mode=mode):
                self.listed("w")
                self.record(**({"mode": mode} if mode else {}))
                with self.assertRaisesRegex(SessionError, "docker session w: a "):
                    self.owned()
                self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
                self.assertEqual(self.execs, [])


class GuardedRowTest(GuardedBase):
    def test_guarded_rows_are_listed_with_lock_and_activity_and_no_exec(self):
        self.listed("w")
        self.guarded_record()
        self.order.clear()
        [row] = self.host.rows()
        self.assertEqual(row.state, StatusState.UNKNOWN)
        self.owner_held()
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, f"{GEN}.activity"), "w") as f:
            f.write("blocked")
        [row] = self.host.rows()
        self.assertEqual((row.name, row.state, row.last_lines), ("w", StatusState.BLOCKED, None))
        self.assertEqual(self.host.list(), ["w"])
        self.assertEqual(self.docker.execs(), [])

    def test_attach_is_the_held_handle_here_and_a_view_elsewhere(self):
        handle = self.owned()
        self.listed("w")
        self.assertIs(self.host.attach("w"), handle)
        other = DockerHost("docker", {"docker": DOCKER}, run=self.docker, broker=self.broker)
        view = other.attach("w")
        self.assertIsInstance(view, worker_module.GuardedView)
        self.assertEqual(view.state(), Liveness.LIVE)
        with self.assertRaisesRegex(SessionError, "owned by its harness"):
            view.send(Paste("x"))
        with self.assertRaisesRegex(SessionError, "owned by its harness"):
            view.close()
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "popen" for c in self.order))

    def test_sweep_reclaims_a_guarded_container_only_with_a_free_owner_lock(self):
        self.listed("w")
        self.guarded_record()
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        fd = self.owner_held()
        self.host.sweep("x")
        self.assertEqual(self.broker.closed, [])
        _close(fd)
        self.host.sweep("x")
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertEqual(self.docker.execs(), [])
