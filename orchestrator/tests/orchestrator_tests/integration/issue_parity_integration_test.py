"""Parity of ensemble-orchestrator issue on a local board and on FakeLinear (spec AC13, AC10):
bin/ensemble-orchestrator as a real process runs one sequence on each tracker; each step's exit
code and stdout shape match, and so do its tracker-independent values. Linear-only forms exit 2
on a local board, writing nothing.
"""

import json
import os
import subprocess
import unittest

from ensemble.orchestrator.engine.config import BOARD_ENV
from ensemble.orchestrator.engine.flow.types import IssueState
from ensemble.orchestrator.engine.types import MOVES, State
from orchestrator_tests.integration.fake_linear import TITLES, seam
from orchestrator_tests.integration.fake_python import write_shim
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    BIN,
    LIGHT_RESEARCH,
    TIMEOUT,
    Flow,
)
from orchestrator_tests.unit.issue_fixtures import CLIENT_GROUP, CLIENT_LABELS, MANAGER_KEY

TASK, CLIENT = "light-research", "claude"
ROLE = "researcher"  # TASK is one of its tasks
VALUES = ("operation", "result", "dry_run", "title", "description")
RELATIONS = ("blocks", "blocked_by", "children")
# each tracker's state names: FakeLinear's titles, the local board's abstract names
LINEAR_STATES = {TITLES[key]: MOVES[State(key)] for key in TITLES}
LOCAL_STATES = {state.value: state for state in IssueState}
LINEAR_ONLY = (
    ("lookup", ("lookup", "states")),
    ("label swap", ("label", "swap", "{seed}", "--from", "a", "--to", "b")),
    ("--label", ("list", "--label", "x")),
    ("--project", ("list", "--project", "p")),
    ("--label", ("create", "--title", "T", "--body-file", "{body}", "--label", "x")),
    ("--project", ("create", "--title", "T", "--body-file", "{body}", "--project", "p")),
    ("--milestone", ("create", "--title", "T", "--body-file", "{body}", "--milestone", "m")),
)


