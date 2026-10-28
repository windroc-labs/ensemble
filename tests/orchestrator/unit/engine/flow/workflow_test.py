"""The workflow: the packaged file's Stages, a stage's defaults, its lazy read and its checks."""

import tomllib
from importlib.resources import files
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow import workflow
from ensemble.orchestrator.engine.flow.types import Stage
from tests.orchestrator.case import Case
from tests.utils.sandbox import scratch

HINT = "To change the target repo, edit the description's `Repo:` line."
GOOD = '[[stage]]\nrole = "pm"\ninput = "design"\ntitle_prefix = "PRD"\n'


def stage(role, input_, **fields):
    """Stage with every field given: the defaults spelled out, then fields."""
    base = {
        "next": "",
        "require_instructions": True,
        "title_prefix": "",
        "retitle": False,
        "start": "",
        "done": "",
        "question": "",
        "failed": "",
        "first_failure": "review",
        "approve": False,
        "assign": False,
        "files": False,
        "deliverable": False,
        "hint": "",
        "ref": False,
        "mapped_only": False,
    }
    return Stage(role=role, input=input_, **{**base, **fields})


class DefaultWorkflow(Case):
    def test_packaged_matches_todays_behaviour(self):
        # the Tasks and [roles.<role>] next/require_instructions, field for field
        self.assertIs(config.TASKS, workflow.DEFAULT)
        self.assertEqual(
            dict(workflow.Workflow()),
            {
                "researcher": stage(
                    "researcher",
                    "research",
                    next="pm",
                    title_prefix="RES",
                    start="Research started:",
                    first_failure="ready",
                    hint=HINT,
                ),
                "pm": stage(
                    "pm",
                    "design",
                    next="engineer",
                    require_instructions=False,
                    title_prefix="PRD",
                    retitle=True,
                    start="PRD started:",
                    approve=True,
                    assign=True,
                ),
                "engineer": stage(
                    "engineer",
                    "build",
                    title_prefix="ENG",
                    start="Build started:",
                    done="Build ready:",
                    question="Question:",
                    failed="Build failed:",
                    files=True,
                ),
            },
        )

    def test_packaged_config_has_no_flow_lines(self):
        with files("ensemble.orchestrator").joinpath("config.toml").open("rb") as f:
            roles = tomllib.load(f)["roles"]
        self.assertEqual(
            {r: sorted({"next", "require_instructions"} & set(p)) for r, p in roles.items()},
            dict.fromkeys(("researcher", "pm", "engineer"), []),
        )

    def test_reads_once_on_first_use(self):
        with mock.patch("tomllib.loads", wraps=tomllib.loads) as loads:
            flow = workflow.Workflow()
            self.assertEqual(loads.call_count, 0)
            self.assertEqual(flow["pm"].title_prefix, "PRD")
            self.assertEqual(sorted(flow), ["engineer", "pm", "researcher"])
            self.assertEqual(loads.call_count, 1)
            flow.load()
            self.assertEqual(loads.call_count, 2)


class StageDefaults(Case):
    def test_a_minimal_stage_takes_the_defaults(self):
        self.assertEqual(
            workflow.stage({"role": "analyst", "input": "research"}),
            stage(
                "analyst",
                "research",
                start="analyst started:",
                done="analyst done:",
                question="Question:",
                failed="analyst failed:",
            ),
        )

    def test_a_set_key_wins_even_when_empty(self):
        got = workflow.stage({"role": "a", "input": "build", "done": "", "first_failure": "ready"})
        self.assertEqual((got.done, got.first_failure, got.failed), ("", "ready", "a failed:"))

    def test_assign_is_off_unless_set(self):
        self.assertFalse(workflow.stage({"role": "a", "input": "build"}).assign)
        self.assertTrue(workflow.stage({"role": "a", "input": "build", "assign": True}).assign)
        self.assertEqual({r for r, s in workflow.Workflow().items() if s.assign}, {"pm"})

    def test_ref_and_mapped_only_are_off_unless_set(self):
        got = workflow.stage({"role": "v", "input": "build", "ref": True, "mapped_only": True})
        self.assertEqual((got.ref, got.mapped_only), (True, True))
        got = workflow.stage({"role": "v", "input": "build"})
        self.assertEqual((got.ref, got.mapped_only), (False, False))
        self.assertTrue(all(not s.ref and not s.mapped_only for s in workflow.Workflow().values()))

    def test_ref_and_mapped_only_are_only_for_build_stages(self):
        for key in ("ref", "mapped_only"):
            for input_ in ("research", "design"):
                with self.subTest(key=key, input=input_):
                    with self.assertRaises(ValueError) as cm:
                        workflow.stage({"role": "v", "input": input_, key: True})
                    self.assertEqual(str(cm.exception), f'stage v: {key}: only for input = "build"')
                    got = workflow.stage({"role": "v", "input": input_, key: False})
                    self.assertFalse(getattr(got, key))

    def test_deliverable_is_off_unless_set(self):
        self.assertFalse(workflow.stage({"role": "a", "input": "build"}).deliverable)
        self.assertTrue(
            workflow.stage({"role": "a", "input": "build", "deliverable": True}).deliverable
        )


