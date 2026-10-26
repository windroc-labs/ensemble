"""Packaged orchestrator settings, registered Delegates and resolved role runs."""

import os
import re
import subprocess
import sys
from enum import StrEnum
from types import MappingProxyType

import ensemble.utils as utils
from ensemble import delegate
from ensemble.orchestrator.engine import types
from ensemble.orchestrator.engine.flow import workflow
from ensemble.orchestrator.engine.log import config_error
from ensemble.orchestrator.engine.types import (
    KEYCHAIN_NAME,
    UUID_RE,
    AppKind,
    Config,
    ConfigError,
    Docs,
    LinearApp,
    LinearSettings,
    Role,
    State,
    TrackerKind,
)
from ensemble.utils import GUARD, STATE, temp_dirs, under

LOCAL = utils.LOCAL

REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")
_ROLE_ISSUE = r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)"

BOARD_ENV = "ENSEMBLE_BOARD_DIR"

TOP_KEYS = {
    "tracker",
    "workflow",
    "task_tiers",
    "client_max_runs",
    "usage_limit",
    "roles",
    "local_clones",
    "core",
    "work_dir",
    "delegates",
    "linear",
    "local",
}
LINEAR_KEYS = {
    "team",
    "states",
    "human_members",
    "harness",
    "manager",
    "task_label_group",
    "task_labels",
    "agent_label_group",
    "agent_labels",
    "roles",
    "project_repos",
}
ROLE_KEYS = {"max_runs"}


class AppKey(StrEnum):
    """An OAuth app table's keys ([linear.harness])."""

    CLIENT_ID = "client_id"
    SECRET_SERVICE = "secret_service"
    TOKEN_SERVICE = "token_service"
    USER_ID = "user_id"


LINEAR_ROLE_KEYS = set(AppKey) | {"renewal"}


REQUIRED = (
    "linear.team",
    *(f"linear.harness.{k}" for k in AppKey),
    "linear.task_label_group",
    *(f"linear.states.{k}" for k in State),
    *(f"linear.roles.*.{k}" for k in AppKey),
)


TASKS = workflow.DEFAULT


def report_error(src: str, error: ConfigError, *, dry: bool = False) -> None:
    if not error.logs_dir:
        print(error.msg, file=sys.stderr)
    else:
        config_error(src, error, dry=dry)


def linear_settings(cfg: Config, command: str) -> LinearSettings:
    """cfg's Linear settings; raises ConfigError, command's refusal, on another tracker."""
    if cfg.linear is None:
        raise ConfigError(f'{command} needs [orchestrator] tracker = "linear"')
    return cfg.linear


def manager_app(linear: LinearSettings) -> LinearApp:
    """The manager's app, the issue commands' identity; ConfigError naming each unset key."""
    if unset := [f"linear.manager.{k}" for k in AppKey if not getattr(linear.manager, k)]:
        raise ConfigError(
            "orchestrator/config.toml: ensemble-orchestrator issue needs the manager's Linear "
            f"OAuth app: required, unset: {', '.join(unset)}: set them in {LOCAL} "
            "(orchestrator/config.toml's `# local:` lines)"
        )
    return linear.manager


def load(*, path: str | None = None) -> Config:
    try:
        cfg = utils.read_resources(
            (("ensemble.orchestrator", "config.toml"),),
            paths={"ensemble.orchestrator": path} if path is not None else None,
        )
    except (OSError, ValueError) as e:
        raise ConfigError(f"orchestrator/config.toml: {e}") from None
    cfg["tracker"] = _tracker(cfg)
    board = _board() if cfg["tracker"] is TrackerKind.LOCAL else None
    work = _work_dir(cfg, board=board)
    try:
        _workflow(cfg)
        kind, delegates = _delegates(cfg)
        index = _index(delegates[kind])
        _load_config(cfg, LOCAL, board)
        return _load(cfg, work, kind, delegates, index, board=board)
    except ConfigError as e:
        e.logs_dir = os.path.join(work, "logs")
        raise


