"""Configuration loading, validation, frozen values and driver names."""

import builtins
import dataclasses
import io
import json
import os
import plistlib
import tomllib
from importlib.resources import files
from pathlib import Path
from unittest import mock

from ensemble import delegate, utils
from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow import workflow
from ensemble.orchestrator.engine.types import AppKind, LinearApp, State, TrackerKind
from ensemble.utils import temp_dirs
from tests.delegate.fakes.delegate import FakeDelegate
from tests.orchestrator.case import REPO, ROOTS, SID, Case
from tests.orchestrator.config_fixtures import (
    BARE,
    BASE,
    CORE_LOCAL,
    HARNESS_DELEGATE,
    PIPELINE,
    SLUG,
    ConfigFile,
    write,
)
from tests.orchestrator.unit.board_ids import (
    HARNESS_APP,
    HARNESS_LINE,
    HEADER,
    MANAGER_APP,
    MANAGER_IDENTITY,
    MANAGER_LINE,
    MANAGER_USER,
    PACKAGED_FLOW,
    STATES,
    TASK_GROUP,
    TEAM,
    role,
    role_app,
    role_id,
    workflow_line,
)
from tests.utils.case import local_section
from tests.utils.process_fixtures import DEVNULL, CompletedProcess
from tests.utils.sandbox import scratch

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
AGENT_GROUP = "00000000-0000-4000-8000-000000000023"

P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"


STATES_LINE = HEADER[HEADER.index("linear.states = ") : HEADER.index("linear.task_label_group")]


def missing(keys):
    return (
        f"required, unset: {keys}: set them in ~/.ensemble/config.local.toml (orchestrator/config.toml's "
        "`# local:` lines)"
    )


def old_key(key):
    return (
        f"core: run overlay: [overlay.roles.researcher] has {key!r}: task-level config and default_task "
        "are gone; set run keys in [overlay.roles.researcher] (a run without a task picks one)"
    )


ROLES = {
    "researcher": (
        role_id("researcher"),
        "linear-app-researcher-token",
        ("deep-research", "research", "light-research"),
        1,
        "pm",
    ),
    "pm": (role_id("pm"), "linear-app-pm-token", ("product-design",), 1, "engineer"),
    "engineer": (
        role_id("engineer"),
        "linear-app-engineer-token",
        ("build", "light-build"),
        1,
        None,
    ),
}


def fields(cfg):
    """Each role's app user id, token service, tasks, max_runs and its stage's next (None: the last)."""
    return {
        r: (
            cfg.linear.roles[r].user_id,
            cfg.linear.roles[r].token_service,
            x.tasks,
            x.max_runs,
            config.TASKS[r].next or None,
        )
        for r, x in cfg.roles.items()
    }


CORE_TOML = """tier = 2
effort = "high"
client = "claude"
idle_timeout = 60
show = ""
output = { type = "local" }

[roles.researcher]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Research/" }

[roles.pm]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }

[roles.engineer]
output = { type = "pull-request" }
"""

DESIGN = 'output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }'

DIRS = {"researcher": "Research/", "pm": "Designs/"}


class CatalogDelegate(FakeDelegate):
    ROLES = {
        "researcher": {"catalog-task": "Research"},
        "pm": {"design-task": "Design"},
        "engineer": {"build-task": "Build"},
    }
    calls = []
    trusted = frozenset()

    @classmethod
    def roles(cls):
        cls.calls.append(("roles",))
        return super().roles()

    @classmethod
    def trusted_dirs(cls):
        cls.calls.append(("trusted_dirs",))
        return cls.trusted

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls.calls.append(("resolve", role, overlays))
        return delegate.RunConfig(
            role=role,
            task=task or "",
            tier=2,
            effort="high",
            client="catalog",
            output={"type": "github", "repo": "acme/notes", "branch": "trunk", "dir": f"{role}/"},
            idle_timeout=60,
            show="",
        )


HARNESS = LinearApp("harness", AppKind.HARNESS, "linear.harness", **HARNESS_APP)

LINEAR_DEFAULTS = {
    **dict.fromkeys(("team", "assignee_id", "task_label_group", "agent_label_group"), ""),
    "harness": {**dict.fromkeys(config.AppKey, ""), "renewal": "transport"},
    "manager": {**dict.fromkeys(config.AppKey, ""), "renewal": "transport"},
    "states": dict.fromkeys(State, ""),
    "human_members": [],
    **dict.fromkeys(("task_labels", "agent_labels", "project_repos"), {}),
    "roles": dict.fromkeys(
        ("researcher", "pm", "engineer"),
        {**dict.fromkeys(config.AppKey, ""), "renewal": "transport"},
    ),
}


ID = "TASK-12"

RESEARCHER_ONLY = workflow_line('[[stage]]\nrole = "researcher"\ninput = "research"\n')

# pm, then engineer: no researcher stage
PM_ENGINEER = workflow_line(
    '[[stage]]\nrole = "pm"\ninput = "design"\nnext = "engineer"\ntitle_prefix = "PRD"\n'
    '[[stage]]\nrole = "engineer"\ninput = "build"\n'
)

# dummy-tester (a core charter, as a custom role), then pm
TESTER_PM = (
    '[[stage]]\nrole = "dummy-tester"\ninput = "research"\nnext = "pm"\n'
    '[[stage]]\nrole = "pm"\ninput = "design"\ntitle_prefix = "PRD"\n'
)

TESTER_ROLE = role("dummy-tester")

ENGINEER_ONLY = workflow_line('[[stage]]\nrole = "engineer"\ninput = "build"\n')

# researcher, then pm, the last stage
RESEARCHER_PM = workflow_line(
    '[[stage]]\nrole = "researcher"\ninput = "research"\nnext = "pm"\n'
    '[[stage]]\nrole = "pm"\ninput = "design"\n'
)

A = "engineer-TASK-12-0b6f2c1e"