class WorkflowFile(Case):
    def write(self, text):
        path = Path(scratch(self, real=False), "flow.toml")
        if text is not None:
            path.write_text(text, encoding="utf-8")
        return str(path)

    def read(self, text):
        """A fresh Workflow loaded from a file holding text (None: no file)."""
        return dict(workflow.Workflow().load(self.write(text)))

    def assert_refused(self, text, message):
        path = self.write(text)
        with self.assertRaises(ValueError) as cm:
            workflow.Workflow().load(path)
        self.assertTrue(str(cm.exception).startswith(f"{path}: "), cm.exception)
        self.assertIn(message, str(cm.exception))

    def test_a_good_file(self):
        self.assertEqual(
            self.read(GOOD + '\n[[stage]]\nrole = "x"\ninput = "build"\nnext = "pm"\n'),
            {
                "pm": workflow.stage({"role": "pm", "input": "design", "title_prefix": "PRD"}),
                "x": workflow.stage({"role": "x", "input": "build", "next": "pm"}),
            },
        )

    def test_the_packaged_file_by_default(self):
        flow = workflow.Workflow()
        self.assertEqual(flow.load(), workflow.Workflow().load(None))
        self.assertEqual(sorted(flow), ["engineer", "pm", "researcher"])

    def test_format_errors(self):
        for text, message in (
            (None, "No such file or directory"),
            ("[[stage]\n", "Expected ']]'"),
            ("", "want one or more [[stage]] tables and nothing else"),
            ("stage = []\n", "want one or more [[stage]] tables and nothing else"),
            ("stage = 1\n", "want one or more [[stage]] tables and nothing else"),
            ("stage = [1]\n", "want one or more [[stage]] tables and nothing else"),
            ("extra = 1\n" + GOOD, "want one or more [[stage]] tables and nothing else"),
            ('[[stage]]\ninput = "design"\n', "stage 1: role is required"),
            ('[[stage]]\nrole = "pm"\n', "stage 1: input is required"),
            (GOOD + '[[stage]]\nrole = "x"\n', "stage 2: input is required"),
            ('[[stage]]\nrole = ""\ninput = "design"\n', "stage 1: role: want a non-empty str: ''"),
            ('[[stage]]\nrole = 1\ninput = "design"\n', "stage 1: role: want a non-empty str: 1"),
            (GOOD + "bogus = 1\nzap = 2\n", "stage 1: unknown keys: bogus, zap"),
            (
                '[[stage]]\nrole = "pm"\ninput = "x"\n',
                "stage pm: input: want one of research, design, build: 'x'",
            ),
            (
                GOOD + 'first_failure = "todo"\n',
                "stage pm: first_failure: want one of ready, review: 'todo'",
            ),
            (GOOD + 'retitle = "yes"\n', "stage pm: retitle: want a bool: 'yes'"),
            (GOOD + "deliverable = 1\n", "stage pm: deliverable: want a bool: 1"),
            (GOOD + "hint = true\n", "stage pm: hint: want a str: True"),
            (GOOD + "next = 1\n", "stage pm: next: want a str: 1"),
            (GOOD + "ref = 1\n", "stage pm: ref: want a bool: 1"),
            (GOOD + 'mapped_only = "yes"\n', "stage pm: mapped_only: want a bool: 'yes'"),
            (GOOD + "ref = true\n", 'stage pm: ref: only for input = "build"'),
            (GOOD + "\n" + GOOD, "stage pm: listed twice"),
        ):
            with self.subTest(text=text):
                self.assert_refused(text, message)

    def test_next_must_name_another_stage(self):
        for nxt in ("pm", "engineer"):
            with self.subTest(nxt):
                self.assert_refused(
                    GOOD + f'next = "{nxt}"\n', f"stage pm: next {nxt!r} must name another stage"
                )

    def test_a_next_cycle(self):
        text = "".join(
            f'[[stage]]\nrole = "{r}"\ninput = "research"\nnext = "{n}"\n'
            for r, n in (("a", "b"), ("b", "c"), ("c", "a"))
        )
        self.assert_refused(text, "the next chain from 'a' has a cycle")

    def test_a_bad_read_keeps_the_last_good_one(self):
        flow = workflow.Workflow()
        flow.load(self.write(GOOD))
        with self.assertRaises(ValueError):
            flow.load(self.write("[[stage]\n"))
        self.assertEqual(list(flow), ["pm"])