def _workflow(cfg):
    """Load TASKS from workflow: "" → the packaged file; else that file, whole."""
    v = cfg.get("workflow", "")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if v != "" and not os.path.isabs(path):
        raise ConfigError(
            f'orchestrator/config.toml: workflow must be "" (the packaged workflow) or a printable absolute or ~ path: {v!r}'
        )
    try:
        TASKS.load(path or None)
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _index(delegate_cls):
    """The Delegate's role → task index; every stage's role must be in it."""
    try:
        index = delegate_cls.roles()
    except (OSError, ValueError, delegate.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for name in TASKS:
        if name not in index:
            raise ConfigError(
                f"{TASKS.path}: stage {name}: not a role the Delegate knows (its charters: core and [delegate] team_dirs)"
            )
    return index


def tui_prefix(role, issue):
    name = f"{role}-{issue}"
    if not re.fullmatch(_ROLE_ISSUE, name):
        raise ValueError(f"TUI session prefix {name!r}: want {_ROLE_ISSUE}")
    return name + "-"


def tui_name(role, issue, sid):
    return tui_prefix(role, issue) + sid[:8]


def path():
    """The PATH the router, promote and their tools and runs get, read at call: ENSEMBLE_PATH (if
    set), this installation's bin/ (ensemble-orchestrator, the harness's write-back callback), then the
    default dirs.
    """
    [root] = utils.module_roots(types)
    default = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"
    found = os.path.join(os.path.dirname(root), "bin")
    return ":".join(p for p in (os.environ.get("ENSEMBLE_PATH"), found, default) if p)


def shell(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path()}
    if argv[:1] == ["gh"] and "--hostname" not in argv:
        env["GH_HOST"] = "github.com"
    return utils.sh(argv, timeout, env=env)


def writable(work):
    return (work, *temp_dirs())


def clone_error(clone, slug, *, run=None, host="github.com"):
    run = run or shell
    if not clone.isprintable():
        return f"{clone!r} has a non-printable character"
    if not os.path.exists(clone):
        return f"{clone} does not exist"
    try:
        found, _ = utils.resolve(
            clone, run=lambda argv, timeout: run(["git", *GUARD, *argv[1:]], timeout)
        )
    except utils.Invalid as e:
        return str(e)
    except (
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as e:  # UnicodeDecodeError, from non-UTF-8 git output, is a ValueError
        return f"git: {e}"
    expected = (
        utils.Repo(*slug.split("/"))
        if slug.count("/") == 2
        else utils.Repo(host.lower(), *slug.split("/"))
    )
    if found.slug.lower() != expected.slug.lower():
        return f"origin is {found.slug}, not {expected.slug}"
    return None


def _tracker(cfg):
    v = cfg.get("tracker", TrackerKind.LOCAL)
    if v not in tuple(TrackerKind):
        raise ConfigError(
            f"orchestrator/config.toml: tracker: want one of {', '.join(TrackerKind)}: {v!r}"
        )
    return TrackerKind(v)


def _board():
    v = os.environ.get(BOARD_ENV) or os.path.join(STATE, "board")
    path = os.path.expanduser(v) if v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError(f"{BOARD_ENV} must be a printable absolute or ~ path: {v!r}")
    return os.path.realpath(path)


def _work_dir(cfg, clones=None, *, board=None):
    """The state root: work_dir, or a local board's <board>/.state."""
    name, v = "work_dir", cfg.get("work_dir", STATE)
    if board is not None:
        name, v = "the board's .state", os.path.join(board, ".state")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError(
            f"orchestrator/config.toml: work_dir must be a printable absolute or ~ path: {v!r}"
        )
    path = os.path.realpath(path)
    try:
        for k, clone in (clones or {}).items():
            if utils.overlaps(path, clone):
                raise ConfigError(
                    f"orchestrator/config.toml: {name} must neither lie in nor contain local_clones.{k} {clone}: {v!r}"
                )
    except OSError as error:
        raise ConfigError(f"orchestrator/config.toml: {name}: {error}") from None
    return path


def _load(cfg, work, kind, delegates, index, *, board):
    roles = _runnable(cfg, work, delegates[kind], index)
    docs = _docs(roles, LOCAL, work)
    _clone_origins(cfg["local_clones"], roles, docs)
    frozen = {
        k: MappingProxyType(dict(cfg[k]))
        for k in ("task_tiers", "client_max_runs", "usage_limit", "local_clones")
    }
    return Config(
        roles=MappingProxyType(roles),
        tracker=cfg["tracker"],
        linear=_linear_settings(cfg["linear"]) if cfg["tracker"] is TrackerKind.LINEAR else None,
        board=board,
        local_prefix=cfg["local"]["issue_prefix"],
        work_dir=work,
        docs=docs,
        stages=MappingProxyType(_stage_order()),
        task_tiers=frozen["task_tiers"],
        client_max_runs=frozen["client_max_runs"],
        usage_limit=frozen["usage_limit"],
        local_clones=frozen["local_clones"],
        delegate_kind=kind,
        delegates=MappingProxyType(delegates),
        core=utils.freeze(cfg.get("core", {})),
    )


def _linear_settings(linear):
    frozen = {
        k: MappingProxyType(dict(linear[k]))
        for k in ("states", "task_labels", "agent_labels", "project_repos")
    }
    return LinearSettings(
        team=linear["team"],
        human_members=tuple(linear.get("human_members") or ()),
        harness=LinearApp(
            "harness",
            AppKind.HARNESS,
            "linear.harness",
            **{k.value: linear["harness"][k] for k in AppKey},
            renewal=linear["harness"].get("renewal", "transport"),
        ),
        manager=LinearApp(
            "manager",
            AppKind.MANAGER,
            "linear.manager",
            **{k.value: linear["manager"].get(k, "") for k in AppKey},
            renewal=linear["manager"].get("renewal", "transport"),
        ),
        task_label_group=linear["task_label_group"],
        agent_label_group=linear["agent_label_group"],
        roles=MappingProxyType(
            {
                name: LinearApp(name, AppKind.ROLE, f"linear.roles.{name}", **p)
                for name, p in linear["roles"].items()
            }
        ),
        **frozen,
    )


def _delegates(cfg) -> tuple[str, dict[str, type]]:
    registry = cfg.get("delegates")
    if not isinstance(registry, dict):
        raise ConfigError("orchestrator/config.toml: [delegates] must be a table")
    classes = {}
    for kind, entry in registry.items():
        if kind == "default":
            continue
        if not isinstance(entry, dict) or set(entry) != {"implementation"}:
            raise ConfigError(
                f"orchestrator/config.toml: [delegates.{kind}] takes only implementation"
            )
        try:
            classes[kind] = utils.load_class(entry["implementation"], delegate.Delegate)
        except ValueError as error:
            raise ConfigError(f"orchestrator/config.toml: [delegates.{kind}]: {error}") from None
    default = registry.get("default")
    if not isinstance(default, str) or default not in classes:
        raise ConfigError(
            f"orchestrator/config.toml: [delegates] default: want one of {', '.join(sorted(classes))}"
        )
    return default, classes


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(linear):
    if not _uuid(linear.get("team")):
        raise ConfigError(
            f"orchestrator/config.toml: linear.team must be a Linear team id (UUID): {linear.get('team')!r}"
        )
    states = linear["states"]
    if extra := sorted(set(states) - set(State)):
        raise ConfigError(
            f"orchestrator/config.toml: [linear.states] has unknown keys: {', '.join(extra)}"
        )
    for k in State:
        if not _uuid(states[k]):
            raise ConfigError(
                f"orchestrator/config.toml: linear.states.{k} must be a Linear workflow state id (UUID): {states[k]!r}"
            )
    if not _uuid(linear.get("task_label_group")):
        raise ConfigError(
            f"orchestrator/config.toml: linear.task_label_group must be a Linear label group id (UUID): {linear.get('task_label_group')!r}"
        )


def _load_config(cfg, local, board):
    board_settings = cfg.get("local", {})
    if not isinstance(board_settings, dict):
        raise ConfigError("orchestrator/config.toml: local must be a table")
    if extra := sorted(set(board_settings) - {"issue_prefix"}):
        raise ConfigError(f"orchestrator/config.toml: unknown keys in [local]: {', '.join(extra)}")
    prefix = board_settings.get("issue_prefix")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z][A-Z0-9]*", prefix):
        raise ConfigError(
            "orchestrator/config.toml: local.issue_prefix: want an uppercase issue prefix"
        )
    linear = cfg.setdefault("linear", {})
    if not isinstance(linear, dict):
        raise ConfigError("orchestrator/config.toml: [linear] must be a table")
    role_tables = linear.setdefault("roles", {})
    if not isinstance(role_tables, dict):
        raise ConfigError("orchestrator/config.toml: [linear.roles] must be a table")
    roles = _stage_roles(cfg, linear)
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise ConfigError(f"orchestrator/config.toml: unknown keys: {', '.join(extra)}")
    if extra := sorted(set(linear) - LINEAR_KEYS):
        raise ConfigError(f"orchestrator/config.toml: unknown keys in [linear]: {', '.join(extra)}")
    if cfg["tracker"] is TrackerKind.LINEAR:
        _linear(cfg, local)
    _role_fields(roles)
    _local_clones(cfg)
    _work_dir(cfg, cfg["local_clones"], board=board)
    _task_tiers(cfg)
    _client_max_runs(cfg)
    _usage_limit(cfg)


def _linear(cfg, local):
    _app_table(cfg["linear"], "harness")
    _app_table(cfg["linear"], "manager")
    if missing := utils.unset(cfg, REQUIRED):
        raise ConfigError(
            f"orchestrator/config.toml: required, unset: {', '.join(missing)}: set them in {local} (orchestrator/config.toml's `# local:` lines)"
        )
    linear = cfg["linear"]
    _check_ids(linear)
    _app(linear, "harness")
    _manager(linear, _linear_roles(linear))
    _project_repos(linear)
    _task_labels(linear)
    _agent_labels(linear)


def _app_table(linear, name):
    """[linear.<name>]: a table of keys only (unset ones REQUIRED reports)."""
    table = linear.setdefault(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"orchestrator/config.toml: [linear.{name}] must be a table")
    if extra := sorted(set(table) - set(AppKey) - {"renewal"}):
        raise ConfigError(
            f"orchestrator/config.toml: [linear.{name}] has unknown keys: {', '.join(extra)}"
        )


def _app(linear, name, keys=tuple(AppKey)):
    """[linear.<name>]: keys of an OAuth app's Client ID, its secret's and token's Keychain
    services (two) and its Linear user id."""
    table = linear[name]
    if table.get("renewal", "transport") not in ("transport", "broker"):
        raise ConfigError(
            f"orchestrator/config.toml: linear.{name}.renewal must be transport or broker"
        )
    for key in keys:
        what, valid = _APP_VALUES[key]
        if not valid(table[key]):
            raise ConfigError(
                f"orchestrator/config.toml: linear.{name}.{key} must be {what}: {table[key]!r}"
            )
    if {AppKey.SECRET_SERVICE, AppKey.TOKEN_SERVICE} <= set(keys) and (
        table["secret_service"] == table["token_service"]
    ):
        raise ConfigError(
            f"orchestrator/config.toml: linear.{name}.secret_service {table['secret_service']!r} "
            f"is also linear.{name}.token_service"
        )


def _client_id(v):
    return isinstance(v, str) and bool(v) and v.isprintable() and not any(c.isspace() for c in v)


def _keychain_name(v):
    return isinstance(v, str) and bool(KEYCHAIN_NAME.fullmatch(v))


_APP_VALUES = {
    AppKey.CLIENT_ID: ("a Linear OAuth app Client ID", _client_id),
    AppKey.SECRET_SERVICE: ("a Keychain service name", _keychain_name),
    AppKey.TOKEN_SERVICE: ("a Keychain service name", _keychain_name),
    AppKey.USER_ID: ("a Linear user id (UUID)", _uuid),
}


def _harness_services(linear):
    """{Keychain service: the harness setting naming it}."""
    return {
        linear["harness"][k]: f"linear.harness.{k}" for k in ("secret_service", "token_service")
    }


def _manager(linear, owners):
    """[linear.manager]: its set keys, each valid and no other app's (owners: _linear_roles');
    unset ones refuse only the issue commands (manager_app)."""
    table = linear["manager"]
    given = tuple(k for k in AppKey if table.get(k, "") != "")
    _app(linear, "manager", given)
    for key in given:
        taken = owners["service" if key in _SERVICES else key]
        if table[key] in taken:
            raise ConfigError(
                f"orchestrator/config.toml: linear.manager.{key} {table[key]!r} is also "
                f"{taken[table[key]]}"
            )


_SERVICES = (AppKey.SECRET_SERVICE, AppKey.TOKEN_SERVICE)


def _stage_roles(cfg, linear):
    """[roles] and [linear.roles]: one table per stage, in the workflow's order. A table for a
    packaged stage the workflow leaves out is dropped (the packaged config.toml has one); one for
    any other non-stage role is an error."""
    packaged = set(workflow.Workflow().load())
    out = {}
    for where, tables, keys in (
        ("roles", cfg.setdefault("roles", {}), ROLE_KEYS),
        ("linear.roles", linear["roles"], LINEAR_ROLE_KEYS),
    ):
        if not isinstance(tables, dict):
            raise ConfigError(f"orchestrator/config.toml: [{where}] must be a table")
        for name, p in tables.items():
            if not isinstance(p, dict):
                raise ConfigError(f"orchestrator/config.toml: [{where}.{name}] must be a table")
            if extra := sorted(set(p) - keys):
                raise ConfigError(
                    f"orchestrator/config.toml: [{where}.{name}] has unknown keys: {', '.join(extra)}"
                )
            if name not in TASKS and name not in packaged:
                raise ConfigError(
                    f"orchestrator/config.toml: [{where}.{name}] names no stage of the workflow"
                )
        out[where] = {name: tables.get(name, {}) for name in TASKS}
    cfg["roles"], linear["roles"] = out["roles"], out["linear.roles"]
    return cfg["roles"]


def _linear_roles(linear):
    """Role apps: validated without reading their credentials; Client IDs and services are
    unique. Returns the harness's and roles' {value: its setting} of client_id, service (both
    services) and user_id."""
    clients = {linear["harness"]["client_id"]: "linear.harness.client_id"}
    services = _harness_services(linear)
    users = {linear["harness"]["user_id"]: "linear.harness.user_id"}
    for name, p in linear["roles"].items():
        table = f"roles.{name}"
        _app({table: p}, table)
        for k, owners in (
            ("client_id", clients),
            ("secret_service", services),
            ("token_service", services),
        ):
            field = f"linear.{table}.{k}"
            if p[k] in owners:
                raise ConfigError(
                    f"orchestrator/config.toml: {field} {p[k]!r} is also {owners[p[k]]}"
                )
            owners[p[k]] = field
        users.setdefault(p["user_id"], f"linear.{table}.user_id")
    return {"client_id": clients, "service": services, "user_id": users}


def _role_fields(roles):
    for name, p in roles.items():
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] max_runs must be a whole number >= 1: {n!r}"
            )


