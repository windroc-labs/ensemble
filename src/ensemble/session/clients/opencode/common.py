"""OpenCode's config checks and session bindings: each run's sid, under its cwd, bound to the
session id (`ses_…`) OpenCode chose, which a resume selects.
"""

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import replace

from ensemble.session.clients.common import (
    CHECKS,
    FLAGS,
    MODES,
    check_config,
    mode_flags,
    scan,
    strings,
)
from ensemble.session.clients.opencode import probe
from ensemble.session.clients.opencode.transcript import NATIVE, Refused, database, titled
from ensemble.session.clients.opencode.transcript import updated as session_updated
from ensemble.session.engine.types import (
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    Effort,
    Launch,
    Permission,
    ProviderProbe,
    Resumable,
    SessionStatus,
    Spec,
    Usage,
)
from ensemble.utils import (
    LOCAL_ENV_VARS,
    STATE,
    GitError,
    drop_stale,
    read_beneath,
    run_git,
    save,
    sh,
)

TABLE = "[clients.opencode]"
# A container Host's session state dir: OpenCode keeps opencode.db under $XDG_DATA_HOME/opencode.
HOME = ClientHome("opencode_dir", "opencode", "XDG_DATA_HOME", "none")
HERE = os.path.dirname(os.path.abspath(__file__))
STORE = f"{STATE}/sessions/opencode"  # under HOME, resolved per call
BINDING_KEYS = frozenset({"version", "cwd", "sid", "native"})
BINDING_MAX = 4096
OLLAMA_PORT = 11434
OLLAMA_CONTEXT = 65536
# Headroom below a window: OpenCode's OUTPUT_TOKEN_MAX and COMPACTION_BUFFER
# (docs/customizing-roles-and-tasks.md › OpenCode sessions).
REPLY, BUFFER = 32000, 20000
# Set by OpenCode for its children (a nested start's are never its own).
IDENTITY = ("OPENCODE", "OPENCODE_PID", "OPENCODE_TERMINAL")
# Config OpenCode merges over or beside the launch's own OPENCODE_CONFIG_CONTENT.
OVERRIDES = (
    "OPENCODE_PERMISSION",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)
UNSET = (*sorted(LOCAL_ENV_VARS), *IDENTITY, *OVERRIDES)
SELECTORS = ("-s", "--session", "-c", "--continue", "--fork")
RESERVED = (*SELECTORS, "--format", "--attach", "--dir", "-m", "--model")
PERMISSIONS = ("--auto", "--yolo", "--dangerously-skip-permissions")
TUI_RESERVED = ("--prompt", "--pure")  # the client's prompt; --pure would skip its plugin
HEADLESS_RESERVED = ("--title",)  # TITLE's
TITLE = "ensemble-{sid}"  # a new headless run's session title, which finds a session never bound
VALUED = (
    "-m",
    "--model",
    "--agent",
    "--variant",
    "--format",
    "-f",
    "--file",
    "--title",
    "--attach",
    "-p",
    "--password",
    "-u",
    "--username",
    "--dir",
    "--port",
    "--prompt",
    "-s",
    "--session",
)
EXECUTABLE = (
    "executable",
    lambda v: isinstance(v, str) and (v == "" or v.strip() != "") and v.isprintable(),
    "a printable command, or empty for opencode on PATH",
)
VERSION = re.compile(r"(\d+)\.\d+\.\d+")
VERSION_WAIT = 10  # seconds
MAJOR = 1  # the OpenCode release the client supports
PORT = (
    "ollama_port",
    lambda v: type(v) is int and 0 < v < 65536,
    "an integer port 1–65535",
)
OLLAMA = "ollama"
SECRETS = "secrets/opencode"  # the broker's key files, under a container's state root
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
WORD = re.compile(r"\S+")


def _window(value) -> bool:
    return type(value) is int and value > 0


