"""The orchestrator's integration flows: the test's HOME with both local files ([linear.task_labels]
included), FakeLinear over HTTP through test-only transport with the harness, role and human
accounts and the task label group, fake_claude as `claude`, a launch (router.outer), promote and
router as `python3 -P` subprocesses, and readers of Linear, the logs and a run's files. TmuxFlow adds a private tmux
server for the runs' harness and agent sessions (a command run as from MANAGER's pane too),
fake_gh as `gh` with REPO a bare repo, and `ensemble-orchestrator` as entry.py under the fake transport,
the harness's write-back callback. BinFlow runs router and promote as bin/ensemble-orchestrator by
absolute path, as launchd does (FR-25, FR-29).
"""

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import quote

from orchestrator_tests.case import PY, Case, child_env
from orchestrator_tests.case import REPO as WORKTREE
from orchestrator_tests.integration import fake_gh, fake_linear
from orchestrator_tests.integration.fake_python import write_shim
from orchestrator_tests.unit import board_ids, run_fixtures
from orchestrator_tests.unit.run_fixtures import ID, SID

from delegate.tests.workdir import run_log
from session.tests import live_tmux
from session.tests.integration.clients.claude import fake_claude
from utils.tests.case import local_section

ENTRY = os.path.join(os.path.dirname(__file__), "entry.py")

BIN = os.path.join(WORKTREE, "bin", "ensemble-orchestrator")


LAUNCH, PROMOTE, ROUTER = ((ENTRY, n) for n in ("launch", "promote", "router"))

LIGHT_RESEARCH = "00000000-0000-4000-8000-000000000021"

TASK_LABELS = f'[linear.task_labels]\nlight-research = "{LIGHT_RESEARCH}"\n'

_CFG = tomllib.loads(run_fixtures.local("/") + TASK_LABELS)

_LINEAR = _CFG["linear"]

ROLES = tuple(_CFG["roles"])

ACCOUNTS = {r: _LINEAR["roles"][r]["account"] for r in ROLES}

KEYS = {r: _LINEAR["roles"][r]["key"] for r in ROLES}

[HUMAN] = _LINEAR["human_members"]

HARNESS, HARNESS_EMAIL = _LINEAR["harness_key"], "harness@agents.test"

PROJECT = {"id": run_fixtures.PROJECT, "name": "Widgets"}

REPO = _LINEAR["project_repos"][PROJECT["id"]]

_OUTPUTS = {
    r: o["output"] for r, o in tomllib.loads(run_fixtures.CORE_LOCAL)["delegate"]["roles"].items()
}

[(DOCS, BRANCH)] = {(o["repo"], o["branch"]) for o in _OUTPUTS.values()}

RESEARCH_DIR, PRD_DIR = _OUTPUTS["researcher"]["dir"], _OUTPUTS["pm"]["dir"]

HEAD = "a" * 40
# an engineer's done outcome: its updraft head is the head of each of GH's views (the ready gate)
UPDRAFT = {"status": "converged", "head": HEAD}

GH = {
    "repos": {REPO: {"full_name": REPO, "default_branch": "main", "permissions": {"push": True}}},
    "contents": {},
    "views": {
        f"https://github.com/{REPO}/pull/{n}": {
            "state": "OPEN",
            "isDraft": True,
            "headRefOid": HEAD,
            "statusCheckRollup": [],
        }
        for n in (1, 7)
    },
}

MANAGER = "manager"

TIMEOUT = 60

TAIL = 20

GUARD = (
    "import json, shutil, sys\n"
    "from ensemble.orchestrator.engine import config\n"
    "print(json.dumps({n: shutil.which(n, path=config.path()) for n in sys.argv[1:]}))"
)


def usage(five_hour):
    """fake_claude's `/usage` panel at five_hour utilization."""
    return f"Current session: {round(100 * five_hour)}% used\n"


def steps(*names, end=0, found=(), more=()):
    """orchestrator.jsonl's lines of the run ID, SID's write-back: its steps, `more`, its end."""
    return [
        *(
            f"writeback step {ID} sid={SID} step={s}" + (" found=true" if s in found else "")
            for s in names
        ),
        *more,
        f"writeback end {ID} sid={SID} exit={end}",
    ]


def jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def tail(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-TAIL:]
    except FileNotFoundError:
        return []


