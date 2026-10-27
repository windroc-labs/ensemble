"""Codex interactive launch plans, hook callbacks and event interpretation.
Each new assistant message in the bound native session file since launch is one Reply, read from a
cursor; a Stop's text that lands later is not repeated. A failed turn's `task_complete` error there
is a TurnFailed.
"""

import json
import os
import re
import shlex
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType

import ensemble.utils
from ensemble.session.clients.codex.common import (
    LIMIT_PREFIXES,
    TABLE,
    VALUED,
    CodexCommon,
    RecoveryError,
    SessionStore,
    launch_options,
    limit_message,
    mcp_off,
    native_id,
    refuse_mode,
    reset_at,
)
from ensemble.session.clients.codex.common.types import Snapshot
from ensemble.session.clients.codex.tui import records, replies
from ensemble.session.clients.common import question, scan, split_flags
from ensemble.session.engine.types import (
    ClientBound,
    ClientHome,
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    Launch,
    LimitHit,
    Reply,
    SessionEvent,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.client_adapters.tui import Tui
from ensemble.utils import json_records, module_roots, read_beneath, source_path

PASSED = ("-c", "--enable", "--disable")

LOOKUP_EVERY = 2.0  # seconds between session file lookups while none is found

START = f"{__name__}.start"  # run as python -P -s -m on this layer's source roots

CLIENT_PYTHONPATH = "ENSEMBLE_CLIENT_PYTHONPATH"  # START's env: the client's PYTHONPATH, never argv


HOOKS = (
    ("SessionStart", "sessionStart", "activity"),
    ("UserPromptSubmit", "userPromptSubmit", "turn_started"),
    ("PostToolUse", "postToolUse", "activity"),
    ("PermissionRequest", "permissionRequest", "blocked"),
    ("PreToolUse", "preToolUse", "blocked"),
    ("Stop", "stop", "turn_end"),
    ("Interrupt", "interrupt", "turn_end"),
)
# Plan mode's question dialog asks no permission: its tool's PreToolUse is the blocked hook, its
# PostToolUse the answer (an Esc interrupts the turn).
QUESTION = "request_user_input"
MATCHERS = MappingProxyType({"PreToolUse": f"^{QUESTION}$"})


FIELDS = frozenset(
    {
        "executable",
        "selection",
        "prompt",
        "hook",
        "workdir",
        "sid",
        "store",
        "permission",
        "flags",
    }
)


@dataclass(frozen=True)
class HookPlan:
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LaunchPlan:
    executable: str
    selection: tuple[str, ...]
    prompt: str
    hook: HookPlan
    workdir: str
    sid: str
    store: str
    permission: tuple[str, ...]  # its permission mode's flags
    flags: tuple[str, ...]  # the config's current flags; the caller's are in the snapshot


def _hook_identity(value) -> tuple[str, str] | None:
    identifier, path = value.get("session_id"), value.get("cwd")
    if (
        value.get("agent_id") is not None
        or not native_id(identifier)
        or not isinstance(path, str)
        or not os.path.isabs(path)
        or "\0" in path
    ):
        return None
    return identifier, os.path.realpath(path)


# Each dialog's title, and the first option or instruction Codex renders under it.
DIALOGS = MappingProxyType(
    {
        "Trust this folder?": "1. Trust and continue",
        "Config, hooks, and exec policies from untrusted folders stay disabled.": "1. Open restricted",
        "Hooks need review": "1. Review hooks",
        "Working directory · resume": "1. Use session directory",
        "This conversation is open in another app": "Close it there and press R to continue here.",
        "Sign in with ChatGPT to use Codex": "1. Sign in with ChatGPT",
    }
)


def _starts_line(text: str, screen: str) -> bool:
    """`text` starts a line of `screen`, after the line's marks (`›`, `🔒`) and its wrapping."""
    words = r"\s+".join(map(re.escape, text.split()))
    return re.search(rf"^[^\w\n]*{words}", screen, re.M) is not None


class CodexTui(Tui):
    hooks = MappingProxyType({label: HookKind(kind) for label, _, kind in HOOKS})
    dialogs = tuple(DIALOGS)
    _remote_owned = True

    def _home(self) -> ClientHome:
        return self.common.home()

    def __init__(self, common: CodexCommon):
        super().__init__()
        self.common = common
        self.state = None
        self.last_reply = ""
        self.native = ""
        self.store = ""
        self.name = ""  # its session's, for its reply lines
        self.transcript, self.start, self.offset = "", 0, 0  # the native session file's cursor
        self.last = self.unseen = ""  # the turn's newest text; its Stop text not yet read
        self.retry = 0.0  # time.monotonic() before which no session file lookup runs
        self._native_turn = ""

    def launch(self, spec: Spec, hook):
        common = self.common
        prompt, resume = spec.prompt, spec.resume
        self.name = spec.name or ""
        self.state = common.prepare(spec)
        snapshot = self.state.snapshot
        self.native = self.state.native_id or ""
        env = common.environment(snapshot, interactive=True)
        store = self.store = common.store.root
        runtime = self._context.container
        if runtime:
            env = {
                key: value
                for key, value in env.items()
                if key not in ("PYTHONPATH", CLIENT_PYTHONPATH)
            }
        exe = (
            os.path.join(runtime.bin_dir, "codex")
            if runtime
            else shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
        )
        if exe is None:
            raise ConfigError("codex not found on PATH")
        exe = os.path.abspath(exe)
        self.transcript = common.transcript(self.state) if resume else ""
        self.start = self.offset = os.path.getsize(self.transcript) if self.transcript else 0
        plan = LaunchPlan(
            exe,
            self.common.selector(self.native, resume=resume),
            prompt,
            HookPlan(tuple(hook)),
            self.state.workdir,
            self.state.sid,
            os.path.join(runtime.state_root, "codex-store") if runtime else store,
            common.modes[self._context.permission],
            common.flags,
        )
        self._runtime_files = SessionStore.project(self.state, plan.store) if runtime else {}
        pythonpath = env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
        try:
            encoded = encode(plan)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        launch = Launch(
            (
                runtime.python if runtime else os.path.realpath(sys.executable),
                "-P",
                "-s",
                "-m",
                START,
                encoded,
            ),
            env={
                **env,
                **(
                    {"PYTHONPATH": runtime.pythonpath}
                    if runtime
                    else source_path(module_roots(sys.modules[__name__], ensemble.utils))
                ),
                **(
                    {CLIENT_PYTHONPATH: pythonpath}
                    if pythonpath is not None and runtime is None
                    else {}
                ),
            },
            unset=(*common.UNSET, CLIENT_PYTHONPATH),
            cwd=snapshot.cwd,
            project=snapshot.project,
            transcript=self.transcript,
            resume=common.resume_command(self.state) if resume else "",
        )
        return launch

    def commit(self) -> None:
        self.state = self.common.commit(self.state)

    def hook_callback(self) -> HookCallback:
        return HookCallback(__name__, {"store": self.store, "start": self.start, "name": self.name})

    def restore(self, spec, state):
        """The launching CodexTui's store, recovery state and native id, from the store it named."""
        store, start = state.get("store"), state.get("start", 0)
        if type(start) is not int or start < 0:
            raise ConfigError("codex tui state: bad transcript cursor")
        self.start = self.offset = start
        try:
            sessions = SessionStore(store if isinstance(store, str) else None)
            self.store = sessions.root
            self.state = sessions.load(spec.workdir, spec.sid)
        except (RecoveryError, OSError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc
        self.native = "" if self.state is None else self.state.native_id or ""

    def _settle_replies(self, workdir, sid):
        replies.settle_pending(self.store, workdir, sid)

    def observe(self):
        if not self.transcript and self.native and self.state is not None:
            now = time.monotonic()
            if now < self.retry:
                return []
            self.retry = now + LOOKUP_EVERY
            try:
                self.transcript = self.common.transcript(replace(self.state, native_id=self.native))
            except (ConfigError, OSError):
                return []
        lines = (read_beneath("/", self.transcript, offset=self.offset) or b"").split(b"\n")[:-1]
        self.offset += sum(len(line) + 1 for line in lines)  # a line still being written waits
        return [event for record in json_records(lines) for event in self._record(record)]

    def replay(self, records):
        """Ends in the latest turn's state: a Stop since the hook log's last prompt leaves its text
        unseen unless it is the session file's newest.
        """
        found: list[SessionEvent | ClientBound] = [*self.observe()]
        newest, ended = self.last, None
        for label, payload in records:
            found += self.hook_events(label, payload)
            if label in ("UserPromptSubmit", "Stop"):
                ended = self.last_reply if label == "Stop" else None
        self.last = newest if ended is None else ""
        self.unseen = ended if ended and ended.strip() and ended != newest else ""
        return [event for event in found if not isinstance(event, TurnStarted)]

    def _record(self, record: dict) -> list[SessionEvent]:
        payload = record.get("payload")
        accepted: list[SessionEvent] = []
        if (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_complete"
            and (payload.get("error") is None or isinstance(payload.get("error"), dict))
        ):
            identifier = payload.get("turn_id")
            if (
                isinstance(identifier, str)
                and identifier
                and (not self._turn_open or not self._turn_id)
                and self._accept_turn(identifier)
            ):
                accepted.append(TurnStarted())
        if (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_started"
        ):
            identifier = payload.get("turn_id")
            self._native_turn = identifier if isinstance(identifier, str) else ""
            if self._accept_turn(identifier):
                self.last = self.unseen = ""
                return [TurnStarted()]
            return []
        if isinstance(payload, dict) and (failed := records.failure(record)) is not None:
            kind, message = failed
            if limit_message(message):
                return []
            if not self._settle_turn(payload.get("turn_id") or self._native_turn, native=True):
                return []
            return [*accepted, TurnFailed(kind, message)]
        if (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_complete"
            and payload.get("error") is None
            and self._settle_turn(payload.get("turn_id") or self._native_turn, native=True)
        ):
            text = payload.get("last_agent_message")
            return [*accepted, TurnEnd(text if isinstance(text, str) else self.last)]
        found = records.message(record)
        if found is None:
            return []
        role, text = found
        if role == "user":  # a prompt: a new turn
            self.last = self.unseen = ""
            return []
        if text.strip() and text.strip() == self.unseen.strip():
            self.unseen = ""
        elif text.strip():
            self.last = text
            return [Reply(text)]
        return []

    def session_id(self, payload):
        if self.native and payload.get("session_id") != self.native:
            return ""
        identity = _hook_identity(payload)
        if identity is None:
            return ""
        native, cwd = identity
        if self.state and cwd != self.state.snapshot.cwd:
            return ""
        self.native = native
        if payload.get("hook_event_name") == "Stop":
            message = payload.get("last_assistant_message")
            self.last_reply = message if isinstance(message, str) else ""
        return native

    def hook_events(self, label, payload):
        if label not in self.hooks or not isinstance(payload, dict):
            return []
        if payload.get("hook_event_name", label) != label:
            return []
        payload = {**payload, "hook_event_name": label}
        if not self.session_id(payload):
            return []
        early = []
        if self.hooks[label] == HookKind.TURN_END and not self.transcript:
            self.retry = 0.0  # the turn's texts, read before its end, are not repeated after it
            early = self.observe()
        return [*early, *super().hook_events(label, payload)]

    def reply(self, payload):
        """An Interrupt's turn end has no text: its turn's came as Replies, and the previous
        turn's is never repeated. Claude Code has no interrupt hook, so its interrupted turn has no
        turn end at all; Codex's Interrupt keeps its `done`, so the session is not left working.
        """
        if payload.get("hook_event_name") == "Interrupt":
            return ""
        message = payload.get("last_assistant_message")
        self.last_reply = message if isinstance(message, str) else ""
        self.unseen = self.last_reply if self.last_reply.strip() != self.last.strip() else ""
        self.last = ""
        return self.last_reply

    def pending(self, payload):
        """0: Codex's Stop reports no background work, whose end starts no turn
        (docs/architecture.md › Client policy).
        """
        return 0

    def activity(self, payload):
        value = payload.get("tool_name")
        return value if isinstance(value, str) else ""

    def blocked_reason(self, payload):
        if payload.get("tool_name") == QUESTION:
            found = payload.get("tool_input")
            asked = found.get("questions") if isinstance(found, dict) else None
            titles = [
                q.get("header") or q.get("question")
                for q in (asked if isinstance(asked, list) else ())
                if isinstance(q, dict)
            ]
            return question(titles)
        return self.activity(payload)

    def shows(self, dialog, screen):
        """Its title and first option each start a line: the title quoted in output is no dialog."""
        return _starts_line(dialog, screen) and _starts_line(DIALOGS[dialog], screen)

    def limit(self, screen):
        screen = screen.replace("’", "'")
        pattern = (
            r"(?m)(?:^[ \t]*|(?<=[ \t]))■[ \t]*"
            r"(?=You've hit your usage|Quota exceeded|Your workspace is out|You hit your spend cap)"
        )
        for match in re.finditer(pattern, screen):
            message = " ".join(screen[match.end() :].split())
            if message.startswith(LIMIT_PREFIXES):
                reset = re.search(r"(?:Try|try) again at [^.]+\.", message)
                return LimitHit(
                    reset_at(
                        reset.group() if reset else message,
                        self.common.config.get("env", {}),
                    )
                )
        return None


def _strs(value: object, *, absolute: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(word, str) or "\0" in word or absolute and not os.path.isabs(word)
        for word in value
    ):
        raise ValueError(
            "Codex tui launch: expected absolute paths"
            if absolute
            else "Codex tui launch: invalid command"
        )
    return tuple(value)


def decode(data: object) -> LaunchPlan:
    if not isinstance(data, dict) or frozenset(data) != FIELDS:
        raise ValueError("Codex tui launch: invalid launch fields")
    selection, hook = _strs(data["selection"]), _strs(data["hook"])
    if any(word.startswith("-") for word in selection):
        raise ValueError("Codex tui launch: invalid session selection")
    if not hook or not os.path.isabs(hook[0]):
        raise ValueError("Codex tui launch: invalid command")
    executable, workdir, store = _strs(
        [data["executable"], data["workdir"], data["store"]], absolute=True
    )
    prompt = data["prompt"]
    if not isinstance(prompt, str) or "\0" in prompt:
        raise ValueError("Codex tui launch: invalid prompt")
    if not isinstance(data["sid"], str):
        raise ValueError("Codex tui launch: invalid recovery identity")
    permission, flags = _strs(data["permission"]), _strs(data["flags"])
    try:
        refuse_mode(permission)
        refuse_mode(flags, f"{TABLE} flags")
    except ConfigError as exc:
        raise ValueError(f"Codex tui launch: {exc}") from None
    return LaunchPlan(
        executable,
        selection,
        prompt,
        HookPlan(hook),
        workdir,
        data["sid"],
        store,
        permission,
        flags,
    )


def encode(plan: LaunchPlan) -> str:
    data = asdict(plan)
    data["selection"] = list(plan.selection)
    data["hook"] = list(plan.hook.argv)
    data["permission"] = list(plan.permission)
    data["flags"] = list(plan.flags)
    decode(data)
    encoded = json.dumps(data, ensure_ascii=False)
    encoded.encode("utf-8")
    return encoded


def commands(plan: HookPlan) -> Mapping[str, str]:
    return MappingProxyType({event: shlex.join((*plan.argv, event)) for event, _, _ in HOOKS})


def hook_flags(plan: HookPlan) -> tuple[str, ...]:
    return tuple(
        word
        for event, command in commands(plan).items()
        for word in (
            "-c",
            (
                f"hooks.{event}=[{{"
                + (f"matcher={json.dumps(MATCHERS[event])}," if event in MATCHERS else "")
                + f'hooks=[{{type="command",command={json.dumps(command, ensure_ascii=False)},'
                f"timeout={3 if event == 'Interrupt' else 10}}}]}}]"
            ),
        )
    )


def passed(flags) -> list[str]:
    return [
        word
        for flag in scan(flags, VALUED)
        if flag.word in PASSED and flag.value is not None
        for word in (flag.word, flag.value)
    ]


def trust_args(plan: LaunchPlan, snapshot: Snapshot) -> list[str]:
    flags = mcp_off((*plan.flags, *snapshot.flags), snapshot)
    return [*hook_flags(plan.hook), *passed((*plan.permission, *flags))]


def execution_argv(plan: LaunchPlan, snapshot: Snapshot, trusted_state: str) -> list[str]:
    supplied, literal = split_flags(snapshot.flags)
    tail = (
        [*literal, *([plan.prompt] if plan.prompt else [])]
        if literal
        else (["--", plan.prompt] if plan.prompt else [])
    )
    return [
        plan.executable,
        *plan.selection,
        "--no-daemon",
        "-C",
        snapshot.cwd,
        "-c",
        'tui.notification_method="bel"',
        *plan.permission,
        *mcp_off((*plan.flags, *supplied), snapshot),
        *launch_options(snapshot),
        *hook_flags(plan.hook),
        "-c",
        trusted_state,
        *tail,
    ]


def bind(label, value, workdir, sid, *, root=None):
    if label not in CodexTui.hooks or value.get("hook_event_name", label) != label:
        return False
    try:
        identity = _hook_identity(value)
        if identity is None:
            return False
        native, cwd = identity
        store = SessionStore(root)
        state = store.load(workdir, sid)
        if state is None:
            raise RecoveryError("missing Codex recovery state")
        if cwd != state.snapshot.cwd:
            return False
        store.bind(workdir, sid, native, timeout=1 if label == "Interrupt" else 5)
    except (RecoveryError, OSError, ValueError) as exc:
        print(f"codex hook: {exc}", file=sys.stderr)
        return False
    return True


def on_hook(call: HookCall) -> dict | None:
    began = time.monotonic()
    if not bind(call.label, call.payload, call.workdir, call.sid, root=call.state["store"]):
        return None
    replies.on_hook(call, began=began)  # a Stop's replies precede the Host hook's done
    return {**call.payload, "hook_event_name": call.label}
