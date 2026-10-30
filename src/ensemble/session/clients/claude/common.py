"""Claude Code client options, Access grants, launch metadata, usage and session inspection."""

import contextlib
import functools
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from ensemble.session.clients import common
from ensemble.session.clients.claude import usage
from ensemble.session.clients.common import UUID, scan, strings
from ensemble.session.engine.types import (
    COMMAND,
    DEFAULT_ACCOUNT,
    ClientHome,
    ConfigError,
    Effort,
    Launch,
    Permission,
    Resumable,
    SessionStatus,
    Spec,
    Usage,
)
from ensemble.utils import (
    LOCAL_ENV_VARS,
    STATE,
    docker_home_of,
    drop_stale,
    environment_ok,
    json_records,
    printable_abs,
    read_beneath,
    read_regular,
    resume_command,
    save,
)

TABLE = "[clients.claude]"
NOT_LITERAL = "*?[]{}()\\,"
STORE = f"{STATE}/sessions/claude"  # under HOME, resolved per call
STATE_KEYS = frozenset(
    {
        "version",
        "cwd",
        "sid",
        "dirs",
        "env",
        "flags",
        "project",
        "commands",
        "reads",
        "model",
        "effort",
        "name",
    }
)
STATE_MAX = 1 << 20
SETTINGS_MAX = 2 * 1024 * 1024
TAIL = 4 << 20
HOME = ClientHome("claude_dir", "claude", "CLAUDE_CONFIG_DIR", "claude-setup-token")
MODEL_KEYS = ("model", "effortLevel", "modelSettings")
MODEL_ENTRY_DROP = frozenset({"autoCompactWindow"})  # host-only keys of a modelSettings entry
ENV_KEYS = ("CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION",)
# A container worker runs unattended.
UNATTENDED = MappingProxyType(
    {
        "skipWorkflowUsageWarning": True,
        "autoCompactEnabled": True,
        "autoContinueAtUsageLimit": True,
        "promptSuggestionEnabled": False,
        "outputStyle": "Concise",
    }
)
HOME_READ_MAX = 64 << 20
# Off: the alternate screen keeps a hidden worker's output out of scrollback.
TUI_ENV = MappingProxyType(
    {"CLAUDE_CODE_DISABLE_AGENT_VIEW": "1", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1"}
)


def added_dirs(flags: tuple[str, ...]) -> Iterator[str]:
    return (
        flag.value
        for flag in scan(flags, {"--add-dir": "--add-dir"})
        if flag.word == "--add-dir" and flag.value is not None
    )


def _read(path: str) -> str:
    """A settings file within Claude's limits: a regular file of at most 2 MiB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            found = os.fstat(fd)
            if not stat.S_ISREG(found.st_mode) or found.st_size > SETTINGS_MAX:
                raise ConfigError(f"--settings {path!r}: want a regular file of at most 2 MiB")
            return b"".join(iter(lambda: os.read(fd, SETTINGS_MAX), b"")).decode()
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(
            f"--settings {path!r}: {getattr(error, 'strerror', None) or error}"
        ) from None


def _parsed(value: str, cwd: str) -> dict:
    """Inline JSON when it starts with "{", else a file path relative to `cwd`."""
    inline = value.lstrip().startswith("{")
    path = os.path.join(cwd, value)
    text = value if inline else _read(path)
    try:
        settings = json.loads(text)
    except (ValueError, RecursionError):
        settings = None
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else None
    if (
        not isinstance(settings, dict)
        or not isinstance(hooks, dict)
        or not all(isinstance(e, list) for e in hooks.values())
    ):
        shown = f"{value!r:.80}" if inline else repr(path)
        raise ConfigError(f"--settings {shown}: want a JSON object with a list of hooks per event")
    return settings


def _split(flags: tuple[str, ...], cwd: str) -> tuple[list[dict], tuple[str, ...]]:
    """Each --settings value before "--", parsed, and the flags without them."""
    found, rest, at = [], [], 0
    for flag in scan(flags, {"--settings": "--settings"}):
        if flag.word == "--settings" and flag.value is not None:
            found.append(_parsed(flag.value, cwd))
            rest.extend(flags[at : flag.start])
            at = flag.end
    return found, (*rest, *flags[at:])


def _settings(*parts: Mapping) -> tuple[str, ...]:
    """`parts` merged, the later winning."""
    return ("--settings", json.dumps(functools.reduce(_merge, parts, {}))) if parts else ()


def _merge(base: dict, over: Mapping) -> dict:
    """Tables merge, lists concatenate, else `over`'s value wins."""
    for key, value in over.items():
        mine = base.get(key)
        if isinstance(mine, dict) and isinstance(value, dict):
            _merge(mine, value)
        elif isinstance(mine, list) and isinstance(value, list):
            base[key] = [*mine, *value]
        else:
            base[key] = value
    return base


def _models(settings: dict) -> dict:
    """MODEL_KEYS; modelSettings entries lose MODEL_ENTRY_DROP, empty ones dropped."""
    found = {k: settings[k] for k in MODEL_KEYS if k in settings}
    entries = found.get("modelSettings")
    if isinstance(entries, dict):
        kept = {}
        for name, entry in entries.items():
            left = (
                {k: v for k, v in entry.items() if k not in MODEL_ENTRY_DROP}
                if isinstance(entry, dict)
                else entry
            )
            if left != {}:
                kept[name] = left
        if kept:
            found["modelSettings"] = kept
        else:
            del found["modelSettings"]
    return found


def _credential(name: str) -> bool:
    return name.startswith("ANTHROPIC_") or name == "CLAUDE_CODE_OAUTH_TOKEN"


def _read_dir(path: str) -> str:
    if not printable_abs(path) or any(c in NOT_LITERAL for c in path):
        raise ConfigError(
            f"claude: read dir {path!r:.80} must be a printable absolute path without {NOT_LITERAL}"
        )
    return path


def _read_rules(reads: tuple[str, ...]) -> tuple[str, ...]:
    dirs = (d for read in reads for d in (_read_dir(read), _read_dir(os.path.realpath(read))))
    return tuple(dict.fromkeys(f"Read(/{d.rstrip('/')}/**)" for d in dirs))


def _state_path(store: str, cwd: str, sid: str) -> str:
    key = hashlib.sha256(f"{os.path.realpath(cwd)}\0{sid}".encode()).hexdigest()
    return os.path.join(store, f"{key}.json")


def _load(store: str, cwd: str, sid: str) -> dict:
    if not os.path.isabs(store):
        raise ConfigError("the store must be an absolute path")
    path = _state_path(store, cwd, sid)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConfigError(f"no resume state for session {sid}") from None
    with os.fdopen(fd, "rb") as f:
        found = os.fstat(f.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise ConfigError(f"{path}: not a regular file")
        data = f.read(STATE_MAX + 1)
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        state = None
    if not (
        isinstance(state, dict)
        and state.get("version") == 2
        and frozenset(state) == STATE_KEYS
        and all(strings(state[k]) for k in ("flags", "commands", "reads"))
        and type(state["project"]) is bool
        and all(state[k] is None or isinstance(state[k], str) for k in ("model", "effort", "name"))
        and state["sid"] == sid
        and isinstance(state["cwd"], str)
        and os.path.realpath(state["cwd"]) == os.path.realpath(cwd)
        and strings(state["dirs"])
        and isinstance(state["env"], dict)
        and environment_ok(state["env"])
    ):
        raise ConfigError(f"{path}: bad resume state")
    try:
        os.utime(path, follow_symlinks=False)
    except OSError as error:
        print(f"claude resume: cannot renew {path}: {error}", file=sys.stderr)
    return state


CHECKS = (
    common.FLAGS,
    common.MODES,
    ("allow", strings, "a list of strings"),
    ("accounts", lambda v: isinstance(v, Mapping), "a table of account tables"),
    *common.CHECKS,
)
ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
NOT_ACCOUNT = (
    "not an account name (1–64 lowercase letters, digits, '.', '_' or '-', starting with a letter"
    " or digit, and not setup-token)"
)


@dataclass(frozen=True)
class _Account:
    """A [clients.claude.accounts.<name>] table: its CLAUDE_CONFIG_DIR and display string."""

    dir: str
    display: str = ""


def _accounts(config: Mapping) -> dict[str, _Account]:
    """The registry, `default` always in it; a config error names the bad key."""
    found = {DEFAULT_ACCOUNT: _Account(os.path.expanduser("~/.claude"))}
    for name, table in config.get("accounts", {}).items():
        at = f"{TABLE}.accounts.{name}"
        if not (isinstance(name, str) and ACCOUNT_NAME.fullmatch(name)) or name == "setup-token":
            raise ConfigError(f"{TABLE}.accounts {name!r}: {NOT_ACCOUNT}")
        if not isinstance(table, Mapping):
            raise ConfigError(f"{at} must be a table")
        for key, value in table.items():
            if key not in ("dir", "account"):
                raise ConfigError(f"{at}.{key}: unknown key; an account has only dir and account")
            if not isinstance(value, str):
                raise ConfigError(f"{at}.{key} must be a string")
        account = replace(found[DEFAULT_ACCOUNT], display=table.get("account", ""))
        if "dir" in table:
            if name == DEFAULT_ACCOUNT:
                raise ConfigError(f"{at}.dir: the default account's dir is ~/.claude; remove it")
            if not printable_abs(path := os.path.expanduser(table["dir"])):
                raise ConfigError(f"{at}.dir must be an absolute path after ~ expansion")
            account = replace(account, dir=path)
        found[name] = account
    return found


def size(path: str) -> int:
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def texts(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]


def read(path: str, *, offset: int = 0, limit: int | None = None, tail: bool = False):
    """read_regular; a container-written home: no link, at most HOME_READ_MAX bytes."""
    path = os.path.abspath(path)
    root = docker_home_of(path, HOME.dir)
    if root is None:
        return read_regular(path, offset=offset, limit=limit, tail=tail)
    cap = HOME_READ_MAX if limit is None else min(limit, HOME_READ_MAX)
    return read_beneath(root, path, offset=offset, limit=cap, tail=tail)


class ClaudeCommon:
    UNSET = (
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_JOB_DIR",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        *sorted(LOCAL_ENV_VARS),
    )
    # claude -p otherwise kills a workflow after 10 idle minutes.
    ENV = MappingProxyType({"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
    NOTE = (
        "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits "
        "on a permission prompt."
    )
    # `/usage` reads the account's limits without a model request (CodexBar's direct probe).
    PROBE = (
        "claude",
        "-p",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources",
        "user",
        "--settings",
        '{"remoteControlAtStartup":false,"disableAllHooks":true}',
        "/usage",
    )

    def __init__(self, config: Mapping, *, selector):
        self.selector = selector
        common.check_config(config, TABLE, CHECKS)
        self.flags = tuple(config.get("flags", ()))
        self.modes = {p: common.mode_flags(config, p) for p in Permission}
        for key, flags in (
            ("flags", self.flags),
            *((f"modes.{p}", f) for p, f in self.modes.items()),
        ):
            if any(flag.startswith("--setting-sources") for flag in flags):
                raise ConfigError(
                    f"--setting-sources is set by the Claude client: remove it from {TABLE}.{key}"
                )
        self.allow = tuple(config.get("allow", ()))
        self.accounts = _accounts(config)
        self.tiers = dict(config.get("tiers", {}))
        self.efforts = dict(config.get("efforts", {}))
        self._env = dict(config.get("env", {}))

    def model(self, tier: int | None) -> str | None:
        if tier is None:
            return None
        if (model := self.tiers.get(str(tier))) is None:
            raise ConfigError(f"no model for tier {tier} in {TABLE}.tiers")
        return model

    def effort(self, effort: Effort | None) -> str | None:
        if effort is None:
            return None
        if (value := self.efforts.get(effort)) is None:
            raise ConfigError(f"no effort for {str(effort)!r} in {TABLE}.efforts")
        return value

    def env(self) -> dict[str, str]:
        return {k: v for k, v in self._env.items() if k not in LOCAL_ENV_VARS}

    def config_dir(self, env: Mapping[str, str] = MappingProxyType({})) -> str:
        """`env`: the run's own, over the config's."""
        return (
            env.get("CLAUDE_CONFIG_DIR")
            or self._env.get("CLAUDE_CONFIG_DIR")
            or os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.expanduser("~/.claude")
        )

    def home(self) -> ClientHome:
        return replace(HOME, files=self.home_files())

    def home_files(self) -> tuple[tuple[str, str], ...]:
        """settings.json: UNATTENDED, the host's `_models` and ENV_KEYS env, if readable."""
        try:
            settings = json.loads(_read(os.path.join(self.config_dir(), "settings.json")))
        except (ConfigError, ValueError, RecursionError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        env = settings.get("env")
        env = {k: env[k] for k in ENV_KEYS if k in env} if isinstance(env, dict) else {}
        found = {
            **UNATTENDED,
            **_models(settings),
            **({"env": env} if env else {}),
        }
        return (("settings.json", json.dumps(found)),)

    def transcript(self, cwd: str, sid: str, env: Mapping[str, str] = MappingProxyType({})) -> str:
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(self.config_dir(env), "projects", project, f"{sid}.jsonl")

    @staticmethod
    def continuation(
        record: dict, sid: str, config_dir: str
    ) -> tuple[str, str | None, str | None] | None:
        """A matching continuation's native id, existing target and copy cutoff."""
        target = record.get("continuedInSessionId")
        if not (
            record.get("type") == "continued-in"
            and record.get("sessionId") == sid
            and isinstance(target, str)
            and UUID.fullmatch(target)
        ):
            return None
        path = None
        projects = os.path.join(config_dir, "projects")
        with contextlib.suppress(OSError), os.scandir(projects) as entries:
            for entry in entries:
                candidate = os.path.join(entry.path, f"{target}.jsonl")
                if read(candidate, limit=0) is not None and os.path.commonpath(
                    (os.path.realpath(candidate), os.path.realpath(projects))
                ) == os.path.realpath(projects):
                    path = candidate
                    break
        timestamp = record.get("timestamp")
        return target, path, timestamp if isinstance(timestamp, str) else None

    @classmethod
    def walk(
        cls, path: str | None, sid: str, config_dir: str
    ) -> list[tuple[str, str | None, str | None]]:
        """Follow the last matching marker in each tail, retaining a missing final target."""
        hops: list[tuple[str, str | None, str | None]] = [(sid, path, None)]
        seen = {sid}
        while path:
            data = read(path, limit=TAIL, tail=True) or b""
            found = None
            for record in json_records(data.splitlines()):
                found = cls.continuation(record, sid, config_dir) or found
            if found is None or found[0] in seen:
                break
            hops.append(found)
            sid, path, _ = found
            seen.add(sid)
        return hops

    @property
    def store(self) -> str:
        return os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))

    def resume(self, cwd: str, sid: str) -> str:
        """A reference to save()'s launch state; it never holds env values."""
        return resume_command(cwd, {}, (COMMAND, "client-resume", "claude", self.store, cwd, sid))

    def save(self, spec: Spec) -> None:
        """What resume_main replays: spec's launch, Access and env."""
        access, sid = spec.access, self.uuid(spec.sid)
        env = {**self.env(), **access.env, **spec.env}
        state = {
            "version": 2,
            "cwd": access.cwd,
            "sid": sid,
            "dirs": list(access.dirs),
            "env": {k: v for k, v in env.items() if k not in LOCAL_ENV_VARS},
            "flags": list(spec.flags),
            "project": access.project,
            "commands": list(access.commands),
            "reads": list(access.reads),
            "model": self.model(spec.tier),
            "effort": self.effort(spec.effort),
            "name": spec.name,
        }
        text = json.dumps(state)
        if len(text) > STATE_MAX:
            raise ConfigError("claude resume state: over 1 MiB")
        try:
            os.makedirs(self.store, 0o700, exist_ok=True)
            save(_state_path(self.store, access.cwd, sid), text, durable=True)
        except OSError as error:
            raise ConfigError(f"claude resume state: {error}") from None
        for error in drop_stale(self.store):
            print(f"claude resume: {error}", file=sys.stderr)

    def resume_main(self, argv) -> int:
        """Execs claude resuming SID in CWD as launch() would. It runs here, not on the Host:
        auto mode, current config, the TUI without hooks.
        """
        try:
            store, cwd, sid = argv
        except ValueError:
            print("claude resume: expected STORE CWD SID", file=sys.stderr)
            return 1
        try:
            state = _load(store, cwd, self.uuid(sid))
            os.chdir(state["cwd"])
            path = self.transcript(state["cwd"], sid, state["env"])
            native, _, _ = next(
                hop
                for hop in reversed(self.walk(path, sid, self.config_dir(state["env"])))
                if hop[1]
            )
            command = self._argv(
                state["cwd"],
                self.selector(native, resume=True),
                Permission.AUTO,
                flags=tuple(state["flags"]),
                project=state["project"],
                dirs=tuple(state["dirs"]),
                commands=tuple(state["commands"]),
                reads=tuple(state["reads"]),
                model=state["model"],
                effort=state["effort"],
                name=state["name"],
            )
            os.execvpe(command[0], list(command), {**os.environ, **TUI_ENV, **state["env"]})
        except (ConfigError, OSError) as exc:
            print(f"claude resume: {exc}", file=sys.stderr)
            return 1
        return 0

    @staticmethod
    def uuid(sid) -> str:
        if isinstance(sid, str) and UUID.fullmatch(sid):
            return sid
        raise ConfigError(f"claude session ids are UUIDs, got {sid!r:.80}")

    def launch(
        self,
        spec: Spec,
        hooks: Mapping = MappingProxyType({}),
        *,
        head: tuple[str, ...] = (),
        output: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
        env: Mapping[str, str] = MappingProxyType({}),
        permission: Permission,
    ) -> Launch:
        sid = spec.sid
        model, effort = self.model(spec.tier), self.effort(spec.effort)
        access = spec.access
        cwd = access.cwd
        if not isinstance(sid, str):
            raise ConfigError(f"session ids are UUIDs, got {sid!r:.80}")
        merged = {
            k: v
            for k, v in {**env, **self.env(), **access.env, **spec.env}.items()
            if k not in LOCAL_ENV_VARS
        }
        transcript = self.transcript(cwd, sid, merged)
        native = sid
        if spec.resume:
            native, transcript, _ = next(
                hop
                for hop in reversed(self.walk(transcript, sid, self.config_dir(merged)))
                if hop[1]
            )
        argv = self._argv(
            cwd,
            self.selector(native, resume=spec.resume),
            permission,
            flags=spec.flags,
            project=access.project,
            dirs=access.dirs,
            commands=access.commands,
            reads=access.reads,
            model=model,
            effort=effort,
            name=spec.name,
            head=head,
            output=output,
            hooks=hooks,
            tail=tail,
        )
        return Launch(
            argv,
            env=merged,
            unset=self.UNSET,
            transcript=transcript or "",
            resume=self.resume(cwd, sid),
        )

    def _argv(
        self,
        cwd: str,
        selection: tuple[str, ...],
        permission: Permission,
        *,
        flags: tuple[str, ...],
        project: bool,
        dirs: tuple[str, ...],
        commands: tuple[str, ...],
        reads: tuple[str, ...],
        model: str | None,
        effort: str | None,
        name: str | None,
        head: tuple[str, ...] = (),
        output: tuple[str, ...] = (),
        hooks: Mapping = MappingProxyType({}),
        tail: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """One merged --settings (config, mode, `flags`, `hooks`): a repeated one is undefined.
        --mcp-config keeps a project's .mcp.json that --strict-mcp-config would drop.
        """
        if any(flag.word == "--setting-sources" for flag in scan(flags)):
            raise ConfigError("--setting-sources is set by the Claude client: use Access.project")
        configured, own = _split((*self.flags, *self.modes[permission]), cwd)
        supplied, rest = _split(flags, cwd)
        hooked = ({"hooks": dict(hooks)},) if hooks else ()
        mcp = os.path.join(cwd, ".mcp.json")
        allow = (
            *(f"Bash({command})" for command in commands),
            *_read_rules(reads),
            *self.allow,
        )
        strict = any(flag.arg == "--strict-mcp-config" for flag in scan(flags))
        return (
            "claude",
            *head,
            *selection,
            *(("--model", model) if model is not None else ()),
            *(("--effort", effort) if effort is not None else ()),
            *(("--name", name) if name is not None else ()),
            *own,
            "--setting-sources",
            "user,project,local" if project else "user",
            *(("--mcp-config", mcp) if project and strict and os.path.isfile(mcp) else ()),
            *output,
            *_settings(*configured, *supplied, *hooked),
            *(arg for d in dirs for arg in ("--add-dir", d)),
            *(("--allowedTools", *allow) if allow else ()),
            *rest,
            *tail,
        )

    def probe(self, account: str | None = None) -> Launch:
        """The config's env, else `account`'s dir (unset for ~/.claude); no API credentials."""
        env = {k: v for k, v in self.env().items() if not _credential(k)}
        found = sorted(
            name for name in {*os.environ, "CLAUDE_CODE_OAUTH_TOKEN"} if _credential(name)
        )
        if account is not None:
            env.pop("CLAUDE_CONFIG_DIR", None)
            path = self.accounts[account].dir
            if path == self.accounts[DEFAULT_ACCOUNT].dir:
                found.append("CLAUDE_CONFIG_DIR")
            else:
                env["CLAUDE_CONFIG_DIR"] = path
        return Launch(
            self.PROBE,
            env={**env, "DISABLE_AUTOUPDATER": "1"},
            unset=tuple(dict.fromkeys((*self.UNSET, *found))),
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """None, logged, when the output holds no usage panel; never retried here."""
        try:
            return usage.parse(output)
        except ValueError as error:
            print(f"claude usage: unknown: {error}", file=sys.stderr)
            return None

    def session_status(
        self,
        sid: str,
        cwd: str,
        *,
        transcript: str = "",
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> SessionStatus:
        """`env`: the run's access and spec env; its CLAUDE_CONFIG_DIR, if any, holds the transcript."""
        try:
            self.uuid(sid)
        except ConfigError:
            return SessionStatus(Resumable.NO, None, "")
        path = self.transcript(cwd, sid, env)
        root = os.path.realpath(os.path.dirname(path))
        chain = self.walk(path, sid, self.config_dir(env))
        if printable_abs(transcript):
            with contextlib.suppress(OSError):
                recorded = os.path.realpath(transcript)
                if os.path.commonpath((recorded, root)) == root and stat.S_ISREG(
                    os.stat(transcript).st_mode
                ):
                    path = recorded
        _, path, _ = next(
            hop
            for hop in reversed(
                chain if len(chain) > 1 else self.walk(path, sid, self.config_dir(env))
            )
            if hop[1]
        )
        assert path is not None
        root = os.path.realpath(os.path.dirname(path))
        try:
            mode = os.stat(path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            resumable = Resumable.NO
        except OSError:
            resumable = Resumable.UNKNOWN
        else:
            resumable = Resumable.YES if stat.S_ISREG(mode) else Resumable.UNKNOWN
        paths = [path]
        folder = os.path.splitext(path)[0]
        if os.path.commonpath((os.path.realpath(folder), root)) == root:
            for top, _, files in os.walk(folder):
                paths += (os.path.join(top, name) for name in files)
        times = []
        for each in paths:
            with contextlib.suppress(OSError):
                times.append(os.lstat(each).st_mtime)
        return SessionStatus(resumable, max(times, default=None), self.resume(cwd, sid))
