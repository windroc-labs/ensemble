"""hosts/docker/host.py: DockerHost's config, start order and rollback, container commands, the Host
record, listing, sweep and reclaim, over a fake docker CLI, a fake broker and a fake guard exec.
"""

import contextlib
import fcntl
import io
import json
import os
import re
import runpy
import shlex
import signal
import stat
import sys
import threading
import time
import tomllib
from dataclasses import fields, replace
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.capacity import Admission
from ensemble.session.engine.config import read_config
from ensemble.session.engine.daemon import (
    CLEARED,
    RECORD_FILE,
    RUN_TIMEOUT,
    client_home,
    docker_call,
    load_record,
    save_record,
)
from ensemble.session.engine.types import (
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    Daemon,
    DaemonRecord,
    EnvironmentSpec,
    Launch,
    Liveness,
    Mode,
    Permission,
    SessionBusy,
    SessionError,
    StatusState,
    Target,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import Host, phase_reporter, tui_lock
from ensemble.session.hosts.docker import worker as worker_module
from ensemble.session.hosts.docker.broker import (
    BROKER,
    TIMEOUT,
    BrokerError,
    BrokerMissing,
    client_keys,
)
from ensemble.session.hosts.docker.host import (
    FILES_STAGE,
    KEPT_RETRY,
    PS_FORMAT,
    DockerHost,
)
from ensemble.session.hosts.docker.types import Maintained, Started
from ensemble.session.hosts.types import CommandView
from ensemble.utils import inode_lock, parse_time
from tests.session.case import REPO, Case
from tests.session.state_volumes import Volumes, reset, sockets
from tests.session.unit.hosts.docker.support import BETA, UPDRAFT, FakePopen, ssh_dir
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
CONTAINER = f"ensemble-w-{GEN}"
PYTHON = "/opt/ensemble/venv/bin/python"
IMAGE = "ensemble-worker:" + "a" * 64
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
DIGEST = "sha256:" + "c" * 64
LOCAL_DAEMON = "8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REMOTE = "ssh://u@pc"
REMOTE_DAEMON = "PC-ID"
HOME = ClientHome("agent_dir", "agent", "AGENT_HOME", "agent-token")
BAD_RECORDS = {
    "deep json": "[" * 2000 + "0" + "]" * 2000,
    "list kind": json.dumps({"generation": GEN, "kind": []}),
    "object kind": json.dumps({"generation": GEN, "kind": {}}),
    "invalid json": "{",
    "non-object": "[]",
    "missing generation": '{"kind":"tui"}',
    "bad generation": '{"kind":"tui","generation":[]}',
    "unknown kind": json.dumps({"generation": GEN, "kind": "other"}),
    "null kind": json.dumps({"generation": GEN, "kind": None}),
}


def done(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeDocker:
    """One daemon's docker CLI at `ep`: `ps` prints `ps`, `volume ls` prints `volumes` (by name: the
    asked state volume when in `states`, None: any); an exec answers from `replies`, {command prefix:
    (rc, out, err), an exception or a callable of the command}, the first matching prefix; else
    success. `down`: every call fails as Docker does when it cannot reach its daemon. `others`:
    another daemon's endpoint → its FakeDocker; a call to any other endpoint fails the test.
    """

    def __init__(self, ep, order):
        self.ep, self.order, self.ps, self.replies, self.inputs = ep, order, [], {}, []
        self.volumes, self.down, self.states = [], False, None
        self.readers, self.volume_rm, self.calls = [], (0, "", ""), []
        self.others = {}

    def __call__(self, argv, env, *, input=None, timeout):
        assert not set(CLEARED) & env.keys() and timeout <= RUN_TIMEOUT
        if argv[:2] == [DOCKER, "--host"] and argv[2] in self.others:
            return self.others[argv[2]](argv, env, input=input, timeout=timeout)
        assert argv[:3] == [DOCKER, "--host", self.ep], argv
        self.order.append(argv[3:])
        self.inputs.append(input)
        self.calls.append((argv[3:], input))
        if self.down:
            return done(
                1, "", f"Cannot connect to the Docker daemon at {self.ep}. Is it running?\n"
            )
        if argv[3:5] == ["ps", "--all"]:
            return done(0, "".join(f"{reader}\n" for reader in self.readers))
        if argv[3:5] == ["volume", "rm"]:
            return done(*self.volume_rm)
        if argv[3:6] == ["volume", "ls", "--filter"] and argv[6].startswith("name="):
            volume = argv[6].removeprefix("name=")
            return done(0, f"{volume}\n" if self.states is None or volume in self.states else "")
        if argv[3] in ("ps", "volume"):
            rows = self.ps if argv[3] == "ps" else self.volumes
            return done(0, "".join(f"{line}\n" for line in rows))
        cmd = argv[6:]
        for prefix, reply in self.replies.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if isinstance(reply, BaseException):
                    raise reply
                return reply(cmd) if callable(reply) else done(*reply)
        return done()

    def execs(self):
        return [call[3:] for call in self.order if isinstance(call, list) and call[0] == "exec"]


class FakeBroker:
    """`failing`: {command: the error it raises}; `found`: the target it lists, counted in
    `targets`; the daemon IDs build, close and workspace-rm were sent, in `built_daemons`,
    `close_daemons` and `rm_daemons`.
    """

    def __init__(self, order, started, found):
        self.order, self.started, self.error, self.closed = order, started, None, []
        self.failing, self.refreshed = {}, threading.Event()
        self.found, self.targets = found, 0
        self.built_daemons, self.close_daemons, self.rm_daemons = [], [], []

    def version(self):
        self.order.append("version")
        if "version" in self.failing:
            raise self.failing["version"]

    def target(self):
        self.targets += 1
        if "target" in self.failing:
            raise self.failing["target"]
        return self.found

    def build(self, package, *, daemon, plugins=(), **environment):
        self.order.append(("build", package))
        self.built_daemons.append(daemon)
        self.plugins, self.built = plugins, environment
        if "build" in self.failing:
            raise self.failing["build"]
        return IMAGE, DIGEST

    def start(self, manifest, *, clients):
        self.order.append(("start", manifest, clients))
        if self.error:
            raise self.error
        return self.started

    def maintain(self, container, manifest):
        self.order.append(("maintain", container, manifest))
        self.refreshed.set()
        if "maintain" in self.failing:
            raise self.failing["maintain"]
        return Maintained([], None, self.started.deadline)

    def close(self, container, *, daemon):
        self.order.append(("close", container))
        self.closed.append(container)
        self.close_daemons.append(daemon)
        if "close" in self.failing:
            raise self.failing["close"]
        return True

    def workspace_rm(self, volume, *, force=False, daemon):
        self.rm_daemons.append(daemon)
        self.order.append(
            ("workspace_rm", volume, "--force") if force else ("workspace_rm", volume)
        )
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
        self.cwd = os.path.join(self.account, "src", "repo")
        os.makedirs(self.cwd)
        self.order = []
        self.docker = FakeDocker(self.ep, self.order)
        self.local = Daemon("local", self.ep, remote=False, path=SYSTEM_PATH, id=LOCAL_DAEMON)
        started = Started(
            CONTAINER,
            self.ep,
            LOCAL_DAEMON,
            "sha256:" + "b" * 64,
            {},
            env={},
            deadline=None,
        )
        self.broker = FakeBroker(self.order, started, Target("local", (self.local,)))
        self.host = DockerHost(
            "docker",
            {"docker": DOCKER},
            run=self.docker,
            broker=self.broker,
        )
        self.state = os.path.join(self.home, ".ensemble", "docker-state", "w")
        self.tui_session("w")
        popen = mock.patch("subprocess.Popen")
        self.spawn_mock = popen.start()
        self.addCleanup(popen.stop)

    def targets(self, default, *daemons):
        """The broker's target from now on, as the Host asks it afresh."""
        self.broker.found = Target(default, daemons)
        self.host._found = None

    def remote_daemon(self):
        """The remote daemon REMOTE, its broker ssh wrapper first on its PATH."""
        path = f"{ssh_dir(self.home)}{os.pathsep}{SYSTEM_PATH}"
        return Daemon("remote", REMOTE, remote=True, path=path, id=REMOTE_DAEMON)

    def two_daemons(self, default="remote"):
        """REMOTE, answered by `self.remote`, beside the local Docker Desktop."""
        self.remote = FakeDocker(REMOTE, self.order)
        self.docker.others[REMOTE] = self.remote
        self.pc = self.remote_daemon()
        self.targets(default, self.pc, self.local)

    def recorded(self, name, found):
        """Session `name`'s daemon record: `found`."""
        save_record(name, DaemonRecord(found, DOCKER))

    def tui_session(self, name, gen=GEN):
        """The tui mode's session.json for `name`, as start writes it before the Host runs."""
        path = os.path.join(self.home, ".ensemble", "tui", name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "session.json"), "w") as f:
            json.dump({"generation": gen}, f)

    def record(self, **changes):
        found = {
            "generation": GEN,
            "sid": "s1",
            "client": "agent",
            "display": "lead/below",
            "record": None,
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

    def guarded_record(self, gen=GEN, kind="tui"):
        self.record(generation=gen, kind=kind)

    def damaged_record(self, name, text):
        state = os.path.join(self.home, ".ensemble", "docker-state", name)
        os.makedirs(state, exist_ok=True)
        path = os.path.join(state, "host.json")
        with open(path, "w") as f:
            f.write(text)
        return path


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
    def test_tui_settings_show_the_status_line_badged_and_keep_the_hidden_size(self):
        host = DockerHost("docker", {"docker": DOCKER, "hidden_size": "100x30"})
        self.assertTrue(self.host.tui_settings().status_line)
        self.assertTrue(host.tui_settings().status_line)
        self.assertEqual(host.tui_settings().badge, "🐳")
        self.assertEqual(host.tui_settings().hidden_size, (100, 30))

    def test_the_packaged_table_parses_and_tmux_stays_the_default(self):
        with open(os.path.join(REPO, "src/ensemble/session/config.toml"), "rb") as f:
            table = tomllib.load(f)["hosts"]
        self.assertEqual(
            table["docker"],
            {
                "implementation": "ensemble.session.hosts.docker.host:DockerHost",
                "docker": "/usr/local/bin/docker",
                "plugins": [UPDRAFT],
                "isolated": True,
            },
        )
        self.assertEqual(Host.parse_hosts(table).default, "tmux")
        settings = {k: v for k, v in table["docker"].items() if k != "implementation"}
        host = DockerHost("docker", settings)
        self.assertEqual(
            (host.docker, host.plugins, host._isolated),
            ("/usr/local/bin/docker", (UPDRAFT,), True),
        )
        bare = DockerHost("docker", {"docker": DOCKER})
        self.assertEqual(
            (bare.plugins, bare._isolated),
            ((), False),
        )
        self.assertTrue(DockerHost("docker", {"docker": DOCKER, "isolated": True})._isolated)
        named = DockerHost(
            "docker",
            {
                "docker": DOCKER,
                "plugins": [UPDRAFT, "b.c@owner/market_1-x#" + "a" * 40],
            },
        )
        self.assertEqual(named.plugins, (UPDRAFT, "b.c@owner/market_1-x#" + "a" * 40))

    def test_legacy_github_config_is_an_unknown_key(self):
        local = os.path.join(self.home, "config.local.toml")
        with open(local, "w") as f:
            f.write('[session.hosts.docker.github]\napp = "ensemble-broker-bot"\n')
        settings = read_config(local=local)["hosts"]["docker"]
        del settings["implementation"]
        with self.assertRaisesRegex(ConfigError, "unknown key 'github' in"):
            DockerHost("docker", settings)

    def test_bad_settings_name_their_key(self):
        for settings, key in (
            ({"docker": DOCKER, "other": 1}, r"^unknown key 'other' in \[hosts.docker\]$"),
            ({"docker": "docker"}, "docker"),
            (
                {"docker": DOCKER, "mount_roots": ["~/src"]},
                r"^unknown key 'mount_roots' in \[hosts.docker\]; delete it: workspaces are volumes$",
            ),
            *(
                (
                    {"docker": DOCKER, "plugins": plugins},
                    r"^\[hosts.docker\] plugins: want unique name@owner/repo#40-hex-commit sources, optionally ::owner/repo#40-hex-commit$",
                )
                for plugins in (
                    UPDRAFT,
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
            {"AGENT_HOME": "/home/agent/.ensemble-state/agent"},
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(self.host._inherit_env)

    def test_home_store_is_the_sessions_state_volume(self):
        self.assertEqual(
            self.host.home_store("w", HOME).persisted(),
            {"kind": "volume", "session": "w", "client": "agent"},
        )
        with self.assertRaisesRegex(ConfigError, "not supported on the Docker Host yet"):
            self.host.home_store("w", None)

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


def _read(path):
    with open(path) as stream:
        return stream.read()


class GuardAttach:
    """`docker attach` to the main-process guard over real pipes: its start frame once the launch
    handoff came (`handoff`, decoded), its exit frame once told to close (or by exit()).
    """

    def __init__(self, argv):
        self.args, self.pid, self.returncode, self.handoff = argv, 7777, None, None
        read_in, write_in = os.pipe()
        read_out, self._out = os.pipe()
        self.stdin = os.fdopen(write_in, "wb", buffering=0)
        self.stdout = os.fdopen(read_out, "rb", buffering=0)
        self._in = os.fdopen(read_in, "rb", buffering=0)
        self.exited = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def write(self, kind, value):
        with contextlib.suppress(OSError):
            os.write(self._out, guard.json_frame(kind, value))

    def _serve(self):
        decoder = guard.Decoder()
        while data := self._in.read(65536):
            for kind, payload in decoder.feed(data):
                if self.handoff is None and kind == b"l":
                    self.handoff = guard.launch(payload)
                    self.write(b"s", {"pid": 1})
                elif kind == b"c":
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


class StartBase(Base):
    """An owned start's metadata and launch, its guard reached by a GuardAttach."""

    METADATA = {
        "sid": "s1",
        "client": "agent",
        "generation": GEN,
        "home_field": "agent_dir",
        "home_dir": "agent",
        "resume": "0",
        "credential": "agent-token",
    }
    OWNER = "/Users/x/.ensemble/harness/" + "a" * 32

    def setUp(self):
        super().setUp()
        self.home_dir = client_home("agent")
        self.volume_dir = os.path.join(self.state, "agent")  # the fake state volume's agent dir
        self.execs, self.handle = [], None
        popen = mock.patch("subprocess.Popen", side_effect=self.guard_popen)
        self.popen_mock = popen.start()
        self.addCleanup(popen.stop)
        self.state_volumes = Volumes()
        self.enterContext(sockets())
        self.addCleanup(reset)
        self.launch = Launch(
            ("agent", "--flag"),
            cwd=self.cwd,
            env={"PATH": "/host/bin", "AGENT_HOME": self.home_dir, "K": "v"},
        )

    def guard_popen(self, argv, **kw):
        if self.state_volumes.handles(argv) or argv[3:4] == ["image"]:
            return self.state_volumes(argv, **kw)
        if argv[3:4] == ["attach"]:
            self.order.append("guard")
            proc = GuardAttach(argv)
            self.execs.append((argv, kw, proc))
            self.addCleanup(proc.exit, 0)
            return proc
        self.order.append(("popen", argv[:3]))
        return mock.Mock(pid=4321)

    def start(self, **metadata):
        """An owned start, as its TuiStore makes it holding the tui lock; the previous one closed,
        so a test can start w again.
        """
        if self.handle is not None:
            self.close_quietly(self.handle)
        self.handle = self.host.worker(
            "w",
            self.launch,
            terminal=True,
            display="lead/below",
            events=os.path.join(self.home, "events"),
            metadata={
                k: v
                for k, v in {**self.METADATA, "owner": self.OWNER, **metadata}.items()
                if v is not None
            },
        )
        self.addCleanup(self.close_quietly, self.handle)
        return self.handle

    def close_quietly(self, handle):
        with contextlib.suppress(SessionError):
            handle.close()

    def guard_env(self):
        """The env the last launch handoff gave its client."""
        return self.execs[-1][2].handoff["env"]

    def owner_held(self):
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "guard.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(_close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def probe_says(self, word, rc=0):
        self.docker.replies[(PYTHON, "-I", worker_module.GUARD, "--probe")] = (rc, word + "\n", "")

    def history(self):
        """A native transcript in the home, which the launch names; its file in the fake volume."""
        stored = os.path.join(self.volume_dir, "projects", "p", "s1.jsonl")
        os.makedirs(os.path.dirname(stored))
        with open(stored, "w") as f:
            f.write("{}\n")
        transcript = os.path.join(self.home_dir, "projects", "p", "s1.jsonl")
        self.launch = replace(self.launch, transcript=transcript)
        return stored

    def refused_before_start(self, error, pattern):
        """start(resume="1") fails with `pattern`, after its build, before its start, manifest or
        supervisor.
        """
        self.order.clear()
        with self.assertRaisesRegex(error, pattern):
            self.start(resume="1")
        self.assertIn(("build", REPO), self.order)
        self.assertNotIn("start", [o[0] for o in self.order if isinstance(o, tuple)])
        self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.json")))
        self.assertEqual(self.spawned(), [])

    def spawned(self):
        """Processes started other than the state reader service and its docker calls."""
        return [
            call.args[0]
            for call in self.popen_mock.call_args_list
            if not (Volumes.handles(call.args[0]) or call.args[0][3:4] == ["image"])
        ]

    def staged(self):
        """The files the last start staged with FILES_STAGE, by container path."""
        found = [json.loads(stdin) for call, stdin in self.docker.calls if FILES_STAGE in call]
        return found[-1] if found else {}


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
                    self.host._stage_files(CONTAINER, {"runtime_files": files}, self.local)
                self.assertNotIn("private", str(raised.exception))

    def test_runtime_projection_and_initial_expiry_are_staged_before_client(self):
        expiry = {"at": "2026-10-18T12:00:00Z", "action": "close", "reason": "r"}
        self.broker.started = replace(self.broker.started, deadline=expiry)
        files = json.dumps({"/run/ensemble/agent-store/" + "a" * 64 + ".json": "{}"})
        self.start(runtime_files=files, home_persistent='["sessions"]')
        self.assertIn(files, self.docker.inputs)
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as f:
            self.assertEqual(json.load(f), expiry)
        with open(os.path.join(self.state, f"{GEN}.plan.json")) as f:
            self.assertEqual(
                json.load(f)["mounts"],
                [{"path": self.home_dir + "/sessions", "rw": True, "volume": "ensemble-state-w"}],
            )

    def test_staged_files_are_written_in_the_container_before_the_guard(self):
        files = {"/run/ensemble/prompt/prompt.md": "雪\n" * 50_000, f"{self.cwd}/inputs/a.md": "a"}
        self.start(staged_files=json.dumps(files))
        staged = [i for i, call in enumerate(self.order) if FILES_STAGE in call]
        self.assertEqual(len(staged), 1)
        self.assertLess(staged[0], self.order.index("guard"))
        self.assertEqual(
            self.order[staged[0]],
            ["exec", "-i", "--user", "agent", CONTAINER, PYTHON, "-I", "-c", FILES_STAGE],
        )
        calls = [call for call in self.order if isinstance(call, list)]
        self.assertEqual(json.loads(self.docker.inputs[calls.index(self.order[staged[0]])]), files)

    def test_no_staged_files_stages_nothing(self):
        self.start()
        self.assertFalse(any(FILES_STAGE in call for call in self.order if isinstance(call, list)))

    def test_staging_errors_name_the_step(self):
        metadata = {"staged_files": json.dumps({"/run/ensemble/prompt/prompt.md": "private"})}
        for result, want in (
            (done(1, "", ""), "^staging files: exit status 1$"),
            (
                done(1, "", "Traceback (most recent call last):\nOSError: [Errno 28] No space\n"),
                r"^staging files: OSError: \[Errno 28\] No space$",
            ),
            (OSError("unavailable"), "^staging files: unavailable$"),
            (TimeoutExpired("exec", 1), "^staging files: "),
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
                self.host._stage_staged_files(CONTAINER, metadata, {}, self.local)
            self.assertNotIn("private", str(raised.exception))

    def test_a_failed_stage_rolls_the_start_back(self):
        for stage, message, options in (
            (
                "_stage_staged_files",
                "no room",
                {"staged_files": json.dumps({"/run/ensemble/prompt/prompt.md": "x"})},
            ),
            ("_stage_files", "projection failed", {}),
        ):
            self.broker.closed.clear()
            self.order.clear()
            with (
                self.subTest(stage),
                mock.patch.object(self.host, stage, side_effect=SessionError(message)),
                self.assertRaisesRegex(SessionError, message),
            ):
                self.start(**options)
            self.assertEqual(self.broker.closed, [CONTAINER])
            self.assertNotIn("guard", self.order)

    def test_the_build_holds_the_whitelisted_plugins(self):
        self.host.plugins = (UPDRAFT, BETA)
        self.start()
        self.assertEqual(self.broker.plugins, (UPDRAFT, BETA))

    def test_start_order(self):
        process = self.start()
        manifest = os.path.join(self.state, f"{GEN}.json")
        ps = ["ps", "-a", "--filter", "label=ensemble.session", "--format"]
        self.assertEqual(
            [step if isinstance(step, str | tuple) else step[:5] for step in self.order][:8],
            [
                ps,
                ps,
                ps,
                "version",
                ("build", REPO),
                ["volume", "ls", "--filter", "label=ensemble.kind", "--format"],
                ("start", manifest, client_keys(REPO)),
                "guard",
            ],
        )
        self.assertEqual(self.broker.built_daemons, [LOCAL_DAEMON])
        self.assertFalse(os.path.exists(self.volume_dir))
        with open(manifest) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": GEN,
                    "daemon": LOCAL_DAEMON,
                    "image": IMAGE,
                    "cwd": self.cwd,
                    "workspace": "ensemble-ws-w",
                    "state": "new",
                    "client": "agent",
                    "agent_dir": self.home_dir,
                    "hook_path": os.path.join(REPO, "bin", "ensemble-session"),
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
                "record": None,
                "events": os.path.join(self.home, "events"),
                "kind": "tui",
                "environment": None,
            },
        )
        self.assertIsInstance(process, worker_module.DockerWorker)

    def test_a_fresh_home_gets_the_clients_files_through_the_container(self):
        self.start(home_files=json.dumps({"settings.json": '{"model": "opus"}'}))
        self.assertEqual(
            self.staged(), {os.path.join(self.home_dir, "settings.json"): '{"model": "opus"}'}
        )
        self.assertFalse(os.path.exists(self.volume_dir))

    CODEX = {
        "client": "codex",
        "home_field": "codex_dir",
        "home_dir": "codex",
        "credential": "codex-access-token",
        "home_persistent": '["sessions"]',
    }

    def state_calls(self):
        """The docker calls this process made on state volumes or their readers."""
        return [
            o
            for o in self.order
            if isinstance(o, list)
            and (o[:1] == ["rm"] or o[:2] == ["volume", "rm"] or any("state" in a for a in o))
        ]

    def manifest_state(self):
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            return json.load(f)["state"]

    def test_a_new_start_asks_the_broker_for_a_new_state_volume(self):
        self.docker.readers = ["r1", "r2"]
        self.start()
        self.assertEqual(self.manifest_state(), "new")
        self.assertEqual(self.state_calls(), [])

    def test_a_failed_new_start_leaves_state_volumes_to_the_broker(self):
        self.broker.error = BrokerError(10, "start_failed", "boom", "not_needed", None)
        with self.assertRaises(BrokerError):
            self.start()
        self.assertEqual(self.state_calls(), [])

    def test_a_daemon_it_cannot_list_fails_a_new_start_before_the_broker(self):
        self.docker.down = True
        with self.assertRaisesRegex(Unanswered, "^docker ps: Cannot connect"):
            self.start()
        self.assertNotIn("start", [o[0] for o in self.order if isinstance(o, tuple)])

    def test_an_unreachable_chosen_daemon_fails_a_new_start_before_any_build_or_record(self):
        why = "Docker Desktop is not running"
        self.targets("local", self.local._replace(id=None, unreachable=why))
        with self.assertRaises(BrokerError) as raised:
            self.start()
        self.assertEqual(
            (raised.exception.code, raised.exception.exit_code, raised.exception.message),
            ("docker_unreachable", 7, f"Cannot reach Docker target {self.ep}: {why}."),
        )
        self.assertEqual(self.order, [])
        self.assertIsNone(load_record("w"))

    def test_a_resume_checks_history_with_the_built_image_after_the_build(self):
        self.history()
        self.start(resume="1")
        (run,) = [c for c in self.state_volumes.calls if c[3] == "run"]
        self.assertIn(DIGEST, run)

    def test_a_resume_keeps_the_state_volume(self):
        self.history()
        self.start(resume="1")
        self.assertEqual(self.manifest_state(), "keep")
        self.assertEqual(self.state_calls(), [])

    def test_every_start_records_its_daemon_before_its_checks(self):
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                self.start(resume=resume)
                self.assertEqual(load_record("w"), DaemonRecord(self.local, DOCKER))
        os.unlink(os.path.join(self.state, RECORD_FILE))
        with self.assertRaisesRegex(ConfigError, "no native history"):
            self.start(resume="1", home_dir="other", home_field="other_dir")
        self.assertEqual(load_record("w"), DaemonRecord(self.local, DOCKER))

    def kept(self, resume="0", files=None):
        return self.start(
            **{**self.CODEX, "home_persistent": '["sessions", "config"]'},
            resume=resume,
            home_files=json.dumps({} if files is None else files),
        )

    def resumable(self):
        """A Codex transcript in the fake state volume, which the launch names."""
        stored = os.path.join(self.state, "codex", "sessions", "s1.jsonl")
        os.makedirs(os.path.dirname(stored), exist_ok=True)
        with open(stored, "w") as f:
            f.write("{}\n")
        transcript = os.path.join(client_home("codex"), "sessions", "s1.jsonl")
        self.launch = replace(self.launch, transcript=transcript)

    def test_a_private_homes_copy_is_staged_on_a_new_start_and_never_on_a_resume(self):
        home = client_home("codex")
        self.kept(files={"config/config.toml": "a = 1"})
        self.assertEqual(self.staged(), {os.path.join(home, "config", "config.toml"): "a = 1"})
        self.resumable()
        self.docker.calls.clear()
        self.kept(resume="1", files={"config/config.toml": "b = 2"})
        self.assertEqual(self.staged(), {})

    def test_a_private_home_file_outside_its_kept_dirs_is_refused(self):
        for key in ("config.toml", "other/config.toml"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    SessionError,
                    f"^docker session w: home file {re.escape(key)} is outside its kept dirs$",
                ),
            ):
                self.kept(files={key: "x"})

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
        home = client_home("codex")
        stored = os.path.join(self.state, "codex", "sessions", "rollout.jsonl")
        transcript = os.path.join(home, "sessions", "rollout.jsonl")
        os.makedirs(os.path.dirname(stored))
        native = '{"session_id":"same-native-identity"}\n'
        with open(stored, "w") as stream:
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
        with open(stored) as stream:
            self.assertEqual(stream.read(), native)
        self.assertEqual(self.host_record()["sid"], "s1")
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as stream:
            self.assertEqual(json.load(stream), until)

    def test_resume_keeps_the_home_and_starts_a_new_container(self):
        transcript = self.history()
        process = self.start(resume="1", home_files=json.dumps({"settings.json": "{}"}))
        self.assertFalse(os.path.exists(os.path.join(self.volume_dir, "settings.json")))
        with open(transcript) as f:
            self.assertEqual(f.read(), "{}\n")
        manifest = os.path.join(self.state, f"{GEN}.json")
        self.assertIn(("start", manifest, client_keys(REPO)), self.order)
        self.assertNotIn(("workspace_rm", "ensemble-ws-w"), self.order)
        with open(manifest) as f:
            found = json.load(f)
        self.assertEqual((found["cwd"], found["agent_dir"]), (self.cwd, self.home_dir))
        self.assertIsInstance(process, worker_module.DockerWorker)

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
        os.makedirs(self.volume_dir)
        with open(os.path.join(self.volume_dir, "keep"), "w"):
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
                self.assertEqual(os.listdir(self.volume_dir), ["keep"])
                self.assertFalse(os.path.exists(os.path.join(self.state, f"{GEN}.json")))
                self.popen_mock.assert_not_called()

    def test_resume_without_native_history_fails_before_the_start(self):
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
                    os.makedirs(self.volume_dir, exist_ok=True)
                    with open(os.path.join(self.volume_dir, "keep"), "w"):
                        pass
                self.launch = replace(self.launch, transcript=transcript)
                self.refused_before_start(
                    ConfigError, f"docker session w: no native history to resume in {self.home_dir}"
                )
                if keep:
                    self.assertEqual(os.listdir(self.volume_dir), ["keep"])

    def test_a_resume_check_docker_cannot_answer_names_its_reason(self):
        self.history()
        self.state_volumes.error = "error during connect: ssh: connect to host pc: refused"
        self.refused_before_start(
            ConfigError,
            f"^docker session w: no native history to resume in {re.escape(self.home_dir)}: "
            "docker volume ls: error during connect: ssh: connect to host pc: refused$",
        )

    def test_a_transcript_in_another_sessions_home_is_no_history(self):
        other = os.path.join(os.path.dirname(self.state), "v", "agent", "s1.jsonl")
        os.makedirs(os.path.dirname(other))
        with open(other, "w") as f:
            f.write("{}\n")
        self.launch = replace(self.launch, transcript=other)
        self.refused_before_start(
            ConfigError,
            f"^docker session w: no native history to resume in {re.escape(self.home_dir)}$",
        )

    def test_resume_through_a_link_fails_before_the_start(self):
        secret = os.path.join(self.account, "secret.jsonl")
        with open(secret, "w") as f:
            f.write("host only\n")
        transcript = self.history()
        os.remove(transcript)
        os.symlink(secret, transcript)
        self.refused_before_start(ConfigError, "no native history")
        project = os.path.dirname(transcript)
        os.remove(transcript)
        os.rmdir(project)
        elsewhere = os.path.join(self.account, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(secret, os.path.join(elsewhere, "s1.jsonl"))
        os.symlink(elsewhere, project)
        self.refused_before_start(ConfigError, "no native history")

    def test_a_missing_or_bad_resume_flag_is_refused_untouched(self):
        os.makedirs(self.volume_dir)
        with open(os.path.join(self.volume_dir, "keep"), "w"):
            pass
        for flag in (None, "", "yes", "2"):
            with self.subTest(flag=flag):
                self.order.clear()
                with self.assertRaisesRegex(SessionError, "docker session w: bad resume flag"):
                    self.start(resume=flag)
                self.assertEqual([step[0] for step in self.order], ["ps"])
                self.assertEqual(os.listdir(self.volume_dir), ["keep"])

    def test_broker_environment_is_applied_without_interpreting_credentials(self):
        identity = {"ENSEMBLE_PR_AUTHOR": BOT, **GIT_IDENTITY, "FUTURE_IDENTITY": "opaque"}
        self.broker.started = replace(self.broker.started, env=identity)
        self.launch = replace(self.launch, env={**self.launch.env, "GIT_AUTHOR_NAME": "caller"})
        spec = ' {"github":{"app":"ensemble-bot","permissions":{"contents":"write"}}} '
        self.start(credentials=spec)
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["credentials"], spec)
        self.assertNotIn("github", manifest)
        self.assertEqual({k: self.guard_env()[k] for k in identity}, identity)
        with open(os.path.join(self.state, f"{GEN}.deadline.json")) as f:
            self.assertIsNone(json.load(f))

    def test_no_broker_environment_adds_no_author(self):
        self.start()
        env = self.guard_env()
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
            LOCAL_DAEMON,
            "sha256:" + "b" * 64,
            {"claude": "2.1.0"},
            env={},
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
                self.assertLessEqual(parse_time(ended), parse_time(found.pop("launched_at")))
                found.pop("phases")
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
        launched = datetime.fromisoformat(result["launched_at"])
        self.assertGreaterEqual(launched, datetime.fromisoformat(result["ended_at"]))

    def test_each_start_records_and_reports_its_phase_times(self):
        result = os.path.join(self.state, f"{GEN}.result.json")
        phases = ["checks", "image", "start", "client"]
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
                        "mounts": [
                            {"path": self.home_dir, "rw": True, "volume": "ensemble-state-w"}
                        ],
                        "credential": "agent-token",
                    },
                )
                head = f"docker plan w {GEN}:"
                self.assertEqual(
                    stderr.getvalue().splitlines(),
                    [
                        f"{head} image {IMAGE} {DIGEST}",
                        f"{head} workspace ensemble-ws-w at {self.cwd} rw",
                        f"{head} mounts {self.home_dir} rw (ensemble-state-w)",
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
                self.assertEqual([step[0] for step in self.order], ["ps"])

    def test_a_result_write_failure_rolls_back(self):
        os.makedirs(os.path.join(self.state, f"{GEN}.result.json", "taken"))
        for resume in ("0", "1"):
            with self.subTest(resume=resume):
                if resume == "1":
                    self.history()
                with self.assertRaisesRegex(SessionError, "docker session w: "):
                    self.start(resume=resume)
                self.assertNotIn("guard", self.order)
                self.assertEqual(self.order[-1], ("close", CONTAINER))

    def test_a_final_result_write_failure_closes_the_started_worker(self):
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
        self.assertIn("guard", self.order)
        self.assertIn(("close", CONTAINER), self.order)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))

    def test_a_container_the_sweep_could_not_remove_refuses_start_naming_it(self):
        old = "f" * 32
        self.docker.ps = [f"ensemble-w-{old}\tw\t{old}\trunning"]
        self.broker.close = mock.Mock(side_effect=SessionError("broker: boom"))
        with (
            self.assertLogs("session", "WARNING"),
            self.assertRaisesRegex(
                SessionError,
                f"^docker session w: container ensemble-w-{old} of an earlier start is still up"
                r" \(its removal failed; see the log\); the next start retries it$",
            ),
        ):
            self.start()
        self.broker.close.assert_called_once_with(f"ensemble-w-{old}", daemon=LOCAL_DAEMON)

    def test_an_unrecognised_container_of_the_name_refuses_start_naming_it(self):
        self.docker.ps = [f"ensemble-w-{GEN.upper()}\tw\t{GEN.upper()}\trunning"]
        with self.assertRaisesRegex(
            SessionError,
            f"^docker session w: container ensemble-w-{GEN.upper()} is labelled for it but not"
            f" named as this Host names one; remove it: docker rm -f ensemble-w-{GEN.upper()}$",
        ):
            self.start()
        self.assertEqual(self.broker.closed, [])

    def test_a_role_runs_cwd_starts_as_any_other(self):
        cwd = os.path.join(self.account, ".ensemble", "adhoc", "x", "agent")
        os.makedirs(cwd)
        self.launch = replace(self.launch, cwd=cwd)
        self.start()
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            found = json.load(f)
        self.assertEqual((found["cwd"], found["workspace"]), (cwd, "ensemble-ws-w"))

    def test_a_reply_at_another_address_of_the_same_daemon_is_accepted(self):
        self.broker.started = Started(
            **{**vars(self.broker.started), "endpoint": "ssh://u@pc.tail1234.ts.net"}
        )
        self.start()
        self.assertIn("guard", self.order)

    def test_a_foreign_container_rolls_back_on_the_chosen_daemon(self):
        self.broker.started = Started(
            **{**vars(self.broker.started), "container": f"ensemble-x-{GEN}"}
        )
        with self.assertRaisesRegex(SessionError, "broker: started"):
            self.start()
        self.assertEqual(self.order[-1], ("close", CONTAINER))
        self.assertEqual(self.broker.close_daemons, [LOCAL_DAEMON])
        self.assertNotIn("guard", self.order)

    def test_the_reply_s_daemon_is_the_broker_s_to_check(self):
        self.broker.started = Started(**{**vars(self.broker.started), "daemon": "ABCD:EFGH:IJKL"})
        self.start()
        self.assertIn("guard", self.order)

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
                step: lambda _: (stop(), done())[1],
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
                self.broker = self.host.broker = FakeBroker(
                    self.order, self.broker.started, self.broker.found
                )
                self.stopped_at("start", signum)
                self.assertEqual(self.broker.closed, [CONTAINER])
                self.popen_mock.assert_not_called()

    def test_a_signal_before_the_broker_start_starts_nothing(self):
        self.stopped_at("build")
        self.assertNotIn("start", [step[0] for step in self.order if isinstance(step, tuple)])
        self.assertEqual(self.broker.closed, [])

    def test_a_signal_while_the_worker_starts_closes_it(self):
        self.METADATA = {**self.METADATA, "runtime_files": json.dumps({"/run/ensemble/x": "{}"})}
        self.stopped_at(("agent", CONTAINER, PYTHON, "-I", "/opt/ensemble/lib/bootstrap.py"))
        self.assertIn("guard", self.order)
        self.assertIn(("close", CONTAINER), self.order)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))

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