class ConfigurationLoading(ConfigFile, Case):
    def test_app_renewal_writer_is_explicit_and_validated(self):
        for table in ("linear.harness", "linear.manager", "linear.roles.engineer"):
            with self.subTest(table=table):
                if table.endswith("harness"):
                    text = PIPELINE.replace(
                        HARNESS_LINE, HARNESS_LINE.replace(" }", ', renewal = "broker" }')
                    )
                elif table.endswith("manager"):
                    text = MANAGER_LINE.replace(" }", ', renewal = "broker" }') + PIPELINE
                else:
                    text = PIPELINE.replace(f"[{table}]", f'[{table}]\nrenewal = "broker"')
                found = self.load(text).linear
                app = (
                    found.harness
                    if table.endswith("harness")
                    else found.manager
                    if table.endswith("manager")
                    else found.roles["engineer"]
                )
                self.assertEqual(app.renewal, "broker")
                with self.assertRaisesRegex(config.ConfigError, "renewal"):
                    self.load(text.replace('renewal = "broker"', 'renewal = "other"'))

    def test_role_apps_require_all_four_fields_and_reject_other_keys(self):
        app = {
            "client_id": "client-dummy",
            "secret_service": "dummy-secret",
            "token_service": "dummy-token",
            "user_id": "00000000-0000-4000-8000-000000000301",
        }

        def text(fields):
            return (
                BARE
                + "[linear.roles.dummy-tester]\n"
                + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())
            )

        found = self.load(text(app)).linear.roles["dummy-tester"]
        self.assertEqual(found.scopes, "read,write,app:assignable,app:mentionable")
        self.assertEqual(found.user_id, app["user_id"])
        for key in app:
            for fields in ({k: v for k, v in app.items() if k != key}, app | {key: ""}):
                with (
                    self.subTest(key=key, fields=fields),
                    self.assertRaisesRegex(config.ConfigError, f"linear.roles.dummy-tester.{key}"),
                ):
                    self.load(text(fields))
        for key in ("typo", "token"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(config.ConfigError, f"has unknown keys: {key}"),
            ):
                self.load(text(app | {key: "old"}))

    def test_stage_order(self):
        self.assertEqual(self.load(PIPELINE).stages, {"researcher": 0, "pm": 1, "engineer": 2})
        researcher_last = workflow_line(PACKAGED_FLOW.replace('next = "pm"\n', ""))
        self.assertEqual(
            self.load(HEADER + researcher_last + PIPELINE[len(HEADER) :]).stages,
            {"researcher": 0, "pm": 0, "engineer": 1},
        )

    def test_rejected(self):
        body = PIPELINE[PIPELINE.index("[linear.roles") :]
        group = "linear.task_label_group must be a Linear label group id (UUID): "
        label = BASE + f'[linear.task_labels]\nlight-research = "{LABEL1}"\n'
        upper = "ABCDEF00-0000-4000-8000-000000000021"
        solo = role("dummy-tester")
        x = BARE + "[roles.dummy-tester]\n[linear.roles.dummy-tester]\n"
        cases = [
            (
                HEADER
                + 'docs = { repo = "acme/notes", clone = "/nonexistent/notes", branch = "trunk" }\n'
                + body,
                "unknown keys: docs",
            ),
            (HEADER + '[projects.p]\nnext = "q"\n' + body, "unknown keys: projects"),
            (
                BASE + '[tasks.x]\nmodel = "opus"\n[docs]\nrepo = "acme/notes"\n',
                "unknown keys: docs, tasks",
            ),
            (
                BASE.replace(solo, role("dummy-tester", 'role = "x"', 'task = "y"')),
                "[roles.dummy-tester] has unknown keys: role, task",
            ),
            (
                BASE.replace(solo, role("dummy-tester", 'tasks = ["x"]', "read_only = []")),
                "[roles.dummy-tester] has unknown keys: read_only",
            ),
            (BARE, missing(", ".join(f"linear.roles.dummy-tester.{k}" for k in config.AppKey))),
            (x + "max_runs = 2\n", "[linear.roles.dummy-tester] has unknown keys: max_runs"),
            (
                BASE + '[linear.roles.ghost]\nclient_id = "g"\n',
                "[linear.roles.ghost] names no stage of the workflow",
            ),
            (BASE + "[roles.ghost]\n", "[roles.ghost] names no stage of the workflow"),
            (BASE + "[roles.ghost]\nnext = 'pm'\n", "[roles.ghost] has unknown keys: next"),
            (
                BASE.replace(
                    "[roles.dummy-tester]\n",
                    '[roles.dummy-tester]\ntypo = "s"\ntoken = "k"\n',
                ),
                "[roles.dummy-tester] has unknown keys: token, typo",
            ),
            ("linear.typo = 1\nlinear.other = 2\n" + BASE, "unknown keys in [linear]: other, typo"),
            (HEADER + "linear.roles = 5\n[roles.x]\n", "[linear.roles] must be a table"),
            (HEADER + "roles = 5\n", "[roles] must be a table"),
            (HEADER + "roles.pm = 5\n", "[roles.pm] must be a table"),
            *[
                (
                    BARE + role("dummy-tester", f"max_runs = {value}"),
                    f"[roles.dummy-tester] max_runs must be a whole number >= 1: {shown}",
                )
                for shown, value in {
                    "0": "0",
                    "-1": "-1",
                    "'2'": '"2"',
                    "True": "true",
                    "1.5": "1.5",
                }.items()
            ],
            (
                BASE.replace(HARNESS_LINE, ""),
                missing(
                    "linear.harness.client_id, linear.harness.secret_service, "
                    "linear.harness.token_service, linear.harness.user_id"
                ),
            ),
            *[
                (
                    BASE.replace(f'{key} = "{HARNESS_APP[key]}"', f'{key} = ""'),
                    missing(f"linear.harness.{key}"),
                )
                for key in HARNESS_APP
            ],
            (
                BASE.replace('user_id = "', 'typo = 1, user_id = "', 1),
                "[linear.harness] has unknown keys: typo",
            ),
            (
                BASE.replace(HARNESS_LINE, 'linear.harness = "x"\n'),
                "[linear.harness] must be a table",
            ),
            *[
                (
                    BASE.replace(f'{key} = "{HARNESS_APP[key]}"', f"{key} = {value}"),
                    f"linear.harness.{key} must be {what}: {shown}",
                )
                for key, value, shown, what in (
                    ("client_id", '"a b"', "'a b'", "a Linear OAuth app Client ID"),
                    ("client_id", "1", "1", "a Linear OAuth app Client ID"),
                    ("secret_service", '"x -s y"', "'x -s y'", "a Keychain service name"),
                    ("token_service", '"-x"', "'-x'", "a Keychain service name"),
                    ("user_id", '"harness"', "'harness'", "a Linear user id (UUID)"),
                )
            ],
            (
                BASE.replace('"linear-app-harness-secret"', '"linear-app-harness-token"'),
                "linear.harness.secret_service 'linear-app-harness-token' is also linear.harness.token_service",
            ),
            (
                BASE.replace(TEAM, "Acme's Agents"),
                'linear.team must be a Linear team id (UUID): "Acme\'s Agents"',
            ),
            (BASE.replace(f'"{TEAM}"', "5"), "linear.team must be a Linear team id (UUID): 5"),
            (BASE.replace(f'linear.team = "{TEAM}"\n', ""), missing("linear.team")),
            (
                'tracker = "linear"\n' + HEADER[HEADER.index("delegates = ") :] + body,
                missing(
                    "linear.team, linear.harness.client_id, linear.harness.secret_service, "
                    "linear.harness.token_service, linear.harness.user_id, "
                    "linear.task_label_group, linear.states.todo, "
                    "linear.states.in_progress, linear.states.in_review, linear.states.handoff, "
                    "linear.states.done, linear.states.canceled, linear.states.backlog"
                ),
            ),
            (
                BASE.replace(STATES_LINE, ""),
                missing(
                    "linear.states.todo, linear.states.in_progress, linear.states.in_review, "
                    "linear.states.handoff, linear.states.done, linear.states.canceled, linear.states.backlog"
                ),
            ),
            (
                BASE.replace(STATES_LINE, 'linear.states = "x"\n'),
                missing(
                    "linear.states.todo, linear.states.in_progress, linear.states.in_review, "
                    "linear.states.handoff, linear.states.done, linear.states.canceled, linear.states.backlog"
                ),
            ),
            (
                BASE.replace(f', canceled = "{STATES["canceled"]}"', ""),
                missing("linear.states.canceled"),
            ),
            (
                BASE.replace(f', backlog = "{STATES["backlog"]}"', ""),
                missing("linear.states.backlog"),
            ),
            (
                BASE.replace(STATES_LINE, STATES_LINE.replace(" }\n", ', triage = "x" }\n')),
                "[linear.states] has unknown keys: triage",
            ),
            (
                BASE.replace(STATES["done"], "Done"),
                "linear.states.done must be a Linear workflow state id (UUID): 'Done'",
            ),
            (
                BASE.replace(f'linear.task_label_group = "{TASK_GROUP}"\n', ""),
                missing("linear.task_label_group"),
            ),
            (BASE.replace(TASK_GROUP, "not-a-uuid"), group + "'not-a-uuid'"),
            (
                BASE.replace(TASK_GROUP, "ABCDEF00-0000-4000-8000-000000000002"),
                group + "'ABCDEF00-0000-4000-8000-000000000002'",
            ),
            (BASE.replace(f'"{TASK_GROUP}"', "5"), group + "5"),
            *[
                (
                    HEADER + f"linear.task_labels = {value}\n" + body,
                    'linear.task_labels must be a table of <task> = "<Linear label id>"',
                )
                for value in ('"x"', "[1]", "5")
            ],
            *[
                (
                    label + f"deep-research = {value}\n",
                    f"linear.task_labels.deep-research must be a Linear label id (UUID): {shown}",
                )
                for shown, value in {
                    "'Light Research'": '"Light Research"',
                    repr(upper): f'"{upper}"',
                    "5": "5",
                    "['x']": '["x"]',
                    "''": '""',
                    "True": "true",
                }.items()
            ],
            (
                label + f'other = "{LABEL2}"\ndeep-research = "{LABEL1}"\n',
                (
                    f"linear.task_labels.light-research and linear.task_labels.deep-research have the "
                    f"same label id {LABEL1}"
                ),
            ),
            ('logs_dir = "/x"\n' + BASE, "unknown keys: logs_dir"),
            (
                HEADER + 'linear.project_repos = "x"\n' + body,
                'linear.project_repos must be a table of "<Linear project id>" = "<owner>/<name>"',
            ),
            *[
                (
                    HEADER + f"local_clones = {value}\n" + body,
                    'local_clones must be a table of "<owner>/<name>" = "<path>"',
                )
                for value in ('"/x"', '["/x"]', "5")
            ],
            (
                BASE + f'[local_clones]\n"{SLUG}" = "/x"\n"Acme/Ensemble" = "/y"\n',
                f"local_clones.{SLUG} and local_clones.Acme/Ensemble are the same repo",
            ),
            (PIPELINE + role("ghost"), "[roles.ghost] names no stage of the workflow"),
            (
                PIPELINE + role("dummy-tester"),
                "[roles.dummy-tester] names no stage of the workflow",
            ),
            (
                PIPELINE.replace("linear-app-pm-", "linear-app-researcher-"),
                "linear.roles.pm.secret_service 'linear-app-researcher-secret' is also linear.roles.researcher.secret_service",
            ),
            (
                PIPELINE.replace("linear-app-engineer-", "linear-app-harness-"),
                "linear.roles.engineer.secret_service 'linear-app-harness-secret' is also linear.harness.secret_service",
            ),
            (
                PIPELINE.replace("linear-app-pm-token", "linear-app-harness-secret"),
                "linear.roles.pm.token_service 'linear-app-harness-secret' is also linear.harness.secret_service",
            ),
            *[
                (
                    text + f'[linear.task_labels]\n{task} = "{LABEL1}"\n',
                    (
                        f"linear.task_labels.{task} is not a task of a role in orchestrator/config.toml"
                    ),
                )
                for task, text in (
                    ("echo", PIPELINE),
                    ("ghost", PIPELINE),
                    ("product-design", HEADER + RESEARCHER_ONLY + role("researcher")),
                )
            ],
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_bad_table_entry_rejected(self):
        cases = [
            ("linear.project_repos", key, value)
            for key, value in (
                ("not-a-uuid", "acme/x"),
                (P1.upper(), "acme/x"),
                (P1, "https://github.com/acme/x"),
                (P1, "acme/."),
                (P1, "acme/.."),
                (P1, ""),
                (P1, 42),
                (P1, "acme"),
            )
        ]
        cases += [
            ("local_clones", key, value)
            for key, value in (
                ("ensemble", "/x"),
                ("https://github.com/acme/ensemble", "/x"),
                ("acme/..", "/x"),
                (SLUG, 42),
                (SLUG, True),
                (SLUG, ["/x"]),
                (SLUG, ""),
                (SLUG, "x/y"),
                (SLUG, "./x"),
                (SLUG, " /x"),
                (SLUG, "/x\ny"),
                (SLUG, "/x\x85y"),
                (SLUG, "/x\u2028y"),
                (SLUG, "/x\u2029y"),
                (SLUG, "/x\ty"),
                (SLUG, "/x\x00y"),
            )
        ]
        for table, key, value in cases:
            with self.subTest(table=table, key=key, value=value):
                with self.assertRaises(config.ConfigError) as cm:
                    self.load(BASE + f'[{table}]\n"{key}" = {json.dumps(value)}\n')
                msg = str(str(cm.exception))
                self.assertTrue(msg.startswith(f"orchestrator/config.toml: {table}.{key} "), msg)
                self.assertIn(repr(value), msg)

    def test_a_complete_manager_app_loads_as_the_manager_identity(self):
        linear = self.load(MANAGER_LINE + PIPELINE).linear
        self.assertEqual(linear.manager, MANAGER_IDENTITY)
        self.assertEqual(config.manager_app(linear), MANAGER_IDENTITY)
        self.assertEqual(MANAGER_IDENTITY.scopes, "read,write,app:assignable")

    def test_an_incomplete_manager_app_loads_but_issue_refuses_it(self):
        none = dict.fromkeys(MANAGER_APP, "")
        for given in ({}, {"user_id": MANAGER_USER}, {**MANAGER_APP, "secret_service": ""}):
            with self.subTest(given=given):
                table = "[linear.manager]\n" + "".join(f'{k} = "{v}"\n' for k, v in given.items())
                linear = self.load(PIPELINE + table).linear
                self.assertEqual(linear.manager.user_id, given.get("user_id", ""))
                unset = [f"linear.manager.{k}" for k in MANAGER_APP if not {**none, **given}[k]]
                with self.assertRaises(config.ConfigError) as cm:
                    config.manager_app(linear)
                self.assertEqual(
                    str(cm.exception),
                    "orchestrator/config.toml: ensemble-orchestrator issue needs the manager's "
                    f"Linear OAuth app: required, unset: {', '.join(unset)}: set them in "
                    f"{config.LOCAL} (orchestrator/config.toml's `# local:` lines)",
                )
                self.assertIsNone(cm.exception.logs_dir)

    def test_manager_app_values_are_checked_when_set(self):
        def table(**fields):
            return (
                PIPELINE
                + "[linear.manager]\n"
                + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())
            )

        for text, message in (
            (
                table(user_id="manager"),
                "linear.manager.user_id must be a Linear user id (UUID): 'manager'",
            ),
            (table(user_id=1), "linear.manager.user_id must be a Linear user id (UUID): 1"),
            (
                table(client_id="client manager"),
                "linear.manager.client_id must be a Linear OAuth app Client ID: 'client manager'",
            ),
            (
                table(secret_service="a b"),
                "linear.manager.secret_service must be a Keychain service name: 'a b'",
            ),
            (
                table(token_service=False),
                "linear.manager.token_service must be a Keychain service name: False",
            ),
            (
                table(secret_service="linear-app-manager-x", token_service="linear-app-manager-x"),
                "linear.manager.secret_service 'linear-app-manager-x' is also linear.manager.token_service",
            ),
            (
                table(user_id=HARNESS_APP["user_id"]),
                f"linear.manager.user_id '{HARNESS_APP['user_id']}' is also linear.harness.user_id",
            ),
            (
                table(user_id=role_id("pm")),
                f"linear.manager.user_id '{role_id('pm')}' is also linear.roles.pm.user_id",
            ),
            (
                table(client_id="client-harness"),
                "linear.manager.client_id 'client-harness' is also linear.harness.client_id",
            ),
            (
                table(client_id="client-pm"),
                "linear.manager.client_id 'client-pm' is also linear.roles.pm.client_id",
            ),
            (
                table(token_service="linear-app-harness-token"),
                "linear.manager.token_service 'linear-app-harness-token' is also linear.harness.token_service",
            ),
            (
                table(secret_service="linear-app-pm-token"),
                "linear.manager.secret_service 'linear-app-pm-token' is also linear.roles.pm.token_service",
            ),
            (
                table(token_service="linear-app-engineer-secret"),
                "linear.manager.token_service 'linear-app-engineer-secret' is also "
                "linear.roles.engineer.secret_service",
            ),
            (table(typo="x"), "[linear.manager] has unknown keys: typo"),
            ("linear.manager = 1\n" + PIPELINE, "[linear.manager] must be a table"),
        ):
            with self.subTest(message):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_two_task_labels_and_projects_sharing_a_repo_load(self):
        cfg = self.load(
            PIPELINE
            + f'[linear.task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n'
        )
        self.assertEqual(
            cfg.linear.task_labels, {"light-research": LABEL1, "deep-research": LABEL2}
        )
        cfg = self.load(
            PIPELINE + f'[linear.project_repos]\n"{P1}" = "acme/x"\n"{P2}" = "acme/x"\n'
        )
        self.assertEqual(cfg.linear.project_repos, {P1: "acme/x", P2: "acme/x"})

    def test_workflow_path_replaces_the_packaged_one(self):
        # whole: the packaged researcher and engineer stages are gone, their tables ignored
        for value in (os.path.join(self.dir, "flow.toml"), "~/flow.toml"):
            write(os.path.expanduser(value), TESTER_PM)
            with self.subTest(value):
                cfg = self.load(
                    HEADER + f'workflow = "{value}"\n' + PIPELINE[len(HEADER) :] + TESTER_ROLE
                )
                self.assertEqual(list(cfg.roles), ["dummy-tester", "pm"])
                self.assertEqual(dict(cfg.stages), {"dummy-tester": 0, "pm": 1})
                self.assertEqual(config.TASKS["dummy-tester"].done, "dummy-tester done:")

    def test_empty_workflow_reads_the_packaged_one(self):
        cfg = self.load(HEADER + 'workflow = ""\n' + PIPELINE[len(HEADER) :])
        self.assertEqual(list(cfg.roles), ["researcher", "pm", "engineer"])
        self.assertEqual(config.TASKS.path, workflow.PACKAGED)

    def test_workflow_value_must_be_an_absolute_path(self):
        for value in ('"flow.toml"', '"./flow.toml"', "1", '"/x\\ty"'):
            with self.subTest(value):
                self.fails(
                    'orchestrator/config.toml: workflow must be "" (the packaged workflow) or a '
                    f"printable absolute or ~ path: {tomllib.loads('v = ' + value)['v']!r}",
                    HEADER + f"workflow = {value}\n" + PIPELINE[len(HEADER) :],
                )

    def test_unreadable_workflow(self):
        flow = os.path.join(self.dir, "missing.toml")
        with self.assertRaises(config.ConfigError) as cm:
            self.load(HEADER + f'workflow = "{flow}"\n' + PIPELINE[len(HEADER) :])
        self.assertTrue(str(cm.exception).startswith(f"{flow}: "), cm.exception)
        self.assertIn("No such file or directory", str(cm.exception))
        self.assertIsNotNone(cm.exception.logs_dir)

    def test_a_stage_role_the_delegate_does_not_know(self):
        # reported before the stage's missing Linear app
        flow = os.path.join(self.dir, "flow.toml")
        write(flow, '[[stage]]\nrole = "ghostwriter"\ninput = "research"\n')
        self.fails(
            f"{flow}: stage ghostwriter: not a role the Delegate knows (its charters: core and "
            "[delegate] team_dirs)",
            HEADER + f'workflow = "{flow}"\n',
        )

    def test_role_tables_outside_the_workflow(self):
        # a packaged stage's tables are dropped, keys checked; any other non-stage table is an error
        flow = os.path.join(self.dir, "flow.toml")
        write(flow, TESTER_PM)
        head = HEADER + f'workflow = "{flow}"\n' + role("pm") + TESTER_ROLE
        cfg = self.load(head + role("researcher", "max_runs = 2") + role("engineer"))
        self.assertEqual(list(cfg.roles), ["dummy-tester", "pm"])
        self.fails(
            "orchestrator/config.toml: [roles.researcher] has unknown keys: next",
            head + role("researcher", 'next = "pm"'),
        )
        self.fails(
            "orchestrator/config.toml: [roles.ghost] names no stage of the workflow",
            head + role("ghost"),
        )

    def test_task_tiers(self):
        self.assertEqual(self.load(PIPELINE).task_tiers, {})
        self.assertEqual(self.load(PIPELINE + "[task_tiers]\n").task_tiers, {})
        cfg = self.load(PIPELINE + "[task_tiers]\nlight-build = 3\nbuild = 1\n")
        self.assertEqual(cfg.task_tiers, {"light-build": 3, "build": 1})

    def test_task_tiers_rejected(self):
        cases = [
            (
                "task_tiers = 3\n" + PIPELINE,
                "task_tiers must be a table of <task> = <tier 1-4>",
            ),
            *[
                (
                    PIPELINE + f"[task_tiers]\nlight-build = {value}\n",
                    f"task_tiers.light-build must be a tier, a whole number 1-4: {shown}",
                )
                for value, shown in (
                    ("0", "0"),
                    ("5", "5"),
                    ("true", "True"),
                    ('"3"', "'3'"),
                    ("2.0", "2.0"),
                )
            ],
            *[
                (
                    text + f"[task_tiers]\n{task} = 3\n",
                    f"task_tiers.{task} is not a task of a role in orchestrator/config.toml",
                )
                for task, text in (
                    ("ghost", PIPELINE),
                    ("build", HEADER + RESEARCHER_ONLY + role("researcher")),
                )
            ],
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_usage_limit(self):
        for text, expected in (
            (PIPELINE, {}),
            (PIPELINE + "[usage_limit]\n", {}),
            (PIPELINE + '[usage_limit]\na = "off"\nb = "on"\n', {"a": False, "b": True}),
        ):
            with self.subTest(expected=expected):
                cfg = self.load(text)
                self.assertEqual(cfg.usage_limit, expected)
                with self.assertRaises(TypeError):
                    cfg.usage_limit["new"] = False
        self.local('[usage_limit]\na = "off"\n')
        self.assertEqual(
            self.load(PIPELINE + '[usage_limit]\na = "on"\nb = "on"\n').usage_limit,
            {"a": False, "b": True},
        )

    def test_usage_limit_rejected(self):
        self.fails(
            'orchestrator/config.toml: usage_limit must be a table of <client> = "on" or "off"',
            'usage_limit = "off"\n' + PIPELINE,
        )
        for value, shown in (
            ('"OFF"', "'OFF'"),
            ('""', "''"),
            ("true", "True"),
            ("false", "False"),
            ("0", "0"),
            ("[]", "[]"),
            ("{}", "{}"),
        ):
            with self.subTest(value=value):
                self.fails(
                    f'orchestrator/config.toml: [usage_limit] a must be "on" or "off": {shown}',
                    PIPELINE + f"[usage_limit]\na = {value}\n",
                )

    def test_client_limits_belong_to_session(self):
        self.fails(
            "orchestrator/config.toml: unknown keys: client_max_runs",
            PIPELINE + "[client_max_runs]\nopencode = 1\n",
        )

    def test_agent_labels(self):
        for top, table, group, labels in (
            ("", "", "", {}),
            ('linear.agent_label_group = ""\n', "[linear.agent_labels]\n", "", {}),
            (
                f'linear.agent_label_group = "{AGENT_GROUP}"\n',
                f'[linear.agent_labels]\nfuture-client = "{LABEL1}"\nother = "{LABEL2}"\n',
                AGENT_GROUP,
                {"future-client": LABEL1, "other": LABEL2},
            ),
        ):
            with self.subTest(group=group, labels=labels):
                cfg = self.load(top + PIPELINE + table)
                self.assertEqual(
                    (cfg.linear.agent_label_group, cfg.linear.agent_labels), (group, labels)
                )
                with self.assertRaises(TypeError):
                    cfg.linear.agent_labels["new"] = LABEL1

    def test_agent_labels_rejected(self):
        top = f'linear.agent_label_group = "{AGENT_GROUP}"\n'
        table = f'[linear.agent_labels]\nfirst = "{LABEL1}"\n'
        paired = (
            "linear.agent_label_group and [linear.agent_labels] must both be set or both be empty"
        )
        cases = [
            (top + PIPELINE, paired),
            (
                top + PIPELINE + f'[linear.agent_labels]\n"" = "{LABEL1}"\n',
                "[linear.agent_labels] client names must not be empty",
            ),
            (PIPELINE + table, paired),
            (top + PIPELINE + "[linear.agent_labels]\n", paired),
            ('linear.agent_label_group = ""\n' + PIPELINE + table, paired),
            *[
                (
                    f"linear.agent_label_group = {value}\n" + PIPELINE + table,
                    f"linear.agent_label_group must be a Linear label group id (UUID) or empty: {shown}",
                )
                for value, shown in (("5", "5"), ("false", "False"), ('"Agent"', "'Agent'"))
            ],
            *[
                (
                    top + f"linear.agent_labels = {value}\n" + PIPELINE,
                    'linear.agent_labels must be a table of <client> = "<Linear label id>"',
                )
                for value in ('"bad"', "[]", "false")
            ],
            *[
                (
                    top + PIPELINE + f"[linear.agent_labels]\nfirst = {value}\n",
                    f"linear.agent_labels.first must be a Linear label id (UUID): {shown}",
                )
                for value, shown in (
                    ('""', "''"),
                    ('"not-a-uuid"', "'not-a-uuid'"),
                    (
                        '"ABCDEF00-0000-4000-8000-000000000021"',
                        "'ABCDEF00-0000-4000-8000-000000000021'",
                    ),
                    ("5", "5"),
                    ("false", "False"),
                )
            ],
            (
                top + PIPELINE + table + f'second = "{LABEL1}"\n',
                f"linear.agent_labels.first and linear.agent_labels.second have the same label id {LABEL1}",
            ),
            (
                f'linear.agent_label_group = "{TASK_GROUP}"\n' + PIPELINE + table,
                "linear.agent_label_group and linear.task_label_group must be different",
            ),
            (
                top + PIPELINE + table + f'[linear.task_labels]\nbuild = "{LABEL1}"\n',
                f"linear.agent_labels.first and linear.task_labels.build have the same label id {LABEL1}",
            ),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_frozen(self):
        cfg = self.load(
            'linear.human_members = ["a@x.com"]\n'
            + PIPELINE
            + f'[linear.task_labels]\nbuild = "{LABEL1}"\n'
        )
        for obj, attr in (
            (cfg, "linear"),
            (cfg.linear, "team"),
            (cfg.linear.roles["pm"], "client_id"),
            (cfg, "roles"),
            (cfg, "work_dir"),
            (cfg.roles["pm"], "max_runs"),
            (cfg.docs, "repo"),
        ):
            with self.subTest(attr), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, attr, None)
        for obj, name in (
            (cfg.linear, "states"),
            (cfg.linear, "roles"),
            (cfg.linear, "task_labels"),
            (cfg.linear, "project_repos"),
            (cfg, "roles"),
            (cfg, "task_tiers"),
            (cfg, "local_clones"),
            (cfg, "stages"),
            (cfg, "delegates"),
        ):
            with self.subTest(name), self.assertRaises(TypeError):
                getattr(obj, name)["x"] = 1
        with self.assertRaises(TypeError):
            cfg.docs.dirs["x"] = "y"
        self.assertEqual(
            (cfg.linear.human_members, cfg.linear.assignee_id, cfg.roles["pm"].tasks),
            (("a@x.com",), "", ("product-design",)),
        )

    def test_roles_keep_delegate_runs_and_task_indexes_with_core_overlay(self):
        cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        overlay = {"roles": {"pm": {"tier": 1}}}
        roles = ["researcher", "pm", "engineer"]
        self.assertEqual(
            {r: x.tasks for r, x in cfg.roles.items()},
            {r: ROLES[r][2] for r in roles},
        )
        self.assertEqual(
            {r: x.run for r, x in cfg.roles.items()},
            {r: HARNESS_DELEGATE.resolve(r, overlays=(overlay,)) for r in roles},
        )

    def test_a_config_error_carries_the_log_dir_once_work_dir_is_valid(self):
        e = self.fails(
            "orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'",
            'work_dir = "w"\n' + PIPELINE,
        )
        self.assertIsNone(e.logs_dir)
        self.assertIsInstance(e, config.ConfigError)
        logs = os.path.join(os.path.realpath(self.ensemble), "logs")
        for message, text in (
            (
                "orchestrator/config.toml: " + missing("linear.team"),
                PIPELINE.replace(f'linear.team = "{TEAM}"\n', ""),
            ),
            (
                "orchestrator/config.toml: [roles.ghost] names no stage of the workflow",
                PIPELINE + role("ghost"),
            ),
        ):
            with self.subTest(message):
                e = self.fails(message, text)
                self.assertEqual((e.msg, e.logs_dir), (message, logs))


class RemovedSelectionKeys(Case):
    """Local keys model selection used: no reader knows them any more."""

    def setUp(self):
        self.local = os.path.join(self.ensemble, "config.local.toml")
        self.board = HEADER + role("researcher") + role("pm") + role("engineer")

    def load(self, extra):
        write(self.local, CORE_LOCAL + "\n" + local_section(self.board, "orchestrator") + extra)
        return config.load()

    def test_leftover_selector_section_is_ignored(self):
        self.assertEqual(
            self.load(""), self.load('[selector.jev]\nurl = "http://127.0.0.1:8000"\n')
        )

    def test_leftover_delegate_role_without_a_charter_is_refused(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load("[delegate.roles.choice-translator]\ntier = 4\n")
        self.assertIn("no charter roles/choice-translator.md", str(cm.exception))


class CommittedConfiguration(Case):
    def test_harness_uses_the_public_delegate_export(self):
        with files("ensemble.orchestrator").joinpath("config.toml").open("rb") as source:
            committed = tomllib.load(source)
        self.assertEqual(
            committed["delegates"]["harness"]["implementation"],
            "ensemble.delegate:HarnessDelegate",
        )
        self.assertIs(HARNESS_DELEGATE, delegate.HarnessDelegate)

    def test_launchd_jobs_run_ensemble_orchestrator_and_core_has_no_gate(self):
        for job in ("router", "promote"):
            plist = os.path.join(REPO, "launchd", f"com.windroc.ensemble.{job}.plist")
            with open(plist, "rb") as f:
                program = plistlib.load(f)["ProgramArguments"][0]
            self.assertTrue(program.endswith("/bin/ensemble-orchestrator"), program)
        with files("ensemble.orchestrator").joinpath("config.toml").open("rb") as source:
            committed = tomllib.load(source)
        self.assertEqual(committed["core"], {})
        self.assertEqual(
            self.load(HEADER + role("researcher") + role("pm") + role("engineer"))
            .roles["researcher"]
            .run.gate,
            "",
        )

    def setUp(self):
        write(os.path.join(self.ensemble, "config.local.toml"), CORE_LOCAL)
        self.local = os.path.join(self.ensemble, "config.local.toml")

    def load(self, local=None):
        if local is not None:
            write(self.local, CORE_LOCAL + "\n" + local_section(local, "orchestrator"))
        return config.load()

    def test_committed_config_holds_no_deployment_values(self):
        with files("ensemble.orchestrator").joinpath("config.toml").open("rb") as f:
            committed = tomllib.load(f)
        self.assertEqual(
            {k: committed[k] for k in ("tracker", "task_tiers", "local_clones", "work_dir")},
            {"tracker": "local", "task_tiers": {}, "local_clones": {}, "work_dir": "~/.ensemble"},
        )
        self.assertEqual(committed["linear"], LINEAR_DEFAULTS)
        self.assertEqual(
            committed["roles"],
            dict.fromkeys(
                ("researcher", "pm", "engineer"), {"max_runs": 3, "credentials": {}, "tasks": {}}
            ),
        )

    def test_packaged_config_has_no_client_run_limit(self):
        local = HEADER + role("researcher") + role("pm") + role("engineer")
        self.assertFalse(hasattr(self.load(local), "client_max_runs"))

    def test_without_local_names_what_is_missing(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('tracker = "linear"\n')
        self.assertEqual(
            str(cm.exception),
            "orchestrator/config.toml: "
            + missing(
                "linear.team, linear.harness.client_id, linear.harness.secret_service, "
                "linear.harness.token_service, linear.harness.user_id, "
                "linear.task_label_group, linear.states.todo, "
                "linear.states.in_progress, linear.states.in_review, linear.states.handoff, "
                "linear.states.done, linear.states.canceled, linear.states.backlog, "
                + ", ".join(f"linear.roles.{r}.{k}" for k in config.AppKey for r in ROLES)
            ),
        )

    def test_real_config(self):
        cfg = self.load(HEADER + role("researcher") + role("pm") + role("engineer"))
        self.assertEqual(
            {r: (*v, config.TASKS[r].require_instructions) for r, v in fields(cfg).items()},
            {
                "researcher": (*ROLES["researcher"][:3], 3, "pm", True),
                "pm": (*ROLES["pm"][:3], 3, "engineer", False),
                "engineer": (*ROLES["engineer"][:3], 3, None, True),
            },
        )
        self.assertEqual(list(cfg.roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            cfg.docs,
            config.Docs(
                "windroc/team-docs", "main", {"researcher": "Research/", "pm": "Product Design/"}
            ),
        )
        self.assertEqual(
            (cfg.linear.team, cfg.linear.task_labels, cfg.linear.project_repos, cfg.local_clones),
            (TEAM, {}, {}, {}),
        )
        self.assertEqual(cfg.stages, {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertEqual(
            {r: (x.run.gate, x.run.task, x.run.language) for r, x in cfg.roles.items()},
            {
                "researcher": ("", "", "Chinese"),
                "pm": ("", "", "Chinese"),
                "engineer": ("", "", "Chinese"),
            },
        )

    def test_every_linear_key_under_orchestrator_linear_loads(self):
        apps = "".join(
            role(r).split(f"[roles.{r}]")[0].replace("[linear.", "[orchestrator.linear.")
            for r in ("researcher", "pm", "engineer")
        )
        write(
            self.local,
            CORE_LOCAL
            + '\n[orchestrator]\ntracker = "linear"\nwork_dir = "~/.ensemble"\n'
            + "[orchestrator.task_tiers]\nlight-build = 3\n"
            + "[orchestrator.roles.engineer]\nmax_runs = 2\n"
            + f'[orchestrator.linear]\nteam = "{TEAM}"\nhuman_members = ["me@x.com"]\n'
            + f'assignee_id = "{P1}"\n'
            + HARNESS_LINE.replace("linear.harness", "harness")
            + f'task_label_group = "{TASK_GROUP}"\n'
            + f'agent_label_group = "{AGENT_GROUP}"\n'
            + "[orchestrator.linear.states]\n"
            + "".join(f'{k} = "{v}"\n' for k, v in STATES.items())
            + f'[orchestrator.linear.task_labels]\nbuild = "{LABEL1}"\n'
            + f'[orchestrator.linear.agent_labels]\nclaude = "{LABEL2}"\n'
            + apps
            + f'[orchestrator.linear.project_repos]\n"{P1}" = "acme/x"\n',
        )
        cfg = self.load()
        self.assertEqual(
            (
                cfg.linear.team,
                cfg.linear.human_members,
                cfg.linear.assignee_id,
                cfg.linear.harness,
                cfg.linear.task_label_group,
                cfg.linear.states,
            ),
            (TEAM, ("me@x.com",), P1, HARNESS, TASK_GROUP, STATES),
        )
        self.assertEqual(
            (
                cfg.linear.task_labels,
                cfg.linear.agent_label_group,
                cfg.linear.agent_labels,
                cfg.linear.project_repos,
            ),
            ({"build": LABEL1}, AGENT_GROUP, {"claude": LABEL2}, {P1: "acme/x"}),
        )
        self.assertEqual(
            (cfg.work_dir, cfg.task_tiers, cfg.local_clones),
            (os.path.realpath(self.ensemble), {"light-build": 3}, {}),
        )
        self.assertEqual(
            fields(cfg),
            {
                "researcher": (*ROLES["researcher"][:3], 3, "pm"),
                "pm": (*ROLES["pm"][:3], 3, "engineer"),
                "engineer": (*ROLES["engineer"][:3], 2, None),
            },
        )

    def test_role_next_and_require_instructions_are_unknown_keys(self):
        # the workflow is their only source
        local = HEADER + role("researcher") + role("engineer")
        for line in ("require_instructions = true", 'next = ""'):
            with self.subTest(line):
                with self.assertRaises(config.ConfigError) as cm:
                    self.load(local + role("pm", line))
                self.assertEqual(
                    str(cm.exception),
                    f"orchestrator/config.toml: [roles.pm] has unknown keys: {line.split()[0]}",
                )

    def test_a_bad_workflow_stops_load(self):
        local = HEADER + role("researcher") + role("pm") + role("engineer")
        tmp = scratch(self, real=False)
        flow = Path(tmp, "workflow.toml")
        for text, message in (
            ("[[stage]\n", "Expected ']]'"),
            (
                '[[stage]]\nrole = "pm"\ninput = "x"\n',
                "stage pm: input: want one of research, design, build: 'x'",
            ),
        ):
            flow.write_text(text, encoding="utf-8")
            with self.subTest(text):
                with (
                    mock.patch("importlib.resources.files", lambda _: Path(tmp)),
                    self.assertRaises(config.ConfigError) as cm,
                ):
                    self.load(local)
                self.assertTrue(
                    str(cm.exception).startswith(f"{workflow.PACKAGED}: "), cm.exception
                )
                self.assertIn(message, str(cm.exception))

    def test_local_lines_uncommented_set_every_required_key(self):
        with files("ensemble.orchestrator").joinpath("config.toml").open() as f:
            local = "".join(
                line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
            )
        write(self.local, CORE_LOCAL + "\n" + local)
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        zero = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            str(cm.exception),
            f"orchestrator/config.toml: linear.project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
        )

    def test_agent_local_lines_load(self):
        with files("ensemble.orchestrator").joinpath("config.toml").open() as source:
            local = tomllib.loads(
                "".join(
                    line.removeprefix("# local: ")
                    for line in source
                    if line.startswith("# local: ")
                )
            )["orchestrator"]["linear"]
        cfg = self.load(
            f'linear.agent_label_group = "{local["agent_label_group"]}"\n'
            + HEADER
            + role("researcher")
            + role("pm")
            + role("engineer")
            + "[linear.agent_labels]\n"
            + "".join(f'"{key}" = "{value}"\n' for key, value in local["agent_labels"].items())
        )
        self.assertEqual(
            (cfg.linear.agent_label_group, dict(cfg.linear.agent_labels)),
            (local["agent_label_group"], local["agent_labels"]),
        )


class WritableDirectories(Case):
    def test_work_then_the_temp_dirs(self):
        self.assertEqual(config.writable("/w"), ("/w", *temp_dirs()))
        with self.assertRaises(TypeError):
            config.writable()


class WorkDirectories(ConfigFile, Case):
    def test_unset_is_in_home(self):
        work = os.path.realpath(self.ensemble)
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.work_dir, cfg.workdirs, cfg.logs_dir, cfg.runs_log, cfg.issue_workdir("TASK-9")),
            (
                work,
                work + "/work",
                work + "/logs",
                work + "/logs/runs.jsonl",
                work + "/work/TASK-9",
            ),
        )
        home = os.path.dirname(work)
        self.fails(
            "orchestrator/config.toml: work_dir must neither lie in nor contain "
            f"local_clones.acme/x {home}: '~/.ensemble'",
            PIPELINE + '[local_clones]\n"acme/x" = "~"\n',
        )


LOCAL_BOARD = (
    HEADER[HEADER.index("delegates = ") :] + "[roles.researcher]\n[roles.pm]\n[roles.engineer]\n"
)


class TrackerChoice(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.board = os.path.join(os.path.realpath(self.home), "boards", "one")
        env = mock.patch.dict(os.environ, {config.BOARD_ENV: self.board})
        env.start()
        self.addCleanup(env.stop)

    def test_tracker_defaults_to_local(self):
        cfg = self.load(LOCAL_BOARD)
        self.assertEqual((cfg.tracker, cfg.board), (TrackerKind.LOCAL, self.board))
        self.assertEqual(self.load('tracker = "local"\n' + LOCAL_BOARD), cfg)

    def test_local_issue_prefix(self):
        self.assertEqual(config.load().local_prefix, "LOC")
        without_prefix = LOCAL_BOARD.replace('local.issue_prefix = "LOC"\n', "")
        self.assertEqual(
            self.load(without_prefix + '[local]\nissue_prefix = "AGE2"\n').local_prefix, "AGE2"
        )
        for value in ('""', '"age"', '"A-B"', '"1A"', '"A\\nB"', "12", "[]"):
            with self.subTest(value=value):
                self.fails(
                    "orchestrator/config.toml: local.issue_prefix: want an uppercase issue prefix",
                    without_prefix + f"[local]\nissue_prefix = {value}\n",
                )
        self.fails(
            "orchestrator/config.toml: local.issue_prefix: want an uppercase issue prefix",
            without_prefix,
        )
        self.fails(
            "orchestrator/config.toml: local must be a table", "local = 1\n" + without_prefix
        )
        self.fails(
            "orchestrator/config.toml: unknown keys in [local]: typo",
            without_prefix + "[local]\ntypo = 1\n",
        )

    def test_tracker_linear(self):
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.tracker, cfg.board, cfg.work_dir, cfg.linear.team),
            (TrackerKind.LINEAR, None, os.path.realpath(self.ensemble), TEAM),
        )

    def test_linear_config_groups_settings(self):
        cfg = self.load(PIPELINE + f'[linear.project_repos]\n"{P1}" = "acme/x"\n')
        linear = cfg.linear
        self.assertIsInstance(linear, config.LinearSettings)
        self.assertEqual(
            (linear.team, linear.states, linear.harness, linear.task_label_group),
            (TEAM, STATES, HARNESS, TASK_GROUP),
        )
        self.assertEqual(
            (linear.human_members, linear.task_labels, linear.agent_label_group),
            ((), {}, ""),
        )
        self.assertEqual((linear.agent_labels, linear.project_repos), ({}, {P1: "acme/x"}))
        self.assertEqual(
            dict(linear.roles),
            {r: role_app(r) for r in ROLES},
        )
        self.assertIs(config.linear_settings(cfg, "promote"), linear)

    def test_local_board_leaves_linear_manager_unread(self):
        self.assertIsNone(self.load(LOCAL_BOARD + '[linear.manager]\nuser_id = "manager"\n').linear)

    def test_linear_settings_on_local(self):
        cfg = self.load(LOCAL_BOARD)
        with self.assertRaises(config.ConfigError) as cm:
            config.linear_settings(cfg, "prune")
        self.assertEqual(str(cm.exception), 'prune needs [orchestrator] tracker = "linear"')

    def test_config_invariant(self):
        linear, local = self.load(PIPELINE), self.load(LOCAL_BOARD)
        for cfg, settings in ((linear, None), (local, linear.linear)):
            with self.subTest(cfg.tracker), self.assertRaises(ValueError):
                dataclasses.replace(cfg, linear=settings)

    def test_tracker_rejects_other(self):
        for value, shown in (('"jira"', "'jira'"), ('"Linear"', "'Linear'"), ("5", "5")):
            with self.subTest(value):
                e = self.fails(
                    f"orchestrator/config.toml: tracker: want one of linear, local: {shown}",
                    f"tracker = {value}\n" + PIPELINE.replace('tracker = "linear"\n', ""),
                )
                self.assertIsNone(e.logs_dir)

    def test_local_config_has_no_linear_settings(self):
        cfg = self.load(
            LOCAL_BOARD
            + '[linear]\nteam = "not-a-uuid"\nharness = { client_id = "a b", user_id = "x" }\n'
        )
        self.assertIsNone(cfg.linear)
        self.assertEqual(list(cfg.roles), ["researcher", "pm", "engineer"])
        self.fails(
            "orchestrator/config.toml: unknown keys in [linear]: bogus",
            LOCAL_BOARD + "[linear]\nbogus = 1\n",
        )

    def test_board_from_env(self):
        real = os.path.join(self.home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.home, "link"))
        for value, want in (
            (os.path.join(self.home, "link"), real),
            ("~/boards/two", os.path.join(os.path.realpath(self.home), "boards", "two")),
        ):
            with self.subTest(value), mock.patch.dict(os.environ, {config.BOARD_ENV: value}):
                self.assertEqual(self.load(LOCAL_BOARD).board, want)

    def test_board_default(self):
        want = os.path.join(os.path.realpath(self.ensemble), "board")
        with mock.patch.dict(os.environ, {config.BOARD_ENV: ""}):
            self.assertEqual(self.load(LOCAL_BOARD).board, want)
        os.environ.pop(config.BOARD_ENV)
        self.assertEqual(self.load(LOCAL_BOARD).board, want)

    def test_board_rejects_relative(self):
        for value in ("boards/one", "/boards/\none"):
            with self.subTest(value), mock.patch.dict(os.environ, {config.BOARD_ENV: value}):
                e = self.fails(
                    f"ENSEMBLE_BOARD_DIR must be a printable absolute or ~ path: {value!r}",
                    LOCAL_BOARD,
                )
                self.assertIsNone(e.logs_dir)
        with mock.patch.dict(os.environ, {config.BOARD_ENV: "boards/one"}):
            self.assertEqual(self.load(PIPELINE).board, None)

    def test_local_state_under_board(self):
        state = os.path.join(self.board, ".state")
        cfg = self.load('work_dir = "~/elsewhere"\n' + LOCAL_BOARD)
        self.assertEqual(
            (cfg.work_dir, cfg.runs_log, cfg.logs_dir, cfg.issue_workdir("LOC-1")),
            (state, f"{state}/logs/runs.jsonl", f"{state}/logs", f"{state}/work/LOC-1"),
        )
        with mock.patch.dict(os.environ, {config.BOARD_ENV: self.board + "-two"}):
            self.assertEqual(self.load(LOCAL_BOARD).work_dir, self.board + "-two/.state")
        e = self.fails(
            "orchestrator/config.toml: [roles.ghost] names no stage of the workflow",
            LOCAL_BOARD + "[roles.ghost]\n",
        )
        self.assertEqual(e.logs_dir, f"{state}/logs")
        self.fails(
            "orchestrator/config.toml: the board's .state must neither lie in nor contain "
            f"local_clones.acme/x {os.path.realpath(self.home)}: {state!r}",
            LOCAL_BOARD + '[local_clones]\n"acme/x" = "~"\n',
        )
        self.assertFalse(os.path.exists(self.board))


class DelegateRegistry(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        CatalogDelegate.calls = []
        CatalogDelegate.trusted = frozenset()

    def catalog(self):
        self.local(
            '[delegates]\ndefault = "catalog"\n[delegates.catalog]\nimplementation = "tests.orchestrator.unit.engine.config_test:CatalogDelegate"\n'
        )

    def test_registered_class_owns_catalog_resolution_and_trust(self):
        self.catalog()
        cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        self.assertEqual((cfg.delegate_kind, cfg.delegate_cls), ("catalog", CatalogDelegate))
        self.assertEqual(
            CatalogDelegate.calls,
            [
                ("roles",),
                ("trusted_dirs",),
                *(
                    ("resolve", name, ({"roles": {"pm": {"tier": 1}}},))
                    for name in ("researcher", "pm", "engineer")
                ),
            ],
        )
        self.assertEqual(
            {name: role.tasks for name, role in cfg.roles.items()},
            {name: tuple(tasks) for name, tasks in CatalogDelegate.ROLES.items()},
        )
        self.assertEqual({role.run.client for role in cfg.roles.values()}, {"catalog"})
        self.assertIn("harness", cfg.delegates)

    def test_registered_trust_prevents_work_dir_loading_project_settings(self):
        self.catalog()
        CatalogDelegate.trusted = frozenset((os.path.realpath(self.home),))
        self.fails(
            f"core: trusted_dirs: {os.path.realpath(self.home)} is or contains work_dir {os.path.realpath(self.ensemble)}",
            PIPELINE,
        )

    def test_a_default_or_registration_that_is_not_a_delegate_class_is_rejected(self):
        for local, message in (
            ('[delegates]\ndefault = "missing"\n', "[delegates] default: want one of harness"),
            (
                '[delegates.harness]\nextra = "no"\n',
                "[delegates.harness] takes only implementation",
            ),
            (
                '[delegates.harness]\nimplementation = "builtins:dict"\n',
                "[delegates.harness]: builtins:dict must be a concrete Delegate subclass",
            ),
        ):
            with self.subTest(message):
                self.local(local)
                self.fails("orchestrator/config.toml: " + message, PIPELINE)

    def test_registration_error_has_the_work_log_directory(self):
        self.local('[delegates.harness]\nimplementation = "ensemble.delegate:Delegate"\n')
        with self.assertRaises(config.ConfigError) as caught:
            self.load(PIPELINE)
        self.assertEqual(
            caught.exception.logs_dir, os.path.join(os.path.realpath(self.ensemble), "logs")
        )
        self.assertIn("concrete Delegate subclass", str(caught.exception))


class DeploymentWritePaths(ConfigFile, Case):
    def test_the_deployment_checkout_or_another_dir_is_a_valid_cwd_and_write_dir(self):
        for directory in (
            REPO,
            os.path.join(REPO, "delegate"),
            os.path.dirname(REPO),
            os.path.join(self.home, "external-work"),
        ):
            with self.subTest(directory=directory):
                cfg = self.load(
                    PIPELINE
                    + f"[core.roles.engineer]\ncwd = {json.dumps(directory)}\n"
                    + f"write = [{json.dumps(directory)}]\n"
                )
                self.assertEqual(
                    (cfg.roles["engineer"].run.cwd, cfg.roles["engineer"].run.write),
                    (directory, [directory]),
                )


class DeploymentDirectories(Case):
    def test_local_clone_equal_containment_and_symlink_boundaries(self):
        root = Path(self.home).resolve()
        clone = root / "clone"
        clone.mkdir()
        alias = root / "alias"
        alias.symlink_to(clone, target_is_directory=True)
        for work in (clone, clone / "missing", clone.parent, alias):
            with (
                self.subTest(work=work),
                self.assertRaisesRegex(config.ConfigError, "local_clones.acme/x"),
            ):
                config._work_dir({"work_dir": str(work)}, {"acme/x": str(clone)})
        sibling = root / "sibling" / "missing"
        self.assertEqual(
            config._work_dir({"work_dir": str(sibling)}, {"acme/x": str(clone)}), str(sibling)
        )
        self.assertFalse(sibling.exists())


class ExecutablePath(Case):
    def path(self, home, env):
        """config.path() with HOME `home` and ENSEMBLE_PATH as in `env` (unset when absent)."""
        rest = {k: v for k, v in os.environ.items() if k != "ENSEMBLE_PATH"}
        with mock.patch.dict(os.environ, {**rest, "HOME": home, **env}, clear=True):
            return config.path()

    def test_ensemble_path_then_this_installations_bin_then_the_default(self):
        installed = os.path.join(REPO, "bin")
        self.assertTrue(os.path.isfile(os.path.join(installed, "ensemble-orchestrator")))
        for home in (self.home, os.path.join(self.home, "other")):
            default = (
                f"{installed}:/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
            )
            for env, front in (
                ({"ENSEMBLE_PATH": "/fakes/bin:/more"}, "/fakes/bin:/more:"),
                ({}, ""),
                ({"ENSEMBLE_PATH": ""}, ""),
            ):
                with self.subTest(home=home, env=env):
                    self.assertEqual(self.path(home, env), front + default)


class ShellExecution(Case):
    def test_configured_default_github_commands_override_inherited_host(self):
        with (
            mock.patch.dict(os.environ, {"GH_HOST": "ghe.example.com"}),
            mock.patch("subprocess.run", return_value="result") as run,
        ):
            config.shell(["gh", "api", "repos/acme/app"], 5)
        self.assertEqual(run.call_args.kwargs["env"]["GH_HOST"], "github.com")

    def test_clone_default_shell_uses_configured_path_and_inherited_environment(self):
        clone = os.path.join(self.home, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        result = CompletedProcess([], 0, "https://github.com/acme/ensemble\n", "")
        for front in ("", "/tools/bin"):
            with (
                self.subTest(front=front),
                mock.patch("subprocess.run", return_value=result) as run,
                mock.patch.dict(os.environ, {"ENSEMBLE_PATH": front, "KEEP": "1"}),
            ):
                self.assertIsNone(config.clone_error(clone, SLUG))
                self.assertEqual(run.call_args.kwargs["env"], {**os.environ, "PATH": config.path()})
                self.assertEqual(run.call_args.kwargs["timeout"], utils.SHORT)

    def test_configured_environment_preserves_inherited_values(self):
        with (
            mock.patch("subprocess.run", return_value="res") as run,
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}),
        ):
            env = {**os.environ, "PATH": config.path()}
            self.assertEqual(config.shell(["git", "status"], 5), "res")
        run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=DEVNULL,
            env=env,
        )
        self.assertEqual(env["KEEP"], "1")


