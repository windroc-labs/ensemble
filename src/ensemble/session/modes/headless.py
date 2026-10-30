"""The headless mode: a Headless's command under its Host's guard (Host.worker, terminal=False),
its input ended at once. A reader thread splits the worker's output into each stream's lines; two
daemon threads pump its events and Stderr lines into one bounded queue, and the session ends at
both streams' end. stop() closes the worker, whose guard ends the client's whole tree; every wait
is bounded.
"""

import codecs
import contextlib
import io
import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Self

from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry
from ensemble.session.engine.types import (
    PROCESS_EXIT,
    Blocked,
    ClientBound,
    ConfigError,
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
    Unanswered,
    WorkerExit,
    WorkerOutput,
)
from ensemble.session.hosts.base import Host, InteractiveProcess
from ensemble.session.modes.base import BaseMode, identity_drop
from ensemble.session.modes.client_adapters.headless import Headless
from ensemble.session.modes.tui import checked_name
from ensemble.utils import Display

QUEUE_MAX = 64  # events and Stderr lines a pump queues before it waits for poll
PUT_WAIT = 0.5
LINE_MAX = 8 << 20  # bytes: a longer output line fails the run
READ_WAIT = 0.5
EXIT_WAIT = 0.05  # between checks for an exit code its Host confirms after the exit frame


class _Lines:
    """One stream's bytes as text lines, as piped_popen's text mode read them."""

    def __init__(self, name: str) -> None:
        self.name, self.partial = name, 0
        self.decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
        )
        self.text = ""

    def feed(self, data: bytes, *, final: bool = False) -> list[str]:
        last = max(data.rfind(b"\n"), data.rfind(b"\r"))
        self.partial = self.partial + len(data) if last < 0 else len(data) - last - 1
        if self.partial > LINE_MAX:
            self.text = ""
            raise SessionError(
                f"headless session {self.name}: a client output line over {LINE_MAX} bytes"
            )
        self.text += self.decoder.decode(data, final=final)
        *lines, self.text = self.text.split("\n")
        lines = [line + "\n" for line in lines]
        if final and self.text:
            lines.append(self.text)
        return lines


class _Feed:
    """A stream's lines from the reader to its pump, dropped once the pump is gone: at most
    LINE_MAX characters held, but always the next line (a full feed stalls the reader).
    """

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.lines: deque[str] = deque()
        self.size = 0
        self.ended = self.dropped = False
        self.error: BaseException | None = None

    def put(self, line: str) -> None:
        with self.condition:
            self.condition.wait_for(
                lambda: self.dropped or not self.lines or self.size + len(line) <= LINE_MAX
            )
            if self.dropped:
                return
            self.lines.append(line)
            self.size += len(line)
            self.condition.notify_all()

    def end(self, error: BaseException | None = None) -> None:
        with self.condition:
            self.ended, self.error = True, error
            self.condition.notify_all()

    def drop(self) -> None:
        with self.condition:
            self.dropped = True
            self.lines.clear()
            self.condition.notify_all()

    def __iter__(self) -> Iterator[str]:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.lines or self.ended)
                if not self.lines:
                    if self.error is not None:
                        raise self.error
                    return
                line = self.lines.popleft()
                self.size -= len(line)
                self.condition.notify_all()
            yield line