ENV_SOURCE = "gamma@owner/market#" + "e" * 40
ENVIRONMENT = EnvironmentSpec(
    image="dummy-env:test",
    gateway="mini-op",
    plugins=("dummy-plugin", BETA, ENV_SOURCE),
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

    def test_manifest_preserves_both_credential_declarations(self):
        for value in (' {"future-kind": [1]}\n', "{", "{}", ""):
            with self.subTest(value=value):
                self.start(credentials=value, environment_credentials=' {"other": 2} ')
                manifest = self.manifest()
                self.assertEqual(manifest["credentials"], value)
                self.assertEqual(manifest["environment_credentials"], ' {"other": 2} ')
        self.start()
        self.assertNotIn("credentials", self.manifest())
        self.assertNotIn("environment_credentials", self.manifest())

    def test_resume_reissues_saved_declarations_after_close(self):
        values = {"credentials": ' {"future-kind": [1]}\n', "environment_credentials": "{"}
        self.start(**values)
        self.assertEqual({key: self.host_record()[key] for key in values}, values)
        self.close_quietly(self.handle)
        self.handle = None
        self.history()
        with open(os.path.join(self.cwd, "credentials.json"), "w") as f:
            f.write('{"replacement": true}')
        self.start(resume="1")
        self.assertEqual({key: self.manifest()[key] for key in values}, values)
        self.start(resume="1", **values)
        self.assertEqual({key: self.manifest()[key] for key in values}, values)

    def test_resume_refuses_different_or_new_declarations_before_start(self):
        self.history()
        for key in ("credentials", "environment_credentials"):
            for saved, given in ((None, "{}"), ("{}", " {} "), ("{}", ""), ("{}", "{")):
                self.record(**{key: saved})
                metadata = {**self.METADATA, "resume": "1", key: given}
                for check in (
                    lambda metadata=metadata: self.host._preflight("w", self.launch, metadata),
                    lambda metadata=metadata: self.host._validate("w", self.launch, metadata),
                    lambda key=key, given=given: self.start(resume="1", **{key: given}),
                ):
                    with self.subTest(key=key, saved=saved, given=given):
                        self.order.clear()
                        with self.assertRaisesRegex(
                            ConfigError, f"resume: {key} differs"
                        ) as caught:
                            check()
                        self.assertNotIn("future-kind", str(caught.exception))
                        self.assertEqual(self.order, [])

    def test_fresh_start_does_not_inherit_saved_credentials(self):
        self.record(credentials="{}", environment_credentials="{}")
        self.start()
        self.assertNotIn("credentials", self.manifest())
        self.assertNotIn("environment_credentials", self.manifest())

    def test_another_identity_does_not_inherit_saved_credentials(self):
        self.record(sid="another", credentials="{}", environment_credentials="{}")
        self.history()
        self.start(resume="1")
        self.assertNotIn("credentials", self.manifest())
        self.assertNotIn("environment_credentials", self.manifest())

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
        self.host.plugins = (BETA,)
        self.start(environment=carried(ENVIRONMENT))
        self.assertEqual(self.broker.plugins, (BETA, ENV_SOURCE))
        self.assertEqual(
            self.broker.built, {"base_image": "dummy-env:test", "image_plugins": ("dummy-plugin",)}
        )

    def test_the_build_is_its_harness_start_phase(self):
        with phase_reporter(lambda phase, wait: self.order.append(("phase", phase, wait))):
            self.start(environment=carried(ENVIRONMENT))
        built = self.order.index(("build", REPO))
        self.assertEqual(
            self.order[built - 1 : built + 2],
            [("phase", "building the worker image", TIMEOUT), ("build", REPO), ("phase", None, 0)],
        )

    def test_worker_image_sends_no_base_image(self):
        worker_image = replace(ENVIRONMENT, image=EnvironmentSpec.WORKER, plugins=())
        self.start(environment=carried(worker_image))
        self.assertEqual(self.broker.built, {"base_image": None, "image_plugins": ()})

    def test_manifest_carries_the_environment(self):
        self.broker.started = replace(
            self.broker.started, env={"ENSEMBLE_PR_AUTHOR": BOT, **GIT_IDENTITY}
        )
        self.start(environment=carried(ENVIRONMENT))
        manifest = self.manifest()
        self.assertEqual(
            {key: manifest.get(key) for key in ("gateway", "lock", "lock_owner")},
            {
                "gateway": "mini-op",
                "lock": "dummy-lock",
                "lock_owner": "w",
            },
        )
        self.assertNotIn("prefetch", manifest)
        self.assertNotIn("github", manifest)
        self.assertNotIn("secrets", manifest)
        self.assertEqual(self.guard_env()["ENSEMBLE_PR_AUTHOR"], BOT)

    def test_prefetch_has_no_implicit_github_credential(self):
        self.start(environment=carried(PREFETCHED))
        manifest = self.manifest()
        self.assertEqual(manifest["prefetch"], {"repo": "o/r", "ref": "main"})
        self.assertNotIn("github", manifest)
        env = self.guard_env()
        self.assertNotIn("ENSEMBLE_PR_AUTHOR", env)
        self.assertFalse(set(GIT_IDENTITY) & set(env))

    def test_prefetch_passes_the_declared_grant_unchanged(self):
        spec = '{"github":{"app":"ensemble-bot","permissions":{"contents":"write"}}}'
        mounts = []
        for credentials in (None, spec):
            self.start(
                environment=carried(PREFETCHED),
                credentials=credentials,
                home_persistent='["sessions"]',
            )
            with open(os.path.join(self.state, f"{GEN}.plan.json")) as f:
                mounts.append(json.load(f)["mounts"])
        self.assertEqual(self.manifest()["credentials"], spec)
        self.assertEqual(self.manifest()["prefetch"], {"repo": "o/r", "ref": "main"})
        self.assertNotIn("github", self.manifest())
        self.assertEqual(mounts[1], mounts[0])

    def test_busy_start_raises_session_busy(self):
        self.broker.error = BrokerError(75, "busy", "Lock held.", "not_needed", None, holder=HOLDER)
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        for call in (
            lambda: self.start(environment=carried(ENVIRONMENT)),
            lambda: self.host.worker(
                "w",
                self.launch,
                terminal=False,
                metadata={**self.METADATA, "environment": carried(ENVIRONMENT)},
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
        self.resumable(PREFETCHED.persisted())
        self.start(resume="1", environment=carried(ENVIRONMENT))
        manifest = self.manifest()
        self.assertEqual(manifest["prefetch"], {"repo": "o/r", "ref": "main"})
        self.assertNotIn("github", manifest)
        self.assertNotIn("ENSEMBLE_PR_AUTHOR", self.guard_env())
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


class ResumeDaemonTest(StartBase):
    """A resume runs on the daemon its record names, found by ID; with no record, the local one."""

    OLD = Daemon(
        "remote", "ssh://u@old", remote=True, path=f"/nowhere{os.pathsep}{SYSTEM_PATH}", id="A"
    )

    def setUp(self):
        super().setUp()
        self.two_daemons()
        self.history()

    def closed(self, found):
        """A started, then closed session w recorded on `found`."""
        self.recorded("w", found)
        self.start(resume="1")
        self.close_quietly(self.handle)
        self.handle = None
        self.order.clear()
        self.broker.built_daemons.clear()

    def refused(self, error, pattern):
        """Each resume path fails with `pattern` before any build or start, the record kept; the
        start lists its daemons, the checks run no docker command.
        """
        with open(os.path.join(self.state, RECORD_FILE), "rb") as f:
            before = f.read()
        metadata = {**self.METADATA, "resume": "1"}
        for check in (
            lambda: self.host._preflight("w", self.launch, metadata),
            lambda: self.host._validate("w", self.launch, metadata),
            lambda: self.start(resume="1"),
        ):
            self.order.clear()
            with self.assertRaisesRegex(error, pattern):
                check()
            self.assertEqual([o for o in self.order if not isinstance(o, list)], [])
            self.assertEqual([o for o in self.order if o[:2] != ["ps", "-a"]], [])
        with open(os.path.join(self.state, RECORD_FILE), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.broker.built_daemons, [])

    def test_resume_where_no_daemon_has_its_id_is_refused_naming_both_targets(self):
        self.recorded("w", self.OLD)
        self.refused(
            ConfigError,
            "^"
            + re.escape(
                "docker session w: cannot resume it here: its Docker target is ssh://u@old (daemon"
                f" A), the current target is {REMOTE} (daemon {REMOTE_DAEMON}); set the Docker"
                " target back to ssh://u@old to resume it"
            )
            + "$",
        )

    def test_a_local_session_is_refused_once_the_local_daemon_is_another(self):
        self.recorded("w", self.local._replace(id="OLD-DESKTOP"))
        self.refused(
            ConfigError,
            "^docker session w: cannot resume it here: its Docker target is the local Docker"
            rf" Desktop \({re.escape(self.ep)}\) \(daemon OLD-DESKTOP\), the current target is"
            rf" {REMOTE} \(daemon {REMOTE_DAEMON}\)",
        )

    def test_a_miss_while_a_daemon_is_unreachable_is_that_daemon_s_unreachable_error(self):
        self.recorded("w", self.OLD)
        why = "ssh: connect to host pc port 22: Connection refused"
        self.targets("remote", self.pc._replace(id=None, unreachable=why), self.local)
        self.refused(BrokerError, f"^broker: Cannot reach Docker target {REMOTE}: {why}\\.$")

    def test_resume_at_another_address_of_the_same_daemon_starts_there(self):
        self.closed(self.pc._replace(endpoint="ssh://u@pc.old"))
        self.assertEqual(self.broker.built_daemons, [])
        self.start(resume="1")
        self.assertIn("guard", self.order)
        self.assertEqual(self.broker.built_daemons, [REMOTE_DAEMON])
        [(argv, _, _), *_] = reversed(self.execs)
        self.assertEqual(argv[:3], [DOCKER, "--host", REMOTE])
        self.assertEqual(load_record("w").daemon.endpoint, "ssh://u@pc.old")

    def test_a_session_with_no_record_resumes_on_the_local_daemon_and_records_it(self):
        self.assertFalse(os.path.exists(os.path.join(self.state, RECORD_FILE)))
        self.start(resume="1")
        self.assertIn("guard", self.order)
        self.assertEqual(self.broker.built_daemons, [LOCAL_DAEMON])
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(json.load(f)["daemon"], LOCAL_DAEMON)
        self.assertEqual(load_record("w"), DaemonRecord(self.local, DOCKER))
        self.assertEqual([call for call, _ in self.remote.calls if call[:2] != ["ps", "-a"]], [])
        [(argv, _, _)] = self.execs
        self.assertEqual(argv[:3], [DOCKER, "--host", self.ep])

    def test_a_session_with_no_record_and_the_local_daemon_down_is_its_unreachable_error(self):
        why = "Docker Desktop is not running"
        self.targets("remote", self.pc, self.local._replace(id=None, unreachable=why))
        metadata = {**self.METADATA, "resume": "1"}
        for check in (
            lambda: self.host._preflight("w", self.launch, metadata),
            lambda: self.host._validate("w", self.launch, metadata),
            lambda: self.start(resume="1"),
        ):
            with self.assertRaisesRegex(
                BrokerError, f"^broker: Cannot reach Docker target {re.escape(self.ep)}: {why}\\.$"
            ):
                check()
        self.assertEqual(self.broker.built_daemons, [])
        self.assertIsNone(load_record("w"))

    def test_a_malformed_record_fails_a_resume_naming_it(self):
        path = os.path.join(self.state, RECORD_FILE)
        with open(path, "w") as f:
            f.write("{")
        metadata = {**self.METADATA, "resume": "1"}
        for check in (
            lambda: self.host._preflight("w", self.launch, metadata),
            lambda: self.host._validate("w", self.launch, metadata),
            lambda: self.start(resume="1"),
        ):
            with self.assertRaisesRegex(
                SessionError, f"^docker session w: daemon record: .*{re.escape(path)}"
            ):
                check()
        self.assertEqual(self.broker.built_daemons, [])
        with open(path) as f:
            self.assertEqual(f.read(), "{")


class PreflightTest(Base):
    def validate(self):
        self.host.validate("w", Launch(("agent",), cwd=self.cwd, env={}))

    def test_preflight_reports_its_time(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.validate()
        self.assertRegex(stderr.getvalue(), r"\Adocker timing w: preflight \d+\.\d\d s\n\Z")

    def test_resume_preflight_builds_with_the_whitelisted_plugins(self):
        self.host.plugins = (UPDRAFT, BETA)
        self.validate()
        self.assertEqual(self.order, ["version", ("build", REPO)])
        self.assertEqual(self.broker.plugins, (UPDRAFT, BETA))
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
            self.host.worker("w", Launch(("agent", over), cwd=self.cwd), terminal=True)
        with self.assertRaisesRegex(ConfigError, want):
            self.host.worker("w", Launch(("agent", over), cwd=self.cwd), terminal=False)
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

        def worker(name, launch):
            return self.host.worker(name, launch, terminal=True)

        def headless(name, launch):
            return self.host.worker(name, launch, terminal=False)

        for check in (self.host.validate, worker, headless):
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


class SweepTest(Base):
    OLD = "f" * 32

    def setUp(self):
        super().setUp()
        self.tui = os.path.join(self.home, ".ensemble", "tui")

    def row(self, name, gen=GEN, state="running"):
        return f"ensemble-{name}-{gen}\t{name}\t{gen}\t{state}"

    def files(self):
        return {
            os.path.relpath(os.path.join(root, name), self.home)
            for root, dirs, names in os.walk(self.home)
            for name in names + dirs
        }

    def host_json(self, name, gen=GEN, **fields):
        state = os.path.join(self.home, ".ensemble", "docker-state", name)
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "host.json"), "w") as f:
            json.dump({"generation": gen, **fields}, f)

    def held(self, name, lock):
        fd = os.open(
            os.path.join(self.home, ".ensemble", "docker-state", name, lock),
            os.O_RDWR | os.O_CREAT,
        )
        self.addCleanup(_close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    def test_sweep_preserves_damaged_records_and_containers(self):
        for label, text in BAD_RECORDS.items():
            for own in ("w", "bad"):
                with self.subTest(record=label, own=own):
                    path = self.damaged_record("bad", text)
                    self.docker.ps = [self.row("bad"), self.row("clean")]
                    self.broker.closed.clear()
                    self.host.sweep(own)
                    self.assertEqual(self.broker.closed, [f"ensemble-clean-{GEN}"])
                    with open(path) as f:
                        self.assertEqual(f.read(), text)

    def test_sweep_closes_debris_and_free_workers_and_keeps_held_and_busy_ones(self):
        self.host_json("b", self.OLD, kind="tui")
        self.host_json("c", mode="guard")  # no kind: an older release's start
        for name, kind in (("d", "tui"), ("e", "tui"), ("k", "headless"), ("l", "headless")):
            self.host_json(name, kind=kind)
        self.held("d", "guard.lock")
        self.held("k", "guard.lock")
        os.makedirs(self.tui, exist_ok=True)
        busy = os.open(os.path.join(self.tui, "f.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, busy)
        fcntl.flock(busy, fcntl.LOCK_EX)
        self.docker.ps = [
            self.row("w", self.OLD),  # own name, no host.json: debris
            self.row("a"),  # no host.json
            self.row("b"),  # host.json of another generation
            self.row("c"),
            self.row("d"),  # guarded, its owner holds it
            self.row("e", state="exited"),  # guarded, its owner gone
            self.row("k"),  # headless, its owner holds it
            self.row("l"),  # headless, its owner gone
            self.row("f"),  # starting or closing elsewhere
            f"ensemble-g-{GEN}\t\t{GEN}\trunning",
            f"ensemble-..-{GEN}\t../x\t{GEN}\trunning",
            f"ensemble-h-{GEN}\ti\t{GEN}\trunning",
            f"ensemble-j-{GEN.upper()}\tj\t{GEN.upper()}\trunning",
        ]
        self.host.sweep("w")
        self.assertEqual(
            self.broker.closed,
            [f"ensemble-w-{self.OLD}"]
            + [f"ensemble-{name}-{GEN}" for name in ("a", "b", "c", "e", "l")],
        )
        self.assertFalse([call for call in self.docker.order if call[:1] == ["exec"]])

    def test_sweep_keeps_another_names_container_while_its_owner_lock_is_held(self):
        os.makedirs(os.path.join(self.home, ".ensemble", "docker-state", "a"))
        self.host_json("b", self.OLD, kind="tui")
        self.host_json("c", mode="guard")  # no kind: an older release's start
        self.host_json("d", kind="tui")
        self.host_json("k", kind="headless")
        for name in ("a", "b", "c", "d", "k"):
            self.held(name, "guard.lock")
        self.docker.ps = [self.row(name) for name in ("a", "b", "c", "d", "k")]
        self.host.sweep("w")
        self.assertEqual(self.broker.closed, [])

    def test_own_rows_keep_the_record_rule(self):
        self.host_json("w", mode="guard")
        self.held("w", "guard.lock")  # the caller's own start holds it
        self.docker.ps = [self.row("w")]
        self.host.sweep("w")
        self.assertEqual(self.broker.closed, [f"ensemble-w-{GEN}"])
        self.broker.closed.clear()
        self.host_json("w", kind="tui")
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

        def close(container, **_):
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

    def kept_marker(self, volume):
        return os.path.join(self.kept, f"{volume}@{LOCAL_DAEMON}")

    def marker(self, volume, text="", age=0):
        os.makedirs(self.kept, exist_ok=True)
        path = self.kept_marker(volume)
        with open(path, "w") as f:
            f.write(text)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def test_a_volume_whose_workspace_is_gone_is_removed_and_others_are_left(self):
        self.host.reclaim_volumes()
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
        self.host.reclaim_volumes()
        self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])

    def test_a_kept_volume_does_not_block_later_volumes(self):
        self.docker.volumes.append(f"ensemble-ws-gone\t{self.account}/gone/w")
        for code in ("workspace_dirty", "workspace_failed"):
            with self.subTest(code=code):
                self.order.clear()
                self.broker.failing = {}

                def remove(volume, code=code, **_):
                    self.order.append(("workspace_rm", volume))
                    if volume == self.GONE:
                        raise BrokerError(12, code, "kept", "not_needed", None)
                    return True

                self.broker.workspace_rm = remove
                with self.assertLogs("session", "WARNING"):
                    self.host.reclaim_volumes()
                self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])
                os.unlink(self.kept_marker(self.GONE))

    def test_a_failed_batch_warns_once_per_volume(self):
        self.docker.volumes.append(f"ensemble-ws-gone\t{self.account}/gone/w")
        self.broker.failing = {
            "workspace_rm": BrokerError(14, "workspace_failed", "check failed", "not_needed", None)
        }
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim_volumes()
        self.assertEqual(self.removed(), [self.GONE, "ensemble-ws-gone"])
        self.assertEqual(len(logs.output), 2)
        self.order.clear()
        with self.assertNoLogs("session", "WARNING"):
            self.host.reclaim_volumes()
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

    def test_background_reclaim_passes_a_set_ssh_agent_socket(self):
        self.spawn_mock.side_effect = None
        with (
            mock.patch("threading.Thread"),
            mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/private/agent.sock"}),
        ):
            self.host._spawn_reclaim()
        env = self.spawn_mock.call_args.kwargs["env"]
        self.assertEqual(set(env), {"PATH", "HOME", "PYTHONPATH", "SSH_AUTH_SOCK"})
        self.assertEqual(env["SSH_AUTH_SOCK"], "/private/agent.sock")

    def test_a_background_spawn_failure_warns_and_returns(self):
        self.spawn_mock.side_effect = OSError("cannot spawn")
        with self.assertLogs("session", "WARNING") as logs:
            self.host._spawn_reclaim()
        self.assertIn("docker reclaim: cannot spawn", logs.output[0])

    def test_the_background_entrypoint_asks_no_unreachable_daemon(self):
        module = sys.modules[DockerHost.__module__]
        down = {
            "default": "local",
            "daemons": [
                {
                    "name": "local",
                    "endpoint": self.ep,
                    "remote": False,
                    "path": SYSTEM_PATH,
                    "unreachable": "Docker Desktop is not running",
                }
            ],
        }
        popen = FakePopen(
            (b'{"protocol_version": "43"}\n', b"", 0), (json.dumps(down).encode(), b"", 0)
        )
        self.spawn_mock.side_effect = popen
        with mock.patch("sys.argv", [module.__file__, DOCKER]):
            runpy.run_path(module.__file__, run_name="__main__")
        self.assertEqual(
            [argv for argv, _, _ in popen.calls], [[BROKER, "version"], [BROKER, "target"]]
        )
        self.assertEqual(self.order, [])

    def test_a_workers_removal_schedules_reclaim_after_closing_the_container(self):
        def spawn():
            self.order.append("reclaim")

        with mock.patch.object(self.host, "_spawn_reclaim", side_effect=spawn):
            self.host._remove(CONTAINER, self.local)
        self.assertEqual(self.order, [("close", CONTAINER), "reclaim"])

    def test_unsaved_work_keeps_the_volume_and_warns_once_for_good(self):
        message = f"Workspace {self.GONE} has uncommitted or unpushed work in: publish."
        self.broker.failing = {
            "workspace_rm": BrokerError(12, "workspace_dirty", message, "not_needed", None)
        }
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim_volumes()
        self.assertEqual(
            logs.output, [f"WARNING:session:docker workspace {self.GONE} kept: {message}"]
        )
        marker = self.kept_marker(self.GONE)
        self.assertTrue(os.path.isfile(marker))
        old = time.time() - 10 * KEPT_RETRY
        os.utime(marker, (old, old))
        self.order.clear()
        with self.assertNoLogs("session", "WARNING"):
            self.host.reclaim_volumes()
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
                    self.host.reclaim_volumes()
                self.assertEqual(self.removed(), [self.GONE])
                self.assertIn(f"docker workspace {self.GONE} kept: ", logs.output[0])
                marker = self.kept_marker(self.GONE)
                with open(marker) as f:
                    self.assertEqual(f.read(), "failed")
                self.order.clear()
                with self.assertNoLogs("session", "WARNING"):
                    self.host.reclaim_volumes()
                self.assertEqual(self.removed(), [])
                self.marker(self.GONE, "failed", age=KEPT_RETRY + 1)
                self.broker.failing = {}
                self.host.reclaim_volumes()
                self.assertEqual(self.removed(), [self.GONE])
                self.assertFalse(os.path.exists(marker))

    def test_a_marker_whose_volume_is_gone_is_deleted(self):
        for name in ("ensemble-run-old-0123abcd", "ensemble-ws-here"):
            self.marker(name)
        self.docker.volumes = self.docker.volumes[1:]
        self.host.reclaim_volumes()
        self.assertEqual(
            sorted(os.listdir(self.kept)), [".lock", f"ensemble-ws-here@{LOCAL_DAEMON}"]
        )
        self.assertEqual(self.removed(), [])

    def test_a_held_lock_or_no_daemon_socket_skips_reclaim(self):
        os.makedirs(self.kept)
        busy = os.open(os.path.join(self.kept, ".lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, busy)
        fcntl.flock(busy, fcntl.LOCK_EX)
        self.host.reclaim_volumes()
        self.assertEqual(self.removed(), [])
        fcntl.flock(busy, fcntl.LOCK_UN)
        self.docker.down = True
        self.order.clear()
        with self.assertLogs("session", "WARNING"):
            self.host.reclaim_volumes()
        self.assertEqual([step[:2] for step in self.order], [["volume", "ls"]])
        self.targets("local", self.local._replace(id=None, unreachable="not running"))
        self.order.clear()
        self.host.reclaim_volumes()
        self.assertEqual((self.order, self.removed()), ([], []))

    def test_without_the_daemon_or_the_cli_a_kept_marker_stays(self):
        marker = self.marker(self.GONE)
        self.docker.down = True
        self.host.reclaim_volumes()
        self.docker.down = False
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "No such file", DOCKER))
        self.host.reclaim_volumes()
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(self.removed(), [])

    def test_a_failed_listing_only_warns(self):
        self.host.run = mock.Mock(return_value=done(1, "", "Cannot connect"))
        with self.assertLogs("session", "WARNING") as logs:
            self.host.reclaim_volumes()
        self.assertIn("docker reclaim: docker volume: Cannot connect", logs.output[0])

    def test_no_orphan_creates_no_file(self):
        self.docker.volumes = self.docker.volumes[1:]
        self.host.reclaim_volumes()
        self.assertFalse(os.path.exists(self.kept))


class RemoveWorkspacesTest(Base):
    def setUp(self):
        super().setUp()
        self.agent = os.path.join(self.account, "work", "TASK-1", "agent")
        os.makedirs(self.agent)
        self.docker.volumes = [
            f"ensemble-ws-engineer-TASK-1-0b6f2c1e\t{self.agent}",
            f"ensemble-ws-other-TASK-2-7c1d9e2f\t{self.account}/work/TASK-2/agent",
            f"ensemble-ws-pm-TASK-1-1a2b3c4d\t{self.agent}",
            "ensemble-ws-bare",
        ]

    def removals(self):
        return [step for step in self.order if isinstance(step, tuple)]

    def test_remove_workspaces_forces_out_every_volume_of_the_cwd(self):
        linked = os.path.join(self.account, "linked")
        os.symlink(os.path.join(self.account, "work"), linked)
        removed = self.host.remove_workspaces(os.path.join(linked, "TASK-1", "agent"))
        self.assertEqual(
            removed, ["ensemble-ws-engineer-TASK-1-0b6f2c1e", "ensemble-ws-pm-TASK-1-1a2b3c4d"]
        )
        self.assertEqual(
            self.removals(),
            [("workspace_rm", volume, "--force") for volume in removed],
        )

    def test_remove_workspaces_stops_at_a_refusal(self):
        self.broker.failing["workspace_rm"] = BrokerError(
            14, "workspace_failed", "volume is in use", "not_needed", None
        )
        with self.assertRaisesRegex(
            SessionError,
            "docker workspace ensemble-ws-engineer-TASK-1-0b6f2c1e kept: broker: volume is in use",
        ):
            self.host.remove_workspaces(self.agent)
        self.assertEqual(
            self.removals(),
            [("workspace_rm", "ensemble-ws-engineer-TASK-1-0b6f2c1e", "--force")],
        )

    def test_remove_workspaces_without_docker_refuses(self):
        self.targets("local", self.local._replace(id=None, unreachable="not running"))
        with self.assertRaisesRegex(
            SessionError, f"workspaces unknown .*Docker target {re.escape(self.ep)} unreachable"
        ):
            self.host.remove_workspaces(self.agent)
        self.targets("local", self.local)
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "No such file", DOCKER))
        with self.assertRaisesRegex(SessionError, "workspaces unknown .*no Docker CLI"):
            self.host.remove_workspaces(self.agent)
        self.assertEqual(self.removals(), [])

    def test_no_workspace_of_the_cwd_removes_none(self):
        self.assertEqual(self.host.remove_workspaces(os.path.join(self.account, "none")), [])
        self.assertEqual(self.removals(), [])

    def session(self, name, cwd, found=None):
        """Session `name`'s Mac records: its generation's manifest at `cwd`, and its daemon record
        `found` if any.
        """
        state = os.path.join(self.home, ".ensemble", "docker-state", name)
        os.makedirs(state)
        with open(os.path.join(state, "host.json"), "w") as f:
            json.dump({"generation": GEN, "kind": "tui"}, f)
        with open(os.path.join(state, f"{GEN}.json"), "w") as f:
            json.dump({"name": name, "generation": GEN, "cwd": cwd}, f)
        if found is not None:
            self.recorded(name, found)

    def test_a_session_on_an_unlisted_daemon_is_removed_by_its_recorded_id(self):
        gone = self.local._replace(id="GONE-ID")
        self.session("reviewer-bbbb", self.agent, gone)
        removed = self.host.remove_workspaces(self.agent)
        self.assertEqual(
            removed,
            [
                "ensemble-ws-engineer-TASK-1-0b6f2c1e",
                "ensemble-ws-pm-TASK-1-1a2b3c4d",
                "ensemble-ws-reviewer-bbbb",
            ],
        )
        self.assertEqual(self.broker.rm_daemons, [LOCAL_DAEMON, LOCAL_DAEMON, "GONE-ID"])

    def test_a_session_on_a_daemon_no_longer_configured_fails_naming_its_id(self):
        self.session("pm-TASK-1-1a2b3c4d", self.agent, self.local._replace(id="GONE-ID"))
        real = self.broker.workspace_rm

        def workspace_rm(volume, *, force=False, daemon):
            real(volume, force=force, daemon=daemon)
            if daemon == "GONE-ID":
                message = "Docker daemon GONE-ID is not configured."
                raise BrokerError(7, "docker_unreachable", message, "not_needed", None)
            return True

        self.broker.workspace_rm = workspace_rm
        with self.assertRaisesRegex(
            SessionError,
            "^docker workspace ensemble-ws-pm-TASK-1-1a2b3c4d kept: broker: Docker daemon GONE-ID"
            " is not configured.$",
        ):
            self.host.remove_workspaces(self.agent)
        self.assertEqual(
            list(zip([step[1] for step in self.removals()], self.broker.rm_daemons, strict=True)),
            [
                ("ensemble-ws-engineer-TASK-1-0b6f2c1e", LOCAL_DAEMON),
                ("ensemble-ws-pm-TASK-1-1a2b3c4d", "GONE-ID"),
            ],
        )

    def test_a_session_with_no_record_is_the_local_daemon_s(self):
        self.session("pm-TASK-1-1a2b3c4d", self.agent)
        self.session("legacy", self.agent)
        self.session("elsewhere", os.path.join(self.account, "none"), self.local._replace(id="X"))
        removed = self.host.remove_workspaces(self.agent)
        self.assertEqual(
            removed, ["ensemble-ws-engineer-TASK-1-0b6f2c1e", "ensemble-ws-pm-TASK-1-1a2b3c4d"]
        )
        self.assertEqual(self.broker.rm_daemons, [LOCAL_DAEMON, LOCAL_DAEMON])

    def test_a_malformed_record_fails_naming_it_and_removes_nothing(self):
        self.session("pm-TASK-1-1a2b3c4d", self.agent)
        path = os.path.join(
            self.home, ".ensemble", "docker-state", "pm-TASK-1-1a2b3c4d", RECORD_FILE
        )
        with open(path, "w") as f:
            json.dump({"name": "local", "id": "../x"}, f)
        with self.assertRaisesRegex(
            SessionError, f"^docker session pm-TASK-1-1a2b3c4d: daemon record: .*{re.escape(path)}"
        ):
            self.host.remove_workspaces(self.agent)
        self.assertEqual(self.removals(), [])