class TuiSessionNames(Case):
    def test_prefix_of_a_role_and_issue(self):
        for role_name in ("engineer", "dummy-tester", "pm2"):
            with self.subTest(role=role_name):
                self.assertEqual(config.tui_prefix(role_name, ID), f"{role_name}-{ID}-")

    def test_a_prefix_outside_the_contract(self):
        for role_name, ident in (
            ("Engineer", ID),
            ("eng_x", ID),
            ("", ID),
            ("2pm", ID),
            ("engineer", "task-12"),
            ("engineer", "TASK-12 "),
        ):
            with (
                self.subTest(role=role_name, ident=ident),
                self.assertRaisesRegex(ValueError, "TUI session prefix"),
            ):
                config.tui_prefix(role_name, ident)
        with self.assertRaises(ValueError) as cm:
            config.tui_prefix("Engineer", ID)
        self.assertEqual(
            str(cm.exception),
            r"TUI session prefix 'Engineer-TASK-12': want ([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)",
        )

    def test_the_name_is_the_prefix_and_the_sid_head(self):
        self.assertEqual(config.tui_name("engineer", ID, SID), A)


class CredentialDeclarations(ConfigFile, Case):
    def test_task_replaces_whole_kinds_and_keeps_unrelated_role_references(self):
        cfg = self.load(
            PIPELINE
            + 'credentials = { github = { app = "role-app", permissions = { contents = "write" } }, env = [{ item = "ref", file = "key" }] }\n'
            + '[roles.engineer.tasks.light-build.credentials]\ngithub = { app = "task-app" }\n'
        )
        expected = {"github": {"app": "task-app"}, "env": [{"item": "ref", "file": "key"}]}
        self.assertEqual(config.credentials(cfg, "engineer", "light-build"), expected)
        self.assertEqual(
            config.credentials(cfg, "engineer", None)["github"],
            {"app": "role-app", "permissions": {"contents": "write"}},
        )
        self.assertEqual(
            config.credentials(cfg, "engineer", "build"),
            config.credentials(cfg, "engineer", None),
        )
        resolved = config.credentials(cfg, "engineer", "light-build")
        resolved["env"][0]["file"] = "changed"
        self.assertEqual(config.credentials(cfg, "engineer", "light-build"), expected)
        with self.assertRaises(TypeError):
            cfg.roles["engineer"].credentials["github"]["app"] = "changed"
        with self.assertRaises(TypeError):
            cfg.roles["engineer"].task_credentials["light-build"]["github"] = {}

    def test_absent_declarations_are_empty_and_local_declarations_overlay_packaged(self):
        self.assertEqual(config.credentials(self.load(PIPELINE), "engineer", None), {})
        self.local('[roles.engineer.tasks.light-build.credentials]\nfuture = ["opaque"]\n')
        cfg = self.load(PIPELINE + "credentials = { future = false }\n")
        self.assertEqual(config.credentials(cfg, "engineer", "light-build"), {"future": ["opaque"]})
        self.assertEqual(config.credentials(cfg, "engineer", None), {"future": False})

    def test_linear_role_expands_only_the_runs_app_and_allowlisted_board_keys(self):
        self.local('linear.human_members = ["user@example.com"]\n')
        cfg = self.load(MANAGER_LINE + PIPELINE + '[roles.engineer.credentials]\nlinear = "role"\n')
        app = role_app("engineer")
        expected = {
            "app": {
                "role": "engineer",
                **{k.value: getattr(app, k) for k in config.AppKey},
            },
            "board": {
                "team": TEAM,
                "states": STATES,
                "labels": {"task_labels": {}, "agent_labels": {}},
                "project_repos": {},
                "human_members": ["user@example.com"],
            },
        }
        self.assertEqual(config.credentials(cfg, "engineer", None), {"linear": expected})
        cfg = self.load(PIPELINE + '[roles.pm.tasks.product-design.credentials]\nlinear = "role"\n')
        self.assertEqual(
            config.credentials(cfg, "pm", "product-design")["linear"]["app"]["role"], "pm"
        )

    def test_broker_owns_kind_names_and_explicit_reference_values(self):
        values = 'future = [1, false]\ngithub = "invalid-for-broker"\nlinear = { app = "opaque" }\n'
        cfg = self.load(PIPELINE + "[roles.engineer.credentials]\n" + values)
        self.assertEqual(
            config.credentials(cfg, "engineer", None),
            {"future": [1, False], "github": "invalid-for-broker", "linear": {"app": "opaque"}},
        )

    def test_declaration_shapes_and_task_names_name_the_bad_table(self):
        cases = (
            ("credentials = []\n", "roles.engineer.credentials"),
            ("tasks = []\n", "roles.engineer.tasks"),
            ('tasks = { build = "bad" }\n', "roles.engineer.tasks.build"),
            (
                "[roles.engineer.tasks.build]\ncredentials = false\n",
                "roles.engineer.tasks.build.credentials",
            ),
            ("[roles.engineer.tasks.build]\nextra = {}\n", "roles.engineer.tasks.build"),
            ("[roles.engineer.tasks.made-up.credentials]\n", "roles.engineer.tasks.made-up"),
            ("[roles.engineer.tasks.research.credentials]\n", "roles.engineer.tasks.research"),
            ('[roles.engineer.credentials]\nlinear = "other"\n', "roles.engineer.credentials"),
            (
                '[roles.engineer.tasks.build.credentials]\nlinear = "other"\n',
                "roles.engineer.tasks.build.credentials",
            ),
        )
        for text, where in cases:
            with self.subTest(text=text), self.assertRaises(config.ConfigError) as cm:
                self.load(PIPELINE + text)
            self.assertIn(f"[{where}]", str(cm.exception))

    def test_linear_shorthand_needs_a_linear_board_but_explicit_values_are_opaque(self):
        self.local('tracker = "local"\n')
        with self.assertRaisesRegex(config.ConfigError, "roles.engineer.credentials.*linear"):
            self.load(PIPELINE + '[roles.engineer.credentials]\nlinear = "role"\n')
        cfg = self.load(PIPELINE + "[roles.engineer.credentials]\nlinear = { future = true }\n")
        self.assertEqual(config.credentials(cfg, "engineer", None), {"linear": {"future": True}})

    def test_core_credentials_are_rejected_even_when_the_delegate_accepts_any_overlay(self):
        self.local(
            '[delegates]\ndefault = "catalog"\n'
            '[delegates.catalog]\nimplementation = "tests.orchestrator.unit.engine.config_test:CatalogDelegate"\n'
        )
        for where in ("core", "core.roles.engineer"):
            with self.subTest(where=where), self.assertRaises(config.ConfigError) as cm:
                self.load(PIPELINE + f"[{where}]\ncredentials = {{}}\n")
            self.assertIn("credentials", str(cm.exception))
            self.assertIn("overlay" + where.removeprefix("core"), str(cm.exception))