class HeadlessMode(BaseMode):
    kind = Mode.HEADLESS

    def __init__(self, prepared: Prepared, worker: InteractiveProcess, generation: str):
        super().__init__(prepared)
        self._generation = generation
        self.worker, self.items, self.open = worker, queue.Queue(QUEUE_MAX), 2
        self.feeds = {"stdout": _Feed(), "stderr": _Feed()}
        self.exited = threading.Event()
        self.closing = threading.Lock()
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
            spec, client, registry=registry, host=host, display=display, generation=uuid.uuid4().hex
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
        generation: str,
    ) -> tuple[Prepared, Headless, Mapping[str, str] | None, tuple[str, str] | None]:
        """metadata: a named run's, for its Host; None: a nameless run's. Last, the prompt file this
        mode writes.
        """
        run = client.headless()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display, named=True)
        spec, account = cls._selected(spec, client)
        if spec.name is None and host._named_headless:
            raise ConfigError(f"a headless session on host {host.name} needs a name")
        drop = identity_drop(registry)
        name, metadata = spec.name, None
        if name is not None:
            spec, metadata = cls._hosted(
                spec, run._home(), host, checked_name(name), account, generation=generation
            )
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
        generation: str,
    ) -> Self:
        prepared, run, metadata, prompt_file = cls._prepare(
            spec, client, registry=registry, host=host, display=display, generation=generation
        )
        if prompt_file:
            cls._write_prompt(*prompt_file)
        run.commit()
        record(prepared.info)
        metadata = {**(metadata or {}), "generation": generation}
        if owner is not None and prepared.spec.name is not None:
            metadata["harness"] = owner
        name = prepared.spec.name or f"headless-{generation}"
        try:
            worker = host.worker(name, _launch(prepared), terminal=False, metadata=metadata)
        except (OSError, ValueError) as e:
            raise SessionError(f"cannot start {prepared.argv[0]}: {e}") from e
        worker = cls._adopted(worker, host, generation)
        try:
            try:
                worker.write(b"")
            except SessionError:
                if worker.state() != Liveness.ABSENT:
                    raise
            mode = cls(prepared, worker, generation)
            out, err = mode.feeds["stdout"], mode.feeds["stderr"]
            for feed, events, eof, done in (
                (out, run.events(out), mode.eof[0], mode.done[0]),
                (err, (Stderr(line.rstrip("\n")) for line in err), mode.eof[1], mode.done[1]),
            ):
                threading.Thread(
                    target=mode._pump, args=(feed, events, eof, done), daemon=True
                ).start()
            threading.Thread(target=mode._read, args=(name,), daemon=True).start()
        except (OSError, ValueError, SessionError) as e:
            with contextlib.suppress(SessionError):
                worker.close()
            raise SessionError(f"cannot start {prepared.argv[0]}: {e}") from e
        return mode

    def _read(self, name: str) -> None:
        """The worker's output into its feeds, until its exit or a failure."""
        streams = {stream: _Lines(name) for stream in self.feeds}
        error: SessionError | None = None
        try:
            while not self.exited.is_set():
                for message in self.worker.read(READ_WAIT):
                    if isinstance(message, WorkerOutput) and message.stream in streams:
                        for line in streams[message.stream].feed(message.data):
                            self.feeds[message.stream].put(line)
                    elif isinstance(message, WorkerExit):
                        for stream, lines in streams.items():
                            for line in lines.feed(b"", final=True):
                                self.feeds[stream].put(line)
                        self.exited.set()
        except SessionError as failure:
            error = failure
        finally:
            for feed in self.feeds.values():
                feed.end(error)
            self.exited.set()
        if error is not None and not isinstance(error, Unanswered):
            self.stopped.set()
            with contextlib.suppress(SessionError):
                self._close()

    def _pump(
        self,
        feed: _Feed,
        events: Iterable[SessionEvent | Stderr | ClientBound],
        eof: threading.Event,
        done: threading.Event,
    ) -> None:
        try:
            for event in events:
                self._put(event)
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
            self._put(e)
        else:
            eof.set()
            self._put(None)
        finally:
            feed.drop()
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
            if not self.stopped.is_set():  # a stop's cut output fails as interrupted instead
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
                PROCESS_EXIT,
                f"worker exited {code}" + (f": {self._diagnostic}" if self._diagnostic else ""),
            )
        else:
            terminal = self._candidate or TurnFailed("incomplete_turn", self._diagnostic)
        self._finished = True
        return [terminal], True

    def returncode(self, timeout: float) -> int | None:
        """Once the worker's exit came and its Host confirmed it; then its handle closes."""
        deadline = time.monotonic() + timeout
        while self._code is None and self.exited.wait(max(0.0, deadline - time.monotonic())):
            self._code = self.worker.returncode()
            if self._code is not None or time.monotonic() >= deadline:
                break
            time.sleep(EXIT_WAIT)
        if self._code is not None:
            self._close()
        return self._code

    def _close(self) -> None:
        with self.closing:
            if not self.closed:
                self.worker.close()
                self.closed = True

    def state(self) -> Liveness:
        return self.worker.state()

    def send(self, text: str) -> None:
        raise SessionError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise SessionError("screen: tui mode only")

    def stop(self) -> None:
        self.stopped.set()
        self._close()


def _launch(prepared: Prepared) -> Launch:
    info = prepared.info
    return Launch(prepared.argv, env=prepared.env, cwd=info.cwd, transcript=info.transcript)