class LastLinesTest(Base):
    def diagnostic(self, value, gen=GEN):
        with open(os.path.join(self.state, f"{gen}.diagnostic.json"), "w") as f:
            f.write(value if isinstance(value, str) else json.dumps(value))

    def test_its_recorded_generations_last_lines_outlive_its_container(self):
        self.record()
        self.assertEqual(self.host.last_lines("w", "s1", GEN), "")
        self.diagnostic({"text": "error: bad flag"})
        self.assertEqual(self.docker.ps, [])
        self.assertEqual(self.host.last_lines("w", "s1", GEN), "error: bad flag")
        self.assertEqual(self.host.last_lines("w", "s1"), "error: bad flag")
        self.assertEqual(self.host.last_lines("w", "s2"), "")
        self.assertEqual(self.host.last_lines("w", "s2", GEN), "")
        self.assertEqual(self.host.last_lines("w", "s1", "f" * 32), "")
        self.assertEqual(self.host.last_lines("v", "s1", GEN), "")

    def test_a_diagnostic_with_only_a_close_reason_has_no_last_lines(self):
        self.record()
        self.diagnostic({"reason": "no harness byte for 61.2 s"})
        self.assertEqual(self.host.last_lines("w", "s1", GEN), "")
        self.diagnostic({"text": "error", "reason": "client exited"})
        self.assertEqual(self.host.last_lines("w", "s1", GEN), "error")

    def test_a_bad_record_or_diagnostic_has_no_last_lines(self):
        self.record(generation="../" + GEN)
        self.diagnostic({"text": "error"}, gen="../" + GEN)
        self.assertEqual(self.host.last_lines("w", "s1"), "")
        self.record()
        for value in ("not json", {"text": 5}, ["error"], {"text": "x" * (1 << 20)}):
            with self.subTest(value=str(value)[:20]):
                self.diagnostic(value)
                self.assertEqual(self.host.last_lines("w", "s1", GEN), "")