class RunnableRoles(ConfigFile, Case):
    def runs(self, text=PIPELINE):
        return self.load(text).roles

    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        self.assertEqual(fields(self.load(PIPELINE)), ROLES)
        roles = self.runs()
        self.assertEqual(list(roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            {r: x.tasks for r, x in roles.items()},
            {r: tuple(HARNESS_DELEGATE.roles()[r]) for r in roles},
        )
        # the workflow's order, whatever the tables'
        roles = self.runs(
            HEADER + role("engineer") + role("pm") + role("researcher", "max_runs = 3")
        )
        self.assertEqual(
            [(r, x.max_runs) for r, x in roles.items()],
            [("researcher", 3), ("pm", 1), ("engineer", 1)],
        )
        text = PIPELINE + f'[linear.task_labels]\nlight-research = "{LABEL1}"\nbuild = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])

    def test_a_stage_without_role_table_runs_one(self):
        apps = "".join(role(r).split(f"[roles.{r}]")[0] for r in ("researcher", "pm", "engineer"))
        roles = self.runs(HEADER + apps + "[roles.pm]\nmax_runs = 2\n")
        self.assertEqual(
            {r: x.max_runs for r, x in roles.items()}, {"researcher": 1, "pm": 2, "engineer": 1}
        )


class ConfigurationRoot(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.ensemble, "config.local.toml"))
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "orchestrator")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.root, "delegate"))
        self.core(CORE_TOML)

    def core(self, text):
        write(os.path.join(self.ensemble, "config.local.toml"), local_section(text, "delegate"))

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(path=path)

    def test_work_dir_may_equal_lie_in_contain_or_link_to_the_checkout(self):
        root = os.path.realpath(self.root)
        link = os.path.join(self.home, "deployment-link")
        os.symlink(root, link)
        packages = tuple(
            os.path.join(REPO, "src", "ensemble", project.replace("-", "_"))
            for project in ("orchestrator", "delegate", "session", "utils")
        )
        for work in (
            root,
            os.path.join(root, "state", "missing"),
            os.path.dirname(root),
            link,
            REPO,
            *packages,
            os.path.join(packages[0], "missing"),
        ):
            with self.subTest(work=work):
                self.assertEqual(config._work_dir({"work_dir": work}), os.path.realpath(work))

    def test_explicit_paths_preserve_nested_overlay_values_and_local_precedence(self):
        local = os.path.join(self.ensemble, "config.local.toml")
        write(
            local,
            local_section(
                CORE_TOML.replace("[roles.engineer]\n", "[roles.engineer]\ntier = 1\n"), "delegate"
            ),
        )
        cfg = self.load(
            PIPELINE + '[core.roles.engineer]\ntier = 3\ncommands = ["echo unchanged"]\n'
        )
        self.assertEqual(
            (cfg.roles["engineer"].run.tier, cfg.roles["engineer"].run.commands),
            (3, ["echo unchanged"]),
        )

    def test_tasks_are_the_core_configs_index(self):
        with self.charter(
            "# Researcher\n\n## Tasks\n\n"
            "unsure → `deep-research` (`<tasks>/deep-research.md`)\n\n"
            "- `deep-research` (`<tasks>/deep-research.md`): research.\n"
        ):
            self.assertEqual(self.load(PIPELINE).roles["researcher"].tasks, ("deep-research",))

    def charter(self, text):
        path = os.path.join(REPO, "src", "ensemble", "delegate", "team", "roles", "researcher.md")
        opened = open

        def read(file, *args, **kwargs):
            return io.StringIO(text) if file == path else opened(file, *args, **kwargs)

        return mock.patch.object(builtins, "open", side_effect=read)

    def test_reads_only_the_paths_passed_the_projects_config_files_and_the_workflow(self):
        write(os.path.join(self.ensemble, "config.local.toml"), "[[[ not toml")
        path = os.path.join(self.dir, "config.toml")
        local = os.path.join(self.ensemble, "config.local.toml")
        write(path, PIPELINE)
        write(
            local,
            local_section('language = "French"\n' + CORE_TOML, "delegate")
            + local_section(role("engineer", "max_runs = 2"), "orchestrator"),
        )
        with (
            mock.patch.object(builtins, "open", side_effect=open) as opened,
            mock.patch.object(io, "open", side_effect=io.open) as resource_opened,
        ):
            cfg = config.load(path=path)
        projects = {
            path
            for root in ROOTS
            for package in ("orchestrator", "delegate", "session", "utils")
            for path in (os.path.join(root, "ensemble", package, "config.toml"),)
        }
        self.assertEqual(
            {
                str(c.args[0])
                for c in [*opened.call_args_list, *resource_opened.call_args_list]
                if str(c.args[0]).endswith(".toml")
            }
            - projects,
            {
                path,
                local,
                str(files("ensemble.orchestrator.engine.flow").joinpath("workflow.toml")),
            },
        )
        engineer = cfg.roles["engineer"]
        self.assertEqual((engineer.max_runs, engineer.run.language), (2, "French"))

    def test_an_index_error_stops_the_caller(self):
        with self.charter(
            "# Researcher\n\n## Tasks\n\n"
            "unsure → `ghost` (`<tasks>/ghost.md`)\n\n"
            "- `ghost` (`<tasks>/ghost.md`): absent.\n"
        ):
            self.fails("core: roles/researcher.md: lists 'ghost' without tasks/ghost.md", PIPELINE)

    def test_overlay_keeps_its_values_verbatim(self):
        cfg = self.load(
            PIPELINE
            + '[core]\ntier = 3\ncommands = ["env PYTHONPATH={{pythonpath}} python3 -P", "true"]\n'
            + '[core.roles.pm]\nlanguage = "French"\n'
        )
        run = cfg.roles["researcher"].run
        self.assertEqual(
            (run.tier, run.commands, run.gate),
            (3, ["env PYTHONPATH={{pythonpath}} python3 -P", "true"], ""),
        )
        self.assertEqual((cfg.roles["pm"].run.gate, cfg.roles["pm"].run.language), ("", "French"))

    def test_a_trusted_dir_holding_the_work_dir_stops_the_caller(self):
        work = os.path.join(os.path.realpath(os.path.dirname(self.root)), "state", "ensemble")
        os.makedirs(work)
        text = f'work_dir = "{work}"\n' + PIPELINE
        for entry in (work, os.path.dirname(work)):
            with self.subTest(entry):
                self.core(f'trusted_dirs = ["{entry}"]\n' + CORE_TOML)
                self.fails(f"core: trusted_dirs: {entry} is or contains work_dir {work}", text)
        self.core(f'trusted_dirs = ["{os.path.join(work, "x")}", "{self.dir}/other"]\n' + CORE_TOML)
        self.assertEqual(self.load(text).work_dir, work)

    def test_no_core_table(self):
        self.assertEqual(self.load(PIPELINE).roles["researcher"].run.gate, "")

    def test_unknown_overlay_keys_roles_and_output_keys(self):
        unknown = "core: run overlay: unknown key "
        cases = [
            ("[core]\nfoo = 1\n", unknown + "'foo' in [overlay]"),
            ('[core]\ngate = "x"\n', unknown + "'gate' in [overlay]"),
            (
                '[core.roles.researcher]\ngate = "x"\n',
                unknown + "'gate' in [overlay.roles.researcher]",
            ),
            ('[core]\nusers = ["octocat"]\n', unknown + "'users' in [overlay]"),
            ('[core]\ntrusted_dirs = ["~/x"]\n', unknown + "'trusted_dirs' in [overlay]"),
            ("[core]\nworkers_per_column = 2\n", unknown + "'workers_per_column' in [overlay]"),
            ('[core]\ngrid_retile = "off"\n', unknown + "'grid_retile' in [overlay]"),
            ('[core]\nteam_dirs = ["~/.ensemble/team"]\n', unknown + "'team_dirs' in [overlay]"),
            ("[core.clients.claude]\nflags = []\n", unknown + "'clients' in [overlay]"),
            ("[core.roles.researcher]\nfoo = 1\n", unknown + "'foo' in [overlay.roles.researcher]"),
        ]
        cases += [
            (
                '[core.roles.researcher]\ndefault_task = "light-research"\nfoo = 1\n',
                old_key("default_task"),
            ),
            ('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n', old_key("tasks")),
            (
                "[core.roles.reseacher]\ntier = 2\n",
                "core: run overlay: unknown role 'reseacher' in [overlay.roles]",
            ),
            ('[core.output]\ntype = "local"\ntypo = 1\n', unknown + "'typo' in [overlay.output]"),
            (
                '[core.roles.researcher.output]\ntype = "local"\ntypo = 1\n',
                unknown + "'typo' in [overlay.roles.researcher.output]",
            ),
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails(message, PIPELINE + text)

    def test_a_core_accounts_table_names_the_session_key(self):
        moved = "; moved to [session.clients.<client>] account"
        for text, message in (
            ('[core.accounts]\nclaude = "w"\n', f"unknown key 'accounts' in [overlay]{moved}"),
            (
                '[core.roles.researcher.accounts]\nclaude = "w"\n',
                f"unknown key 'accounts' in [overlay.roles.researcher]{moved}",
            ),
        ):
            with self.subTest(message):
                self.fails(f"core: run overlay: {message}", PIPELINE + text)

    def test_core_errors(self):
        cases = [
            (
                'trusted_dirs = ["rel/dir"]\n' + CORE_TOML,
                PIPELINE,
                "core: trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path",
            ),
            (
                CORE_TOML,
                PIPELINE + '[core]\ncwd = "rel"\n',
                "core: cwd must be a printable absolute or ~ path, got 'rel'",
            ),
            (
                CORE_TOML.replace("[roles.engineer]\n", "[roles.engineer]\ntier = 9\n"),
                PIPELINE,
                "core: tier must be an integer 1–4, got 9",
            ),
            (
                CORE_TOML,
                PIPELINE + '[core.roles.researcher]\ngate = "echo `id`"\n',
                "core: run overlay: unknown key 'gate' in [overlay.roles.researcher]",
            ),
        ]
        for core, text, message in cases:
            with self.subTest(message):
                self.core(core)
                self.fails(message, text)

    def test_verifier_accepts_each_client_in_the_core_overlay(self):
        for client in ("codex", "opencode", "claude"):
            with self.subTest(client=client):
                cfg = self.load(PIPELINE + f'[core.roles.verifier]\nclient = "{client}"\n')
                run = cfg.delegate_cls.resolve("verifier", overlays=(cfg.core,))
                self.assertEqual(run.client, client)

    def test_docs(self):
        self.assertEqual(self.load(PIPELINE).docs, config.Docs("acme/notes", "trunk", DIRS))
        self.assertEqual(
            self.load(HEADER + PM_ENGINEER + role("pm") + role("engineer")).docs,
            config.Docs("acme/notes", "trunk", {"pm": "Designs/"}),
        )
        self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", ', host = "github.com" }')))
        self.assertEqual(self.load(PIPELINE).docs, config.Docs("acme/notes", "trunk", DIRS))

    def test_docs_mismatch(self):
        message = (
            "core: GitHub document roles must publish to one host, repo and branch (their [output] in "
            "~/.ensemble/config.local.toml)"
        )
        for design in (
            DESIGN.replace("acme/notes", "acme/other"),
            DESIGN.replace('"trunk"', '"main"'),
            DESIGN.replace(" }", ', host = "ghe.example.com" }'),
        ):
            with self.subTest(design):
                self.core(CORE_TOML.replace(DESIGN, design))
                self.fails(message, PIPELINE)
        self.core(CORE_TOML)
        self.assertEqual(self.load(HEADER + ENGINEER_ONLY + role("engineer")).docs.dirs, {})

    def test_enterprise_document_and_build_hosts(self):
        self.core(
            CORE_TOML.replace('type = "github",', 'type = "github", host = "ghe.example.com",')
        )
        cfg = self.load(PIPELINE)
        self.assertEqual(cfg.docs.host, "ghe.example.com")
        self.assertEqual(cfg.repo_host("engineer"), "ghe.example.com")
        cfg = self.load(
            PIPELINE
            + '[core.roles.engineer.output]\ntype = "pull-request"\nhost = "build.example.com"\n'
        )
        self.assertEqual(cfg.repo_host("engineer"), "build.example.com")

    def test_local_document_roles_and_mixed_destinations(self):
        self.core(CORE_TOML.replace('type = "github"', 'type = "local"'))
        cfg = self.load(PIPELINE)
        self.assertEqual((cfg.docs.repo, cfg.docs.branch), ("", ""))
        self.assertEqual(cfg.docs.dirs["pm"], os.path.join(cfg.work_dir, "docs", "pm"))
        self.assertEqual(
            cfg.docs.local_out("pm", "TASK-1", SID),
            os.path.join(cfg.docs.dirs["pm"], f"document-TASK-1-{SID}.md"),
        )
        self.assertEqual(cfg.repo_host("engineer"), "github.com")
        self.core(CORE_TOML.replace(DESIGN, 'output = { type = "local" }'))
        cfg = self.load(PIPELINE)
        self.assertEqual(cfg.docs.dirs["researcher"], "Research/")
        self.assertTrue(os.path.isabs(cfg.docs.dirs["pm"]))
        self.assertIsNone(cfg.docs.local_out("researcher", "TASK-1", SID))

    def test_document_destination_must_be_supported(self):
        self.core(CORE_TOML.replace(DESIGN, 'output = { type = "pull-request" }'))
        with self.assertRaisesRegex(config.ConfigError, "pm.*pull-request"):
            self.load(PIPELINE)
        self.core(CORE_TOML.replace('dir = "Designs/"', 'dir = "/documents"'))
        with self.assertRaisesRegex(config.ConfigError, "github dir must be relative"):
            self.load(PIPELINE)

    def test_enterprise_clone_keys_are_host_specific(self):
        clone = os.path.join(self.home, "enterprise-clone")
        os.makedirs(os.path.join(clone, ".git"))
        remote = CompletedProcess([], 0, "git@ghe.example.com:acme/app.git\n", "")
        with mock.patch("subprocess.run", return_value=remote):
            cfg = self.load(PIPELINE + f'[local_clones]\n"ghe.example.com/acme/app" = "{clone}"\n')
            self.assertEqual(cfg.local_clones["ghe.example.com/acme/app"], clone)
            self.assertIsNone(config.clone_error(clone, "acme/app", host="ghe.example.com"))
            self.assertIn("not github.com/acme/app", config.clone_error(clone, "acme/app"))

    def test_clone_repository_names_ending_in_git_keep_their_identity(self):
        clone = os.path.join(self.home, "suffix-clone")
        os.makedirs(os.path.join(clone, ".git"))
        for key in ("acme/app.git", "github.com/acme/app.git"):
            with self.subTest(key=key):
                text = PIPELINE + f'[local_clones]\n"{key}" = "{clone}"\n'
                remote = CompletedProcess([], 0, "git@github.com:acme/app.git.git\n", "")
                with mock.patch("subprocess.run", return_value=remote):
                    self.assertEqual(self.load(text).local_clones[key], clone)
                wrong = CompletedProcess([], 0, "git@github.com:acme/app.git\n", "")
                with (
                    mock.patch("subprocess.run", return_value=wrong),
                    self.assertRaisesRegex(config.ConfigError, "not github.com/acme/app.git"),
                ):
                    self.load(text)

    def test_bare_clone_keys_infer_enterprise_host_and_reject_public_origin(self):
        self.core(
            CORE_TOML.replace('type = "github",', 'type = "github", host = "ghe.example.com",')
        )
        clone = os.path.join(self.home, "inferred-clone")
        os.makedirs(os.path.join(clone, ".git"))
        text = PIPELINE + f'[local_clones]\n"acme/app" = "{clone}"\n'
        remote = CompletedProcess([], 0, "git@ghe.example.com:acme/app.git\n", "")
        with mock.patch("subprocess.run", return_value=remote):
            self.assertEqual(self.load(text).local_clones["acme/app"], clone)
        wrong = CompletedProcess([], 0, "git@github.com:acme/app.git\n", "")
        with (
            mock.patch("subprocess.run", return_value=wrong),
            self.assertRaisesRegex(config.ConfigError, "not ghe.example.com/acme/app"),
        ):
            self.load(text)

    def test_bare_clone_keys_are_ambiguous_with_multiple_build_hosts(self):
        self.core(
            CORE_TOML.replace(
                'type = "pull-request"', 'type = "pull-request", host = "other.example.com"'
            )
        )
        stages = workflow_line(
            '[[stage]]\nrole = "researcher"\ninput = "research"\n[[stage]]\nrole = "pm"\ninput = "build"\n[[stage]]\nrole = "engineer"\ninput = "build"\n'
        )
        with self.assertRaisesRegex(
            config.ConfigError, "use host/owner/name when build hosts differ"
        ):
            self.load(
                HEADER
                + stages
                + role("researcher")
                + role("pm")
                + role("engineer")
                + '[local_clones]\n"acme/app" = "/clone"\n'
            )

    def test_invalid_host_and_duplicate_qualified_clone_fail(self):
        for host in ("https://ghe.example.com", "bad host", "ghe.example.com/path"):
            with self.subTest(host=host):
                self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", f', host = "{host}" }}')))
                with self.assertRaisesRegex(config.ConfigError, "invalid host"):
                    self.load(PIPELINE)
        self.core(CORE_TOML)
        with self.assertRaisesRegex(config.ConfigError, "same repo"):
            self.load(
                PIPELINE
                + '[local_clones]\n"acme/app" = "/clone"\n"github.com/acme/app" = "/clone2"\n'
            )


# A local table that stays empty is dropped (src/ensemble/utils/config_file.py), so a role
# the replaced config.toml lacks needs a key in its local [roles.<role>].
LOCAL_PIPELINE = PIPELINE.replace(role("engineer"), role("engineer", "max_runs = 1"))


class LocalOverlay(ConfigFile, Case):
    def test_local_on_top(self):
        self.local(
            HEADER
            + 'linear.human_members = ["b@x.com"]\n'
            + role("researcher", "max_runs = 2")
            + role("pm")
            + f'[linear.project_repos]\n"{P2}" = "acme/y"\n'
        )
        cfg = self.load(
            RESEARCHER_PM + 'linear.human_members = ["a@x.com", "c@x.com"]\n'
            "[roles.researcher]\nmax_runs = 3\n[roles.pm]\n"
            f'[linear.project_repos]\n"{P1}" = "acme/x"\n[core.roles.pm]\ntier = 1\n'
        )
        self.assertEqual(
            (cfg.linear.team, cfg.linear.states, cfg.linear.human_members),
            (TEAM, STATES, ("b@x.com",)),
        )
        self.assertEqual(
            fields(cfg),
            {
                "researcher": (
                    role_id("researcher"),
                    "linear-app-researcher-token",
                    ROLES["researcher"][2],
                    2,
                    "pm",
                ),
                "pm": (role_id("pm"), "linear-app-pm-token", ROLES["pm"][2], 1, None),
            },
        )
        self.assertEqual(cfg.linear.project_repos, {P1: "acme/x", P2: "acme/y"})
        self.assertEqual(cfg.roles["pm"].run.tier, 1)

    def test_local_alone_and_no_local(self):
        self.local(LOCAL_PIPELINE)
        self.assertEqual(self.load("").roles, self.load(PIPELINE).roles)
        write(os.path.join(self.ensemble, "config.local.toml"), CORE_LOCAL)
        self.assertEqual(self.load(PIPELINE).linear.team, TEAM)

    def test_an_empty_local_role_table_adds_no_role(self):
        self.local(LOCAL_PIPELINE)
        want = set(self.load("").roles)
        self.local(LOCAL_PIPELINE + "[roles.ghost]\n")
        self.assertEqual(set(self.load("").roles), want)
        self.local(LOCAL_PIPELINE + role("ghost"))
        self.fails(
            "orchestrator/config.toml: [linear.roles.ghost] names no stage of the workflow", ""
        )

    def test_a_bad_value_wrong_type_unknown_or_old_task_local_key_is_rejected_by_name(self):
        for local, text, message in (
            (
                '[core.roles.researcher.tasks.deep-research]\ngate = "x"\n',
                PIPELINE,
                old_key("tasks"),
            ),
            (
                f'linear.team = "{TEAM}"\nlinear.states = {{ done = "Done" }}\n',
                BASE,
                "orchestrator/config.toml: linear.states.done must be a Linear workflow state id (UUID): 'Done'",
            ),
            (
                'linear.harness.token_service = "a b"\n',
                BASE,
                "orchestrator/config.toml: linear.harness.token_service must be a Keychain service name: 'a b'",
            ),
            (
                'linear = "x"\n',
                BASE,
                "orchestrator/config.toml: [linear] must be a table",
            ),
            (
                "linear.typo = 1\n",
                BASE,
                "orchestrator/config.toml: unknown keys in [linear]: typo",
            ),
            (
                "[linear.roles.pm]\ntypo = 1\n",
                BASE,
                "orchestrator/config.toml: [linear.roles.pm] has unknown keys: typo",
            ),
            *[
                (
                    flat,
                    BASE,
                    "orchestrator/config.toml: ~/.ensemble/config.local.toml: unknown key "
                    f"{key!r} in [orchestrator]",
                )
                for key, flat in (
                    ("team", f'team = "{TEAM}"\n'),
                    ("harness", '[harness]\nclient_id = "client-harness"\n'),
                    ("human_members", 'human_members = ["a@x.com"]\n'),
                    ("states", f'[states]\ndone = "{STATES["done"]}"\n'),
                    ("task_labels", f'[task_labels]\nbuild = "{LABEL1}"\n'),
                    ("project_repos", f'[project_repos]\n"{P1}" = "acme/x"\n'),
                )
            ],
            (
                '[roles.researcher]\ntypo = "r"\ntoken = "k"\n',
                BASE,
                "orchestrator/config.toml: [roles.researcher] has unknown keys: token, typo",
            ),
            (
                role("researcher", 'max_runs = "2"'),
                BASE,
                "orchestrator/config.toml: [roles.researcher] max_runs must be a whole number >= 1: '2'",
            ),
            (
                "bogus = 1\n",
                BASE,
                "orchestrator/config.toml: ~/.ensemble/config.local.toml: unknown key 'bogus' in [orchestrator]",
            ),
            (
                role("researcher", "bogus = 1"),
                BASE,
                "orchestrator/config.toml: [roles.researcher] has unknown keys: bogus",
            ),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.fails(message, text)