def _provider(name, table) -> bool:
    return (
        isinstance(name, str)
        and NAME.fullmatch(name) is not None
        and name != OLLAMA
        and isinstance(table, Mapping)
        and set(table) - {"context"} == {"base_url", "models"}
        and _window(table.get("context", OLLAMA_CONTEXT))
        and isinstance(table["base_url"], str)
        and probe.valid_url(table["base_url"])
        and strings(table["models"])
        and all(WORD.fullmatch(m) and m.isprintable() for m in table["models"])
    )


CONTEXT = ("ollama_context", _window, "a positive integer token count")
MODEL = (
    "model",
    lambda v: isinstance(v, str) and v.strip() != "" and v.isprintable(),
    "a provider/model name",
)
PROVIDERS = (
    "providers",
    lambda v: isinstance(v, Mapping) and all(_provider(k, t) for k, t in v.items()),
    "a table of OpenAI-compatible providers other than ollama, named by up to 64 letters, digits,"
    " '.', '_' or '-', each with exactly base_url (an http(s) URL) and models (a list of model"
    " names), and optionally context (its context window, a positive integer token count)",
)


PROTECTED = (".git", ".opencode", "opencode.json", "opencode.jsonc")  # names no run edits
CASED = (".git",)  # also denied in every case: OpenCode's patterns are case-sensitive, macOS's not
GIT_WAIT = 10  # seconds


def _ancestors(path: str) -> list[str]:
    """`path`'s proper ancestors, nearest first, "/" last."""
    found = []
    while (parent := os.path.dirname(path)) != path:
        found.append(parent)
        path = parent
    return found


def _spellings(name: str) -> list[str]:
    if name not in CASED:
        return [name]
    return [
        "".join(c)
        for c in itertools.product(*(dict.fromkeys((ch.lower(), ch.upper())) for ch in name))
    ]


def _check_paths(paths: Iterable[str]) -> None:
    for path in paths:
        if "*" in path or "?" in path:
            raise ConfigError(f"{path!r}: a wildcard in a path the opencode client grants")


def _grant_paths(paths: Iterable[str]) -> list[str]:
    """OpenCode tools check lexical paths; retain those alongside their resolved targets."""
    found = list(
        dict.fromkeys(p for path in paths for p in (os.path.abspath(path), os.path.realpath(path)))
    )
    _check_paths(found)
    return found


def _git(argv: list[str], timeout: int):
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_ENV_VARS}
    return sh(argv, timeout, env=env)


def worktree(cwd: str) -> str:
    """OpenCode's worktree for `cwd` (its core/src/git.ts): the toplevel of the nearest .git's dir;
    "/" without one or when git cannot read it.
    """
    found = next(
        (p for p in (cwd, *_ancestors(cwd)) if os.path.lexists(os.path.join(p, ".git"))), None
    )
    if found is None:
        return "/"
    try:
        run_git(found, "rev-parse", "--git-dir", "--git-common-dir", run=_git, timeout=GIT_WAIT)
    except (GitError, OSError, subprocess.SubprocessError):
        return "/"
    try:
        top = run_git(found, "rev-parse", "--show-toplevel", run=_git, timeout=GIT_WAIT)
    except (GitError, OSError, subprocess.SubprocessError):
        return found
    top = top.rstrip("\r\n")
    return os.path.realpath(os.path.join(found, top)) if top else found