class ListTest(Base):
    def test_without_a_broker_nothing_is_listed_and_admission_fails(self):
        self.broker.failing["target"] = BrokerMissing("broker /usr/local/libexec/x: missing")
        self.assertEqual(self.host.list(), [])
        self.assertEqual(self.host.rows(), [])
        with self.assertRaisesRegex(SessionError, "inventory unavailable .*x: missing"):
            self.host._capacity_rows()
        self.assertEqual(self.docker.order, [])

    def test_a_failing_target_is_reported_not_hidden(self):
        self.broker.failing["target"] = SessionError("broker: invalid target setting")
        with self.assertRaisesRegex(SessionError, "invalid target setting"):
            self.host.list()
        with self.assertLogs("session", "WARNING") as logs:
            self.assertEqual(self.host.rows(), [])
        self.assertIn("docker rows: broker: invalid target setting", logs.output[0])

    def test_damaged_records_leave_healthy_inventory_available(self):
        self.listed("bad", "w")
        self.guarded_record()
        for label, text in BAD_RECORDS.items():
            with self.subTest(record=label):
                self.damaged_record("bad", text)
                self.assertEqual(self.host.list(), ["w"])
                self.assertEqual([row.name for row in self.host.rows()], ["w"])
                self.assertEqual([row.name for row in self.host._capacity_rows()], ["w"])
                self.assertEqual(self.host.worker_liveness("bad", GEN), Liveness.UNKNOWN)
                self.assertIsNotNone(self.host.attach_target("w"))

    def test_admission_inventory_distinguishes_missing_docker_from_empty(self):
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "no docker"))
        with self.assertRaisesRegex(SessionError, "inventory unavailable"):
            self.host._capacity_rows()
        self.host.run = self.docker
        self.targets("local", self.local._replace(id=None, unreachable="not running"))
        with self.assertRaisesRegex(
            SessionError, f"inventory unavailable .*Docker target {re.escape(self.ep)} unreachable"
        ):
            self.host._capacity_rows()
        self.assertEqual(self.order, [])
        self.targets("local", self.local)
        self.docker.down = True
        with self.assertRaisesRegex(Unanswered, "^docker ps: Cannot connect"):
            self.host._capacity_rows()

    def test_admission_inventory_reads_successful_empty_and_populated_results(self):
        self.assertEqual(self.host._capacity_rows(), [])
        self.listed("w")
        self.guarded_record()
        self.assertEqual(
            [(row.name, row.generation) for row in self.host._capacity_rows()], [("w", GEN)]
        )

    def test_list_and_rows_from_labelled_guarded_containers(self):
        self.listed("w", "v")
        self.docker.ps += [
            f"ensemble-x-{GEN}\tnot a name\t{GEN}\trunning",
            f"someone-else\tz\t{GEN}\trunning",
            f"ensemble-y-{GEN.upper()}\ty\t{GEN.upper()}\trunning",
            f"ensemble-u-{GEN}\tu\t{GEN}",
            "bad line",
        ]
        self.guarded_record()
        self.state = os.path.join(self.home, ".ensemble", "docker-state", "v")
        self.record(sid=None, client="other", display=None, kind="tui")
        self.assertEqual(self.host.list(), ["v", "w"])
        before = len(self.docker.order)
        rows = {row.name: row for row in self.host.rows()}
        ran = [call[0] for call in self.docker.order[before:] if isinstance(call, list)]
        self.assertEqual(ran, ["ps"])
        self.assertEqual(
            (rows["w"].sid, rows["w"].client, rows["w"].display, rows["w"].state),
            ("s1", "agent", "lead/below", StatusState.UNKNOWN),
        )
        self.assertEqual((rows["v"].sid, rows["v"].client), (None, "other"))

    def test_only_guarded_rows_are_listed_or_attachable(self):
        self.listed("w", "v", "o", "h")
        self.guarded_record()
        for name, found in (
            ("v", {"mode": "guard"}),  # no kind: an older release's start
            ("o", {"kind": "tui", "generation": "f" * 32}),  # another generation
            ("h", {"kind": "headless"}),
        ):
            self.state = os.path.join(self.home, ".ensemble", "docker-state", name)
            self.record(**found)
        self.assertEqual(self.host.list(), ["w"])
        self.assertEqual([row.name for row in self.host.rows()], ["w"])
        self.assertEqual([row.name for row in self.host._capacity_rows()], ["w"])
        for name in ("v", "o", "h"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(SessionError, f"^no docker session {name}$"),
            ):
                self.host.attach_target(name)

    def test_docker_ps_failure_raises(self):
        for run in (
            mock.Mock(return_value=done(1, "", "Cannot connect")),
            mock.Mock(side_effect=PermissionError(13, "denied")),
        ):
            with self.subTest(run=run):
                self.host.run = run
                with self.assertRaisesRegex(SessionError, "docker ps"):
                    self.host.list()

    def test_a_docker_ps_that_does_not_answer_leaves_attach_target_unanswered(self):
        self.listed("w")
        for run in (
            mock.Mock(side_effect=TimeoutExpired("docker", RUN_TIMEOUT)),
            mock.Mock(return_value=done(1, "", "Cannot connect")),
            mock.Mock(side_effect=PermissionError(13, "denied")),
        ):
            with self.subTest(run=run):
                self.host.run = run
                with self.assertRaisesRegex(Unanswered, "^docker ps: "):
                    self.host.attach_target("w")
                with self.assertRaisesRegex(Unanswered, "^docker ps: "):
                    self.host.list()

    def test_a_daemon_that_does_not_answer_lists_no_rows_but_fails_list(self):
        self.listed("w")
        self.host.run = mock.Mock(side_effect=TimeoutExpired("docker", RUN_TIMEOUT))
        with self.assertLogs("session", "WARNING") as logs:
            self.assertEqual(self.host.rows(), [])
        self.assertIn("docker ps", logs.output[0])
        with self.assertRaisesRegex(SessionError, "docker ps"):
            self.host.list()

    def test_an_unreachable_daemon_or_no_cli_lists_nothing(self):
        self.listed("w")
        self.host.run = mock.Mock(side_effect=FileNotFoundError(2, "no docker"))
        self.assertEqual(self.host.list(), [])
        self.host.run = self.docker
        self.targets("local", self.local._replace(id=None, unreachable="not running"))
        self.assertEqual((self.host.list(), self.host.rows()), ([], []))
        self.assertEqual(self.order, [])

    def test_attach_target_of_an_unknown_or_unguarded_row_is_no_session(self):
        for found in ({}, {"mode": "guard"}, {"kind": "headless"}, {"kind": "tui"}):
            with self.subTest(found=found):
                self.record(**found)
                self.listed(*(() if found == {"kind": "tui"} else ("w",)))
                with self.assertRaisesRegex(SessionError, "^no docker session w$") as caught:
                    self.host.attach_target("w")
                self.assertNotIsInstance(caught.exception, Unanswered)
        self.assertEqual(self.docker.execs(), [])