def _project_repos(linear):
    projects = linear.setdefault("project_repos", {})
    if not isinstance(projects, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.project_repos must be a table of "<Linear project id>" = "<owner>/<name>"'
        )
    for k, v in projects.items():
        if not _uuid(k) or not utils.repo_slug(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}"
            )


def _local_clones(cfg):
    clones = cfg.setdefault("local_clones", {})
    if not isinstance(clones, dict):
        raise ConfigError(
            'orchestrator/config.toml: local_clones must be a table of "<owner>/<name>" = "<path>"'
        )
    seen = {}
    for k, v in clones.items():
        try:
            parts = k.split("/")
            valid = bool(utils.repo_slug(k)) or (
                len(parts) == 3
                and bool(utils.repo_slug("/".join(parts[1:])))
                and utils.Repo.parse(f"{parts[0]}/owner/repo").host == parts[0].lower()
            )
        except utils.Invalid:
            valid = False
        if (
            not valid
            or not isinstance(v, str)
            or not v.startswith(("/", "~"))
            or not v.isprintable()
        ):
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{k} must map <owner>/<name> to a printable path starting with / or ~: {v!r}"
            )
        if k.lower() in seen:
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{seen[k.lower()]} and local_clones.{k} are the same repo"
            )
        seen[k.lower()] = k
        clones[k] = os.path.realpath(os.path.expanduser(v))