def same_shape(case, left, right, path="$"):
    """case fails unless left and right have the same keys at every object level, list lengths and
    scalar JSON types, a null on either side matching anything."""
    if left is None or right is None:
        return
    case.assertIs(type(left), type(right), f"{path}: {left!r} vs {right!r}")
    if isinstance(left, dict):
        case.assertEqual(sorted(left), sorted(right), path)
        for key in left:
            same_shape(case, left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        case.assertEqual(len(left), len(right), f"{path}: {left!r} vs {right!r}")
        for n, (a, b) in enumerate(zip(left, right, strict=True)):
            same_shape(case, a, b, f"{path}[{n}]")


def abstract(name, states):
    """name, a state name in states (a tracker's), as its IssueState; None stays None."""
    return None if name is None else states[name]


def related(entry, states):
    """A relation column's entry as its title and abstract state."""
    return entry["title"], abstract(entry["state"], states)


def values(payload, states):
    """payload's tracker-independent values, its state names read through states: VALUES at its
    top (a description as a local board's file reads it, stripped); a read's state, comment
    bodies (stripped, in order), relation columns and parent as titles and states; its items'
    titles and states as a set."""
    found = {key: payload[key] for key in VALUES if key in payload}
    if found.get("description") is not None:
        found["description"] = found["description"].strip()
    if "comments" in payload:
        found["state"] = abstract(payload["state"] and payload["state"]["name"], states)
        found["comments"] = [comment["body"].strip() for comment in payload["comments"]]
        for key in RELATIONS:
            found[key] = [related(entry, states) for entry in payload[key]]
        found["parent"] = payload["parent"] and related(payload["parent"], states)
    if "items" in payload:
        found["items"] = sorted(
            (item["title"], abstract(item["state"] and item["state"]["name"], states))
            for item in payload["items"]
        )
    return found


def tree(board):
    found = {}
    for top, _, files in os.walk(board):
        for name in files:
            with open(path := os.path.join(top, name), "rb") as f:
                found[os.path.relpath(path, board)] = (f.read(), os.lstat(path).st_mtime_ns)
    return found


class Parity(Flow):
    def setUp(self):
        super().setUp()
        self.python = write_shim(os.path.join(self.root, "python"))
        self.board = os.path.join(self.root, "board")
        self.config = os.path.join(self.ensemble, "config.local.toml")
        with open(self.config, encoding="utf-8") as f:
            self.linear_text = f.read()
        self.assertEqual(self.linear_text.count('tracker = "linear"\n'), 1)
        self.fake.user("manager@agents.test", "Manager", MANAGER_KEY)
        with open(self.seam, encoding="utf-8") as f:  # its keys: the users when it was written
            self.seam = seam(self, self.fake, json.load(f)["url"])
        self.fake.label_group(CLIENT_GROUP, "Agent", {CLIENT_LABELS[CLIENT]: "Claude"})
        self.files = {}
        for name, text in (
            ("a", "Body A."),
            ("b", "Body B."),
            ("edit", "Edited A."),
            ("note", "A note."),
        ):
            self.files[name] = os.path.join(self.root, f"{name}.md")
            with open(self.files[name], "w", encoding="utf-8") as f:
                f.write(text + "\n")

    def use(self, tracker):
        text = self.linear_text.replace(
            'tracker = "linear"\n',
            f'tracker = "{tracker}"\nlinear.manager_key = "{MANAGER_KEY}"\n'
            f'linear.agent_label_group = "{CLIENT_GROUP}"\n',
        )
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(
                text
                + f'\n[orchestrator.linear.agent_labels]\n{CLIENT} = "{CLIENT_LABELS[CLIENT]}"\n'
            )

    def issue(self, *argv):
        env = {k: v for k, v in self.env().items() if k != "PYTHONPATH"}
        env.update(PATH=f"{self.python}:/usr/bin:/bin", ENSEMBLE_PATH=f"{self.bin}:{self.python}")
        return subprocess.run(
            [BIN, "issue", *argv],
            cwd=self.root,
            env={**env, BOARD_ENV: self.board},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def seed_linear(self):
        """TASK-1 (engineer, ready, task and client) and TASK-2 (unassigned, backlog)."""
        engineer = self.ids[ACCOUNTS["engineer"]]
        self.fake.issue(
            "Seed 1",
            identifier="TASK-1",
            state="todo",
            assignee=engineer,
            description="Seed 1.",
            labels=[LIGHT_RESEARCH, CLIENT_LABELS[CLIENT]],
        )
        self.fake.issue("Seed 2", identifier="TASK-2", state="backlog", description="Seed 2.")
        return "TASK-1", "TASK-2"

    def seed_board(self):
        """LOC-1 and LOC-2 as seed_linear's."""
        for n, folder, front in (
            (1, ("engineer", "ready"), [f'task: "{TASK}"', f'client: "{CLIENT}"']),
            (2, ("unassigned", "backlog"), []),
        ):
            os.makedirs(directory := os.path.join(self.board, *folder), exist_ok=True)
            text = "\n".join(
                [
                    "---",
                    f"id: LOC-{n}",
                    f'title: "Seed {n}"',
                    "priority: 0",
                    "created: 2026-10-01T09:00:00+00:00",
                    *front,
                    "---",
                    "### Description",
                    f"Seed {n}.",
                ]
            )
            with open(os.path.join(directory, f"LOC-{n} Seed {n}.md"), "w", encoding="utf-8") as f:
                f.write(text + "\n")
        return "LOC-1", "LOC-2"

    def ident(self, res):
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["issue"]["identifier"]

    def operations(self, seeds):
        """The sequence on the configured tracker: each step's name and result, the read --full
        file's text as a step of its own."""
        steps, files = [], self.files

        def step(name, *argv):
            steps.append((name, res := self.issue(*argv)))
            return res

        a = self.ident(
            step(
                "create A",
                *("create", "--title", "Parity A", "--body-file", files["a"], "--state", "ready"),
                *("--role", ROLE, "--task", TASK, "--client", CLIENT, "--priority", "high"),
            )
        )
        b = self.ident(
            step(
                "create B",
                *("create", "--title", "Parity B", "--body-file", files["b"], "--parent", a),
                *("--blocked-by", seeds[0], "--blocks", seeds[1]),
            )
        )
        step("read A", "read", a)
        step("read B", "read", b)
        step("list ready", "list", "--state", "ready")
        step("list children", "list", "--parent", a)
        first = step("list page 1", "list", "--state", "ready", "--limit", "1")
        cursor = json.loads(first.stdout)["next_cursor"]
        step("list page 2", "list", "--state", "ready", "--limit", "1", "--cursor", cursor)
        step(
            "edit A",
            "edit",
            a,
            "--title",
            "Parity A2",
            "--priority",
            "low",
            "--body-file",
            files["edit"],
        )
        step("move B review", "move", b, "review")
        step("comment A", "comment", a, "--body-file", files["note"])
        step("relation add", "relation", "add", a, "--blocks", seeds[1])
        step("relation remove", "relation", "remove", a, "--blocks", seeds[1])
        step("relation add again", "relation", "add", a, "--blocks", seeds[1])
        step("relation remove reverse", "relation", "remove", seeds[1], "--blocked-by", a)
        step("archive dry run refused", "archive", a, "--dry-run")
        step("archive refused", "archive", a)
        step("move B done", "move", b, "done")
        step("move A done", "move", a, "done")
        step("archive dry run", "archive", a, "--dry-run")
        step("archive", "archive", a)
        output = os.path.join(self.root, "full.json")
        step("read A full", "read", a, "--full", "--output", output)
        with open(output, encoding="utf-8") as f:
            steps.append(("read A full file", subprocess.CompletedProcess((), 0, f.read(), "")))
        return a, steps

    def test_same_operations_same_format(self):
        self.use("linear")
        on_linear = self.operations(self.seed_linear())
        self.use("local")
        on_local = self.operations(self.seed_board())
        self.assertEqual([n for n, _ in on_linear[1]], [n for n, _ in on_local[1]])
        pages = {}
        for (name, left), (_, right) in zip(on_linear[1], on_local[1], strict=True):
            with self.subTest(name):
                self.assertEqual(
                    (left.returncode, bool(left.stdout), bool(left.stderr)),
                    (right.returncode, bool(right.stdout), bool(right.stderr)),
                    f"linear: {left.stderr}local: {right.stderr}",
                )
                if not left.stdout:
                    continue
                linear, local = json.loads(left.stdout), json.loads(right.stdout)
                same_shape(self, linear, local)
                linear, local = values(linear, LINEAR_STATES), values(local, LOCAL_STATES)
                if name.startswith("list page"):
                    pages[name] = (linear.pop("items"), local.pop("items"))
                self.assertEqual(linear, local)
        ready = [("Parity A", IssueState.READY), ("Seed 1", IssueState.READY)]
        self.assertEqual(sorted(sum((p[0] for p in pages.values()), [])), ready)
        self.assertEqual(sorted(sum((p[1] for p in pages.values()), [])), ready)
        for ident, (_, steps) in zip(
            (on_linear[0], on_local[0]), (on_linear, on_local), strict=True
        ):
            refused = dict(steps)["archive refused"]
            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertTrue(
                refused.stderr.startswith(
                    f"ensemble-orchestrator issue: archive {ident} refused, nothing archived: "
                    "2 unfinished of 2 "
                ),
                refused.stderr,
            )
            dry = json.loads(dict(steps)["archive dry run refused"].stdout)
            self.assertEqual((dry["result"], dry["unfinished_count"]), ("refused", 2))

    def test_linear_only_exits_2_on_local(self):
        self.use("local")
        seed, _ = self.seed_board()
        before = tree(self.board)
        for what, argv in LINEAR_ONLY:
            argv = [a.format(seed=seed, body=self.files["a"]) for a in argv]
            with self.subTest(argv=argv):
                res = self.issue(*argv)
                self.assertEqual(
                    (res.returncode, res.stdout, res.stderr),
                    (
                        2,
                        "",
                        f"ensemble-orchestrator issue: {what}: Linear only, not on a local board "
                        '([orchestrator] tracker = "local")\n',
                    ),
                )
                self.assertEqual(tree(self.board), before)


if __name__ == "__main__":
    unittest.main()