class AttachTargetTest(Base):
    def test_attach_target_runs_the_guard_display_with_docker_variables_cleared(self):
        self.listed("w")
        self.guarded_record()
        argv = [
            *(DOCKER, "--host", self.ep, "exec", "-it", CONTAINER, PYTHON, "-I"),
            *(worker_module.GUARD, "--attach", f"/run/ensemble/guard/{GEN}/display"),
        ]
        cleared = tuple(arg for key in CLEARED for arg in ("-u", key))
        self.assertTrue(DockerHost.attachable)
        self.assertEqual(
            self.host.attach_target("w"),
            CommandView(
                printed=shlex.join(argv),
                argv=("/usr/bin/env", *cleared, *argv),
                run=f"/run/ensemble/guard/{GEN}/display",
            ),
        )
        with self.assertRaisesRegex(SessionError, "^no docker session x$"):
            self.host.attach_target("x")


class TwoDaemonsTest(Base):
    """REMOTE beside the local Docker Desktop: listing, sweep and liveness on both."""

    def setUp(self):
        super().setUp()
        self.two_daemons()

    def worker(self, docker, name, *, kind="tui"):
        docker.ps.append(f"ensemble-{name}-{GEN}\t{name}\t{GEN}\trunning")
        state = os.path.join(self.home, ".ensemble", "docker-state", name)
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "host.json"), "w") as f:
            json.dump({"generation": GEN, "sid": "s1", "client": "agent", "kind": kind}, f)
        self.recorded(name, self.pc if docker is self.remote else self.local)

    def test_ls_lists_workers_on_both_daemons(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rows = self.host.rows()
        self.assertEqual(([row.name for row in rows], err.getvalue()), (["mini", "w"], ""))
        self.assertEqual(self.host.list(), ["mini", "w"])

    def test_ls_names_an_unreachable_daemon_and_lists_the_other(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.remote.down = True
        with (
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertLogs("session", "WARNING"),
        ):
            rows = self.host.rows()
        self.assertEqual([row.name for row in rows], ["mini"])
        self.assertIn(f"{REMOTE} unreachable; its workers are not listed", err.getvalue())
        with self.assertLogs("session", "WARNING"):
            self.assertEqual(self.host.list(), ["mini"])

    def test_a_daemon_the_target_could_not_reach_is_never_asked(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.targets("remote", self.pc._replace(id=None, unreachable="refused"), self.local)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rows = self.host.rows()
        self.assertEqual([row.name for row in rows], ["mini"])
        self.assertIn(f"{REMOTE} unreachable; its workers are not listed", err.getvalue())
        self.host.sweep("other")
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.broker.targets, 1)

    def test_admission_counts_reachable_daemons(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.remote.down = True
        with self.assertLogs("session", "WARNING"):
            self.assertEqual([row.name for row in self.host._capacity_rows()], ["mini"])

    def test_admission_fails_when_no_daemon_answers(self):
        self.remote.down = self.docker.down = True
        with (
            self.assertLogs("session", "WARNING"),
            self.assertRaisesRegex(SessionError, "inventory unavailable") as caught,
        ):
            self.host._capacity_rows()
        self.assertIn(REMOTE, str(caught.exception))
        self.assertIn(self.ep, str(caught.exception))

    def test_sweep_closes_free_rows_on_both_daemons(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.host.sweep("other")
        self.assertEqual(
            sorted(zip(self.broker.closed, self.broker.close_daemons, strict=True)),
            [(f"ensemble-mini-{GEN}", LOCAL_DAEMON), (f"ensemble-w-{GEN}", REMOTE_DAEMON)],
        )

    def test_a_daemon_that_times_out_is_down_and_the_other_still_answers(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.remote.replies = {}
        run = self.host.run

        def timing_out(argv, env, **kw):
            if argv[2] == REMOTE and argv[3:5] == ["ps", "-a"]:
                raise TimeoutExpired(argv, RUN_TIMEOUT)
            return run(argv, env, **kw)

        self.host.run = timing_out
        with (
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertLogs("session", "WARNING"),
        ):
            self.assertEqual([row.name for row in self.host.rows()], ["mini"])
            self.assertEqual([row.name for row in self.host._capacity_rows()], ["mini"])
            self.host.sweep("other")
        self.assertIn(f"{REMOTE} unreachable", err.getvalue())
        self.assertEqual(self.broker.closed, [f"ensemble-mini-{GEN}"])

    def test_a_worker_on_an_unreachable_daemon_is_not_taken_as_ended(self):
        self.worker(self.remote, "w")
        self.remote.down = True
        with self.assertLogs("session", "WARNING"):
            self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)

    def test_a_session_on_a_daemon_the_target_does_not_list_is_unknown_never_absent(self):
        self.worker(self.docker, "w")
        self.recorded("w", self.pc._replace(id="GONE-ID"))
        self.assertEqual(self.host._row_liveness("w", GEN), Liveness.UNKNOWN)
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)
        self.docker.ps = []
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)
        self.assertEqual(self.docker.execs(), [])

    def test_a_session_s_liveness_looks_only_at_its_own_daemon(self):
        self.worker(self.remote, "w")
        self.docker.ps.append(f"ensemble-w-{GEN}\tw\t{GEN}\trunning")
        self.remote.ps = []
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.ABSENT)
        self.assertEqual(self.docker.execs(), [])

    def test_sweep_leaves_an_unreachable_daemon_alone(self):
        self.worker(self.docker, "mini")
        self.worker(self.remote, "w")
        self.remote.down = True
        with self.assertLogs("session", "WARNING"):
            self.host.sweep("other")
        self.assertEqual(
            (self.broker.closed, self.broker.close_daemons),
            ([f"ensemble-mini-{GEN}"], [LOCAL_DAEMON]),
        )
        self.assertFalse([call for call in self.remote.calls if call[0][0] != "ps"])
        self.remote.down = False
        self.assertIn("w", self.host.list())

    def test_a_local_row_s_liveness_and_display_use_the_local_daemon(self):
        self.worker(self.docker, "mini")
        self.host.attach_target("mini")
        view = self.host.attach_target("mini")
        self.assertEqual(view.argv[view.argv.index(DOCKER) + 2], self.ep)
        self.host._row_liveness("mini", GEN)
        self.assertTrue(self.docker.execs())
        self.assertFalse([c for c in self.remote.calls if c[0][0] == "exec"])


class DaemonChoiceTest(StartBase):
    """A new session's daemon: its environment's, else the target's default (REMOTE)."""

    def setUp(self):
        super().setUp()
        self.two_daemons()

    def manifest(self):
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            return json.load(f)

    def remote_work(self):
        """The remote daemon's calls other than inventory listings."""
        return [call for call, _ in self.remote.calls if call[:2] != ["ps", "-a"]]

    def test_an_environment_naming_local_starts_there_throughout(self):
        handle = self.start(environment=carried(replace(ENVIRONMENT, daemon="local")))
        self.assertEqual(load_record("w"), DaemonRecord(self.local, DOCKER))
        self.assertEqual(self.broker.built_daemons, [LOCAL_DAEMON])
        self.assertEqual(self.manifest()["daemon"], LOCAL_DAEMON)
        [(argv, _, _)] = self.execs
        self.assertEqual(argv[:3], [DOCKER, "--host", self.ep])
        self.assertEqual(self.remote_work(), [])
        self.close_quietly(handle)
        self.handle = None
        self.assertEqual(self.broker.close_daemons, [LOCAL_DAEMON])
        self.assertEqual(self.remote_work(), [])

    def test_a_session_without_a_daemon_takes_the_default(self):
        for environment in (None, carried(ENVIRONMENT)):
            with self.subTest(environment=environment):
                self.broker.built_daemons.clear()
                self.start(environment=environment)
                self.assertEqual(load_record("w"), DaemonRecord(self.pc, DOCKER))
                self.assertEqual(self.broker.built_daemons, [REMOTE_DAEMON])
                self.assertEqual(self.manifest()["daemon"], REMOTE_DAEMON)
                [(argv, _, _), *_] = reversed(self.execs)
                self.assertEqual(argv[:3], [DOCKER, "--host", REMOTE])
                os.unlink(os.path.join(self.state, RECORD_FILE))

    def test_a_new_remote_start_runs_only_the_docker_calls_it_needs(self):
        self.start()
        ps = ["ps", "-a", "--filter", "label=ensemble.session", "--format", PS_FORMAT]
        self.assertEqual(
            [call for call, _ in self.remote.calls],
            [
                ps,
                ps,
                ps,
                [
                    *("volume", "ls", "--filter", "label=ensemble.kind", "--format"),
                    '{{.Name}}\t{{.Label "ensemble.workspace"}}',
                ],
            ],
        )
        self.assertEqual([call for call, _ in self.docker.calls], [ps, ps, ps])
        self.assertFalse([c for c in self.order if isinstance(c, list) and "info" in c])
        self.assertEqual(self.broker.targets, 1)

    def test_an_unreachable_remote_default_falls_back_to_the_local_daemon(self):
        why = "Docker daemon not running"
        self.targets("remote", self.pc._replace(id=None, unreachable=why), self.local)
        notice = (
            f"ensemble-session: docker: {REMOTE} unreachable ({why});"
            f" starting on the local Docker Desktop ({self.ep})\n"
        )
        for check in (
            lambda: self.host.validate("w", self.launch),
            lambda: self.host._preflight("w", self.launch, self.METADATA),
            lambda: self.host._validate("w", self.launch, self.METADATA),
        ):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                check()
            self.assertIn(notice, err.getvalue())
        self.assertIsNone(load_record("w"))
        self.broker.built_daemons.clear()
        for environment in (None, carried(ENVIRONMENT)):
            with self.subTest(environment=environment):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.start(environment=environment)
                self.assertIn(notice, err.getvalue())
                self.assertEqual(load_record("w"), DaemonRecord(self.local, DOCKER))
                self.assertEqual(self.manifest()["daemon"], LOCAL_DAEMON)
                self.assertEqual(self.remote_work(), [])
                os.unlink(os.path.join(self.state, RECORD_FILE))
        self.assertEqual(set(self.broker.built_daemons), {LOCAL_DAEMON})

    def test_with_both_daemons_unreachable_a_new_start_fails_on_the_remote_s_reason(self):
        why = "host key unknown: not in ~/.ssh/known_hosts"
        self.targets(
            "remote",
            self.pc._replace(id=None, unreachable=why),
            self.local._replace(id=None, unreachable="Docker daemon not running"),
        )
        want = f"^broker: Cannot reach Docker target {REMOTE}: {re.escape(why)}\\.$"
        for check in (
            lambda: self.host.validate("w", self.launch),
            lambda: self.host._preflight("w", self.launch, self.METADATA),
            lambda: self.host._validate("w", self.launch, self.METADATA),
            self.start,
        ):
            with self.assertRaisesRegex(BrokerError, want) as raised:
                check()
            self.assertEqual(raised.exception.code, "docker_unreachable")
        self.assertEqual(self.broker.built_daemons, [])
        self.assertNotIn("start", [o[0] for o in self.order if isinstance(o, tuple)])
        self.assertIsNone(load_record("w"))

    def test_a_preflight_builds_on_the_default_and_records_nothing(self):
        self.host._preflight("w", self.launch, self.METADATA)
        self.host._validate("w", self.launch, self.METADATA)
        self.assertEqual(self.broker.built_daemons, [REMOTE_DAEMON, REMOTE_DAEMON])
        self.assertIsNone(load_record("w"))

    def test_a_start_failing_in_transport_closes_on_the_chosen_daemon(self):
        self.broker.error = SessionError("broker: start timed out after 1800 s")
        with self.assertRaisesRegex(SessionError, "start timed out"):
            self.start()
        self.assertEqual(
            (self.broker.closed, self.broker.close_daemons), ([CONTAINER], [REMOTE_DAEMON])
        )


class ConfigChangedTest(StartBase):
    """A session recorded on REMOTE once the target lists only the local Docker Desktop: close
    and reclaim send its recorded ID, which the broker refuses.
    """

    NOT_CONFIGURED = BrokerError(
        7,
        "docker_unreachable",
        f"Docker daemon {REMOTE_DAEMON} is not configured; set the Docker target back to reach it.",
        "not_needed",
        None,
    )

    def setUp(self):
        super().setUp()
        self.two_daemons()
        self.close_quietly(self.start())
        self.handle = None
        with open(os.path.join(self.state, f"{GEN}.json")) as f:
            self.assertEqual(json.load(f)["daemon"], REMOTE_DAEMON)
        self.targets("local", self.local)
        self.docker.ps = [f"{CONTAINER}\tw\t{GEN}\trunning"]  # a namesake on the local daemon
        self.docker.volumes = [f"ensemble-ws-w\t{self.cwd}"]
        self.order.clear()
        self.broker.closed.clear()
        self.broker.close_daemons.clear()
        self.docker.calls.clear()
        self.remote.calls.clear()

    def test_release_closes_by_the_recorded_id(self):
        with mock.patch.object(self.host, "_spawn_reclaim"):
            self.host._release("w", GEN)
        self.assertEqual(
            (self.broker.closed, self.broker.close_daemons), ([CONTAINER], [REMOTE_DAEMON])
        )
        self.assertEqual((self.docker.calls, self.remote.calls), ([], []))

    def test_release_fails_naming_the_id_the_broker_refuses(self):
        self.broker.failing["close"] = self.NOT_CONFIGURED
        with self.assertRaisesRegex(BrokerError, f"^broker: Docker daemon {REMOTE_DAEMON} is not"):
            self.host._release("w", GEN)
        self.assertEqual(self.broker.close_daemons, [REMOTE_DAEMON])
        self.assertEqual((self.docker.calls, self.remote.calls), ([], []))

    def test_reclaim_removes_by_the_recorded_id(self):
        self.assertEqual(self.host.remove_workspaces(self.cwd), ["ensemble-ws-w"])
        self.assertEqual(self.broker.rm_daemons, [REMOTE_DAEMON])

    def test_reclaim_fails_naming_the_id_the_broker_refuses(self):
        self.broker.failing["workspace_rm"] = self.NOT_CONFIGURED
        with self.assertRaisesRegex(
            SessionError,
            f"^docker workspace ensemble-ws-w kept: broker: Docker daemon {REMOTE_DAEMON} is not"
            " configured",
        ):
            self.host.remove_workspaces(self.cwd)
        self.assertEqual(self.broker.rm_daemons, [REMOTE_DAEMON])
        listing = ["volume", "ls", "--filter", "label=ensemble.kind"]
        self.assertEqual([call[:4] for call, _ in self.docker.calls], [listing])
        self.assertEqual(self.remote.calls, [])

    def test_its_liveness_is_unknown_and_its_namesake_untouched(self):
        self.assertEqual(self.host.worker_liveness("w", GEN), Liveness.UNKNOWN)
        self.assertEqual(self.docker.execs(), [])

    def test_a_malformed_record_fails_release_naming_it(self):
        path = os.path.join(self.state, RECORD_FILE)
        with open(path, "w") as f:
            f.write("{}")
        with self.assertRaisesRegex(
            SessionError, f"^docker session w: daemon record: .*{re.escape(path)}"
        ):
            self.host._release("w", GEN)
        self.assertEqual(self.broker.closed, [])
        self.assertEqual(self.docker.calls, [])


class TwoDaemonWorkspacesTest(Base):
    """Workspaces on REMOTE and on the local Docker Desktop beside it."""

    def setUp(self):
        super().setUp()
        self.two_daemons()
        self.agent = os.path.join(self.account, "work", "TASK-1", "agent")
        os.makedirs(self.agent)
        self.kept = os.path.join(self.home, ".ensemble", "docker-state", ".kept")

    def session(self, name, found):
        state = os.path.join(self.home, ".ensemble", "docker-state", name)
        os.makedirs(state)
        with open(os.path.join(state, "host.json"), "w") as f:
            json.dump({"generation": GEN, "kind": "tui"}, f)
        manifest = {"name": name, "generation": GEN, "cwd": self.agent, "daemon": found.id}
        with open(os.path.join(state, f"{GEN}.json"), "w") as f:
            json.dump(manifest, f)
        self.recorded(name, found)

    def removals(self):
        return list(
            zip(
                [step[1] for step in self.order if step[:1] == ("workspace_rm",)],
                self.broker.rm_daemons,
                strict=True,
            )
        )

    def test_removes_each_session_s_volume_on_its_own_daemon(self):
        self.session("mini-a", self.local)
        self.session("pm-b", self.pc)
        self.docker.volumes = [f"ensemble-ws-mini-a\t{self.agent}"]
        self.remote.volumes = [f"ensemble-ws-pm-b\t{self.agent}"]
        removed = self.host.remove_workspaces(self.agent)
        self.assertEqual(sorted(removed), ["ensemble-ws-mini-a", "ensemble-ws-pm-b"])
        self.assertEqual(
            sorted(self.removals()),
            [("ensemble-ws-mini-a", LOCAL_DAEMON), ("ensemble-ws-pm-b", REMOTE_DAEMON)],
        )

    def test_keeps_a_volume_on_a_daemon_its_session_is_not_recorded_on(self):
        self.session("pm-c", self.pc)
        self.docker.volumes = [f"ensemble-ws-pm-c\t{self.agent}"]
        self.assertEqual(self.host.remove_workspaces(self.agent), [])
        self.assertEqual(self.removals(), [])

    def test_an_unreachable_daemon_s_workspaces_are_unknown(self):
        self.targets("remote", self.pc._replace(id=None, unreachable="refused"), self.local)
        with self.assertRaisesRegex(SessionError, f"workspaces unknown .*{REMOTE} unreachable"):
            self.host.remove_workspaces(self.agent)
        self.assertEqual(self.removals(), [])

    def test_reclaim_leaves_an_unreachable_daemon_s_volumes_and_markers(self):
        gone = os.path.join(self.account, "gone")
        self.docker.volumes = [f"ensemble-ws-local-gone\t{gone}"]
        self.remote.volumes = [f"ensemble-ws-remote-gone\t{gone}"]
        os.makedirs(self.kept)
        stale = os.path.join(self.kept, f"ensemble-ws-remote-kept@{REMOTE_DAEMON}")
        with open(stale, "w") as f:
            f.write("dirty")
        self.targets("remote", self.pc._replace(id=None, unreachable="refused"), self.local)
        self.host.reclaim_volumes()
        self.assertEqual(self.removals(), [("ensemble-ws-local-gone", LOCAL_DAEMON)])
        self.assertTrue(os.path.exists(stale))
        self.assertEqual(self.remote.calls, [])
        self.targets("remote", self.pc, self.local)
        self.order.clear()
        self.broker.rm_daemons.clear()
        self.host.reclaim_volumes()
        self.assertEqual(
            sorted(self.removals()),
            [("ensemble-ws-local-gone", LOCAL_DAEMON), ("ensemble-ws-remote-gone", REMOTE_DAEMON)],
        )
        self.assertFalse(os.path.exists(stale))

    def test_a_kept_volume_s_marker_is_its_daemon_s_whatever_the_default(self):
        gone = os.path.join(self.account, "gone")
        self.docker.volumes = [f"ensemble-ws-same\t{gone}"]
        self.remote.volumes = [f"ensemble-ws-same\t{gone}"]
        self.broker.failing = {
            "workspace_rm": BrokerError(13, "workspace_dirty", "unsaved", "not_needed", None)
        }
        markers = [
            ".lock",
            f"ensemble-ws-same@{LOCAL_DAEMON}",
            f"ensemble-ws-same@{REMOTE_DAEMON}",
        ]
        with self.assertLogs("session", "WARNING"):
            self.host.reclaim_volumes()
        self.assertEqual(sorted(os.listdir(self.kept)), markers)
        self.assertEqual(
            sorted(self.removals()),
            [("ensemble-ws-same", LOCAL_DAEMON), ("ensemble-ws-same", REMOTE_DAEMON)],
        )
        self.order.clear()
        self.broker.rm_daemons.clear()
        self.host = DockerHost("docker", {"docker": DOCKER}, run=self.docker, broker=self.broker)
        self.targets("local", self.pc, self.local)
        with self.assertNoLogs("session", "WARNING"):
            self.host.reclaim_volumes()
        self.assertEqual(sorted(os.listdir(self.kept)), markers)
        self.assertEqual(self.removals(), [])


class RemoteAttachTargetTest(Base):
    def setUp(self):
        super().setUp()
        self.two_daemons()

    def test_attach_target_on_a_remote_daemon_carries_its_path(self):
        self.remote.ps = [f"{CONTAINER}\tw\t{GEN}\trunning"]
        self.guarded_record()
        self.recorded("w", self.pc)
        argv = [
            *(DOCKER, "--host", REMOTE, "exec", "-it", CONTAINER, PYTHON, "-I"),
            *(worker_module.GUARD, "--attach", f"/run/ensemble/guard/{GEN}/display"),
        ]
        cleared = tuple(arg for key in CLEARED for arg in ("-u", key))
        path = f"PATH={self.pc.path}"
        for sock, entries in (
            ("/private/agent.sock", (path, "SSH_AUTH_SOCK=/private/agent.sock")),
            ("", (path,)),
        ):
            with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock, "PATH": "/tmux/server"}):
                view = self.host.attach_target("w")
            self.assertEqual(view.argv, ("/usr/bin/env", *cleared, *entries, *argv))
            self.assertEqual(view.printed, shlex.join(argv))