def _task_labels(linear):
    labels = linear.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.task_labels must be a table of <task> = "<Linear label id>"'
        )
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.task_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: linear.task_labels.{seen[v]} and linear.task_labels.{k} have the same label id {v}"
            )
        seen[v] = k


def _task_tiers(cfg):
    tiers = cfg.setdefault("task_tiers", {})
    if not isinstance(tiers, dict):
        raise ConfigError(
            "orchestrator/config.toml: task_tiers must be a table of <task> = <tier 1-4>"
        )
    for k, v in tiers.items():
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 4:
            raise ConfigError(
                f"orchestrator/config.toml: task_tiers.{k} must be a tier, a whole number 1-4: {v!r}"
            )


def _agent_labels(linear):
    group = linear.setdefault("agent_label_group", "")
    labels = linear.setdefault("agent_labels", {})
    if group != "" and not _uuid(group):
        raise ConfigError(
            f"orchestrator/config.toml: linear.agent_label_group must be a Linear label group id (UUID) or empty: {group!r}"
        )
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.agent_labels must be a table of <client> = "<Linear label id>"'
        )
    if bool(group) != bool(labels):
        raise ConfigError(
            "orchestrator/config.toml: linear.agent_label_group and [linear.agent_labels] must both be set or both be empty"
        )
    if group == linear["task_label_group"]:
        raise ConfigError(
            "orchestrator/config.toml: linear.agent_label_group and linear.task_label_group must be different"
        )
    tasks = {v: k for k, v in linear["task_labels"].items()}
    seen = {}
    for k, v in labels.items():
        if not k:
            raise ConfigError(
                "orchestrator/config.toml: [linear.agent_labels] client names must not be empty"
            )
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{seen[v]} and linear.agent_labels.{k} have the same label id {v}"
            )
        if v in tasks:
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{k} and linear.task_labels.{tasks[v]} have the same label id {v}"
            )
        seen[v] = k