def edit_rules(cwd: str, dirs: list[str], reads: list[str]) -> dict[str, str]:
    """OpenCode's edit rules: the last match wins, a deny even under --auto, on a path relative to
    `worktree`. Each path by its deepest grant (cwd and dirs allow, reads deny), else deny; never
    PROTECTED. One rule per level above the worktree, as "*" also matches "../".
    """
    _check_paths((cwd, *dirs, *reads))
    top = worktree(cwd)
    grants = dict(
        sorted(
            [*((path, "allow") for path in (cwd, *dirs)), *((path, "deny") for path in reads)],
            key=lambda grant: len(_ancestors(grant[0])),
        )
    )
    lineage = _ancestors(top)

    def above(level: int) -> str:
        return next((grants[p] for p in lineage[level - 1 :] if p in grants), "deny")

    rules = {"*": grants.get(top) or above(1)}
    levels = range(1, len(lineage) + 1) if any(p in grants for p in lineage) else (1,)
    rules.update({"../" * level + "*": above(level) for level in levels})
    for path, action in grants.items():
        if path != top and path not in lineage:
            rules[f"{os.path.relpath(path, top)}/*"] = action
    for name in PROTECTED:
        for spelled in _spellings(name):
            rules.update(
                dict.fromkeys((spelled, f"{spelled}/*", f"*/{spelled}", f"*/{spelled}/*"), "deny")
            )
    return rules


def image_path(found: Container, local: str) -> str:
    """`local`, a file of this package, at its path in the container's image."""
    package = __name__.split(".")[:-1]
    return os.path.join(found.source, *package, os.path.relpath(os.path.abspath(local), HERE))


def refuse(flags: Iterable[str], banned: tuple[str, ...], where: str) -> None:
    for flag in scan(tuple(flags), {word: word for word in VALUED}):
        # yargs also takes an option's camelCase spelling.
        kebab = re.sub(r"[A-Z]", lambda m: f"-{m.group().lower()}", flag.word)
        if flag.word in banned or kebab in banned:
            raise ConfigError(f"{flag.arg}: set by the opencode client, not {where}")


def refuse_env(env: Mapping[str, str], where: str) -> None:
    if found := sorted(set(env) & set(OVERRIDES)):
        raise ConfigError(f"{where}: {found[0]} would override the opencode client's config")


