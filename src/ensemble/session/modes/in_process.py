"""The in_process mode: an InProcess's run iterated on a daemon thread into a queue; no child
process (owned: the session harness's thread). A run that raises ends with its traceback as Stderr lines and code 1. stop() never
waits: what came before it is still polled, the code is -SIGKILL unless the run had ended, and the
run is closed at its next yield.
"""

import contextlib
import queue
import signal
import threading
import traceback
import uuid
from collections.abc import Callable
from typing import Self

from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry
from ensemble.session.engine.types import (
    ClientBound,
    ConfigError,
    Liveness,
    Mode,
    Prepared,
    SessionError,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from ensemble.session.hosts.base import Host
from ensemble.session.modes.base import BaseMode
from ensemble.session.modes.client_adapters.in_process import InProcess
from ensemble.utils import Display

END = object()
RUN_THREAD = "in_process run"


class InProcessMode(BaseMode):
    kind = Mode.IN_PROCESS

    def __init__(self, prepared: Prepared):
        super().__init__(prepared)
        self.stopped, self.code = False, None
        self.items, self.lock, self.over = queue.Queue(), threading.Lock(), threading.Event()
        self._generation = uuid.uuid4().hex

    @property
    def generation(self) -> str:
        return self._generation

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        return cls._prepare(spec, client, display=display)[0]

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        client: Client,
        *,
        display: Display,
    ) -> tuple[Prepared, InProcess]:
        run = client.in_process()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display)
        spec, _ = cls._selected(spec, client)
        spec = cls._noted(spec, client)
        if spec.sid is None:
            raise ConfigError("sid: missing")
        run.check(spec)
        prepared = Prepared(
            spec, SessionInfo(spec.sid, spec.access.cwd, spec.access.project, "", "")
        )
        return prepared, run

    @classmethod
    def start(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
        owner: str | None = None,
    ) -> Self:
        prepared, run = cls._prepare(spec, client, display=display)
        record(prepared.info)
        mode = cls(prepared)
        try:
            threading.Thread(
                target=mode._pump, args=(run, prepared.spec), name=RUN_THREAD, daemon=True
            ).start()
        except RuntimeError as e:
            raise SessionError(f"cannot start the in_process run: {e}") from e
        return mode

    def _pump(self, run: InProcess, spec: Spec) -> None:
        events, code = None, 1
        try:
            events = iter(run.run(spec))
            for item in events:
                with self.lock:
                    if self.stopped:
                        break
                    self.items.put(item)
            code = 0
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
            lines = "".join(traceback.format_exception(e)).rstrip().split("\n")
            with self.lock:
                if not self.stopped:
                    for line in lines:
                        self.items.put(Stderr(line.rstrip()))
        finally:
            if self.stopped:
                with contextlib.suppress(BaseException):
                    getattr(events, "close", lambda: None)()
            with self.lock:
                if not self.stopped:
                    self.code = code
                    self.items.put(END)
                self.over.set()

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        with contextlib.suppress(queue.Empty):
            item = self.items.get(timeout=timeout)
            return ([], True) if item is END else ([item], False)
        return [], False

    def returncode(self, timeout: float) -> int | None:
        self.over.wait(timeout)
        return self.code

    def state(self) -> Liveness:
        return Liveness.ABSENT if self.over.is_set() else Liveness.LIVE

    def send(self, text: str) -> None:
        raise SessionError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise SessionError("screen: tui mode only")

    def stop(self) -> None:
        # A run blocked in a call goes on until its next yield or the process exits.
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if not self.over.is_set():
                self.code = -signal.SIGKILL
            self.items.put(END)
            self.over.set()
