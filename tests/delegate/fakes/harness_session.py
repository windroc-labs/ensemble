"""Process-free Session, Hosts and Viewer for HarnessDelegate's ownership and view contracts. A
RecordingHost given a tui run's harness starts its agent session as the harness would, unless
harness_starts_agent is False.
"""

import json
import os
from dataclasses import replace

from ensemble.delegate.engine.types import Layout
from ensemble.session import (
    Access,
    EnvironmentSpec,
    Exited,
    Host,
    Launch,
    Mode,
    NotShowable,
    Process,
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


class RecordingProcess(Process):
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

    def display(self):
        return self.display_value


class RecordingHost(Host):
    processes: dict[tuple[str, str], RecordingProcess] = {}
    launches: list[dict] = []
    harness_starts_agent = True

    def detach(self, name, launch, *, display=None, events=None, record=None):
        process = RecordingProcess(name, display=display, record=record)
        self.processes[self.name, name] = process
        self.launches.append(
            {
                "host": self.name,
                "name": name,
                "argv": list(launch.argv),
                "cwd": launch.cwd,
                "env": dict(launch.env),
                "blocked_signals": launch.blocked_signals,
                "display": display,
                "events": events,
                "record": record,
            }
        )
        if RecordingHost.harness_starts_agent and record is not None and len(record.sessions) == 2:
            _start_agent(record, display)
        return process

    def terminal(self, name, launch, *, display=None, events=None, record=None, metadata=None):
        raise NotImplementedError("HarnessSession owns its terminal without a process")

    def attach(self, name):
        if (self.name, name) not in self.processes:
            raise SessionError(f"no {self.name} session {name}")
        return self.processes[self.name, name]

    def list(self):
        return [
            name
            for (host, name), process in self.processes.items()
            if host == self.name and process.alive()
        ]


def _start_agent(record, display):
    """The run's agent session on its agent Host, through its stored session class, in its stored
    environment."""
    layout = Layout(record.workdir)
    with open(os.path.join(layout.run, "delegate.json"), encoding="utf-8") as file:
        state = json.load(file)
    session_cls = (
        Session if state["session_cls"] is None else load_class(state["session_cls"], Session)
    )
    environment = state["plan"]["session"].get("environment")
    spec = SessionSpec(
        workdir=layout.run,
        access=Access(cwd=layout.agent),
        client=state["plan"]["session"]["client"],
        mode=Mode.TUI,
        sid=record.sid,
        name=record.sessions[1],
        environment=None if environment is None else EnvironmentSpec.from_fields(environment),
    )
    session_cls.start(spec, host=state["host"], display=display)


def harness_launches():
    return [launch for launch in RecordingHost.launches if launch["argv"] != ["fake-agent"]]


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
        RecordingHost.harness_starts_agent = True
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
        cls.host(host).detach(name, Launch(("fake-agent",), cwd=spec.workdir), display=display)
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

    def stop(self):
        self.liveness = Liveness.ABSENT
        self.host(self.host_names[self.name]).attach(self.name).close()
        super().stop()

    def events(self):
        for event in super().events():
            if isinstance(event, Exited) and self.mode != "tui":
                self.stop()
            yield event


class DiskProcess(Process):
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

    def detach(self, name, launch, *, display=None, events=None, record=None):
        path = self.path(name)
        save(
            path,
            json.dumps(
                {
                    "alive": True,
                    "display": display,
                    "record": record.to_line() if record else None,
                    "sid": record.sid if record else None,
                }
            ),
        )
        append_line(
            os.path.join(self.root(), "launches.jsonl"),
            json.dumps({"host": self.name, "name": name, "argv": list(launch.argv)}) + "\n",
        )
        return DiskProcess(name, path)

    def attach(self, name):
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
