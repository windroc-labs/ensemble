"""The root-owned broker at the PROTOCOL version (src/broker/protocol/docker-broker/protocol.schema.json): version, build, start, maintain,
refresh, notices, close, workspace-rm and lock-query with strict response parsing. A missing, malformed or inconsistent response is a transport failure;
a valid error response is a BrokerError.
"""

import json
import logging
import os
import re
import select
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import datetime

from ensemble.session.engine.types import ConfigError, SessionError
from ensemble.session.hosts.docker.types import Maintained, Noticed, Refreshed, Started
from ensemble.utils import save

# Installed by src/broker/install.sh and the worker image (src/broker/internal/launcher/assets/Dockerfile).
BROKER = "/usr/local/libexec/ensemble/ensemble-broker"
RELAY = "/opt/ensemble/bin/relay"
PROTOCOL = "29"
SCHEMA = "src/broker/protocol/docker-broker/protocol.schema.json"
INSTALL = "install or upgrade it: see docs/docker-host.md#install"
TIMEOUT = 1800
GRACE = 5  # for a broker whose pipes closed by the deadline to be reaped

TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
IMAGE = re.compile(r"ensemble-worker:[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER = re.compile(r"ensemble-[A-Za-z0-9_-]+-[0-9a-f]{32}")
ENDPOINT = re.compile(r"unix:///[^\x00-\x1f\x7f]+/\.docker/run/docker\.sock")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
CREDENTIAL = re.compile(r"[a-z][a-z0-9-]{0,31}")
COMMANDS = frozenset(
    {
        "version",
        "build",
        "start",
        "maintain",
        "refresh",
        "notices",
        "close",
        "status",
        "workspace-rm",
        "probe-provider",
        "lock-query",
        "unknown",
    }
)
CODES = {  # code → (exit code, the commands that report it)
    "usage": (2, COMMANDS),
    "manifest_rejected": (3, {"start", "maintain", "refresh", "notices"}),
    "mount_rejected": (4, {"start", "maintain", "refresh", "notices"}),
    "login_required": (5, {"start", "refresh", "probe-provider"}),
    "image_rejected": (6, {"start"}),
    "docker_unreachable": (7, {"build", "start", "refresh", "close", "workspace-rm", "lock-query"}),
    "build_failed": (8, {"build"}),
    "start_failed": (9, {"start", "lock-query"}),
    "close_failed": (10, {"close"}),
    "keychain_failed": (11, {"start", "refresh", "close", "status", "probe-provider"}),
    "workspace_dirty": (12, {"workspace-rm"}),
    "workspace_rejected": (13, {"workspace-rm"}),
    "workspace_failed": (14, {"workspace-rm"}),
    "github_failed": (15, {"start", "refresh"}),
    "refresh_failed": (16, {"refresh"}),
    "egress_unavailable": (17, {"start"}),
    "internal_error": (70, COMMANDS),
    "busy": (75, {"start"}),
}
ERROR_KEYS = frozenset({"command", "code", "message", "cleanup"})
ERROR_OPTIONAL = frozenset({"cause", "cleanup_error", "container", "holder"})
LOG = logging.getLogger("session")
AUDIT_WARNING = (
    "audit_write_failed: broker audit persistence failed; the credential/resource operation "
    "result still applies. Do not repeat a successful start; check the broker audit log."
)


class BrokerError(SessionError):
    def __init__(
        self,
        exit_code: int,
        code: str,
        message: str,
        cleanup: str,
        container: str | None,
        *,
        cause: str | None = None,
        holder: Mapping[str, str] | None = None,
    ) -> None:
        """holder: a busy lock's {"container", "owner"}."""
        self.exit_code, self.code, self.message, self.cleanup, self.container, self.cause = (
            exit_code,
            code,
            message,
            cleanup,
            container,
            cause,
        )
        self.holder = holder
        super().__init__(f"broker: {message}" + (f" ({cause})" if cause else ""))


class _Bad(Exception):
    pass


def _unique(pairs):
    found = dict(pairs)
    if len(found) != len(pairs):
        raise _Bad("a duplicate key")
    return found


def _constant(name):
    raise _Bad(f"constant {name}")


def _object(text: str) -> dict:
    try:
        found = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise _Bad("not one JSON object") from error
    if not isinstance(found, dict):
        raise _Bad("not a JSON object")
    return found


def _text(value) -> bool:
    return isinstance(value, str) and bool(TEXT.fullmatch(value))


def _matches(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _expiries(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(e, dict)
        and e.keys() == {"account", "expires_at"}
        and _matches(LOGIN, e["account"])
        and e["expires_at"] is not None
        and _credential_expiry(e["expires_at"])
        for e in value
    )


def _credential_expiry(value) -> bool:
    if value is None:
        return True
    if not _matches(TIME, value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _deadline(value) -> bool:
    return value is None or (
        isinstance(value, dict)
        and value.keys() == {"at", "action", "reason"}
        and _matches(TIME, value["at"])
        and _credential_expiry(value["at"])
        and value["action"] == "close"
        and _text(value["reason"])
    )


def _result(value) -> bool:
    error = value.get("error") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and value.keys() == {"credential", "expires_at", "error"}
        and _matches(CREDENTIAL, value["credential"])
        and _credential_expiry(value["expires_at"])
        and (
            error is None
            or isinstance(error, dict)
            and {"code", "message"} <= error.keys() <= {"code", "message", "cause"}
            and error["code"] in CODES
            and all(_text(error[key]) for key in error.keys() - {"code"})
        )
    )


def _holder_ok(value) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == {"container", "owner"}
        and all(map(_text, value.values()))
    )


def _user_id(value) -> bool:
    return value is None or (type(value) is int and value >= 1)


def _error(command: str, returncode: int, data: dict) -> BrokerError:
    error, exit_code = data.get("error"), data.get("exit_code")
    if not (
        set(data) == {"exit_code", "error"}
        and type(exit_code) is int
        and exit_code == returncode
        and isinstance(error, dict)
        and ERROR_KEYS <= set(error) <= ERROR_KEYS | ERROR_OPTIONAL
        and all(isinstance(error[key], str) for key in ERROR_KEYS)
        and ("holder" in error) == (error["code"] == "busy")
    ):
        raise _Bad("error shape")
    code, cleanup, container = error["code"], error["cleanup"], error.get("container")
    if not (
        CODES.get(code, (None,))[0] == exit_code
        and error["command"] in CODES[code][1]
        and error["command"] in {command, "unknown"}
        and cleanup in {"not_needed", "removed", "failed"}
        and (command == "start" or cleanup == "not_needed")
        and (cleanup == "failed") == ("cleanup_error" in error)
        and (cleanup != "failed" or container is not None)
        and (container is None or _matches(CONTAINER, container))
        and all(_text(error[key]) for key in ("message", "cause", "cleanup_error") if key in error)
        and ("holder" not in error or _holder_ok(error["holder"]) and cleanup != "failed")
    ):
        raise _Bad("error fields")
    return BrokerError(
        exit_code,
        code,
        error["message"],
        cleanup,
        container,
        cause=error.get("cause"),
        holder=error.get("holder"),
    )


def _stream(argv, *, stdin, env, timeout) -> subprocess.CompletedProcess:
    """argv's text result, copying stderr (build's progress) to ours line by line as it arrives,
    but a failing run's error object. TimeoutExpired past `timeout`.
    """
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    ) as proc:
        try:
            out, err, shown = _copy(proc, deadline, timeout)
            code = proc.wait(max(GRACE, deadline - time.monotonic()))
        except BaseException:
            proc.kill()
            raise
    rest = err[shown:]
    if code == 0 or not rest.startswith(b"{"):
        _show(rest)
    return subprocess.CompletedProcess(argv, code, out.decode(), err.decode(errors="replace"))


