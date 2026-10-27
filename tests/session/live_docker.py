"""The Docker Host's live acceptance gate, run by hand on macOS with Docker Desktop
(docs/architecture.md › Docker live acceptance). Each command prints a PASS line per check and a timing
line per generation, and exits 1 at the first failed check:

  python3 tests/session/live_docker.py worker --client claude|codex|opencode CWD [NAME]
      from a manager session: build once, start a worker on the default Host, check its
      container's security baseline and isolation, restart, close and resume, `docker rm -f` and
      resume, each generation answering a prompt and its close reclaiming its credentials; for
      opencode its opencode.db in the Docker home, Ollama on 127.0.0.1 only, only GitHub secrets
      and the listed providers' keys in the container, each resume continuing one OpenCode session
  python3 tests/session/live_docker.py role --repo OWNER/NAME [--timeout S] [NAME]
      a headless engineer run on OWNER/NAME, an authorized test repo: one forced token refresh,
      then a draft PR by windroc-bot[bot], opened after it; the run's credentials reclaimed
  python3 tests/session/live_docker.py pr URL
      the PR at URL was opened as a draft by windroc-bot[bot]
"""

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.base import Host
from ensemble.session.hosts.docker.broker import BROKER, Broker, plugin_root
from ensemble.session.hosts.docker.host import LABEL
from ensemble.session.hosts.docker.mounts import state_dir
from ensemble.utils import docker_home

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "bin")
WAIT = 300
QUIET = 30
POLL = 2
REFRESH_WAIT = 120
REFRESH_RETRY = 10
DOCKER_HOST = "ensemble.session.hosts.docker.host:DockerHost"
PROMPT = "Reply with the single word ready."
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([0-9]+)")
OWNER_NAME = re.compile(r"[\w.-]+/[\w.-]+")
ROLE_INPUT = (
    "Repo: {repo}\n\nUse light-build. Add the file docker-live/{stamp}.md holding the one line"
    " `Docker Host live acceptance {stamp}`. Change nothing else."
)
AUDIT = os.path.expanduser("~/Library/Logs/ensemble/broker.jsonl")
GITHUB = "github-installation-token"
CLIENT_KIND = {"claude": "claude-setup-token", "codex": "codex-access-token", "opencode": "none"}
NO_CREDENTIAL = "none"
OLLAMA_PORT = 11434  # [session.clients.opencode] ollama_port's default
SECRETS = "/run/ensemble/secrets"
LISTENER = re.compile(r"\s(\S+):(\d+) \(LISTEN\)$")
BOT = "app/windroc-bot"
PHASES = ("checks", "image", "start", "client")
BROKER_PHASES = frozenset(
    (
        "preflight",
        "image",
        "checks",
        "create",
        "bootstrap",
        "clients",
        "credentials",
        "github",
        "total",
    )
)
BROKER_LINE = re.compile(r"^ensemble-broker: start (\S+) ([0-9.]+) s$", re.MULTILINE)
# Resource → the command docs/architecture.md › What a container cannot reach runs as `sh -c`.
EGRESS = "windroc.egress."  # the egress proxy's label prefix
UNREACHABLE = {
    "host keychain": r"find / \( -path /proc -o -path /sys \) -prune -o -path "
    r'"*/Library/Keychains*" -print 2>/dev/null | wc -l; command -v security | wc -c',
    "Linear tokens": r"env | grep -ci linear; find / \( -path /proc -o -path /sys \) -prune -o "
    r"-name config.local.toml -print 2>/dev/null | wc -l",
    "host `gh` login": r"find / \( -path /proc -o -path /sys \) -prune -o -path "
    r'"*/gh/hosts.yml" -print 2>/dev/null | wc -l; env | grep -c "^GH_TOKEN="',
    "Docker socket": r"find / \( -path /proc -o -path /sys \) -prune -o -type s -name "
    r'"*docker*" -print 2>/dev/null | wc -l; command -v docker | wc -c; '
    r'env | grep -c "^DOCKER_HOST="',
}


def audit_lines(text: str) -> list[dict]:
    found = []
    for raw in text.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _credentials(lines: list[dict], *events: str) -> list[dict]:
    return [
        c
        for x in lines
        if not events or x.get("event") in events
        for c in x.get("credentials") or []
        if isinstance(c, dict)
    ]


def _tokens(credentials: list[dict]) -> dict[tuple, dict]:
    """Each installation's last GitHub token entry."""
    return {
        (c.get("installation"), c.get("account")): c for c in credentials if c.get("kind") == GITHUB
    }