def _usage_limit(cfg):
    switches = cfg.setdefault("usage_limit", {})
    if not isinstance(switches, dict):
        raise ConfigError(
            'orchestrator/config.toml: usage_limit must be a table of <client> = "on" or "off"'
        )
    for client, value in switches.items():
        if value not in ("on", "off"):
            raise ConfigError(
                f'orchestrator/config.toml: [usage_limit] {client} must be "on" or "off": {value!r}'
            )
        switches[client] = value == "on"


def _client_max_runs(cfg):
    limits = cfg.setdefault("client_max_runs", {})
    if not isinstance(limits, dict):
        raise ConfigError(
            "orchestrator/config.toml: client_max_runs must be a table of <client> = <whole number >= 1>"
        )
    for k, n in limits.items():
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"orchestrator/config.toml: [client_max_runs] {k} must be a whole number >= 1: {n!r}"
            )


def _runnable(cfg, work, delegate_cls, index):
    overlay = cfg.get("core", {})
    tables = {"overlay": overlay}
    if isinstance(overlay, dict) and isinstance(roles := overlay.get("roles", {}), dict):
        tables.update({f"overlay.roles.{role}": table for role, table in roles.items()})
    for where, table in tables.items():
        if isinstance(table, dict) and "gate" in table:
            raise ConfigError(f"core: run overlay: unknown key 'gate' in [{where}]")
    try:
        trusted = delegate_cls.trusted_dirs()
    except (OSError, ValueError, delegate.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for d in sorted(trusted):
        if under(work, [d]):
            raise ConfigError(f"core: trusted_dirs: {d} is or contains work_dir {work}")
    out = _resolve_roles(cfg, overlay, delegate_cls, index)
    _runnable_inputs(cfg, out)
    return out


def _resolve_roles(cfg, overlay, delegate_cls, index):
    out = {}
    for name, p in cfg["roles"].items():
        try:
            run = delegate_cls.resolve(name, overlays=(overlay,))
            tasks = tuple(index[name])
        except delegate.ConfigError as e:
            raise ConfigError(f"core: {e}") from None
        if host := run.output.get("host"):
            try:
                if utils.Repo.parse(f"{host}/owner/repo").host != host.lower():
                    raise utils.Invalid(host)
            except utils.Invalid:
                raise ConfigError(f"core: [roles.{name}.output] invalid host: {host!r}") from None
        out[name] = Role(tasks, p.get("max_runs", 1), run)
    return out


def _runnable_inputs(cfg, roles):
    tasks = {t for r in roles.values() for t in r.tasks}
    tables = {"task_tiers": cfg["task_tiers"]}
    if cfg["tracker"] is TrackerKind.LINEAR:
        tables = {"linear.task_labels": cfg["linear"]["task_labels"], **tables}
    for table, entries in tables.items():
        for task in entries:
            if task not in tasks:
                raise ConfigError(
                    f"orchestrator/config.toml: {table}.{task} is not a task of a role in orchestrator/config.toml"
                )


def _clone_origins(clones, roles, docs):
    hosts = {
        (r.run.output.get("host") or docs.host).lower()
        for name, r in roles.items()
        if TASKS[name].input == "build"
    }
    seen, resolved = {}, {}
    for key in clones:
        if key.count("/") == 1 and len(hosts) > 1:
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{key}: use host/owner/name when build hosts differ"
            )
        host = next(iter(hosts), docs.host)
        repo = (
            utils.Repo(host, *key.split("/"))
            if key.count("/") == 1
            else utils.Repo(*key.split("/"))
        )
        canonical = repo.slug.lower()
        if canonical in seen:
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{seen[canonical]} and local_clones.{key} are the same repo"
            )
        seen[canonical] = key
        resolved[key] = repo.slug
    for key, slug in resolved.items():
        if reason := clone_error(clones[key], slug):
            raise ConfigError(f"orchestrator/config.toml: local_clones.{key}: {reason}")


