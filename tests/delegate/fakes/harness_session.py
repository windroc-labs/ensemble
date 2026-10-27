"""Process-free Session, Hosts, drive control and Viewer for HarnessDelegate's ownership and view
contracts. DrivenDelegate's drive is a FakeControl: a tui run's spawned drive starts its agent
session as the drive would, unless harness_starts_agent is False.
"""

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import replace

from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.types import Layout
from ensemble.session import (
    Access,
    EnvironmentSpec,
    Exited,
    Host,
    Launch,
    Mode,
    NotShowable,
    Resumable,
    Session,
    SessionBusy,
    SessionError,
    SessionStatus,
)
from ensemble.session import Spec as SessionSpec
from ensemble.session.engine.types import Liveness
from ensemble.utils import Record, append_line, load_class, read_regular, save
from tests.delegate.fakes.session import SessionClass


class RecordingProcess:
    """A RecordingHost's agent session: its liveness, closes and Record."""

    def __init__(self, name, *, display=None, record=None):
        self.name, self.display_value, self.record_value = name, display, record
        self.live = True
        self.closed = 0
        self.sid = None if record is None else record.sid

    def session_identity(self):
        return self.sid, None

    def alive(self):
        return self.live

    def close(self):
        self.closed += 1
        self.live = False

    def wait_exit(self, timeout):
        return not self.live

    def kill(self):
        self.live = False

    def record(self):
        return self.record_value

    def publish(self, record):
        self.record_value = record

    def display(self):
        return self.display_value


class RecordingHost(Host):
    """HarnessSession's agent sessions by (host, name): `run` records each launch, `process`
    finds one.
    """

    processes: dict[tuple[str, str], RecordingProcess] = {}
    launches: list[dict] = []

    def run(self, name, launch, *, display=None):
        process = RecordingProcess(name, display=display)
        self.processes[self.name, name] = process
        self.launches.append({"host": self.name, "name": name, "argv": list(launch.argv)})
        return process

    def process(self, name):
        if (self.name, name) not in self.processes:
            raise SessionError(f"no {self.name} session {name}")
        return self.processes[self.name, name]

    def list(self):
        return [
            name
            for (host, name), process in self.processes.items()
            if host == self.name and process.alive()
        ]


class FakeControl:
    """ProcessControl's drive surface, on disk under HOME so other processes see it: each control
    a ~/.ensemble/control-fake/apm-group-* dir holding state.json, {"alive", "launch", ...}. spawn
    stands in for HarnessDelegate._spawn: it records the launch (spawn_error raises instead) and
    starts the drive (ends_at_start: one that ends as it starts, after calling `ending`). stubborn: "term" outlives SIGTERM,
    "kill" SIGKILL too.
    """

    harness_starts_agent = True
    ends_at_start = False
    ending = None
    spawn_error: BaseException | None = None
    stubborn: str | None = None

    def __init__(self, path):
        self.path = path

    @staticmethod
    def root():
        return os.path.expanduser("~/.ensemble/control-fake")

    @classmethod
    def reset(cls):
        shutil.rmtree(cls.root(), ignore_errors=True)
        cls.harness_starts_agent, cls.spawn_error, cls.stubborn = True, None, None
        cls.ends_at_start, cls.ending = False, None

    @classmethod
    def prepare(cls, argv, cwd, *, env=None, blocked_signals=()):
        os.makedirs(cls.root(), exist_ok=True)
        control = cls(tempfile.mkdtemp(prefix="apm-group-", dir=cls.root()))
        launch = {"argv": list(argv), "cwd": cwd, "env": dict(env or {})}
        control._save(alive=False, launch={**launch, "blocked_signals": tuple(blocked_signals)})
        return control

    @classmethod
    def attach(cls, path):
        return cls(path)

    @classmethod
    def spawn(cls, control, launch, log):
        if cls.spawn_error is not None:
            raise cls.spawn_error
        workdir = os.path.dirname(launch.cwd)
        with open(os.path.join(launch.cwd, "delegate.json"), encoding="utf-8") as file:
            state = json.load(file)
        control._save(
            alive=True,
            launch={
                **control.data()["launch"],
                "name": state["harness_name"],
                "display": state["display"],
                "events": state["events"],
                "log": log,
            },
        )
        append_line(os.path.join(cls.root(), "launches.jsonl"), json.dumps(control.path) + "\n")
        if cls.harness_starts_agent and state["spec"]["mode"] == Mode.TUI:
            _start_agent(workdir, state)
        if cls.ends_at_start:
            if cls.ending is not None:
                cls.ending()
            control.close()

    @classmethod
    def launches(cls) -> list[dict]:
        """Each spawned drive's launch, in order."""
        try:
            with open(os.path.join(cls.root(), "launches.jsonl"), encoding="utf-8") as file:
                paths = [json.loads(line) for line in file]
        except FileNotFoundError:
            return []
        return [cls(path).data()["launch"] for path in paths]

    @classmethod
    def named(cls, name) -> "FakeControl":
        """The last spawned drive named `name`."""
        with open(os.path.join(cls.root(), "launches.jsonl"), encoding="utf-8") as file:
            paths = [json.loads(line) for line in file]
        return next(cls(p) for p in reversed(paths) if cls(p).data()["launch"]["name"] == name)

    def data(self) -> dict:
        with contextlib.suppress(FileNotFoundError):
            return json.loads(read_regular(os.path.join(self.path, "state.json")) or b"{}")
        return {}

    def _save(self, **fields):
        save(os.path.join(self.path, "state.json"), json.dumps({**self.data(), **fields}))

    def command(self):
        return ["fake-drive", self.path]

    def wait_started(self):
        pass

    def cancel(self):
        if os.path.isdir(self.path):
            self._save(alive=False, cancelled=True)

    def running(self):
        return self.data().get("alive", False)

    def live(self):
        return self.running()

    def terminate(self):
        self._signal("terminate", self.stubborn is None)

    def kill(self):
        self._signal("kill", self.stubborn != "kill")

    def _signal(self, word, ends):
        if os.path.isdir(self.path):
            data = self.data()
            self._save(signals=[*data.get("signals", []), word], alive=data["alive"] and not ends)

    def close(self):
        """The drive ending."""
        if os.path.isdir(self.path):
            self._save(alive=False)