def _copy(proc, deadline: float, timeout: float) -> tuple[bytes, bytes, int]:
    """proc's stdout and stderr, and how much of stderr it showed: every complete line but a last
    one starting with `{`, which may be the error object.
    """
    out, err = bytearray(), bytearray()
    found = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
    shown = 0
    while found:
        ready = select.select(list(found), [], [], max(0.0, deadline - time.monotonic()))[0]
        if not ready:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        for fd in ready:
            chunk = os.read(fd, 1 << 16)
            found[fd] += chunk
            if not chunk:
                del found[fd]
        end = err.rfind(b"\n") + 1
        last = err.rfind(b"\n", 0, max(end - 1, 0)) + 1
        if err.startswith(b"{", last):
            end = last
        if end > shown:
            _show(err[shown:end])
            shown = end
    return bytes(out), bytes(err), shown


def _show(progress: bytes | bytearray) -> None:
    sys.stderr.write(progress.decode(errors="replace"))
    sys.stderr.flush()


class Broker:
    def __init__(self, runner=_stream, *, warning=None) -> None:
        self.runner = runner
        self.warning = warning or LOG.warning

    def _run(self, command: str, *args: str) -> dict:
        try:
            res = self.runner(
                [BROKER, command, *args],
                stdin=subprocess.DEVNULL,
                env={"PATH": os.defpath},
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise SessionError(f"broker: {command} timed out after {TIMEOUT} s") from None
        except UnicodeDecodeError:
            raise SessionError(f"broker: bad response to {command}: not UTF-8") from None
        except OSError as error:
            raise SessionError(f"broker {BROKER}: {error.strerror}; {INSTALL}") from None
        try:
            if res.returncode == 0:
                found = _object(res.stdout)
            else:
                err = res.stderr
                if command == "build":  # its progress lines come before the error object
                    err = err[err.rfind("\n{") + 1 :]
                found = _object(err)
            if "audit_warning" in found:
                if (
                    command not in {"start", "maintain", "refresh", "notices", "close", "status"}
                    or found.pop("audit_warning") != "audit_write_failed"
                ):
                    raise _Bad("audit warning")
                try:
                    self.warning(AUDIT_WARNING)
                except (OSError, ValueError):
                    LOG.warning(AUDIT_WARNING)
            if res.returncode == 0:
                return found
            raise _error(command, res.returncode, found)
        except _Bad as why:
            raise SessionError(f"broker: bad response to {command}: {why}") from None

    def version(self) -> None:
        found = self._parsed("version", lambda d: set(d) == {"protocol_version"})
        if found["protocol_version"] != PROTOCOL:
            raise SessionError(
                f"broker {BROKER}: protocol {found['protocol_version']!r:.20}, want {PROTOCOL!r};"
                f" {INSTALL}"
            )

    def probe_provider(self, provider: str, base_url: str) -> bool:
        found = self._parsed(
            "probe-provider",
            lambda d: set(d) == {"available"} and type(d["available"]) is bool,
            "--provider",
            provider,
            "--base-url",
            base_url,
        )
        return found["available"]

    def build(
        self,
        package: str,
        *,
        plugins: Sequence[str] = (),
        base_image: str | None = None,
        image_plugins: Sequence[str] = (),
    ) -> tuple[str, str]:
        """`package`'s image tag and digest on `base_image` (None: ~/.ensemble/docker/worker), with
        `plugins` installed from repository sources and `image_plugins` from the base image.
        """
        named = [
            *(("--base-image", base_image) if base_image else ()),
            *(arg for name in plugins for arg in ("--plugin", name)),
            *(arg for name in image_plugins for arg in ("--image-plugin", name)),
        ]
        found = self._parsed(
            "build",
            lambda d: (
                set(d) == {"image", "digest", "rebuilt"}
                and _matches(IMAGE, d["image"])
                and _matches(DIGEST, d["digest"])
                and type(d["rebuilt"]) is bool
            ),
            "--ensemble",
            package,
            *named,
        )
        return found["image"], found["digest"]

    def start(self, manifest: str, *, clients: frozenset[str]) -> Started:
        """`clients`: the client names the protocol requires in the response."""
        required = {f.name for f in fields(Started)}
        found = self._parsed(
            "start",
            lambda d: set(d) == required,
            "--manifest",
            manifest,
        )
        started = Started(**found)
        if not (
            _matches(CONTAINER, started.container)
            and _matches(ENDPOINT, started.endpoint)
            and _matches(DIGEST, started.image_digest)
            and isinstance(started.clients, dict)
            and started.clients.keys() == clients
            and all(map(_text, started.clients.values()))
            and _expiries(started.github)
            and _user_id(started.github_user_id)
            and _credential_expiry(started.credential_expires_at)
            and _deadline(started.deadline)
        ):
            raise SessionError("broker: bad response to start: fields")
        return started

    def lock_query(self, lock: str) -> dict[str, str] | None:
        """The running holder of `lock`, {"container", "owner"}, or None; takes nothing."""
        found = self._parsed(
            "lock-query",
            lambda d: d.keys() == {"holder"} and (d["holder"] is None or _holder_ok(d["holder"])),
            "--lock",
            lock,
        )
        return found["holder"]

    def refresh(self, container: str, manifest: str) -> Refreshed:
        """Public credential deadlines; `manifest`: the one the container was started with."""
        found = self._parsed(
            "refresh",
            lambda d: (
                d.keys() == {"container", "github", "credential_expires_at"}
                and _matches(CONTAINER, d["container"])
                and d["container"] == container
                and _expiries(d["github"])
                and _credential_expiry(d["credential_expires_at"])
            ),
            "--container",
            container,
            "--manifest",
            manifest,
        )
        return Refreshed(found["github"], found["credential_expires_at"])

    def maintain(
        self,
        container: str,
        manifest: str,
        delivered: Sequence[str] = (),
        *,
        renew: bool = False,
        root: str,
    ) -> Maintained:
        """`delivered`: notice keys told; `renew`: renew every token now."""
        found = self._parsed(
            "maintain",
            lambda d: (
                d.keys() == {"container", "next_check", "deadline", "results", "notices"}
                and d["container"] == container
                and _credential_expiry(d["next_check"])
                and _deadline(d["deadline"])
                and isinstance(d["results"], list)
                and all(map(_result, d["results"]))
                and _notices(d["notices"])
            ),
            "--container",
            container,
            "--manifest",
            manifest,
            *(("--renew",) if renew else ()),
            *(arg for key in delivered for arg in ("--delivered", key)),
        )
        _check_keys(found["notices"], root, "maintain")
        return Maintained(
            found["results"], found["notices"], found["next_check"], found["deadline"]
        )

    def notices(
        self, container: str, manifest: str, delivered: Sequence[str] = (), *, root: str
    ) -> Noticed:
        """`container`'s due notices; `delivered`: keys told; keys per `root`'s schema."""
        found = self._parsed(
            "notices",
            lambda d: (
                d.keys() == {"container", "notices", "next_check"}
                and d["container"] == container
                and _notices(d["notices"])
                and _credential_expiry(d["next_check"])
            ),
            "--container",
            container,
            "--manifest",
            manifest,
            *(arg for key in delivered for arg in ("--delivered", key)),
        )
        _check_keys(found["notices"], root, "notices")
        return Noticed(found["notices"], found["next_check"])

    def close(self, container: str) -> bool:
        """Whether it removed `container`; False: it was already gone."""
        found = self._parsed(
            "close",
            lambda d: (
                d.keys() == {"container", "removed"}
                and d["container"] == container
                and type(d["removed"]) is bool
            ),
            "--container",
            container,
        )
        return found["removed"]

    def workspace_rm(self, volume: str) -> bool:
        """Whether it removed workspace `volume` after its check found no unsaved work; False: none."""
        found = self._parsed(
            "workspace-rm",
            lambda d: (
                d.keys() == {"volume", "removed"}
                and d["volume"] == volume
                and type(d["removed"]) is bool
            ),
            "--volume",
            volume,
        )
        return found["removed"]

    def _parsed(self, command: str, valid, *args: str) -> dict:
        found = self._run(command, *args)
        if not valid(found):
            raise SessionError(f"broker: bad response to {command}: fields")
        return found


def _notices(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(n, dict) and n.keys() == {"key", "text"} and _text(n["text"]) for n in value
    )


def _check_keys(notices: list[dict], root: str, command: str) -> None:
    if not notices:
        return
    try:
        key = notice_key(root)
    except ConfigError as error:
        raise SessionError(f"broker: {error}") from None
    if not all(_matches(key, n["key"]) for n in notices):
        raise SessionError(f"broker: bad response to {command}: fields")


def hook_root(hook_path: str) -> str:
    """The parent of the bin/ holding `hook_path`."""
    return os.path.dirname(os.path.dirname(os.path.abspath(hook_path)))


def plugin_root(hook_path: str) -> str:
    """The running plugin's root: hook_root, holding the protocol schema."""
    root = hook_root(hook_path)
    if not os.path.isfile(os.path.join(root, SCHEMA)):
        raise ConfigError(f"ensemble plugin {root}: no {SCHEMA}")
    return root


def client_keys(root: str) -> frozenset[str]:
    """The client names a start response must report: the required keys of start_output's one
    inline object property in the protocol schema under `root`.
    """
    try:
        properties = _schema(root)["$defs"]["start_output"]["properties"]
        (keys,) = (
            p["required"]
            for p in properties.values()
            if isinstance(p, dict) and p.get("type") == "object" and "required" in p
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f"{SCHEMA}: no start_output client map ({error})") from None
    return frozenset(keys)


def _schema(root: str) -> dict:
    try:
        with open(os.path.join(root, SCHEMA), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as error:
        raise ConfigError(f"{SCHEMA}: unreadable ({error})") from None


def notice_key(root: str) -> re.Pattern:
    """The schema's notice key pattern."""
    try:
        return re.compile(_schema(root)["$defs"]["notice_key"]["pattern"])
    except (KeyError, TypeError, re.error):
        raise ConfigError(f"{SCHEMA}: no notice_key pattern") from None


def write_manifest(
    path: str,
    *,
    name: str,
    generation: str,
    image: str,
    cwd: str,
    workspace: str,
    home: tuple[str, str],
    hook_path: str,
    github: str,
    permissions: Mapping[str, str] | None = None,
    providers: tuple[str, ...] = (),
    account: str = "",
    gateway: str | None = None,
    secrets: Sequence[Mapping[str, str]] = (),
    lock: str | None = None,
    lock_owner: str | None = None,
    prefetch: Mapping[str, str] | None = None,
) -> None:
    """The start manifest, 0600; home: (its manifest field, the client home's path); github: the
    App whose tokens the container gets, "": none; permissions: each token's repository
    permissions; providers: those whose API keys it gets; account: the one whose credential it
    gets, "": the default. An environment's gateway, secrets, lock and prefetch go in when set.
    """
    field, home_path = home
    client = field.removesuffix("_dir")
    manifest = {
        "name": name,
        "generation": generation,
        "image": image,
        "cwd": cwd,
        "workspace": workspace,
        "client": client,
        field: home_path,
        "hook_path": hook_path,
        "github": {"app": github, "permissions": dict(permissions or {})} if github else None,
        "labels": {},
        **({f"{client}_providers": list(providers)} if providers else {}),
        **({f"{client}_account": account} if account else {}),
        **({"gateway": gateway} if gateway else {}),
        **({"secrets": [dict(secret) for secret in secrets]} if secrets else {}),
        **({"lock": lock, "lock_owner": lock_owner} if lock else {}),
        **({"prefetch": dict(prefetch)} if prefetch else {}),
    }
    save(path, json.dumps(manifest), durable=True)
