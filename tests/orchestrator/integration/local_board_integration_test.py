"""router --dry-run on a local board; the same issues on FakeLinear print the same lines."""

import json
import os
import re
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from ensemble.orchestrator.engine.config import BOARD_ENV, credentials, load
from ensemble.orchestrator.engine.router import CAP
from tests.orchestrator.integration.flow_fixtures import (
    BIN,
    HUMAN,
    LIGHT_RESEARCH,
    PROJECT,
    ROLES,
    TIMEOUT,
    BinFlow,
    usage,
)
from tests.orchestrator.unit.board_ids import role_id

AGENT_GROUP = "00000000-0000-4000-8000-000000000101"
CLAUDE = "00000000-0000-4000-8000-000000000102"
LINEAR_STATES = {"ready": "todo", "review": "in_review", "done": "done"}
T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)
USAGE = "five_hour=50% weekly=none"
BAD_TASK = (
    'Task "light-research" is not one of engineer\'s tasks (build, light-build, decompose). '
    "Fix the task or the assignee, then move the issue back to ready/."
)


class Seed(NamedTuple):
    """owner None: archived; hours: created after T0."""

    n: int
    owner: str | None
    state: str
    priority: int = 0
    hours: int = 0
    blocked_by: tuple[int, ...] = ()
    task: str | None = None
    client: str | None = None


ISSUES = (
    Seed(1, "engineer", "ready", 2, 3),
    Seed(2, "pm", "ready", 2, 1),  # after LOC-1: an earlier stage
    Seed(3, "engineer", "ready", 0, 0),  # last: no priority
    Seed(4, "engineer", "ready", 1, 5),  # first: urgent
    Seed(5, "engineer", "ready", 2, 2),  # before LOC-1: created earlier
    Seed(6, "engineer", "ready", 1, 0, (7,)),  # blocked: LOC-7 is in review
    Seed(7, "pm", "review"),
    Seed(8, "engineer", "ready", 3, 0, (9,)),  # queued: LOC-9 is done
    Seed(9, "engineer", "done"),
    Seed(10, "researcher", "ready", 3, 1, (11,)),  # blocked: archived LOC-11 last in review
    Seed(11, None, "review"),
    Seed(12, "unassigned", "ready", 1),  # never listed
    Seed(13, "me", "ready", 1),  # never listed
    Seed(14, "researcher", "ready", 4, 0, task="light-research", client="claude"),
    Seed(15, "engineer", "ready", 4, 1, task="light-research"),  # not an engineer task
)


def local(n):
    return f"LOC-{n}"


def text(seed):
    rows = [
        "---",
        f"id: {local(seed.n)}",
        f'title: "Issue {seed.n}"',
        f"priority: {seed.priority}",
        f"created: {(T0 + timedelta(hours=seed.hours)).isoformat()}",
        f"blocked_by: [{', '.join(map(local, seed.blocked_by))}]",
        *(f"{key}: {value}" for key, value in (("task", seed.task), ("client", seed.client))),
        "---",
        "### Description",
        f"Issue {seed.n}.",
    ]
    if seed.owner is None:
        rows += ["### History", f"- 2026-10-01 10:00 {seed.state} self"]
    return "\n".join(line for line in rows if not line.endswith(": None")) + "\n"


def write(board, seed, *, mtime=None):
    if seed.owner is None:
        folder = os.path.join(board, "archive")
    else:
        folder = os.path.join(board, seed.owner, seed.state)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{local(seed.n)} Issue {seed.n}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text(seed))
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def tree(board):
    """Each path under board: (bytes or None for a dir, mtime)."""
    found = {}
    for top, dirs, files in os.walk(board):
        for name in dirs + files:
            path = os.path.join(top, name)
            content = None
            if name in files:
                with open(path, "rb") as f:
                    content = f.read()
            found[os.path.relpath(path, board)] = (content, os.lstat(path).st_mtime_ns)
    return found


def lines(res):
    out = []
    for line in res.stderr.splitlines():
        event = json.loads(line)
        del event["ts"]
        out.append(event)
    return out


def router(*events):
    return [{"src": "router", **e} for e in events]