class Flow(Case):
    SHIMS = ("claude",)

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        state = os.path.join(self.root, "state")
        self.workdirs, self.logs = os.path.join(state, "work"), os.path.join(state, "logs")
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(
                local_section(run_fixtures.local(state) + TASK_LABELS, "orchestrator")
                + "\n"
                + run_fixtures.CORE_LOCAL
            )
        self.fake = fake_linear.FakeLinear(harness=HARNESS)
        self.ids = {HARNESS_EMAIL: self.fake.user(HARNESS_EMAIL, "Harness", HARNESS)}
        for r in ROLES:
            self.ids[ACCOUNTS[r]] = self.fake.user(ACCOUNTS[r], r.title(), KEYS[r])
        self.ids[HUMAN] = self.fake.user(HUMAN, "Me")
        self.fake.label_group(board_ids.TASK_GROUP, "Tasks", {LIGHT_RESEARCH: "Light Research"})
        self.seam = fake_linear.seam(self, self.fake, fake_linear.serve(self, self.fake))
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.path = fake_claude.install(self.bin)
        self.claude_scenario, self.claude_log = (
            os.path.join(self.root, n) for n in ("claude.json", "claude.jsonl")
        )

    def base(self):
        return {"HOME": self.home, "PATH": self.path, "PYTHONUTF8": "1"}

    def env(self):
        return {
            **child_env({}, WORKTREE, os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            # prune lists tmux sessions: a private dir (or base()'s server), never the developer's
            "TMUX_TMPDIR": self.root,
            **self.base(),
            "TZ": "UTC",
            "ENSEMBLE_PATH": self.bin,
            "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude-config"),
            "FAKE_LINEAR_TRANSPORT": self.seam,
            fake_claude.ENV: self.claude_scenario,
        }

    def guard(self):
        """config.path() must find each of SHIMS in self.bin first: never a real one."""
        res = self.script("-c", GUARD, *self.SHIMS)
        want = {n: os.path.join(self.bin, n) for n in self.SHIMS}
        self.assertEqual((res.returncode, res.stdout), (0, json.dumps(want) + "\n"), res.stderr)

    def scene(self, steps, **scenario):
        with open(self.claude_scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.claude_log}, f)

    def script(self, *argv, **env):
        """argv under python3, env added to self.env()."""
        return subprocess.run(
            [*PY, *argv],
            cwd=self.root,
            env={**self.env(), **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def promote(self, *argv):
        return self.script(*PROMOTE, *argv)

    def router(self, *argv, **env):
        return self.script(*ROUTER, *argv, **env)

    def logged(self):
        return run_fixtures.logged(self.logs)

    def said(self):
        return [f"{e['src']} {run_fixtures.show(e)}" for e in self.logged()]

    def runs(self):
        return [
            {k: v for k, v in e.items() if k != "ts"}
            for e in jsonl(os.path.join(self.logs, "runs.jsonl"))
        ]

    def calls(self):
        return jsonl(self.claude_log)

    def records(self, ident):
        return jsonl(run_log(os.path.join(self.workdirs, ident)))

    def ledger(self, ident):
        """Each sid's write-back steps."""
        try:
            with open(os.path.join(self.workdirs, ident, "writeback.json")) as f:
                return {sid: entry.get("steps", []) for sid, entry in json.load(f).items()}
        except FileNotFoundError:
            return {}

    def requests(self):
        with self.fake.lock:
            return list(self.fake.requests)

    def sent(self, op, account=None):
        return [r.variables for r in self.requests() if r.op == op and account in (None, r.account)]

    def problems(self):
        return [(r.op, r.account, r.result) for r in self.requests() if r.result != "ok"]

    def email(self, uid):
        return uid and self.fake.users[uid]["email"]

    def issue(self, ident):
        with self.fake.lock:
            return json.loads(json.dumps(self.fake.find(ident)))

    def attachments(self, ident):
        return self.issue(ident)["attachments"]

    def subscribers(self, ident):
        return self.issue(ident)["subscribers"]

    def comments(self, ident):
        return [(self.email(c["user"]), c["body"]) for c in self.issue(ident)["comments"]]

    def history(self, ident):
        return [
            (
                fake_linear.NAMES[h["fromStateId"]],
                fake_linear.NAMES[h["toStateId"]],
                self.email(h["actorId"]),
            )
            for h in self.issue(ident)["history"]
        ]

    def state(self, ident):
        return fake_linear.NAMES[self.issue(ident)["state"]]


class TmuxFlow(Flow):
    """A private tmux server (live_tmux) with session MANAGER for the sessions; fake_gh as
    `gh` over self.gh (write_gh), REPO a bare repo that HOME's .gitconfig puts in place of
    github.com.
    """

    SHIMS = ("claude", "gh", "ensemble-orchestrator")

    def setUp(self):
        super().setUp()
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.bin = self.server.bin
        fake_gh.install(self.bin)
        repos = os.path.join(self.root, "repos")
        fake_gh.bare(repos, *REPO.split("/"))
        fake_gh.gitconfig(self.home, repos)
        fake_gh.git_shim(self.bin, self.home)
        self.gh_scenario, self.gh = os.path.join(self.root, "gh.json"), copy.deepcopy(GH)
        self.write_gh()
        with open(callback := os.path.join(self.bin, "ensemble-orchestrator"), "w") as f:
            f.write(
                f"#!/bin/sh\nPYTHONPATH={shlex.quote(self.env()['PYTHONPATH'])} "
                f'exec {shlex.quote(sys.executable)} -P -s {shlex.quote(ENTRY)} "$@"\n'
            )
        os.chmod(callback, 0o755)
        self.guard()

    def base(self):
        return self.server.env()

    def env(self):
        return {
            **super().env(),
            fake_gh.ENV: self.gh_scenario,
        }

    def write_gh(self):
        # atomically: the fake reads it on each call
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.gh, f)
        os.replace(tmp, self.gh_scenario)

    def publish(self, path, text):
        self.gh["contents"].setdefault(DOCS, {}).setdefault(BRANCH, {})[path] = text
        self.write_gh()

    @staticmethod
    def doc_url(path):
        return f"https://github.com/{DOCS}/blob/{BRANCH}/{quote(path)}"

    @staticmethod
    def harness(role, ident, sid):
        return f"{role}-{ident}-{sid[:8]}-drive"

    def in_manager(self):
        """The environment of a command run in MANAGER's pane, a client attached to MANAGER."""
        if not self.server.clients:
            self.server.attach(MANAGER)
        return self.server.inside(MANAGER)

    def live(self, session):
        # tmux on Linux (CI: 3.4) can leave an exited pane process unreaped, its session listed,
        # until another child of the server exits: a no-op job is one
        self.server.tmux("run-shell", "-b", "true")
        return self.server.tmux("has-session", "-t", f"={session}").returncode == 0

    def wait_end(self, sid, ident, ends=1, role="engineer", timeout=TIMEOUT):
        """Until orchestrator.jsonl holds `ends` write-back end lines of sid and the run's harness
        session is gone; else fails with the tails of orchestrator.jsonl and the run's run.jsonl.
        """
        harness = self.harness(role, ident, sid)

        def ended():
            try:
                events = self.logged()
            except ValueError:  # a line half appended
                return False
            end = ("writeback", "end", sid)
            n = sum((e["src"], e["kind"], e.get("sid")) == end for e in events)
            return n >= ends and not self.live(harness)

        try:
            live_tmux.wait(
                ended, timeout, f"the write-back end #{ends} of {sid} and {harness} gone"
            )
        except AssertionError as e:
            timed_out = str(e)
        else:
            return
        self.fail(
            "\n".join(
                [
                    timed_out,
                    "orchestrator.jsonl:",
                    *tail(os.path.join(self.logs, "orchestrator.jsonl")),
                    "run.jsonl:",
                    *tail(run_log(os.path.join(self.workdirs, ident))),
                ]
            )
        )


class BinFlow(TmuxFlow):
    """bin/ensemble-orchestrator by absolute path with a launchd-like PATH and no PYTHONPATH; its python3
    is fake_python.py: this interpreter under the fake Linear transport with the test roots its
    fakes import, the only command added to PATH. The harness calls this installation's
    `ensemble-orchestrator` back.
    """

    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.bin, "ensemble-orchestrator"))
        self.python = write_shim(os.path.join(self.root, "python"))

    def bin_env(self):
        env = {k: v for k, v in self.env().items() if k != "PYTHONPATH"}
        env.update(PATH=f"{self.python}:/usr/bin:/bin", ENSEMBLE_PATH=f"{self.bin}:{self.python}")
        return env

    def start(self, *argv):
        """bin/ensemble-orchestrator argv, running: the test reads its output with communicate()."""
        proc = subprocess.Popen(
            [BIN, *argv],
            cwd=self.root,
            env=self.bin_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.addCleanup(stop)
        return proc

    def linear(self, *argv):
        return subprocess.run(
            [BIN, *argv],
            cwd=self.root,
            env=self.bin_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def router(self, *argv):
        return self.linear("router", *argv)

    def promote(self, *argv):
        return self.linear("promote", *argv)
