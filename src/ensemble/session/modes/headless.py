"""The headless mode: a Headless's command on pipes, started by its Host (Host.pipe); two daemon
threads pump its events and Stderr lines into one bounded queue, and the session ends at both
pipes' EOF. stop() ends its Piped, whose local one kills the whole group, so a grandchild holding
the pipes dies too; every wait is bounded.
"""

import queue
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Self

from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry
from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    Launch,
    LimitHit,
    Liveness,
    Mode,
    Prepared,
    SessionError,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
)
from ensemble.session.hosts.base import Host, Piped
from ensemble.session.modes.base import BaseMode, identity_drop
from ensemble.session.modes.client_adapters.headless import Headless
from ensemble.session.modes.tui import checked_name
from ensemble.utils import Display

QUEUE_MAX = 64  # events and Stderr lines a pump queues before it waits for poll
PUT_WAIT = 0.5


class HeadlessMode(BaseMode):
    kind = Mode.HEADLESS

    def __init__(self, prepared: Prepared, piped: Piped, generation: str | None = None):
        super().__init__(prepared)
        self._generation = generation or uuid.uuid4().hex
        self.piped, self.items, self.open = piped, queue.Queue(QUEUE_MAX), 2
        self.eof = (threading.Event(), threading.Event())
        self.done = (threading.Event(), threading.Event())
        self.closed, self.stopped = False, threading.Event()
        self._candidate: TurnEnd | None = None
        self._failure: TurnFailed | None = None
        self._diagnostic = ""
        self._finished = False
        self._code: int | None = None

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
        prepared, _, metadata, _ = cls._prepare(
            spec, client, registry=registry, host=host, display=display
        )
        if metadata is not None:
            host._preflight(prepared.spec.name or "", _launch(prepared), metadata)
        return prepared

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        host: Host,
        display: Display,
    ) -> tuple[Prepared, Headless, Mapping[str, str] | None, tuple[str, str] | None]:
        """metadata: a named run's, for its Host; None: a nameless run, as before Hosts piped. Last,
        the prompt file this mode writes.
        """
        run = client.headless()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display, named=True)
        spec, account = cls._selected(spec, client)
        drop = identity_drop(registry)
        name, metadata = spec.name, None
        if name is not None:
            spec, metadata = cls._hosted(spec, run._home(), host, checked_name(name), account)
        spec = cls._noted(spec, client)
        run._context = host._client_context()
        spec, prompt_file = cls._pointed(spec, run._context, spec.workdir)
        if prompt_file and run._context.container:
            path, text = prompt_file
            metadata, prompt_file = {**(metadata or {}), "prompt_file": path, "prompt": text}, None
        prepared = cls._launched(spec, run.launch, drop, inherit=host._inherit_env)
        if name is not None:
            prepared = cls._host_resume(prepared, host, name)
        return prepared, run, metadata, prompt_file

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
        prepared, run, metadata, prompt_file = cls._prepare(
            spec, client, registry=registry, host=host, display=display
        )
        if prompt_file:
            cls._write_prompt(*prompt_file)
        run.commit()
        record(prepared.info)
        try:
            piped = host.pipe(prepared.spec.name, _launch(prepared), metadata=metadata)
            mode = cls(prepared, piped, (metadata or {}).get("generation"))
            out, err = piped.stdout, piped.stderr
            for pipe, events, eof, done in (
                (out, run.events(out), mode.eof[0], mode.done[0]),
                (err, (Stderr(line.rstrip("\n")) for line in err), mode.eof[1], mode.done[1]),
            ):
                threading.Thread(
                    target=mode._pump, args=(pipe, events, eof, done), daemon=True
                ).start()
        except (OSError, ValueError) as e:
            raise SessionError(f"cannot start {prepared.argv[0]}: {e}") from e
        return mode

    def _pump(
        self,
        pipe,
        events: Iterable[SessionEvent | Stderr | ClientBound],
        eof: threading.Event,
        done: threading.Event,
    ) -> None:
        try:
            with pipe:
                for event in events:
                    self._put(event)
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
            self._put(e)
        else:
            eof.set()
            self._put(None)
        finally:
            done.set()

    def _put(self, item) -> None:
        """On a full queue, waits until stop()."""
        while True:
            try:
                return self.items.put(item, timeout=PUT_WAIT)
            except queue.Full:
                if self.stopped.is_set():
                    return

    @property
    def generation(self) -> str:
        """Its Host's for a named run, else its own."""
        return self._generation

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        if self._finished:
            return [], True
        ended = self.stopped.is_set() and all(done.is_set() for done in self.done)
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:  # ended read first: a last item may land meanwhile
            return self._finish(timeout) if ended or self.open == 0 else ([], False)
        if isinstance(item, BaseException):
            raise item
        if item is None:
            self.open -= 1
            return self._finish(timeout) if self.open == 0 else ([], False)
        if isinstance(item, TurnFailed):
            self._failure = self._failure or item
            return [], False
        if isinstance(item, TurnEnd):
            self._candidate = item
            return [], False
        if isinstance(item, Blocked):
            self._failure = self._failure or TurnFailed("permission_required", item.reason)
            self.stop()
            return [], False
        if isinstance(item, LimitHit):
            self._failure = self._failure or TurnFailed("usage_limit")
        if isinstance(item, Stderr) and item.text:
            self._diagnostic = item.text
        return [item], False

    def _finish(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        code = self.returncode(timeout)
        if code is None and not self.stopped.is_set():
            return [], False
        if self._failure is not None:
            terminal = self._failure
        elif code is None or self.stopped.is_set():
            terminal = TurnFailed("interrupted", self._diagnostic)
        elif code != 0:
            terminal = TurnFailed(
                "process_exit",
                f"worker exited {code}" + (f": {self._diagnostic}" if self._diagnostic else ""),
            )
        else:
            terminal = self._candidate or TurnFailed("incomplete_turn", self._diagnostic)
        self._finished = True
        return [terminal], True

    def returncode(self, timeout: float) -> int | None:
        code = self._code if self._code is not None else self.piped.wait(timeout)
        self._code = code
        if code is not None and not self.closed:
            self.closed = True
            self.piped.close()
        return code

    def state(self) -> Liveness:
        return Liveness.LIVE if self.piped.wait(0) is None else Liveness.ABSENT

    def send(self, text: str) -> None:
        raise SessionError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise SessionError("screen: tui mode only")

    def stop(self) -> None:
        self.closed = True
        self.stopped.set()
        self.piped.stop(ended=all(eof.is_set() for eof in self.eof))


def _launch(prepared: Prepared) -> Launch:
    info = prepared.info
    return Launch(prepared.argv, env=prepared.env, cwd=info.cwd, transcript=info.transcript)