class GuardedStartTest(StartBase):
    def test_the_guard_takes_the_hosts_hidden_size(self):
        self.host = DockerHost(
            "docker",
            {"docker": DOCKER, "hidden_size": "100x30"},
            run=self.docker,
            broker=self.broker,
        )
        handle = self.start()
        [(_, _, proc)] = self.execs
        self.assertEqual(proc.handoff["size"], "100x30")
        handle.close()

    def test_owned_terminal_attaches_to_the_main_process_guard_without_tmux(self):
        handle = self.start()
        self.assertIsInstance(handle, worker_module.DockerWorker)
        self.assertTrue(handle.remote)
        [(argv, kw, proc)] = self.execs
        self.assertEqual(
            argv, [DOCKER, "--host", self.ep, "attach", "--sig-proxy=false", CONTAINER]
        )
        self.assertEqual(kw["env"], docker_call(DOCKER, daemon=self.local)[1])
        self.assertEqual(
            proc.handoff,
            {
                "argv": ["with-secrets", "--client", "agent", "agent", "--flag"],
                "env": {"AGENT_HOME": self.home_dir, "K": "v"},
                "terminal": True,
                "generation": GEN,
                "size": "80x24",
            },
        )
        self.assertEqual((kw["stderr"], kw["stdin"], kw["stdout"]), (DEVNULL, PIPE, PIPE))
        self.assertTrue(kw["close_fds"] and kw["start_new_session"])
        self.assertNotIn("pass_fds", kw)
        execs = self.docker.execs()
        self.assertFalse(any("tmux" in call for call in execs), execs)
        self.assertFalse(any(worker_module.GUARD in call for call in execs), execs)
        self.assertFalse([c for c in self.order if isinstance(c, list) and c[:1] == ["start"]])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "popen" for c in self.order))
        self.assertEqual(self.host_record()["kind"], "tui")
        self.assertNotIn("mode", self.host_record())
        self.assertFalse(_free(os.path.join(self.state, "guard.lock")))
        self.assertEqual((self.host_record()["sid"], handle.generation), ("s1", GEN))
        handle.close()
        self.assertIn(CONTAINER, self.broker.closed)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        self.assertEqual(handle.state(), Liveness.ABSENT)
        self.assertEqual(handle.returncode(), 0)

    def test_the_terminal_path_relies_on_the_tui_lock_its_store_holds(self):
        lock = tui_lock("w")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with inode_lock(lock):
            self.start()
            with self.assertRaisesRegex(SessionError, "^docker session w: busy$"):
                self.host.worker("w", self.launch, terminal=False, metadata=self.METADATA)

    def test_a_start_failing_after_its_handle_closes_the_owner_lock_once(self):
        class Broken(GuardAttach):
            def write(self, kind, value):
                super().write(b"z" if kind == b"s" else kind, value)

        def popen(argv, **_):
            if argv[3:4] == ["attach"]:
                proc = Broken(argv)
                self.addCleanup(proc.exit, 0)
                return proc
            return mock.Mock(pid=4321)

        self.popen_mock.side_effect = popen
        spare = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(_close, spare)
        with self.assertRaisesRegex(SessionError, "^docker session w: "):
            self.start()
        self.assertIn(CONTAINER, self.broker.closed)
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        os.fstat(spare)

    def test_a_held_owner_lock_is_busy_before_any_container(self):
        self.owner_held()
        with self.assertRaises(SessionBusy):
            self.start()
        self.assertNotIn(("start", os.path.join(self.state, f"{GEN}.json"), ()), self.order)
        self.assertEqual(self.execs, [])