class OpenCodeCommon:
    def __init__(self, config: Mapping, *, selector):
        self.selector = selector
        check_config(
            config, TABLE, (FLAGS, MODES, PORT, CONTEXT, EXECUTABLE, MODEL, PROVIDERS, *CHECKS)
        )
        self.executable = config.get("executable") or "opencode"
        self.flags = tuple(config.get("flags", ()))
        self.modes = {p: mode_flags(config, p) for p in Permission}
        refuse(self.flags, (*RESERVED, *PERMISSIONS), f"{TABLE}.flags")
        for permission, flags in self.modes.items():
            refuse(flags, RESERVED, f"{TABLE}.modes.{permission}")
        self.port = config.get("ollama_port", OLLAMA_PORT)
        self.ollama_context = config.get("ollama_context", OLLAMA_CONTEXT)
        self.providers = {name: dict(t) for name, t in config.get("providers", {}).items()}
        self.tiers = dict(config.get("tiers", {}))
        self.default: str | None = config.get("model")
        named = {f"tiers.{t}": m for t, m in self.tiers.items()}
        for key, model in (*named.items(), *((("model", self.default),) if self.default else ())):
            self._listed(key, model)
        self.efforts = dict(config.get("efforts", {}))
        self.env = dict(config.get("env", {}))
        refuse_env(self.env, f"{TABLE}.env")

    def _listed(self, key: str, model: str) -> None:
        """Refuses a model whose provider is neither ollama nor a [clients.opencode.providers] one."""
        provider, _, name = model.partition("/")
        if not name or (provider != OLLAMA and provider not in self.providers):
            raise ConfigError(
                f"{TABLE}.{key} {model!r}: not ollama/<model> or <provider>/<model> of a"
                f" {TABLE[:-1]}.providers.<provider>] table"
            )

    def _mapped(self, table: Mapping[str, str], key: str | None, missing: str) -> str | None:
        if key is None:
            return None
        if (found := table.get(key)) is None:
            raise ConfigError(f"{missing} in {TABLE}")
        return found

    def model(self, tier: int | None) -> str:
        """The tier's model, else `model`: never OpenCode's pick."""
        if tier is None:
            if self.default is None:
                raise ConfigError(f"no model for a run without a tier: set {TABLE} model")
            return self.default
        if (found := self.tiers.get(str(tier))) is None:
            raise ConfigError(f"no model for tier {tier} in {TABLE}")
        return found

    def variant(self, effort: Effort | None) -> str | None:
        return self._mapped(self.efforts, effort, f"no variant for effort {str(effort)!r}")

    def selection(
        self, spec: Spec, context: ClientContext, extra: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """A launch's checks and its session selection: on resume, (cwd, sid)'s session.
        OpenCode 1.x runs any cwd's .opencode plugins and opencode.json, so a cwd outside a
        container starts only when trusted; an isolated Host's context alone is yolo.
        """
        if context.permission is not Permission.YOLO and not spec.access.project:
            raise ConfigError(
                f"cwd {spec.access.cwd} is untrusted on a non-isolated Host, and OpenCode runs a"
                " cwd's .opencode plugins and opencode.json: run it on an isolated Host (Docker)"
                " or in a trusted cwd"
            )
        refuse(spec.flags, (*RESERVED, *PERMISSIONS, *extra), "a run's flags")
        if not spec.resume:
            return ()
        sid = spec.sid or ""
        env = {**self.env, **spec.access.env, **spec.env}
        db = database(env if context.container is not None else {**os.environ, **env})
        native = self.session(spec.access.cwd, sid, db)
        if native is None:
            raise ConfigError(f"no OpenCode session bound to run ID {sid!r}")
        return self.selector(native, resume=True)

    def home(self) -> ClientHome:
        return replace(HOME, providers=tuple(sorted(self.providers)))

    def executable_for(self, env: Mapping[str, str], cwd: str, context: ClientContext) -> str:
        """A container's image opencode (the broker checks its version), else program's."""
        if (found := context.container) is not None:
            return os.path.join(found.bin_dir, "opencode")
        return self.program(env, cwd)

    @staticmethod
    def transcript(env: Mapping[str, str], context: ClientContext) -> str:
        """A container launch's opencode.db, which its Host checks before a resume; else ""."""
        return database(env) if context.container is not None else ""

    def program(self, env: Mapping[str, str], cwd: str) -> str:
        """The executable, once its --version, run as the launch would run it, is OpenCode 1.x."""
        exe, fix = self.executable, f"set {TABLE} executable to an OpenCode {MAJOR}.x"
        run = {k: v for k, v in {**os.environ, **env}.items() if k not in LOCAL_ENV_VARS}
        try:
            done = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=VERSION_WAIT,
                stdin=subprocess.DEVNULL,
                env=run,
                cwd=cwd
                if os.path.isdir(cwd)
                else None,  # a run's cwd may not exist before its start
            )
        except OSError as error:
            raise ConfigError(
                f"opencode executable {exe!r}: {error.strerror or error}: {fix}"
            ) from None
        except subprocess.TimeoutExpired:
            raise ConfigError(f"opencode executable {exe!r}: --version timed out: {fix}") from None
        found = VERSION.search(done.stdout or "") if done.returncode == 0 else None
        if found is None:
            raise ConfigError(f"opencode executable {exe!r}: --version printed no version: {fix}")
        if int(found[1]) != MAJOR:
            raise ConfigError(f"opencode {found[0]} ({exe}) is not {MAJOR}.x: {fix}")
        return exe

    def inline(self, spec: Spec, context: ClientContext, plugin: str | None = None) -> dict:
        """The launch's providers, permissions and plugin. A provider's API key: the container's
        broker-written file, else OpenCode's auth.json.
        """
        access = spec.access
        commands = dict.fromkeys(access.commands, "allow")
        permission: dict = {
            "bash": {"*": "ask", **commands} if commands else "ask",
        }
        dirs = _grant_paths(access.dirs)
        reads = _grant_paths(access.reads)
        if outside := [*reads, *dirs]:
            permission["external_directory"] = {
                pattern: "allow" for d in outside for pattern in (d, f"{d}/*")
            }
        permission["edit"] = edit_rules(
            os.path.realpath(access.cwd), [os.path.realpath(d) for d in access.dirs], reads
        )
        if context.permission is Permission.YOLO:
            # --auto replies in the TUI after permission.asked; approve at the backend so the
            # plugin never reports those transient asks as blocked. Keep agent and edit denies.
            permission.update(
                dict.fromkeys(("bash", "read", "external_directory", "doom_loop"), "allow")
            )
        ollama = f"http://{context.host_address}:{self.port}/v1"
        providers = {
            OLLAMA: self._provider("Ollama", ollama, OLLAMA, (), self.ollama_context),
            **{
                name: self._provider(
                    name,
                    table["base_url"],
                    name,
                    table["models"],
                    table.get("context", OLLAMA_CONTEXT),
                )
                for name, table in sorted(self.providers.items())
            },
        }
        if (found := context.container) is not None:
            for name in self.providers:
                key = f"{found.state_root}/{SECRETS}/{name}.key"
                providers[name]["options"]["apiKey"] = f"{{file:{key}}}"
        return {
            "$schema": "https://opencode.ai/config.json",
            **({"plugin": [plugin]} if plugin is not None else {}),
            **(
                {"skills": {"paths": ["/opt/ensemble/plugins", "/opt/windroc/mini-op/skills"]}}
                if context.container
                else {}
            ),
            "autoupdate": False,
            # A refused call ends OpenCode's turn otherwise (session/processor.ts).
            "experimental": {"continue_loop_on_deny": True},
            "compaction": {"reserved": 0},  # it compacts at limit.input - reserved
            # Built-in providers (OpenCode Zen's included) off.
            "enabled_providers": list(providers),
            "provider": providers,
            "permission": permission,
        }

    def _provider(self, title: str, url: str, name: str, listed, window: int) -> dict:
        """An OpenAI-compatible provider: its listed models, then those tiers or model name."""
        prefix = f"{name}/"
        named = (*self.tiers.values(), *((self.default,) if self.default else ()))
        mapped = (m.removeprefix(prefix) for m in named if m.startswith(prefix))
        output = min(REPLY, window // 4)
        limit = {
            "context": window,
            "input": window - min(output + BUFFER, window // 2),
            "output": output,
        }
        return {
            "npm": "@ai-sdk/openai-compatible",
            "name": title,
            "options": {"baseURL": url},
            "models": {
                model: {"name": model, "limit": limit}
                for model in dict.fromkeys((*listed, *mapped))
            },
        }

    def environment(
        self, spec: Spec, context: ClientContext, plugin: str | None = None
    ) -> dict[str, str]:
        """The config's env, then Access's and the Spec's, under the client's own."""
        access = spec.access
        supplied = {**access.env, **spec.env}
        refuse_env(supplied, "a run's env")
        env = {k: v for k, v in {**self.env, **supplied}.items() if k not in LOCAL_ENV_VARS}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.inline(spec, context, plugin))
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        return env

    def usage_provider(self) -> ProviderProbe | None:
        provider = self.model(None).partition("/")[0]
        return (
            None
            if provider == OLLAMA
            else ProviderProbe(provider, self.providers[provider]["base_url"])
        )

    def probe(self) -> Launch:
        """On this machine, so the loopback, never the Host's address."""
        request = self.usage_provider()
        args = (
            (str(self.port),)
            if request is None
            else ("--provider", request.provider, request.base_url)
        )
        return Launch(
            (os.path.realpath(sys.executable), "-I", probe.__file__, *args),
            env={k: v for k, v in self.env.items() if k not in LOCAL_ENV_VARS},
            unset=UNSET,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """A model listing: Ollama is up, and a local model has no limits; anything else unknown."""
        try:
            found = json.loads(output)
        except (ValueError, RecursionError):
            return None
        if isinstance(found, dict) and isinstance(found.get("models"), list):
            return Usage(None, None)
        if isinstance(found, dict) and set(found) == {"available"} and found["available"] is True:
            return Usage(None, None)
        return None

    @property
    def store(self) -> str:
        return os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))

    def bind(self, cwd: str, sid: str, native: str) -> None:
        Bindings(self.store).bind(cwd, sid, native)

    def native(self, cwd: str, sid: str) -> str | None:
        return Bindings(self.store).native(cwd, sid)

    def unbind(self, cwd: str, sid: str) -> None:
        Bindings(self.store).unbind(cwd, sid)

    def session(self, cwd: str, sid: str, db: str) -> str | None:
        """(cwd, sid)'s bound session, else its headless run's by TITLE in `db`, bound then: opencode
        prints its session id only once its model streams, so a harness killed before then bound none.
        """
        native = self.native(cwd, sid)
        if native is None and os.path.isfile(db):
            try:
                native = titled(db, TITLE.format(sid=sid))
            except Refused as error:
                raise ConfigError(f"opencode session lookup: {error}") from None
            if native is not None:
                self.bind(cwd, sid, native)
        return native

    def session_status(
        self, cwd: str, sid: str, env: Mapping[str, str], *, transcript: str = ""
    ) -> SessionStatus:
        """From the bound session's row in opencode.db: the run's `transcript`, else the db the
        run's opencode would find.
        """
        db = transcript or database({**os.environ, **self.env, **env})
        try:
            native = self.session(cwd, sid, db)
        except ConfigError:
            return SessionStatus(Resumable.UNKNOWN, None, "")
        if native is None:
            return SessionStatus(Resumable.NO, None, "")
        try:
            updated = session_updated(db, native)
        except Refused:
            return SessionStatus(Resumable.UNKNOWN, None, "")
        if updated is None:
            return SessionStatus(Resumable.NO, None, "")
        return SessionStatus(Resumable.YES, updated, "")