class DrivenDelegate(HarnessDelegate):
    _controls = FakeControl
    _spawn = staticmethod(FakeControl.spawn)
    _stop_wait = 0.05


def _start_agent(workdir, state):
    layout = Layout(workdir)
    session_cls = (
        Session if state["session_cls"] is None else load_class(state["session_cls"], Session)
    )
    environment = state["plan"]["session"].get("environment")
    spec = SessionSpec(
        workdir=layout.run,
        access=Access(cwd=layout.agent),
        client=state["plan"]["session"]["client"],
        mode=Mode.TUI,
        sid=state["spec"]["sid"],
        name=state["agent_name"],
        environment=None if environment is None else EnvironmentSpec.from_fields(environment),
    )
    session_cls.start(spec, host=state["host"], display=state["display"])


def harness_launches():
    return FakeControl.launches()


class ShowingHost(RecordingHost):
    attachable = True


class FakeViewer:
    """The Viewer's four methods, recorded; attach_command is `<host> attach <name>`."""

    shown: list[tuple[str, str | None, str | None]] = []
    hidden: list[str] = []
    closed: list[str] = []

    def show(self, name, host, *, display=None, command=None):
        self.attach_command(name, host)
        FakeViewer.shown.append((name, display, command))
        return self.attach_command(name, host)

    def hide(self, name, host, *, if_closed=False):
        self.attach_command(name, host)
        FakeViewer.hidden.append(name)

    def attach_command(self, name, host):
        if not host.attachable:
            raise NotShowable(f"host {host.name} cannot show")
        return f"{host.name} attach {name}"

    def close(self, name, host):
        if host.attachable:
            FakeViewer.closed.append(name)

    def layout(self, host, *, display=None):
        return None


