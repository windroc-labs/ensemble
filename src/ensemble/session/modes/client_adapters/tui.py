from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import ClassVar

from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    ClientHome,
    HookCallback,
    HookKind,
    Key,
    Launch,
    LimitHit,
    Paste,
    SessionEvent,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)


class Tui(ABC):
    _context: ClientContext = LOCAL_CONTEXT
    _runtime_files: Mapping[str, str] = MappingProxyType({})
    hooks: Mapping[str, HookKind] = MappingProxyType({})  # hook label → its kind
    dialogs: tuple[str, ...] = ()
    opening: tuple[str, str] | None = None  # (hook label, text)
    _opening_prompt: ClassVar[bool] = False  # the prompt: not deferred past a launch turn
    client = ""  # last ClientBound's id
    _turn_id = ""
    _turn_open: bool | None = None
    _turn_ids: frozenset[str] = frozenset()  # a subclass's __init__ may skip Tui's
    _remote_owned: ClassVar[bool] = False  # also as a remote tui, its hooks through a guard

    def __init__(self) -> None:
        self._turn_ids: frozenset[str] = frozenset()  # every turn accepted so far

    @abstractmethod
    def launch(self, spec: Spec, hook: tuple[str, ...]) -> Launch:
        """As Headless.launch, plus each label L of `hooks` and `opening` as a hook running
        `hook + (L,)`, its payload on stdin.
        """

    def _home(self) -> ClientHome | None:
        return None

    def commit(self) -> None:
        return None

    def hook_callback(self) -> HookCallback | None:
        return None

    def restore(self, spec: Spec, state: Mapping) -> None:
        """On attach, before replay; state: hook_callback()'s state."""
        return None

    @abstractmethod
    def reply(self, payload: dict) -> str: ...

    def pending(self, payload: dict) -> int:
        """Background work whose end starts a new turn."""
        return 0

    def activity(self, payload: dict) -> str:
        return ""

    def blocked_reason(self, payload: dict) -> str:
        return ""

    def session_id(self, payload: dict) -> str:
        native = payload.get("session_id")
        return native if isinstance(native, str) else ""

    def observe(self) -> list[SessionEvent]:  # its client's own record, since the last call
        return []

    def replay(self, records: Iterable[tuple[str, dict]]) -> list[SessionEvent | ClientBound]:
        """On attach: observe(), then the hook log `records`' events."""
        found: list[SessionEvent | ClientBound] = [*self.observe()]
        found += [event for label, payload in records for event in self.hook_events(label, payload)]
        return [event for event in found if not isinstance(event, TurnStarted)]

    def _accept_turn(self, identifier: object) -> bool:
        identifier = identifier if isinstance(identifier, str) else ""
        if not identifier and self._turn_id:
            return False
        if identifier and identifier in self._turn_ids:
            return False
        if identifier:
            self._turn_ids |= {identifier}
            if not self._turn_id and self._turn_open:
                self._turn_id = identifier
                return False
        self._turn_id, self._turn_open = identifier, True
        return True

    def _settle_turn(self, identifier: object, *, native: bool = False) -> bool:
        if not self._turn_id:  # legacy hooks also report background-work completion
            self._turn_open = False
            return True
        if not self._turn_open:
            return False
        if self._turn_id and identifier != self._turn_id and (identifier or not native):
            return False
        self._turn_open = False
        return True

    def failure(self, payload: dict) -> list[TurnFailed | LimitHit]:
        return [TurnFailed("unknown")]

    def shows(self, dialog: str, screen: str) -> bool:
        return " ".join(dialog.split()) in " ".join(screen.split())

    def limit(self, screen: str) -> LimitHit | None:
        """The client's usage-limit message on the visible terminal `screen`; None: none shown."""
        return None

    def submit(self, text: str) -> tuple[Paste | Key, ...]:
        return Paste(text), Key("Enter")

    def hook_events(self, label: str, payload: dict) -> list[SessionEvent | ClientBound]:
        found: list[SessionEvent | ClientBound] = []
        client = self.session_id(payload)
        if client and client != self.client:
            self.client = client
            found.append(ClientBound(client))
        kind = self.hooks.get(label)
        if kind == HookKind.TURN_STARTED:
            if self._accept_turn(payload.get("turn_id")):
                found.append(TurnStarted())
            return found
        if kind in (HookKind.TURN_END, HookKind.TURN_FAILED) and not self._settle_turn(
            payload.get("turn_id")
        ):
            return found
        if kind == HookKind.TURN_END:
            found.append(TurnEnd(self.reply(payload), self.pending(payload)))
        elif kind == HookKind.TURN_FAILED:
            found.extend(self.failure(payload))
        elif kind == HookKind.BLOCKED:
            found.append(Blocked(self.blocked_reason(payload)))
        elif kind == HookKind.ACTIVITY:
            found.append(Activity(self.activity(payload)))
        return found