class Homed:
    """The Docker home both OpenCode adapters name."""

    common: OpenCodeCommon

    def _home(self) -> ClientHome:
        return self.common.home()


class Bindings:
    """Each run's (cwd, sid) bound to its OpenCode session, one file each under `store`."""

    def __init__(self, store: str):
        self.store = store

    def _binding(self, cwd: str, sid: str) -> str:
        key = hashlib.sha256(f"{os.path.realpath(cwd)}\0{sid}".encode()).hexdigest()
        return os.path.join(self.store, f"{key}.json")

    def bind(self, cwd: str, sid: str, native: str) -> None:
        if not NATIVE.fullmatch(native):
            raise ConfigError(f"invalid OpenCode session id {native!r:.80}")
        record = {"version": 1, "cwd": os.path.realpath(cwd), "sid": sid, "native": native}
        try:
            os.makedirs(self.store, 0o700, exist_ok=True)
            save(self._binding(cwd, sid), json.dumps(record), durable=True)
        except OSError as error:
            raise ConfigError(f"opencode session binding: {error}") from None
        for error in drop_stale(self.store):
            print(f"opencode session binding: {error}", file=sys.stderr)

    def unbind(self, cwd: str, sid: str) -> None:
        try:
            os.unlink(self._binding(cwd, sid))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ConfigError(f"opencode session binding: {error}") from None

    def native(self, cwd: str, sid: str) -> str | None:
        """The session bound to (cwd, sid); None when none is (a symlink counts as none)."""
        path = self._binding(cwd, sid)
        data = read_beneath(self.store, path, limit=BINDING_MAX + 1)
        if data is None:
            return None
        try:
            record = json.loads(data) if len(data) <= BINDING_MAX else None
        except (ValueError, RecursionError):
            record = None
        if (
            not isinstance(record, dict)
            or frozenset(record) != BINDING_KEYS
            or record["version"] != 1
            or record["cwd"] != os.path.realpath(cwd)
            or record["sid"] != sid
            or not isinstance(record["native"], str)
            or not NATIVE.fullmatch(record["native"])
        ):
            raise ConfigError(f"{path}: not an opencode session binding for run ID {sid!r}")
        return record["native"]