def reclaimed(lines: list[dict], generation: str, client: str) -> list[str]:
    mine = [x for x in lines if x.get("generation") == generation]
    closes = [i for i, x in enumerate(mine) if x.get("event") == "close"]
    if not closes:
        return [f"{generation}: no close line in the audit log"]
    after = mine[closes[0] :]
    problems = []
    tokens = _tokens(_credentials(after, "close", "revocation-retry"))
    if not tokens:
        problems.append(f"{generation}: close revoked no {GITHUB}")
    for (_, account), entry in tokens.items():
        if entry.get("result") != "revoked":
            reason = f" ({entry.get('reason')})" if entry.get("result") == "pending" else ""
            problems.append(f"{generation}: {GITHUB} of {account} {entry.get('result')}{reason}")
    kind = CLIENT_KIND[client]
    if kind != NO_CREDENTIAL and not any(
        c.get("kind") == kind and c.get("result") == "withdrawn"
        for c in _credentials(after, "close")
    ):
        problems.append(f"{generation}: close left {kind} not withdrawn")
    return problems


def removed_tokens(lines: list[dict], generation: str) -> str:
    tokens = _tokens(_credentials([x for x in lines if x.get("generation") == generation]))
    revoked = sum(e.get("result") == "revoked" for e in tokens.values())
    if tokens and revoked == len(tokens):
        return f"removed {generation}: github tokens revoked {revoked}/{len(tokens)}"
    expires = max((str(e.get("expires_at")) for e in tokens.values()), default="n/a")
    return (
        f"removed {generation}: github tokens not revoked (revoked {revoked}/{len(tokens)}),"
        f" expires {expires}"
    )


def _related(a: str, b: str) -> bool:
    a, b = os.path.normpath(a), os.path.normpath(b)
    return os.path.commonpath([a, b]) in (a, b)


def _host_path(source: str) -> str:
    # Docker Desktop reports a shared-folder bind's source under /host_mnt
    prefix = "/host_mnt/"
    return source[len(prefix) - 1 :] if source.startswith(prefix) else source


def egress_proxy(docker: str) -> tuple[dict, str]:
    """The egress proxy's inspect and workers network, by its label contract (src/broker/internal/launcher/egress.go)."""
    ids = call(
        [docker, "ps", "--all", "--filter", f"label={EGRESS}contract", "--format", "{{.ID}}"]
    )[0]
    if len(ids.split()) != 1:
        raise SystemExit(
            f"want one egress proxy (label {EGRESS}contract), found {len(ids.split())}"
        )
    (found,) = json.loads(call([docker, "inspect", ids.split()[0]])[0])
    network = ((found.get("Config") or {}).get("Labels") or {}).get(f"{EGRESS}network.workers.name")
    if not network:
        raise SystemExit(f"the egress proxy has no {EGRESS}network.workers.name label")
    return found, network


