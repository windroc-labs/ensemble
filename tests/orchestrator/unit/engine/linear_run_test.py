"""LinearRun owns exact plans and forwards views to the saved Delegate."""

import json
from dataclasses import replace
from unittest import mock

from ensemble.delegate import ConfigError as DelegateConfigError
from ensemble.orchestrator.engine import runs
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.types import ConfigError
from ensemble.utils import OutcomeStatus, Record, StatusState
from tests.delegate.fakes.delegate import FakeDelegate
from tests.orchestrator.linear_run_case import SID, ReadyDelegate, RunCase


class CapturedDelegate(ReadyDelegate):
    def __init__(self, role, spec, **options):
        self.accepted = (role, spec, options)
        super().__init__(role, spec, **options)


class OtherDelegate(FakeDelegate):
    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        raise AssertionError("current default must not attach this run")


class CreatedDelegate(CapturedDelegate):
    def __init__(self, role, spec, **options):
        raise AssertionError("use the public create method")

    @classmethod
    def create(cls, role, spec, *, display=None, callback=None, overlays=()):
        view = cls.__new__(cls)
        CapturedDelegate.__init__(
            view, role, spec, display=display, callback=callback, overlays=overlays
        )
        return view


class CheckedDelegate(FakeDelegate):
    checks: list = []

    @classmethod
    def check_tui(cls, *, display=None, events=None):
        cls.checks.append((display, events))
        if display == "refused":
            raise DelegateConfigError("no place for the TUI")