class WorkerLivenessTest(StartBase):
    def test_start_reclaims_a_pre_upgrade_container(self):
        self.record()
        self.listed("w")

        def close(container, **_):
            self.broker.closed.append(container)
            self.docker.ps = []
            return True

        self.broker.close = close
        handle = self.start()
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertEqual(len(self.execs), 1)
        handle.close()

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
                    self.start()
                self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        self.probe_says("exited")
        self.docker.ps = []
        self.listed("w", gen=old)

        def close(container, **_):
            self.broker.closed.append(container)
            self.docker.ps = []
            return True

        self.broker.close = close
        self.start()
        self.assertEqual(self.broker.closed[0], f"ensemble-w-{old}")
        self.assertEqual(len(self.execs), 1)

    def test_a_debris_row_is_left_to_the_container_refusal(self):
        self.listed("w")
        self.record()
        refusal = "container ensemble-w-.* of an earlier start is still up"
        with self.assertRaisesRegex(SessionError, f"^docker session w: {refusal}"):
            self.start()
        self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
        self.assertEqual(self.execs, [])

    def test_damaged_own_records_refuse_start_without_cleanup_or_overwrite(self):
        self.listed("w")
        for label, text in BAD_RECORDS.items():
            with self.subTest(record=label):
                path = self.damaged_record("w", text)
                with self.assertRaises(SessionBusy) as raised:
                    self.start()
                self.assertIn(path, str(raised.exception))
                with open(path) as f:
                    self.assertEqual(f.read(), text)
                self.assertTrue(_free(os.path.join(self.state, "guard.lock")))
                self.assertEqual(self.broker.closed, [])
                self.assertEqual(self.execs, [])

    def test_damaged_own_records_block_name_without_container(self):
        for label, text in BAD_RECORDS.items():
            with self.subTest(record=label):
                path = self.damaged_record("w", text)
                with self.assertRaises(SessionBusy) as raised:
                    self.start()
                self.assertIn(path, str(raised.exception))
                with open(path) as f:
                    self.assertEqual(f.read(), text)
                self.assertEqual(self.broker.closed, [])
                self.assertEqual(self.execs, [])

    def test_other_damaged_records_allow_start_and_close(self):
        for label, text in BAD_RECORDS.items():
            with self.subTest(record=label):
                self.damaged_record("bad", text)
                self.listed("bad")
                handle = self.start()
                self.assertNotIn(f"ensemble-bad-{GEN}", self.broker.closed)
                before = self.broker.closed.count(CONTAINER)
                handle.close()
                self.assertEqual(self.broker.closed.count(CONTAINER), before + 1)
                self.assertTrue(_free(os.path.join(self.state, "guard.lock")))