def _docs(roles, local, work):
    outs, dirs = {}, {}
    for role, resolved in roles.items():
        if TASKS[role].input == "build":
            continue
        output = resolved.run.output
        if output["type"] == "github":
            if os.path.isabs(output["dir"]):
                raise ConfigError(f"core: document role {role}: github dir must be relative")
            outs[role] = output
            dirs[role] = output["dir"]
        elif output["type"] in ("local", "orchestrator"):
            dirs[role] = os.path.join(work, "docs", role)
        else:
            raise ConfigError(
                f"core: document role {role}: unsupported destination {output['type']!r}"
            )
    targets = {
        (o.get("repo"), o.get("branch"), o.get("host", "github.com").lower()) for o in outs.values()
    }
    name, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if outs and (not name or not branch or not host):
        raise ConfigError(
            f"core: GitHub document roles must publish to one host, repo and branch (their [output] in {local})"
        )
    return Docs(
        name or "", branch or "", MappingProxyType(dirs), host.lower() if host else "github.com"
    )


def _stage_order():
    targets = {s.next for s in TASKS.values() if s.next}
    order = dict.fromkeys(TASKS, 0)
    for start in TASKS:
        if start in targets:
            continue
        name, i = start, 0
        while name:
            order[name] = i
            name, i = TASKS[name].next, i + 1
    return order