class LocalBoard(BinFlow):
    def setUp(self):
        super().setUp()
        self.board = os.path.join(self.root, "board")
        self.config = os.path.join(self.ensemble, "config.local.toml")
        with open(self.config, encoding="utf-8") as f:
            self.linear_text = f.read()
        self.assertEqual(self.linear_text.count('tracker = "linear"\n'), 1)
        self.use("local")

    def use(self, tracker):
        text = self.linear_text.replace(
            'tracker = "linear"\n',
            f'tracker = "{tracker}"\nlinear.agent_label_group = "{AGENT_GROUP}"\n',
        )
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(text + f'\n[orchestrator.linear.agent_labels]\nclaude = "{CLAUDE}"\n')

    def run_router(self, *argv, board=None):
        return subprocess.run(
            [BIN, "router", *argv],
            cwd=self.root,
            env={**self.bin_env(), BOARD_ENV: board or self.board},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def dry_run(self, board=None):
        self.scene([], usage=usage(0.5))
        res = self.run_router("--dry-run", board=board)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        return lines(res)

    def seed_board(self):
        for seed in ISSUES:
            write(self.board, seed)

    def seed_linear(self):
        self.fake.label_group(AGENT_GROUP, "Agent", {CLAUDE: "Claude"})
        owners = {"unassigned": None, "me": self.ids[HUMAN], None: role_id("pm")}
        owners.update((role, role_id(role)) for role in ROLES)
        for seed in ISSUES:
            labels = [LIGHT_RESEARCH] if seed.task else []
            labels += [CLAUDE] if seed.client else []
            self.fake.issue(
                f"Issue {seed.n}",
                identifier=f"TASK-{seed.n}",
                state=LINEAR_STATES[seed.state],
                delegate=owners[seed.owner],
                project=PROJECT,
                description=f"Issue {seed.n}.",
                priority=seed.priority,
                labels=labels,
                created=T0 + timedelta(hours=seed.hours),
            )
            if seed.owner is None:
                with self.fake.lock:
                    self.fake.find(f"TASK-{seed.n}")["archived"] = True
        for seed in ISSUES:
            for n in seed.blocked_by:
                self.fake.relate("blocks", f"TASK-{n}", f"TASK-{seed.n}")

    def test_dry_run_lists_the_queue_writes_nothing_and_matches_linear(self):
        """The blocked, then the queue by priority (0 last), later stage, created; a task not the
        role's bounces. Nothing written; Linear routes the same."""
        self.seed_board()
        before = tree(self.board)
        on_board = self.dry_run()

        def picked(n, source="default"):
            return [
                {"kind": "pick", "issue": local(n), "queue": 8},
                {
                    "kind": "preview",
                    "issue": local(n),
                    "client": "claude",
                    "client_source": source,
                    "credentials": {},
                },
            ]

        self.assertEqual(
            on_board,
            router(
                {"kind": "blocked", "issue": "LOC-6", "by": ["LOC-7"]},
                {"kind": "blocked", "issue": "LOC-10", "by": ["LOC-11"]},
                {"kind": "plan", "mode": "new", "queue": 8},
                *picked(4),
                {"kind": "usage", "client": "claude", "usage": USAGE, "allowed": True},
                *picked(5),
                *picked(1),
                *picked(2),
                *picked(8),
                *picked(15),
                {
                    "kind": "claim-skip",
                    "issue": "LOC-15",
                    "to": "in_review",
                    "reason": "bad task label",
                },
                {"kind": "would comment", "issue": "LOC-15", "body": BAD_TASK, "to": "in_review"},
                *picked(14, "label"),
                *picked(3),
            ),
        )
        self.assertEqual(tree(self.board), before)
        for name in (".lock", ".state"):
            self.assertFalse(os.path.lexists(os.path.join(self.board, name)), name)
        self.use("linear")
        self.seed_linear()
        on_linear = [e for e in self.dry_run() if e["kind"] != "token"]  # Linear's: the harness's
        unowned = [
            e
            for e in on_linear
            if e["kind"] == "claim-skip" and e.get("reason") == "no configured role delegate"
        ]
        self.assertEqual(
            unowned,
            router(
                {"kind": "claim-skip", "issue": "TASK-12", "reason": "no configured role delegate"},
                {"kind": "claim-skip", "issue": "TASK-13", "reason": "no configured role delegate"},
            ),
        )
        on_linear = [e for e in on_linear if e not in unowned]
        linear_comment = next(e for e in on_linear if e["kind"] == "would comment")
        self.assertEqual(
            linear_comment["body"],
            BAD_TASK.replace("ready/", "Todo").replace("assignee", "delegate"),
        )
        normalized = [
            json.loads(re.sub(r"\bLOC-(\d+)", r"TASK-\1", json.dumps(e))) for e in on_board
        ]
        next(e for e in normalized if e["kind"] == "would comment")["body"] = linear_comment["body"]
        cfg = load()
        remote_credentials = {
            f"TASK-{seed.n}": credentials(cfg, seed.owner, seed.task)
            for seed in ISSUES
            if seed.owner in cfg.roles
        }
        for event in normalized:
            if "credentials" in event:
                self.assertEqual(event["credentials"], {})
        for event in on_linear:
            if "credentials" in event:
                self.assertEqual(event["credentials"], remote_credentials[event["issue"]])
                event["credentials"] = {}
        self.assertEqual(normalized, on_linear)

    def test_parse_error_exit_2(self):
        """A tick and --issue name the file and the reason before any pick."""
        self.seed_board()
        bad = os.path.join(self.board, "pm", "ready", "LOC-16 Bad.md")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("id: LOC-16\n")
        for argv in ((), ("--issue", "LOC-1")):
            with self.subTest(argv=argv):
                res = self.run_router(*argv, "--dry-run")
                self.assertEqual(
                    (res.returncode, res.stdout, res.stderr),
                    (2, "", f"router.py: {bad}: no front matter\n"),
                )

    def test_missing_board_exit_2(self):
        for name, made, reason in (
            ("missing", (), "no such board directory"),
            ("empty", (".state",), "empty board: no assignee directory"),
        ):
            with self.subTest(name):
                for dirname in made:
                    os.makedirs(os.path.join(self.board, dirname))
                res = self.run_router("--dry-run")
                self.assertEqual(
                    (res.returncode, res.stdout, res.stderr),
                    (2, "", f"router.py: {self.board}: {reason}\n"),
                )

    def test_two_boards_keep_state_apart(self):
        """Board A's runs.jsonl counts LOC-1's failed launches on A only."""
        one = Seed(1, "engineer", "ready", 2)
        boards = [os.path.join(self.root, name) for name in ("a", "b")]
        for board in boards:
            write(board, one, mtime=T0)
        runs = os.path.join(boards[0], ".state", "logs", "runs.jsonl")
        os.makedirs(os.path.dirname(runs))
        with open(runs, "w", encoding="utf-8") as f:
            for _ in range(CAP):
                at = datetime.now(UTC).isoformat()
                f.write(json.dumps({"ts": at, "kind": "failed", "issue": "LOC-1"}) + "\n")
        plan = {"kind": "plan", "mode": "new", "queue": 1}
        self.assertEqual(
            self.dry_run(boards[0]),
            router(
                plan,
                {
                    "kind": "claim-skip",
                    "issue": "LOC-1",
                    "to": "in_review",
                    "reason": f"reached {CAP} attempts",
                },
                {"kind": "pick-none", "reason": "nothing claimable"},
                {"kind": "skip", "reason": "nothing claimed"},
            ),
        )
        self.assertEqual(
            self.dry_run(boards[1]),
            router(
                plan,
                {"kind": "pick", "issue": "LOC-1", "queue": 1},
                {
                    "kind": "preview",
                    "issue": "LOC-1",
                    "client": "claude",
                    "client_source": "default",
                    "credentials": {},
                },
                {"kind": "usage", "client": "claude", "usage": USAGE, "allowed": True},
            ),
        )
        self.assertFalse(os.path.lexists(os.path.join(boards[1], ".state")))


if __name__ == "__main__":
    unittest.main()