class HarnessSession(SessionClass):
    name: str
    hosts: dict[str, RecordingHost] = {}
    host_names: dict[str, str] = {}
    attached: list[tuple[str, str | None]] = []
    inspected: list = []
    history = SessionStatus(Resumable.YES, None, "")

    @classmethod
    def reset(cls, *, showable=False):
        cls.started, cls.live, cls.attached, cls.inspected = [], {}, [], []
        cls.preflights, cls.preflight_error, cls.owns = [], None, False
        cls.host_names = {}
        RecordingHost.processes, RecordingHost.launches = {}, []
        FakeViewer.shown, FakeViewer.hidden, FakeViewer.closed = [], [], []
        kind = ShowingHost if showable else RecordingHost
        cls.hosts = {name: kind(name) for name in ("agent", "harness", "changed")}
        cls.script = ()

    @classmethod
    def host(cls, name=None):
        key = name or "agent"
        if key not in cls.hosts:
            raise SessionError(f"unknown host {key}")
        return cls.hosts[key]

    @classmethod
    def start(cls, spec, *, display=None, host=None, idle_timeout=None):
        session = super().start(spec, display=display, host=host, idle_timeout=idle_timeout)
        name = spec.name or f"{spec.client}-{session.sid[:8]}"
        cls.host_names[name] = host or "agent"
        cls.host(host).run(name, Launch(("fake-agent",), cwd=spec.workdir), display=display)
        return session

    @classmethod
    def attach(cls, name, *, sid=None, host=None, idle_timeout=None):
        cls.attached.append((name, host))
        cls.host(host)
        session = super().attach(name, sid=sid, host=host, idle_timeout=idle_timeout)
        if cls.host_names[name] != (host or "agent"):
            raise SessionError(f"no {host} session {name}")
        return session

    @classmethod
    def inspect(cls, spec):
        cls.inspected.append(spec)
        return cls.history

    @classmethod
    def layout(cls, *, display=None):
        host = cls.host()
        return FakeViewer().layout(host, display=display) if host.attachable else None

    @property
    def showable(self):
        return self.host(self.host_names[self.name]).attachable

    def show(self, *, display=None, command=None):
        host = self.host(self.host_names[self.name])
        return FakeViewer().show(self.name, host, display=display, command=command)

    def hide(self):
        FakeViewer().hide(self.name, self.host(self.host_names[self.name]))

    def status(self):
        return replace(super().status(), hidden=self.name in FakeViewer.hidden)

    def attach_command(self):
        return FakeViewer().attach_command(self.name, self.host(self.host_names[self.name]))

    def publish(self, record):
        self.host(self.host_names[self.name]).process(self.name).publish(record)

    def stop(self):
        self.liveness = Liveness.ABSENT
        self.host(self.host_names[self.name]).process(self.name).close()
        super().stop()

    def events(self):
        for event in super().events():
            if isinstance(event, Exited) and self.mode != "tui":
                self.stop()
            yield event


class DiskProcess:
    """A DiskHost's agent session, on disk so other processes see it."""

    def __init__(self, name, path):
        self.name, self.path = name, path

    def data(self):
        return json.loads(read_regular(self.path) or b"{}")

    def alive(self):
        return self.data().get("alive", False)

    def session_identity(self):
        return self.data().get("sid"), None

    def close(self):
        save(self.path, json.dumps({**self.data(), "alive": False}))

    def wait_exit(self, timeout):
        return not self.alive()

    def kill(self):
        self.close()

    def publish(self, record):
        save(self.path, json.dumps({**self.data(), "record": record.to_line(), "sid": record.sid}))

    def record(self):
        line = self.data().get("record")
        return None if line is None else Record.parse(line)

    def display(self):
        return self.data().get("display")


class DiskHost(RecordingHost):
    def root(self):
        return os.path.expanduser("~/.ensemble/harness-fake")

    def path(self, name):
        return os.path.join(self.root(), f"{self.name}-{name}.json")

    def run(self, name, launch, *, display=None):
        path = self.path(name)
        save(path, json.dumps({"alive": True, "display": display, "record": None, "sid": None}))
        append_line(
            os.path.join(self.root(), "launches.jsonl"),
            json.dumps({"host": self.name, "name": name, "argv": list(launch.argv)}) + "\n",
        )
        return DiskProcess(name, path)

    def process(self, name):
        if not os.path.isfile(self.path(name)):
            raise SessionError(f"no {self.name} session {name}")
        return DiskProcess(name, self.path(name))

    def list(self):
        return []


class PersistentSession(HarnessSession):
    @classmethod
    def host(cls, name=None):
        return DiskHost(name or "agent")


class LockingSession(HarnessSession):
    """A broker lock per environment: a start is busy while another live session holds it; a
    close, a stop or the client's exit (liveness not LIVE) frees it."""

    @classmethod
    def start(cls, spec, *, display=None, host=None, idle_timeout=None):
        lock = None if spec.environment is None else spec.environment.lock
        for other in cls.live.values():
            held = other.spec.environment is not None and other.spec.environment.lock == lock
            if lock and held and other.alive():
                raise SessionBusy(lock, f"{other.name} (owner {other.name})")
        return super().start(spec, display=display, host=host, idle_timeout=idle_timeout)


class FailingSession(HarnessSession):
    @classmethod
    def start(cls, spec, *, display=None, host=None, idle_timeout=None):
        raise SessionError("fake spawn refused")