class GuardedRowTest(StartBase):
    def test_guarded_rows_are_listed_with_lock_and_activity_and_no_exec(self):
        self.listed("w")
        self.guarded_record()
        self.order.clear()
        [row] = self.host.rows()
        self.assertEqual(row.state, StatusState.UNKNOWN)
        self.owner_held()
        folder = os.path.join(os.path.expanduser("~/.ensemble/tui"), "w")
        os.makedirs(folder, 0o700, exist_ok=True)
        with open(os.path.join(folder, "activity"), "w") as f:
            f.write(f"{GEN} blocked\n")
        [row] = self.host.rows()
        self.assertEqual((row.name, row.state, row.last_lines), ("w", StatusState.BLOCKED, None))
        self.assertEqual(self.host.list(), ["w"])
        self.assertEqual(self.docker.execs(), [])

    def test_an_ownerless_stopped_container_is_dead(self):
        self.guarded_record()
        for state, want in (
            ("created", StatusState.DEAD),
            ("exited", StatusState.DEAD),
            ("dead", StatusState.DEAD),
            ("running", StatusState.UNKNOWN),
            ("paused", StatusState.UNKNOWN),
            ("restarting", StatusState.UNKNOWN),
        ):
            with self.subTest(state=state):
                self.listed("w", state=state)
                self.assertEqual([row.state for row in self.host.rows()], [want])
                self.assertEqual([row.state for row in self.host._capacity_rows()], [want])
        self.owner_held()
        self.listed("w", state="exited")
        self.assertEqual([row.state for row in self.host.rows()], [StatusState.WORKING])

    def test_an_ownerless_running_container_is_dead_to_capacity_once_its_guard_exited(self):
        self.guarded_record()
        self.listed("w")
        for word, rc, want in (
            ("exited", 0, StatusState.DEAD),
            ("absent", 0, StatusState.DEAD),
            ("live", 0, StatusState.UNKNOWN),
            ("dead", 0, StatusState.UNKNOWN),
            ("exited", 1, StatusState.UNKNOWN),
        ):
            with self.subTest(word=word, rc=rc):
                self.probe_says(word, rc)
                self.order.clear()
                self.assertEqual([row.state for row in self.host._capacity_rows()], [want])
                self.assertEqual(len(self.docker.execs()), 1)
                self.order.clear()
                self.assertEqual([row.state for row in self.host.rows()], [StatusState.UNKNOWN])
                self.assertEqual(self.docker.execs(), [])
        self.docker.replies[(PYTHON, "-I", worker_module.GUARD, "--probe")] = TimeoutExpired(
            "docker", 10
        )
        self.assertEqual([row.state for row in self.host._capacity_rows()], [StatusState.UNKNOWN])
        self.probe_says("exited")
        self.owner_held()
        self.order.clear()
        self.assertEqual([row.state for row in self.host._capacity_rows()], [StatusState.WORKING])
        self.assertEqual(self.docker.execs(), [])

    def test_a_held_worker_is_live_elsewhere_and_released_only_by_its_owner(self):
        handle = self.start()
        self.listed("w")
        other = DockerHost("docker", {"docker": DOCKER}, run=self.docker, broker=self.broker)
        self.assertEqual(other.worker_liveness("w", GEN), Liveness.LIVE)
        with self.assertRaisesRegex(SessionError, r"\Adocker session w: owned by its harness\Z"):
            other._release("w", GEN)
        self.assertEqual(self.broker.closed, [])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "popen" for c in self.order))
        handle.close()
        self.assertEqual(self.broker.closed, [CONTAINER])

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


class AdmissionTest(StartBase):
    def setUp(self):
        super().setUp()
        self.guarded_record()
        self.runs = os.path.join(self.home, ".ensemble", "client-runs", "agent")
        os.makedirs(self.runs)
        self.held_run = os.path.join(self.runs, f"{GEN}.run")  # its run's, named by generation
        with open(self.held_run, "w") as f:
            json.dump({"kind": "tui", "name": "w", "host": "docker"}, f)

    def admit(self):
        with Admission(
            "agent",
            1,
            label="n",
            mode=Mode.TUI,
            hosts={"docker": self.host},
            host="docker",
        ):
            pass

    def test_a_killed_generation_frees_its_slot_once_its_container_is_stopped(self):
        self.listed("w", state="exited")
        self.admit()
        self.assertFalse(os.path.exists(self.held_run))

    def test_a_killed_generation_frees_its_slot_once_its_guard_exited_in_a_running_container(self):
        self.listed("w")
        self.probe_says("exited")
        self.admit()
        self.assertFalse(os.path.exists(self.held_run))

    def test_a_running_or_owned_generation_keeps_its_slot(self):
        self.probe_says("live")
        for state, owned in (("running", False), ("exited", True)):
            with self.subTest(state=state, owned=owned):
                self.listed("w", state=state)
                if owned:
                    self.owner_held()
                with self.assertRaisesRegex(SessionError, "reached max_runs limit 1; held by: w"):
                    self.admit()
                self.assertTrue(os.path.exists(self.held_run))

    def test_a_swept_generation_frees_its_slot(self):
        self.docker.ps = []
        self.admit()
        self.assertFalse(os.path.exists(self.held_run))


class Release(StartBase):
    """_release: a dead generation's container goes once its owner lock is free."""

    def test_that_generations_container_is_removed_once_its_owner_lock_is_free(self):
        self.listed("w")
        self.host._release("w", GEN)
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertEqual(self.docker.execs(), [])

    def test_a_held_owner_lock_refuses_removing_nothing(self):
        self.listed("w")
        self.owner_held()
        with self.assertRaisesRegex(SessionError, r"\Adocker session w: owned by its harness\Z"):
            self.host._release("w", GEN)
        self.assertEqual(self.broker.closed, [])

    def test_another_generations_container_or_none_is_left(self):
        self.listed("w", gen="e" * 32)
        self.host._release("w", GEN)
        self.docker.ps = []
        self.host._release("w", GEN)
        self.assertEqual(self.broker.closed, [])

    def test_no_lingering_guard_so_no_client_reads_as_exited(self):
        self.listed("w")
        self.assertFalse(self.host._client_exited("w", GEN))


class HeadlessWorkerTest(StartBase):
    """A headless run's guarded worker: started directly, kind headless, never listed as a tui."""

    def headless(self, **metadata):
        handle = self.host.worker(
            "w", self.launch, terminal=False, metadata={**self.METADATA, **metadata}
        )
        self.addCleanup(self.close_quietly, handle)
        return handle

    def test_a_headless_worker_records_its_kind_and_is_never_listed_or_attachable(self):
        handle = self.headless()
        [(_, _, proc)] = self.execs
        self.assertEqual(
            proc.handoff,
            {
                "argv": ["with-secrets", "--client", "agent", "agent", "--flag"],
                "env": {"AGENT_HOME": self.home_dir, "K": "v"},
                "terminal": False,
                "generation": GEN,
                "size": "80x24",
            },
        )
        self.assertEqual(self.host_record()["kind"], "headless")
        self.assertNotIn("mode", self.host_record())
        self.listed("w")
        self.assertEqual(self.host.list(), [])
        self.assertEqual(self.host.rows(), [])
        self.assertEqual(self.host._capacity_rows(), [])
        with self.assertRaisesRegex(SessionError, "^no docker session w$"):
            self.host.attach_target("w")
        handle.close()
        self.assertIn(CONTAINER, self.broker.closed)

    def test_a_held_owner_lock_refuses_either_kind(self):
        handle = self.headless()
        with self.assertRaises(SessionBusy):
            self.start()
        self.assertEqual(len(self.execs), 1)
        handle.close()
        self.start()
        with self.assertRaises(SessionBusy):
            self.headless()
        self.assertEqual(len(self.execs), 2)

    def test_a_direct_headless_start_drops_its_tui_lock_file_without_a_session(self):
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        self.headless()
        self.assertFalse(os.path.lexists(tui_lock("w")))

    def test_a_headless_environment_worker_runs_maintenance(self):
        self.headless(environment=carried(ENVIRONMENT))
        manifest = os.path.join(self.state, f"{GEN}.json")
        deadline = time.monotonic() + 5
        while ("maintain", CONTAINER, manifest) not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(("maintain", CONTAINER, manifest), self.order)
        self.assertEqual(self.host_record()["kind"], "headless")
        self.assertEqual(self.host_record()["environment"], ENVIRONMENT.persisted())

    def test_a_headless_environment_worker_closes_at_its_deadline(self):
        reason = "Client authentication expired; run ensemble-broker login, then resume."
        self.broker.started = replace(
            self.broker.started,
            deadline={"at": "2000-01-01T00:00:00Z", "action": "close", "reason": reason},
        )
        expired = threading.Event()
        original = self.broker.close

        def close(container, *, daemon):
            result = original(container, daemon=daemon)
            expired.set()
            return result

        self.broker.close = close
        with self.assertLogs("session", "WARNING") as logs:
            handle = self.headless(environment=carried(ENVIRONMENT))
            self.assertTrue(expired.wait(5))
        self.assertEqual(self.host_record()["kind"], "headless")
        self.assertEqual(
            logs.output, [f"WARNING:session:docker session w: client_auth_expired {reason}"]
        )
        self.assertEqual(self.broker.closed, [CONTAINER])
        handle.close()
        self.assertEqual(self.broker.closed, [CONTAINER])
        self.assertEqual((handle.state(), handle.returncode()), (Liveness.ABSENT, 0))

    def test_a_harnessed_headless_worker_records_its_harness_and_host(self):
        os.unlink(os.path.join(self.home, ".ensemble", "tui", "w", "session.json"))
        harness = os.path.join(self.home, ".ensemble", "harness", "a" * 32)
        self.headless(harness=harness)
        self.assertEqual(
            {key: self.host_record()[key] for key in ("owner", "host", "sid", "generation")},
            {"owner": harness, "host": "docker", "sid": "s1", "generation": GEN},
        )
        self.assertFalse(os.path.lexists(tui_lock("w")))

    def test_a_direct_headless_or_tui_worker_records_no_owner(self):
        self.headless().close()
        self.assertIsNone(self.host_record()["owner"])
        self.start().close()
        self.assertNotIn("owner", self.host_record())


class HeadlessOwnerTest(Base):
    """_headless_owner: a named headless run's harness while its owner lock is held; no docker."""

    def setUp(self):
        super().setUp()
        self.harness = os.path.join(self.home, ".ensemble", "harness", "a" * 32)

    def held(self, **changes):
        self.record(**{"kind": "headless", "owner": self.harness, "host": "docker", **changes})
        if not hasattr(self, "lock"):
            self.lock = os.open(os.path.join(self.state, "guard.lock"), os.O_RDWR | os.O_CREAT)
            self.addCleanup(_close, self.lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX)

    def test_a_held_headless_record_names_its_owner_sid_and_generation(self):
        self.held()
        self.assertEqual(
            self.host._headless_owner("w"),
            {"owner": self.harness, "sid": "s1", "generation": GEN},
        )
        self.assertEqual(self.order, [])

    def test_no_owner_otherwise(self):
        for changes in (
            {"kind": "tui"},
            {"host": "other"},
            {"owner": None},
            {"owner": "/tmp/" + "a" * 32},
            {"owner": os.path.join(self.home, ".ensemble", "harness", "x")},
            {"owner": os.path.join(self.home, ".ensemble", "harness", "a" * 32, "..", "b" * 32)},
        ):
            with self.subTest(changes=changes):
                self.held(**changes)
                self.assertIsNone(self.host._headless_owner("w"))
        self.assertIsNone(self.host._headless_owner("absent"))
        self.assertEqual(self.order, [])

    def test_a_free_owner_lock_has_no_owner(self):
        self.record(kind="headless", owner=self.harness, host="docker")
        self.assertIsNone(self.host._headless_owner("w"))
        self.assertIsNone(Host._headless_owner(self.host, "w"))
