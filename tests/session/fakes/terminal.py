"""Session boundary doubles; GuardHost alone spawns real guards."""

import os
import signal
from unittest import mock

from ensemble.session import Host, SessionError, Viewer
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Launch, Liveness, SessionRow, StatusState
from ensemble.session.hosts.base import InteractiveProcess, worker_state
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.types import CommandView
from ensemble.session.viewer.placement import Placement
from tests.session.fakes.worker import ScriptedWorker


class MemoryTerminal(InteractiveProcess):
    def __init__(self, host, name, identity, receipt=None, *, metadata=None, display=None):
        self.host, self.name, self._identity = host, name, identity
        self.calls = mock.Mock()
        self.calls.alive.return_value = True
        self.calls.state.return_value = Liveness.LIVE
        self.calls.returncode.return_value = None
        self.calls.activity.return_value = None
        self.calls.read.return_value = []
        self.calls.record.return_value = receipt
        self.calls.row.return_value = None
        self.calls.session_identity.return_value = (
            (metadata or {}).get("sid"),
            (metadata or {}).get("generation"),
        )
        self.metadata = dict(metadata or {})
        self._display = display

    @property
    def identity(self):
        return self._identity

    def _own(self):
        return self.host.processes.get(self.name) is self

    def alive(self):
        return self._own() and self.calls.alive()

    def state(self):
        return self.calls.state() if self._own() else Liveness.ABSENT

    def returncode(self):
        return self.calls.returncode() if self._own() else -signal.SIGHUP

    def activity(self):
        return self.calls.activity() if self._own() else None

    def mark(self, word, events):
        if self._own():
            self.calls.mark(word, events)
            self.calls.activity.return_value = word

    def read(self, timeout):
        return self.calls.read(timeout)

    def send(self, action):
        if not self.alive():
            raise SessionError(f"terminal {self.name} is not running")
        self.calls.send(action)

    def press(self, keys):
        keys = tuple(keys)
        if not self.alive():
            raise SessionError(f"terminal {self.name} is not running")
        self.calls.press(keys)

    def close(self):
        self.calls.close()
        if self._own():
            self.host.processes.pop(self.name)

    def kill(self):
        self.calls.kill()
        self.calls.alive.return_value = False

    def wait_exit(self, timeout):
        self.calls.wait_exit(timeout)
        return not self.alive()

    def record(self):
        return self.calls.record() if self._own() else None

    def session_identity(self):
        return self.calls.session_identity() if self._own() else (None, None)

    def display(self):
        receipt = self.record()
        return self._display if receipt is None else receipt.display

    def row(self):
        if (row := self.calls.row()) is not None:
            return row
        receipt = self.record()
        return SessionRow(
            self.name,
            self.metadata.get("sid") if receipt is None else receipt.sid,
            self.metadata.get("client") if receipt is None else receipt.kind,
            StatusState.WORKING if self.alive() else StatusState.DEAD,
            self.display(),
            None,
            None,
            own=receipt is not None or self.metadata.get("sid") is not None,
        )


class ScreenTerminal(MemoryTerminal):
    def screen(self, lines=None):
        if not self._own():
            raise SessionError(f"terminal {self.name} is not running")
        return self.host.calls.screen(self, lines)


class MemoryHost(Host):
    terminal_class = MemoryTerminal

    def __init__(self, name="memory"):
        super().__init__(name)
        self.calls = mock.Mock()
        self.processes = {}
        self.serial = 0
        self.calls.list.return_value = None
        self.calls.worker_liveness.return_value = Liveness.ABSENT

    def create(self, name, sid=None, *, identity=None, receipt=None, metadata=None, display=None):
        self.serial += 1
        generation = identity or f"generation-{self.serial}"
        if metadata is None:
            metadata = {"sid": sid, "generation": generation}
        process = self.terminal_class(
            self, name, generation, receipt, metadata=metadata, display=display
        )
        self.processes[name] = process
        return process

    def validate(self, name, launch: Launch):
        self.calls.validate(name, launch)
        super().validate(name, launch)

    def worker(self, name, launch: Launch, *, terminal, display=None, events=None, metadata=None):
        self.calls.worker(
            name, launch, terminal=terminal, display=display, events=events, metadata=metadata
        )
        if not terminal:
            return self.headless_worker(name, launch, metadata)
        if events:
            fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        return self.create(name, metadata=metadata, display=display)

    def headless_worker(self, name, launch: Launch, metadata) -> InteractiveProcess:
        return ScriptedWorker(name, (metadata or {}).get("generation") or "g" * 32)

    def worker_liveness(self, name, generation):
        return self.calls.worker_liveness(name, generation)

    def _release(self, name, generation):
        self.calls.release(name, generation)
        process = self.processes.get(name)
        if process is not None and process.session_identity()[1] == generation:
            process.close()

    def process(self, name):
        """Its terminal of `name` (calls.process first: a test's race), else SessionError."""
        self.calls.process(name)
        if name not in self.processes:
            raise SessionError(f"no {self.name} session {name}")
        return self.processes[name]

    def list(self):
        listed = self.calls.list()
        return sorted(self.processes) if listed is None else listed

    def rows(self):
        rows = []
        for name in self.list():
            try:
                rows.append(self.process(name).row())
            except SessionError:
                if name in self.list():
                    raise
        return rows


class ScreenHost(MemoryHost):
    """Attachable: MemoryViewer shows its sessions through its calls."""

    terminal_class = ScreenTerminal
    attachable = True

    def __init__(self, name="memory", **options):
        super().__init__(name, **options)
        self.calls.screen.return_value = ""
        self.calls.attach_command.return_value = None

    def attach_target(self, name):
        return CommandView(f"/fake/attach {name}", ("/fake/attach", name))


class MemoryViewer(Viewer):
    """Viewer boundary double recording Host calls."""

    def __init__(self, settings=None):
        tmux = mock.create_autospec(TmuxCli, instance=True)
        tmux.runner = mock.Mock(side_effect=AssertionError("no tmux in MemoryViewer"))
        super().__init__(settings, tmux=tmux)
        self.placement = self._placement = mock.create_autospec(Placement, instance=True)
        self._placement.workers.return_value = []

    def show(self, name, host, *, display=None, command=None):
        if display is None and (state := worker_state(name)) is not None:
            display = state.display
        return host.calls.show(name, display=display, command=command)

    def hide(self, name, host, *, if_closed=False):
        host.calls.hide(name)

    def attach_command(self, name, host):
        return host.calls.attach_command(name)

    def _unclaimed(self, name, host):
        if host.attachable:
            host.calls.unclaimed(name)


class GuardHost(MemoryHost):
    """A MemoryHost whose headless runs start their client under a real local guard."""

    tmux = None

    def headless_worker(self, name, launch: Launch, metadata) -> InteractiveProcess:
        return managed.start(self, name, launch, terminal=False, metadata=metadata)