class Runs(RunCase):
    def test_new_run_receives_resolved_credentials_and_resume_omits_them(self):
        role = self.cfg.roles["researcher"]
        self.cfg = replace(
            self.cfg,
            roles={
                **self.cfg.roles,
                "researcher": replace(
                    role,
                    credentials={"future": {"ref": "role"}, "another": ["kept"]},
                    task_credentials={"light-research": {"future": {"ref": "task"}}},
                ),
            },
        )
        owner, _ = self.start(delegate_cls=CapturedDelegate)
        self.assertEqual(
            json.loads(owner.delegate.accepted[1].credentials),
            {"future": {"ref": "task"}, "another": ["kept"]},
        )
        changed = replace(self.cfg, roles={**self.cfg.roles, "researcher": role})
        owner = LinearRun(
            "TASK-1",
            "Answer with credentials: linear",
            self.cfg.issue_workdir("TASK-1"),
            cfg=changed,
            role="researcher",
            sid=SID,
            resume=True,
            delegate_cls=CapturedDelegate,
        )
        self.assertIsNone(owner.delegate.accepted[1].credentials)

    def test_issue_input_cannot_supply_credentials_and_absence_is_an_empty_spec(self):
        owner = LinearRun(
            "TASK-1",
            'credentials: {"linear": "role"}',
            self.cfg.issue_workdir("TASK-1"),
            cfg=self.cfg,
            role="engineer",
            sid=SID,
            delegate_cls=CapturedDelegate,
        )
        self.assertEqual(json.loads(owner.delegate.accepted[1].credentials), {})

    def test_document_host_is_canonicalized_in_delegate_output_overlay(self):
        for host in ("GHE.EXAMPLE.COM", "GitHub.com"):
            with self.subTest(host=host):
                role = self.cfg.roles["researcher"]
                output = dict(role.run.output) | {"host": host}
                self.cfg = replace(
                    self.cfg,
                    roles=dict(self.cfg.roles)
                    | {"researcher": replace(role, run=replace(role.run, output=output))},
                    docs=replace(self.cfg.docs, host=host.lower()),
                )
                owner, _ = self.start(delegate_cls=CapturedDelegate)
                overlays = owner.delegate.accepted[2]["overlays"]
                normalized = next(
                    o["roles"]["researcher"]["output"]
                    for o in overlays
                    if "output" in o.get("roles", {}).get("researcher", {})
                )
                self.assertEqual(normalized, output | {"host": host.lower()})

    def test_local_document_out_is_stable_and_separate_from_issue_workdir(self):
        local_dir = self.cfg.work_dir + "/docs/researcher"
        cfg = replace(self.cfg, docs=replace(self.cfg.docs, dirs={"researcher": local_dir}))
        self.cfg = cfg
        owner, _ = self.start(delegate_cls=CapturedDelegate)
        out = owner.delegate.accepted[1].out
        self.assertEqual(out, f"{local_dir}/document-TASK-1-{SID}.md")
        with mock.patch.object(owner.delegate, "resume"):
            owner.resume(input="revision")
        self.assertEqual(owner.delegate.accepted[1].out, out)

    def test_enterprise_build_host_fallback_reaches_delegate_overlay(self):
        cfg = replace(self.cfg, docs=replace(self.cfg.docs, host="ghe.example.com"))
        owner = LinearRun(
            "TASK-1",
            "Build",
            cfg.issue_workdir("TASK-1"),
            cfg=cfg,
            role="engineer",
            sid=SID,
            client="fake",
            delegate_cls=CapturedDelegate,
        )
        self.assertEqual(
            owner.delegate.accepted[2]["overlays"][-1],
            {
                "roles": {
                    "engineer": {
                        "output": dict(cfg.roles["engineer"].run.output)
                        | {"host": "ghe.example.com"}
                    }
                }
            },
        )

    def test_detach_sets_outer_once_and_preserves_the_provider_receipt(self):
        owner, record = self.start()
        with mock.patch.object(owner.delegate, "detach", return_value=record) as detach:
            self.assertIs(owner.detach(), record)
        detach.assert_called_once_with(
            outer={
                "kind": "pipeline",
                "name": "TASK-1",
                "ctl": ("ensemble-orchestrator",),
                "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
            }
        )

    def test_resume_preserves_the_saved_provider_receipt(self):
        owner, record = self.start()
        with mock.patch.object(owner.delegate, "resume", return_value=record) as resume:
            self.assertIs(owner.resume(input="answer"), record)
        resume.assert_called_once_with(input="answer")

    def test_a_provider_with_a_private_constructor_is_created_through_its_factory(self):
        owner, record = self.start(delegate_cls=CreatedDelegate)
        self.assertIsInstance(owner.delegate, CreatedDelegate)
        self.assertEqual(owner.delegate.accepted[0], "researcher")
        self.assertEqual((record.sid, record.kind), (SID, "pipeline"))

    def test_content_preserves_the_delegates_status_line(self):
        owner, _ = self.start()
        self.assertEqual(owner.content(), owner.delegate.status().to_line())

    def test_content_preserves_each_current_delegate_response_without_inspecting_liveness(self):
        owner, _ = self.start()
        status = owner.delegate.status()
        responses = [
            "screen\nworking",
            "",
            replace(status, name="Status-shaped agent reply").to_line(),
            replace(status, name="completed-live", outcome=OutcomeStatus.DONE).to_line(),
            replace(status, name="recorded-error", state=StatusState.GONE).to_line(),
            replace(status, name="headless").to_line(),
            "a new turn's screen",
        ]
        with (
            mock.patch.object(owner.delegate, "content", side_effect=responses) as content,
            mock.patch.object(
                owner.delegate, "status", side_effect=AssertionError("content owns it")
            ),
            mock.patch.object(
                owner.delegate, "alive", side_effect=AssertionError("content owns it")
            ),
        ):
            for expected in responses:
                with self.subTest(expected=expected):
                    self.assertEqual(owner.content(), expected)
            self.assertEqual(content.call_count, len(responses))

    def test_constructed_delegate_receives_exact_spec_and_display(self):
        owner, record = self.start(delegate_cls=CapturedDelegate, display="opaque:\nvalue")
        role, spec, options = owner.delegate.accepted
        self.assertEqual(role, "researcher")
        self.assertEqual(spec.input, "Echo input.")
        self.assertEqual(
            (spec.task, spec.client, spec.sid, spec.resume), ("light-research", "fake", SID, False)
        )
        self.assertEqual(spec.name, "researcher-TASK-1-1234abcd")
        self.assertEqual(
            options,
            {
                "display": "opaque:\nvalue",
                "callback": ["ensemble-orchestrator", "writeback", "--issue", "TASK-1", "--workdir"]
                + [spec.workdir],
                "overlays": (
                    self.cfg.core,
                    {
                        "roles": {
                            "researcher": {
                                "gate": "ensemble-orchestrator router --brake researcher"
                            }
                        }
                    },
                ),
            },
        )
        self.assertEqual(record.display, "opaque:\nvalue")
        self.assertEqual((record.name, record.kind), ("TASK-1", "pipeline"))
        self.assertEqual(record.sessions, ("researcher-TASK-1-1234abcd",))
        self.assertEqual(record.ctl, ("ensemble-orchestrator",))
        self.assertEqual(record.resume, ("ensemble-orchestrator", "resume", "TASK-1"))

    def test_a_tier_overlays_its_role_after_core(self):
        self.start()
        for resume in (False, True):
            with self.subTest(resume=resume):
                owner = LinearRun(
                    "TASK-1",
                    "Echo input.",
                    self.cfg.issue_workdir("TASK-1"),
                    cfg=self.cfg,
                    role="researcher",
                    sid=SID,
                    client="fake",
                    resume=resume,
                    tier=3,
                    delegate_cls=CapturedDelegate,
                )
                self.assertEqual(
                    owner.delegate.accepted[2]["overlays"],
                    (
                        self.cfg.core,
                        {"roles": {"researcher": {"tier": 3}}},
                        {
                            "roles": {
                                "researcher": {
                                    "gate": "ensemble-orchestrator router --brake researcher"
                                }
                            }
                        },
                    ),
                )

    def test_only_researcher_receives_the_orchestrators_brake(self):
        for role in ("researcher", "engineer", "pm"):
            with self.subTest(role=role):
                owner = LinearRun(
                    "TASK-1",
                    "input",
                    self.cfg.issue_workdir("TASK-1"),
                    cfg=self.cfg,
                    role=role,
                    sid=SID,
                    delegate_cls=CapturedDelegate,
                )
                self.assertEqual(
                    owner.delegate.accepted[2]["overlays"],
                    (self.cfg.core,)
                    + (
                        (
                            {
                                "roles": {
                                    role: {
                                        "gate": "ensemble-orchestrator router --brake researcher"
                                    }
                                }
                            },
                        )
                        if role == "researcher"
                        else ()
                    ),
                )

    def test_spec_passes_only_the_workdir(self):
        owner, _ = self.start(delegate_cls=CapturedDelegate)
        spec = owner.delegate.accepted[1]
        self.assertEqual(
            (spec.workdir, spec.cwd, spec.out), (self.cfg.issue_workdir("TASK-1"), None, None)
        )

    def test_check_tui_asks_the_configured_delegate_kind_and_passes_on_its_refusal(self):
        cfg = replace(
            self.cfg,
            delegate_kind="checked",
            delegates={**self.cfg.delegates, "checked": CheckedDelegate},
        )
        CheckedDelegate.checks = []
        LinearRun.check_tui(cfg=cfg, display="opaque", events="/e")
        with self.assertRaisesRegex(DelegateConfigError, "no place for the TUI"):
            LinearRun.check_tui(cfg=cfg, display="refused", events="/e")
        self.assertEqual(CheckedDelegate.checks, [("opaque", "/e"), ("refused", "/e")])
        with self.assertRaisesRegex(ConfigError, "no longer configured"):
            LinearRun.check_tui(cfg=replace(cfg, delegate_kind="gone"), display=None, events="/e")

    def test_a_resumed_run_asks_its_delegate_to_resume_the_session(self):
        self.start()
        owner = LinearRun(
            "TASK-1",
            "Answer.",
            self.cfg.issue_workdir("TASK-1"),
            cfg=self.cfg,
            role="researcher",
            sid=SID,
            client="fake",
            resume=True,
            delegate_cls=CapturedDelegate,
        )
        _, spec, _ = owner.delegate.accepted
        self.assertEqual(
            (spec.sid, spec.resume, spec.task, spec.input), (SID, True, None, "Answer.")
        )

    def test_saved_provider_survives_a_changed_default_and_live_resume_is_idempotent(self):
        owner, record = self.start(display="foreign display")
        changed = replace(
            self.cfg,
            delegate_kind="other",
            delegates={**self.cfg.delegates, "other": OtherDelegate},
        )
        view = LinearRun.attach("TASK-1", cfg=changed)
        self.assertTrue(view.delegate.alive())
        self.assertEqual(view.delegate_kind, "fake")
        self.assertEqual(view.status(), replace(owner.delegate.status(), name="TASK-1"))
        self.assertEqual(view.delegate.session_status(), owner.delegate.session_status())
        self.assertEqual(Record.parse(view.resume().to_line()), record)
        view.delegate.close()
        self.assertFalse(owner.delegate.alive())

    def test_completed_resume_keeps_record_and_interrupted_resume_starts_once(self):
        owner, record = self.start()
        FakeDelegate.finish(self.cfg.issue_workdir("TASK-1"))
        view = LinearRun.attach("TASK-1", cfg=self.cfg)
        self.assertEqual(view.delegate.outcome(), FakeDelegate.outcome_result)
        self.assertEqual(view.resume(), record)
        self.assertFalse(view.delegate.alive())
        FakeDelegate.end(self.cfg.issue_workdir("TASK-1"))
        self.assertEqual(view.resume(), record)
        self.assertEqual(view.resume(), record)
        self.assertTrue(owner.delegate.alive())

    def test_missing_record_provider_or_invalid_identity_refuses_attach(self):
        with self.assertRaisesRegex(ConfigError, "no recorded run"):
            LinearRun.attach("TASK-1", cfg=self.cfg)
        self.start()
        with self.assertRaisesRegex(ConfigError, "no longer configured"):
            LinearRun.attach("TASK-1", cfg=replace(self.cfg, delegates={}))
        for issue, sid in (("../TASK-1", None), ("TASK-1", "bad sid")):
            with self.subTest(issue=issue, sid=sid), self.assertRaises(ConfigError):
                LinearRun.attach(issue, cfg=self.cfg, sid=sid)

    def test_a_newer_failed_launch_leaves_the_issue_no_run_but_its_sid_attaches(self):
        self.start()
        failed = "abcdef12-0000-4000-8000-000000000000"
        runs.note(self.cfg.runs_log, "failed", issue="TASK-1", sid=failed)
        with self.assertRaisesRegex(ConfigError, "TASK-1: its latest launch failed at "):
            LinearRun.attach("TASK-1", cfg=self.cfg)
        self.assertEqual(LinearRun.attach("TASK-1", cfg=self.cfg, sid=SID).sid, SID)

    def test_nonlatest_sid_is_refused_before_attaching_the_wrong_view(self):
        self.start()
        newer = "abcdef12-0000-4000-8000-000000000000"
        self.start(sid=newer)
        with self.assertRaisesRegex(ConfigError, "no longer the latest"):
            LinearRun.attach("TASK-1", cfg=self.cfg, sid=SID)

    def test_a_newer_row_without_its_own_delegate_state_never_views_the_previous_session(self):
        self.start()
        newer = "abcdef12-0000-4000-8000-000000000000"
        runs.note(
            self.cfg.runs_log,
            "start",
            issue="TASK-1",
            sid=newer,
            role="researcher",
            client="fake",
            delegate_kind="fake",
        )
        with self.assertRaisesRegex(DelegateConfigError, f"sid {newer} is not the run's"):
            LinearRun.attach("TASK-1", cfg=self.cfg)

    def test_a_kindless_row_is_a_bad_row_whatever_the_current_default(self):
        owner = LinearRun(
            "TASK-1", "Input", self.cfg.issue_workdir("TASK-1"), cfg=self.cfg, role="dummy", sid=SID
        )
        owner.detach()
        runs.note(self.cfg.runs_log, "start", issue="TASK-1", sid=SID, role="dummy", client="fake")
        changed = replace(
            self.cfg,
            delegate_kind="other",
            delegates={**self.cfg.delegates, "other": OtherDelegate},
        )
        with self.assertRaisesRegex(
            ConfigError, f"session {SID}'s runs.jsonl row has no delegate kind"
        ):
            LinearRun.attach("TASK-1", cfg=changed)