def baseline(inspect: dict, name: str, cwd: str, plan_mounts: list[str], network: str) -> list[str]:
    host, config = inspect.get("HostConfig") or {}, inspect.get("Config") or {}
    problems = []
    if "ALL" not in (host.get("CapDrop") or []):
        problems.append("HostConfig.CapDrop lacks ALL")
    if not any(str(o).startswith("no-new-privileges") for o in host.get("SecurityOpt") or []):
        problems.append("HostConfig.SecurityOpt lacks no-new-privileges")
    if config.get("User") != "agent":
        problems.append("Config.User is not agent")
    if host.get("Privileged"):
        problems.append("HostConfig.Privileged is true")
    if host.get("NetworkMode") != network:  # the broker's --network, the proxy's workers network
        problems.append(f"HostConfig.NetworkMode is {host.get('NetworkMode')}, want {network}")
    networks = sorted(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    if networks != [network]:
        problems.append(f"NetworkSettings.Networks is {networks}, want only {network}")
    if host.get("ExtraHosts"):
        problems.append(f"HostConfig.ExtraHosts is set: {host['ExtraHosts']}")
    problems += [f"HostConfig.{k} is host" for k in ("PidMode", "IpcMode") if host.get(k) == "host"]
    mounts = inspect.get("Mounts") or []
    volumes = [m for m in mounts if m.get("Type") == "volume"]
    if [(v.get("Name"), v.get("Destination")) for v in volumes] != [(f"ensemble-ws-{name}", cwd)]:
        problems.append(f"want one volume, ensemble-ws-{name} at {cwd}")
    binds = [m for m in mounts if m.get("Type") == "bind"]
    risky = []
    for m in binds:
        source, target = _host_path(m.get("Source", "")), m.get("Destination", "")
        if _related(source, cwd):
            risky.append(f"bind {source} overlaps the cwd {cwd}")
        elif "docker.sock" in source or "docker.sock" in target:
            risky.append(f"bind {source} mounts docker.sock")
    if not risky and sorted(_host_path(m.get("Source", "")) for m in binds) != sorted(plan_mounts):
        problems.append("bind mounts differ from the plan's mounts")
    return problems + risky


def proxy_baseline(egress: dict, network: dict, worker: dict) -> list[str]:
    labels = (egress.get("Config") or {}).get("Labels") or {}
    name = labels.get(f"{EGRESS}network.workers.name", "")
    proxy = labels.get(f"{EGRESS}network.workers.proxy", "")
    state = egress.get("State") or {}
    problems = []
    if labels.get(f"{EGRESS}contract") != "1":
        problems.append("proxy contract must be 1")
    if "workers" not in labels.get(f"{EGRESS}networks", "").split(",") or not name or not proxy:
        problems.append("proxy lacks workers labels")
    if state.get("Running") is not True or (state.get("Health") or {}).get("Status") != "healthy":
        problems.append("proxy must be running and healthy")
    attached = (egress.get("NetworkSettings") or {}).get("Networks") or {}
    if network.get("Internal") is not True or network.get("Name") != name or name not in attached:
        problems.append("workers network must be internal and have the proxy attached")
    env = dict(
        item.split("=", 1) for item in (worker.get("Config") or {}).get("Env", []) if "=" in item
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if env.get(key) != proxy:
            problems.append(f"{key} differs from proxy label")
    for key in ("NO_PROXY", "no_proxy"):
        if set(env.get(key, "").split(",")) != {"localhost", "127.0.0.1", "::1"}:
            problems.append(f"{key} must bypass only container loopback")
    return problems


def probe_problems(kind: str, status: str, code: int) -> list[str]:
    valid = {
        "public": code == 0 and status.strip() == "200 200",
        "denied": code == 56 and status.strip() == "407 000",
        "direct": code in (6, 7, 28) and status.strip() == "000 000",
    }
    return [] if valid[kind] else [f"{kind}: curl exit={code}, status={status.strip()!r}"]


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        pass


@contextmanager
def listener(host: str):
    with HTTPServer((host, 0), ProbeHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def loopback_only(lsof: str, port: int) -> list[str]:
    """Problems with `lsof -nP -iTCP:<port> -sTCP:LISTEN`'s listeners: any but 127.0.0.1, or none."""
    found = [
        m[1] for line in lsof.splitlines() if (m := LISTENER.search(line)) and m[2] == str(port)
    ]
    if not found:
        return [f"no listener on TCP port {port}"]
    return [f"listens on {address}:{port}" for address in found if address != "127.0.0.1"]


def held_secrets(listing: str, providers: list[str]) -> list[str]:
    """Problems with `find SECRETS -mindepth 1 -maxdepth 2`'s paths: anything but GitHub's dir and
    its files, and an opencode dir holding exactly each listed provider's key file.
    """
    found = {path.removeprefix(f"{SECRETS}/") for path in listing.split()}
    keys = {f"opencode/{name}.key" for name in providers}
    allowed = {"github", *(("opencode", *keys) if providers else ())}
    problems = [] if "github" in found else ["no github dir"]
    problems += [f"no {key}" for key in sorted(keys - found)]
    problems += [
        f"holds {SECRETS}/{path}"
        for path in sorted(found - allowed)
        if not path.startswith("github/")
    ]
    return problems


def session_ids(hooks: str, since: str) -> list[str]:
    """The OpenCode session ids of hook payloads journaled after `since`, first seen first."""
    after, found = datetime.fromisoformat(since), {}
    for record in audit_lines(hooks):
        ts, payload = record.get("ts"), record.get("payload")
        native = payload.get("session_id") if isinstance(payload, dict) else None
        if isinstance(ts, str) and isinstance(native, str) and datetime.fromisoformat(ts) > after:
            found[native] = None
    return list(found)


def one_session(ids: list[str], before: str | None) -> tuple[str, list[str]]:
    """The generation's one session id and its problems; `before`: the id it must continue."""
    if not ids:
        return "", ["no OpenCode session id in the hook journal"]
    if len(ids) > 1:
        return ids[0], [f"several OpenCode sessions: {', '.join(ids)}"]
    if before is not None and ids[0] != before:
        return ids[0], [f"OpenCode session {ids[0]}, want {before}"]
    return ids[0], []


def zero_counts(output: str) -> bool:
    counts = [x.strip() for x in output.splitlines() if x.strip()]
    return bool(counts) and all(x == "0" for x in counts)


def _seconds(pattern: str, stderr: str) -> str:
    found = re.search(pattern, stderr, re.MULTILINE)
    return f"{float(found[1]):.1f} s" if found else "n/a"


def _ready(result: dict, hooks: str) -> str:
    if not result.get("launched_at"):
        return "n/a"
    launched = datetime.fromisoformat(result["launched_at"])
    for record in audit_lines(hooks):
        ts = record.get("ts")
        if isinstance(ts, str) and (at := datetime.fromisoformat(ts)) > launched:
            return f"{(at - launched).total_seconds():.1f} s"
    return "n/a"


def timing(result: dict, hooks: str, stderr: str, name: str) -> str:
    parts = [f"{phase} {seconds:.1f} s" for phase, seconds in (result.get("phases") or {}).items()]
    parts.append(f"client ready {_ready(result, hooks)}")
    parts.append(
        f"preflight {_seconds(rf'^docker timing {re.escape(name)}: preflight ([0-9.]+) s$', stderr)}"
    )
    return " | ".join(parts)


def refreshed_at(lines: list[dict], container: str) -> str | None:
    for x in lines:
        if (
            x.get("event") == "refresh"
            and x.get("container") == container
            and any(c.get("result") == "renewed" for c in _credentials([x]))
        ):
            return x.get("time")
    return None


def new_generation(before: set[str], after: set[str], *, step: str = "start") -> str:
    new = after - before
    if len(new) != 1:
        raise SystemExit(f"{step}: want one new generation, got {len(new)}")
    return next(iter(new))


def install_steps(result: dict, stderr: str) -> list[str]:
    problems = [f"install step {p} at start" for p in result.get("phases") or {} if p not in PHASES]
    problems += [
        f"install step {p} at start"
        for p, _ in BROKER_LINE.findall(stderr)
        if p not in BROKER_PHASES
    ]
    return problems


def opened_draft(events: list[str], *, is_draft: bool) -> bool:
    for event in events:
        if event in ("ready_for_review", "convert_to_draft"):
            return event == "ready_for_review"
    return is_draft


def bot_pr(
    view: dict, events: list[str], refreshed: str | None, *, need_refresh: bool
) -> list[str]:
    problems = []
    login = (view.get("author") or {}).get("login")
    if login != BOT:
        problems.append(f"PR author is {login!r}, want {BOT}")
    if not opened_draft(events, is_draft=bool(view.get("isDraft"))):
        problems.append("PR was not opened as a draft")
    if need_refresh and refreshed is None:
        problems.append("no token refresh recorded")
    elif need_refresh and datetime.fromisoformat(view["createdAt"]) <= datetime.fromisoformat(
        refreshed
    ):
        problems.append(
            f"PR created at {view['createdAt']}, before the token refresh at {refreshed}"
        )
    return problems


def own_containers(names: set[str], name: str) -> list[str]:
    prefix = f"ensemble-{name}-"
    return sorted(
        n for n in names if n.startswith(prefix) and re.fullmatch(r"[0-9a-f]{32}", n[len(prefix) :])
    )


def pending_except(status: dict, removed: str | None) -> list[str]:
    return [
        f"pending revocation for {e.get('container')} ({e.get('account')})"
        for e in status.get("revocations") or []
        if e.get("container") != removed
    ]


@dataclass(frozen=True)
class Args:
    command: str
    name: str
    client: str = ""
    cwd: str = ""
    repo: str = ""
    timeout: float = 7200.0
    url: str = ""


def _split(argv: list[str]) -> tuple[dict[str, str], list[str]] | None:
    options, positional, rest = {}, [], list(argv)
    while rest:
        arg = rest.pop(0)
        if arg in ("--client", "--repo", "--timeout") and rest and arg[2:] not in options:
            options[arg[2:]] = rest.pop(0)
        elif arg.startswith("-"):
            return None
        else:
            positional.append(arg)
    return options, positional


def parse(argv: list[str]) -> Args | None:
    split = _split(argv[1:]) if argv else None
    if split is None:
        return None
    (options, positional), command = split, argv[0]
    if command == "worker":
        client = options.get("client")
        if set(options) != {"client"} or client not in CLIENT_KIND or len(positional) not in (1, 2):
            return None
        if not os.path.isabs(positional[0]):
            return None
        name = positional[1] if len(positional) == 2 else f"docker-live-{client}"
        return Args("worker", name, client=client, cwd=positional[0])
    if command == "role":
        repo, timeout = options.get("repo", ""), options.get("timeout", "7200")
        if set(options) - {"repo", "timeout"} or not OWNER_NAME.fullmatch(repo):
            return None
        try:
            seconds = float(timeout)
        except ValueError:
            return None
        if seconds <= 0 or len(positional) > 1:
            return None
        name = positional[0] if positional else "docker-live-role"
        return Args("role", name, repo=repo, timeout=seconds)
    if command == "pr" and not options and len(positional) == 1 and PR_URL.fullmatch(positional[0]):
        return Args("pr", "", url=positional[0])
    return None


def call(argv: list[str], timeout: float = WAIT) -> tuple[str, str]:
    res = subprocess.run(
        argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False
    )
    if res.returncode:
        raise SystemExit(f"{' '.join(argv[:3])}: exit {res.returncode}: {res.stderr.strip()}")
    return res.stdout, res.stderr


def run(*argv: str, timeout: float = WAIT) -> str:
    return call([os.path.join(BIN, argv[0]), *argv[1:]], timeout)[0]


def passed(problems: list[str], label: str) -> None:
    if problems:
        raise SystemExit(f"FAIL {label}:\n  " + "\n  ".join(problems))
    print(f"PASS {label}", flush=True)


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def generations(name: str) -> set[str]:
    try:
        files = os.listdir(state_dir(name))
    except FileNotFoundError:
        return set()
    return {f.removesuffix(".plan.json") for f in files if f.endswith(".plan.json")}


def docker_host() -> tuple[str, tuple[str, ...]]:
    """The default Host's docker CLI and plugins, read as ensemble-session reads them."""
    hosts = Host.parse_hosts(read_config().get("hosts"))
    table = hosts.tables.get(hosts.default, {})
    if table.get("implementation") != DOCKER_HOST:
        raise SystemExit(
            f"default Host is {hosts.default}, not a Docker Host (docs/architecture.md#docker-host)"
        )
    return table["docker"], tuple(table.get("plugins", ()))


def containers(docker: str, *, running: bool = False, label: str = "") -> set[str]:
    listed = () if running else ("--all",)
    labelled = ("--filter", f"label={LABEL}={label}") if label else ()
    return set(call([docker, "ps", *listed, *labelled, "--format", "{{.Names}}"])[0].split())


def broker_status() -> dict:
    return json.loads(call([BROKER, "status"])[0])


def drained(events: tuple[str, ...], after: str) -> str:
    """The line of the newest event already in the roster's events: a backlog is not this run's."""
    while True:
        try:
            after = run(*events, "--after", after, timeout=2).split()[0]
        except subprocess.TimeoutExpired:
            return after


def timed(times: dict, step: str, action):
    started = time.monotonic()
    found = action()
    times[step] = time.monotonic() - started
    return found


@dataclass
class Start:
    step: str
    generation: str
    stderr: str


class Worker:
    def __init__(self, args: Args, manager: str, docker: str) -> None:
        self.name, self.client, self.cwd = args.name, args.client, args.cwd
        self.manager, self.docker = manager, docker
        self.starts: list[Start] = []
        self.events: tuple[str, ...] = ()
        self.cursor = ""
        self.open = False
        self.listed = True  # in the manager's roster until the final close

    def container(self, start: Start) -> str:
        return f"ensemble-{self.name}-{start.generation}"

    def launch(self, step: str, argv: list[str]) -> Start:
        before = generations(self.name)
        self.open = True
        stderr = call(argv, 1920)[1]
        start = Start(step, new_generation(before, generations(self.name), step=step), stderr)
        self.starts.append(start)
        if self.container(start) not in containers(self.docker, running=True):
            raise SystemExit(f"{step}: {self.container(start)} is not running")
        return start

    def event(self, step: str, after: str) -> None:
        try:
            self.cursor = run(*self.events, "--after", after).split()[0]
        except subprocess.TimeoutExpired:
            raise SystemExit(f"FAIL {step}: no event in {WAIT} s") from None

    def answer(self, step: str) -> None:
        run("ensemble-session", "send", self.name, PROMPT)
        self.event(step, self.cursor)
        print(f"PASS {step}: an event after a prompt", flush=True)

    def shut(self, *, final: bool) -> None:
        """Close the session; the final close also drops it from the manager's roster."""
        if final:
            run("ensemble-crew", "close", "--manager", self.manager, self.name)
            self.listed = False
        else:
            run("ensemble-session", "close", self.name)
        self.open = False

    def cleanup(self) -> None:
        """After a failure: the crew close, else the session close, then any container left."""
        for final in (True, False):
            if self.listed if final else self.open:
                self._quietly(lambda final=final: self.shut(final=final))
        self._quietly(self._remove_leftovers)

    def _remove_leftovers(self) -> None:
        for container in own_containers(containers(self.docker, label=self.name), self.name):
            self._quietly(lambda c=container: self._remove(c))

    def _remove(self, container: str) -> None:
        call([self.docker, "rm", "--force", container])
        print(f"cleanup: removed {container}", file=sys.stderr)

    @staticmethod
    def _quietly(action: Callable[[], object]) -> None:
        try:
            action()
        except (SystemExit, subprocess.SubprocessError, OSError) as error:
            print(f"cleanup: {error}", file=sys.stderr)

    def close(self, start: Start, *, final: bool = False) -> None:
        self.shut(final=final)
        if self.container(start) in containers(self.docker):
            raise SystemExit(f"close left {self.container(start)}")
        lines = audit_lines(read_text(AUDIT))
        passed(
            reclaimed(lines, start.generation, self.client), f"close reclaims {start.generation}"
        )

    def isolation(self, start: Start) -> None:
        container = self.container(start)
        plan = load(os.path.join(state_dir(self.name), f"{start.generation}.plan.json"))
        (found,) = json.loads(call([self.docker, "inspect", container])[0])
        mounts = [m["path"] for m in plan["mounts"]]
        egress, network_name = egress_proxy(self.docker)
        (network,) = json.loads(call([self.docker, "network", "inspect", network_name])[0])
        passed(proxy_baseline(egress, network, found), "egress proxy and worker proxy settings")
        passed(
            baseline(found, self.name, plan["workspace"]["path"], mounts, network_name),
            "security baseline",
        )
        self.network(container, egress["Config"]["Labels"][f"{EGRESS}network.workers.proxy"])
        for resource, command in UNREACHABLE.items():
            # grep -c exits 1 when it counts 0, the passing case
            counts = call([self.docker, "exec", container, "sh", "-c", f"{command}; true"])[0]
            if not zero_counts(counts):
                raise SystemExit(f"FAIL cannot reach {resource}: {' '.join(counts.split())}")
            print(f"PASS cannot reach {resource}: {' '.join(counts.split())}", flush=True)

    def network(self, container: str, proxy: str) -> None:
        self.probe(container, proxy, "https://example.com", "public")
        self.probe(container, "", "https://1.1.1.1", "direct")
        route = call(["route", "-n", "get", "default"])[0]
        interfaces = re.findall(r"^\s*interface:\s*(\S+)", route, re.MULTILINE)
        if len(interfaces) != 1:
            raise SystemExit("FAIL LAN control: no unique default interface")
        lan = call(["ipconfig", "getifaddr", interfaces[0]])[0].strip()
        try:
            address = ipaddress.ip_address(lan)
        except ValueError:
            address = None
        if address is None or address.version != 4 or not address.is_private or address.is_loopback:
            raise SystemExit("FAIL LAN control: need this mini's private LAN IPv4 address")
        for bind, target in (("127.0.0.1", "host.docker.internal"), (lan, lan)):
            with listener(bind) as port:
                call(
                    [
                        "curl",
                        "--noproxy",
                        "*",
                        "-fsS",
                        "-o",
                        "/dev/null",
                        "--max-time",
                        "5",
                        f"http://{bind}:{port}/",
                    ]
                )
                print(f"PASS host control: {bind}:{port} reachable", flush=True)
                url = f"http://{target}:{port}/"
                self.probe(container, proxy, url, "denied")
                self.probe(container, "", url, "direct")

    def probe(self, container: str, proxy: str, url: str, kind: str) -> None:
        argv = [
            self.docker,
            "exec",
            container,
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "--connect-timeout",
            "3",
            "--max-time",
            "10",
            "-w",
            "%{http_connect} %{http_code}",
        ]
        argv += (
            ["--noproxy", "", "--proxytunnel", "--proxy", proxy] if proxy else ["--noproxy", "*"]
        )
        result = subprocess.run(
            [*argv, url],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        passed(probe_problems(kind, result.stdout, result.returncode), f"network {kind}: {url}")


class Checks:
    """A client's extra worker checks: none."""

    def __init__(self, worker: Worker) -> None:
        self.worker = worker

    def after_start(self, start: Start) -> None:
        """Once, after the first generation's first event."""

    def after_answer(self, start: Start) -> None:
        """After each generation's answer, the first included."""


class OpenCodeChecks(Checks):
    def __init__(self, worker: Worker) -> None:
        super().__init__(worker)
        self.session: str | None = None

    def after_start(self, start: Start) -> None:
        w = self.worker
        db = os.path.join(docker_home(w.name, "opencode"), "opencode", "opencode.db")
        if not os.path.isfile(db):
            raise SystemExit(f"FAIL no opencode.db at {db}")
        print(f"PASS opencode.db at {db}", flush=True)
        argv = ["lsof", "-nP", f"-iTCP:{OLLAMA_PORT}", "-sTCP:LISTEN"]
        try:
            listeners = subprocess.run(
                argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=QUIET
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise SystemExit(f"FAIL Ollama listener: {error}") from None
        passed(loopback_only(listeners, OLLAMA_PORT), "Ollama listens on 127.0.0.1 only")
        listing = call(
            [
                w.docker,
                "exec",
                w.container(start),
                "find",
                SECRETS,
                "-mindepth",
                "1",
                "-maxdepth",
                "2",
            ]
        )[0]
        providers = read_config().get("clients", {}).get("opencode", {}).get("providers", {})
        passed(
            held_secrets(listing, sorted(providers)),
            f"the container holds only GitHub secrets and the listed providers' keys {sorted(providers)}",
        )

    def after_answer(self, start: Start) -> None:
        w = self.worker
        launched = load(os.path.join(state_dir(w.name), f"{start.generation}.result.json"))
        hooks = read_text(os.path.expanduser(f"~/.ensemble/tui/{w.name}/hooks.jsonl"))
        resumed = start.step.startswith("resume")
        native, problems = one_session(
            session_ids(hooks, launched["launched_at"]), self.session if resumed else None
        )
        if problems:
            raise SystemExit(f"FAIL {start.step}: {problems[0]}")
        if resumed:
            print(f"PASS {start.step}: same OpenCode session {native}", flush=True)
        self.session = native


EXTRA_CHECKS: dict[str, type[Checks]] = {"opencode": OpenCodeChecks}


def worker(args: Args) -> int:
    manager = os.environ.get("ENSEMBLE_MANAGER")
    if not manager:
        raise SystemExit("run from a manager session: ENSEMBLE_MANAGER is unset")
    docker, plugins = docker_host()
    times: dict[str, float] = {}
    broker = Broker()
    broker.version()
    package = plugin_root(os.path.join(BIN, "ensemble-session"))
    _, digest = timed(times, "build", lambda: broker.build(package, plugins=plugins))
    named = [arg for p in plugins for arg in ("--plugin", p)]
    again = json.loads(call([BROKER, "build", "--ensemble", package, *named], 1920)[0])
    if again.get("rebuilt") is not False or again.get("digest") != digest:
        raise SystemExit("FAIL a second build rebuilt the image or changed its digest")
    print("PASS a current image is not rebuilt", flush=True)
    w = Worker(args, manager, docker)
    cursor = run("ensemble-crew", "attach", "--manager", manager)
    after, gen = (re.search(rf"\b{key}=(\d+)", cursor) for key in ("N", "GEN"))
    if after is None or gen is None:
        raise SystemExit(f"ensemble-crew attach printed no N= and GEN=: {cursor!r:.200}")
    w.events = ("ensemble-crew", "next-event", "--manager", manager, "--gen", gen.group(1))
    start_after = drained(w.events, after.group(1))
    removed = None
    try:
        crew = [os.path.join(BIN, "ensemble-crew"), "worker", "--manager", manager, w.name]
        first = w.launch(
            "start",
            [*crew, "--client", w.client, "--cwd", w.cwd, "--display", "none", "--prompt", PROMPT],
        )
        w.event("start", start_after)
        print("PASS start: the default Host put the worker in a container; first event", flush=True)
        checks = EXTRA_CHECKS.get(w.client, Checks)(w)
        checks.after_start(first)
        checks.after_answer(first)
        w.isolation(first)
        session = os.path.join(BIN, "ensemble-session")
        w.launch("restart", [session, "restart", w.name])
        w.answer("restart")
        checks.after_answer(w.starts[-1])
        w.close(w.starts[-1])
        w.launch("resume after close", [session, "resume", w.name])
        w.answer("resume after close")
        checks.after_answer(w.starts[-1])
        removed = w.container(w.starts[-1])
        call([docker, "rm", "--force", removed])
        w.launch("resume after docker rm -f", [session, "resume", w.name])
        w.answer("resume after docker rm -f")
        checks.after_answer(w.starts[-1])
        w.close(w.starts[-1], final=True)
    except BaseException:
        w.cleanup()
        raise
    passed(pending_except(broker_status(), removed), "no pending revocation but the removed one")
    lines = audit_lines(read_text(AUDIT))
    if removed:
        print(removed_tokens(lines, removed.rsplit("-", 1)[1]), flush=True)
    report(w.name, w.starts, digest, times)
    return 0


def report(name: str, starts: list[Start], digest: str, times: dict) -> None:
    hooks = read_text(os.path.expanduser(f"~/.ensemble/tui/{name}/hooks.jsonl"))
    problems = []
    for start in starts:
        result = load(os.path.join(state_dir(name), f"{start.generation}.result.json"))
        if result.get("image_digest") != digest:
            problems.append(
                f"{start.generation}: started on {result.get('image_digest')}, built {digest}"
            )
        problems += [f"{start.generation}: {p}" for p in install_steps(result, start.stderr)]
        print(
            f"timing {name} {start.generation} ({start.step}): build {times.get('build', 0):.1f} s"
            f" | {timing(result, hooks, start.stderr, name)}",
            flush=True,
        )
    passed(problems, "every start reused the built image, with no install step")


def pr_checks(url: str) -> tuple[dict, list[str]]:
    repo, number = PR_URL.fullmatch(url).groups()
    view = json.loads(
        call(["gh", "pr", "view", url, "--json", "author,isDraft,createdAt,number"])[0]
    )
    timeline = ["gh", "api", f"repos/{repo}/issues/{number}/timeline", "--paginate"]
    events = call([*timeline, "--jq", ".[].event"])[0].split()
    return view, events


class RoleRun:
    def __init__(self, name: str, docker: str, broker: Broker | None = None) -> None:
        self.name, self.docker = name, docker
        self.broker = Broker() if broker is None else broker

    def sessions(self) -> set[tuple[str, str]]:
        """(session, generation) of the run's agent sessions: `name` and `name-*`."""
        root = os.path.dirname(state_dir(self.name))
        if not os.path.isdir(root):
            return set()
        names = [n for n in os.listdir(root) if n == self.name or n.startswith(f"{self.name}-")]
        return {(n, g) for n in names for g in generations(n)}

    def generation(self, before: set[tuple[str, str]], proc) -> tuple[str, str]:
        while not (new := self.sessions() - before):
            if proc.poll() is not None:
                raise SystemExit(f"the run exited {proc.returncode} before its container started")
            time.sleep(POLL)
        if len(new) != 1:
            raise SystemExit(f"want one new role generation, got {len(new)}")
        (found,) = new
        return found

    def running(self, container: str, proc) -> None:
        while container not in containers(self.docker, running=True):
            if proc.poll() is not None:
                raise SystemExit(f"the run exited {proc.returncode} before {container} ran")
            time.sleep(POLL)

    def renew(self, container: str, manifest: str) -> None:
        deadline = time.monotonic() + REFRESH_WAIT
        while True:
            try:
                found = self.broker.maintain(container, manifest, renew=True, root=REPO)
                if failed := [r for r in found.results if r["error"] is not None]:
                    raise SessionError(f"broker: {failed[0]['error']['message']}")
                break
            except SessionError as error:
                if time.monotonic() > deadline:
                    raise SystemExit(f"FAIL token refresh of {container}: {error}") from None
                time.sleep(REFRESH_RETRY)
        print(f"PASS token refresh of {container}", flush=True)


def role(args: Args) -> int:
    docker, _ = docker_host()
    role_run = RoleRun(args.name, docker)
    before = role_run.sessions()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    text = ROLE_INPUT.format(repo=args.repo, stamp=stamp)
    workdir = os.path.expanduser(f"~/.ensemble/work/{args.name}-{stamp}")
    argv = [
        os.path.join(BIN, "ensemble-delegate"),
        "run",
        "--role",
        "engineer",
        "--workdir",
        workdir,
    ]
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(
            [*argv, "--name", args.name, "--input", text],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            text=True,
        )
        try:
            session, generation = role_run.generation(before, proc)
            container = f"ensemble-{session}-{generation}"
            role_run.running(container, proc)
            role_run.renew(container, os.path.join(state_dir(session), f"{generation}.json"))
            try:
                proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"FAIL the run did not finish in {args.timeout:.0f} s") from None
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
        err.seek(0)
        stderr = err.read()
    outcome = json.loads(run("ensemble-delegate", "outcome", workdir))
    url = (outcome or {}).get("url", "")
    if not PR_URL.fullmatch(url):
        raise SystemExit(f"FAIL the run's outcome names no PR: {(outcome or {}).get('status')}")
    print(f"PR {url}", flush=True)
    lines = audit_lines(read_text(AUDIT))
    view, events = pr_checks(url)
    refreshed = refreshed_at(lines, container)
    passed(bot_pr(view, events, refreshed, need_refresh=True), "windroc-bot[bot] opened a draft PR")
    if container in containers(docker):
        raise SystemExit(f"FAIL the run left {container}")
    plan = load(os.path.join(state_dir(session), f"{generation}.plan.json"))
    client = {kind: c for c, kind in CLIENT_KIND.items()}[plan["credential"]]
    passed(reclaimed(lines, generation, client), f"close reclaims {generation}")
    result = load(os.path.join(state_dir(session), f"{generation}.result.json"))
    print(
        f"timing {session} {generation} (role): {timing(result, '', stderr, session)}", flush=True
    )
    return 0


def pr(args: Args) -> int:
    view, events = pr_checks(args.url)
    passed(bot_pr(view, events, None, need_refresh=False), "windroc-bot[bot] opened a draft PR")
    return 0


def main(argv: list[str]) -> int:
    args = parse(argv)
    if args is None:
        print(__doc__, file=sys.stderr)
        return 2
    return {"worker": worker, "role": role, "pr": pr}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
