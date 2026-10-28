"""delegate/engine/ready_gate.py over a fake `gh`: G1–G3, the draft/ready writes and every error path."""

import json
import os
import unittest
import unittest.mock

from ensemble.delegate.engine.ready_gate import gate, hosts
from ensemble.delegate.engine.types import GateStatus
from ensemble.delegate.event.types import Outcome, Updraft, UpdraftStatus
from ensemble.utils import SHORT, OutcomeStatus, one_line
from tests.delegate.case import Case
from tests.delegate.repo import PROCESS_ERRORS, fail, ok
from tests.utils.sandbox import scratch

URL = "https://github.com/o/n/pull/7"
HEAD = "a" * 40
OLDER = "b" * 40
CONVERGED = Updraft(UpdraftStatus.CONVERGED, HEAD)
VIEW = ["gh", "pr", "view", URL, "--json", "state,isDraft,headRefOid,statusCheckRollup"]
READY = ["gh", "pr", "ready", URL]
UNDO = ["gh", "pr", "ready", URL, "--undo"]
JOB_URL = "https://github.com/o/n/actions/runs/100/job/200"
JOB_API = ["gh", "api", "--hostname", "github.com", "repos/o/n/actions/jobs/200"]


def run_(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def context(name, state):
    return {"__typename": "StatusContext", "context": name, "state": state}


def view(checks=(), *, draft=True, state="OPEN", head=HEAD):
    data = {"state": state, "isDraft": draft, "headRefOid": head, "statusCheckRollup": list(checks)}
    return ok(json.dumps(data))


failed, OK = fail, ok()
TIMED_OUT = f"gh pr ready: {one_line(PROCESS_ERRORS[1])}"


class Gh:
    """A fake `run`: answers each argv from `answers` by its first four words (view/ready/undo),
    records calls and timeouts; an exception answer is raised; a list answers views in turn.
    """

    def __init__(self, view_answer=None, ready=OK, undo=OK, api=OK):
        self.answers = {"view": view_answer, "ready": ready, "undo": undo, "api": api}
        self.calls, self.timeouts = [], []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        self.timeouts.append(timeout)
        key = (
            "api"
            if argv[1] == "api"
            else "view"
            if argv[2] == "view"
            else "undo"
            if "--undo" in argv
            else "ready"
        )
        answer = self.answers[key]
        if key == "api" and isinstance(answer, dict):
            answer = answer.get(argv[-1], failed("unexpected API path"))
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def outcome(status=OutcomeStatus.DONE, url=URL, summary="Built X.\nVerify: run Y."):
    return Outcome(status, "T", summary, url=url)


class Base(Case):
    def go(self, gh, updraft=CONVERGED, out=None):
        self.gh = gh
        return gate(out or outcome(), updraft, run=gh)

    def assert_failed(self, gated, reason):
        self.assertEqual(gated.status, GateStatus.FAILED)
        self.assertEqual(gated.outcome.status, OutcomeStatus.FAILED)
        self.assertEqual(gated.outcome.url, URL)
        last = gated.outcome.summary.splitlines()[-1]
        self.assertTrue(last.startswith("ready gate: "), gated.outcome.summary)
        self.assertIn(reason, last)
        self.assertNotIn(READY, self.gh.calls)

    def assert_safe_calls(self):
        """NFR-1/NFR-3: only view and ready [--undo], each on the full PR URL with SHORT."""
        for argv in self.gh.calls:
            self.assertIn(argv, (VIEW, READY, UNDO))
        self.assertEqual(set(self.gh.timeouts), {SHORT} if self.gh.calls else set())


class Done(Base):
    def test_the_prds_cases_only_no_checks_and_all_passing_pass(self):
        cases = (
            ("no updraft", None, view(), "G1: no updraft result"),
            ("capped", Updraft(UpdraftStatus.CAPPED, HEAD), view(), "G1: updraft capped-without-pass"),
            ("ancestor head", Updraft(UpdraftStatus.CONVERGED, OLDER), view(), "G2: updraft head bbbbbbbbbbbb is not PR head aaaaaaaaaaaa"),
            ("pending", CONVERGED, view([run_("lint"), run_("unit", None, "IN_PROGRESS")]), "G3: check unit IN_PROGRESS"),
            ("failure", CONVERGED, view([run_("lint"), run_("unit", "FAILURE")]), "G3: check unit FAILURE"),
            ("all skipped", CONVERGED, view([run_("a", "SKIPPED"), run_("b", "SKIPPED")]), "G3: CI did not run (CI 未运行): all 2 checks skipped"),
        )  # fmt: skip
        for why, updraft, answer, reason in cases:
            with self.subTest(why):
                gated = self.go(Gh(answer), updraft)
                self.assert_failed(gated, reason)
                self.assertEqual(self.gh.calls, [VIEW])
                self.assert_safe_calls()
        for why, checks in (
            ("no checks", []),
            ("all passing", [run_("a"), run_("b", "NEUTRAL"), run_("c", "SKIPPED"), context("d", "SUCCESS")]),
        ):  # fmt: skip
            with self.subTest(why):
                gated = self.go(Gh(view(checks)))
                self.assertEqual((gated.status, gated.reason), (GateStatus.PASSED, ""))
                self.assertEqual(gated.outcome, outcome())
                self.assertEqual(self.gh.calls, [VIEW, READY])
                self.assert_safe_calls()

    def test_a_null_rollup_is_no_checks(self):
        answer = view()
        answer.stdout = answer.stdout.replace("[]", "null")
        self.assertEqual(self.go(Gh(answer)).status, GateStatus.PASSED)

    def test_a_ready_pr_that_passes_gets_no_ready_call(self):
        gated = self.go(Gh(view(draft=False)))
        self.assertEqual((gated.status, self.gh.calls), (GateStatus.PASSED, [VIEW]))

    def test_g1_g2_g3_are_judged_in_order(self):
        capped = Updraft(UpdraftStatus.STOPPED, OLDER)
        self.assert_failed(self.go(Gh(view([run_("u", "FAILURE")], state="MERGED")), capped), "G1:")
        self.assert_failed(
            self.go(Gh(view([run_("u", "FAILURE")], state="CLOSED", head=OLDER))),
            "G2: PR is CLOSED",
        )
        self.assert_failed(
            self.go(Gh(view([run_("u", "FAILURE")], head=OLDER))), "G2: updraft head"
        )

    def test_a_closed_or_merged_pr_is_never_written(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                gated = self.go(Gh(view(draft=False, state=state)))
                self.assert_failed(gated, f"G2: PR is {state}")
                self.assertEqual(self.gh.calls, [VIEW])

    def test_check_kinds_and_unknown_states(self):
        for check, reason in (
            (context("ci/x", "PENDING"), "G3: check ci/x PENDING"),
            (context("ci/x", "EXPECTED"), "G3: check ci/x EXPECTED"),
            (context("ci/x", "ERROR"), "G3: check ci/x ERROR"),
            (run_("u", "CANCELLED"), "G3: check u CANCELLED"),
            (run_("u", "TIMED_OUT"), "G3: check u TIMED_OUT"),
            (run_("u", "ACTION_REQUIRED"), "G3: check u ACTION_REQUIRED"),
            (run_("u", "STALE"), "G3: check u STALE"),
            (run_("u", None, "QUEUED"), "G3: check u QUEUED"),
            ({"__typename": "Other", "name": "z"}, "G3: check z"),
            ("junk", "G3: check ?"),
        ):
            with self.subTest(check=check):
                self.assert_failed(self.go(Gh(view([run_("a"), check]))), reason)

    def test_the_first_bad_check_in_rollup_order_is_the_reason(self):
        checks = [run_("a", "SKIPPED"), run_("b", None, "QUEUED"), run_("c", "FAILURE")]
        self.assert_failed(self.go(Gh(view(checks))), "G3: check b QUEUED")

    def test_a_failure_drafts_a_ready_pr(self):
        gated = self.go(Gh(view([run_("unit", "FAILURE")], draft=False)))
        self.assert_failed(gated, "G3: check unit FAILURE")
        self.assertEqual(self.gh.calls, [VIEW, UNDO])
        self.assert_safe_calls()

    def test_the_gate_line_ends_a_summary_capped_at_the_schemas_length(self):
        for summary in ("", "short", "x" * 300, "y" * 290 + "\n" + "z" * 9):
            with self.subTest(length=len(summary)):
                gated = self.go(Gh(view([run_("unit", "FAILURE")])), out=outcome(summary=summary))
                text = gated.outcome.summary
                self.assertLessEqual(len(text), 300)
                self.assertTrue(
                    text.endswith("\nready gate: G3: check unit FAILURE") or not summary
                )
                self.assertEqual(text.splitlines()[-1], "ready gate: G3: check unit FAILURE")
                self.assertTrue(summary.startswith(text.rpartition("\n")[0]))

    def test_reasons_are_one_printable_line(self):
        gated = self.go(Gh(view([run_("x\n\x1b[2Jevil", "FAILURE")])))
        last = gated.outcome.summary.splitlines()[-1]
        self.assertTrue(last.isprintable(), last)
        self.assertIn("G3: check x [2Jevil FAILURE", last)
        self.assertEqual(gated.reason, last.removeprefix("ready gate: "))
        long = self.go(Gh(view([run_("n" * 1000, "FAILURE")])), out=outcome(summary="x" * 300))
        self.assertEqual(long.outcome.summary, f"ready gate: {long.reason}")
        self.assertLessEqual(len(long.outcome.summary), 300)


class Cancelled(Base):
    def check(self, **changes):
        return {**run_("arbitrary CI leg", "CANCELLED"), "detailsUrl": JOB_URL, **changes}

    def job(self, **changes):
        return {
            "id": 200,
            "run_id": 100,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "cancelled",
            "runner_id": 0,
            "runner_name": "",
            "steps": [],
            "started_at": "2026-10-26T00:00:00Z",
            **changes,
        }

    def go_job(self, job, *, checks=None, api=None, draft=True):
        checks = [run_("passing"), self.check()] if checks is None else checks
        return self.go(Gh(view(checks, draft=draft), api=api or ok(json.dumps(job))))

    def test_cancelled_before_start_readies_with_other_checks_passing(self):
        for changes in ({}, {"runner_id": None, "runner_name": None, "started_at": None}):
            with self.subTest(changes=changes):
                gated = self.go_job(self.job(**changes))
                self.assertEqual((gated.status, gated.outcome), (GateStatus.PASSED, outcome()))
                self.assertEqual(self.gh.calls, [VIEW, JOB_API, READY])
                self.assertEqual(set(self.gh.timeouts), {SHORT})

    def test_started_cancellation_and_incomplete_evidence_still_fail(self):
        cases = [
            {"runner_id": 9},
            {"runner_name": "runner"},
            {"steps": [{"name": "Set up"}]},
            {"runner_id": False},
            {"runner_id": "0"},
            {"steps": None},
            {"id": 201},
            {"id": "200"},
            {"run_id": 101},
            {"head_sha": OLDER},
            {"status": "in_progress"},
            {"conclusion": "failure"},
        ]
        jobs = [self.job(**changes) for changes in cases]
        jobs.extend(
            {k: v for k, v in self.job().items() if k != missing}
            for missing in ("runner_id", "runner_name", "steps", "id", "run_id", "head_sha")
        )
        jobs.extend((None, [], "junk"))
        for job in jobs:
            with self.subTest(job=job):
                self.assert_failed(
                    self.go_job(job, draft=False), "G3: check arbitrary CI leg CANCELLED"
                )
                self.assertEqual(self.gh.calls, [VIEW, JOB_API, UNDO])

    def test_api_errors_fail_closed(self):
        for api in (failed("HTTP 403"), ok("not json"), *PROCESS_ERRORS):
            with self.subTest(api=api):
                self.assert_failed(
                    self.go_job(None, api=api), "G3: check arbitrary CI leg CANCELLED"
                )

    def test_untrusted_or_missing_job_links_are_not_followed(self):
        for link in (
            None,
            "",
            200,
            "https://evil.example/o/n/actions/runs/100/job/200",
            "https://github.com/other/n/actions/runs/100/job/200",
            "http://github.com/o/n/actions/runs/100/job/200",
            "https://github.com/o/n/actions/runs/100/job/200/extra",
            "https://[",
        ):
            with self.subTest(link=link):
                self.assert_failed(
                    self.go_job(None, checks=[self.check(detailsUrl=link)]), "CANCELLED"
                )
                self.assertEqual(self.gh.calls, [VIEW])

    def test_no_executed_checks_does_not_pass(self):
        for checks in ([self.check()], [self.check(), run_("skipped", "SKIPPED")]):
            with self.subTest(checks=checks):
                self.assert_failed(self.go_job(self.job(), checks=checks), "G3: CI did not run")

    def test_failure_and_pending_never_use_the_cancelled_exception(self):
        for changes, said in (
            ({"conclusion": "FAILURE"}, "FAILURE"),
            ({"status": "QUEUED", "conclusion": None}, "QUEUED"),
        ):
            with self.subTest(changes=changes):
                self.assert_failed(
                    self.go_job(self.job(), checks=[run_("pass"), self.check(**changes)]), said
                )
                self.assertEqual(self.gh.calls, [VIEW])

    def test_g1_g2_fail_without_reading_job_evidence(self):
        for updraft in (None, Updraft(UpdraftStatus.CONVERGED, OLDER)):
            with self.subTest(updraft=updraft):
                self.assert_failed(
                    self.go(Gh(view([self.check()])), updraft), "G1:" if updraft is None else "G2:"
                )
                self.assertEqual(self.gh.calls, [VIEW])

    def test_enterprise_host_and_another_repo_use_their_own_api(self):
        url = "https://ghe.example.com/company/project/pull/3"
        check = self.check(
            detailsUrl="https://ghe.example.com/company/project/actions/runs/100/job/200"
        )
        gh = Gh(view([run_("pass"), check]), api=ok(json.dumps(self.job())))
        self.assertEqual(self.go(gh, out=outcome(url=url)).status, GateStatus.PASSED)
        self.assertEqual(
            gh.calls[1],
            [
                "gh",
                "api",
                "--hostname",
                "ghe.example.com",
                "repos/company/project/actions/jobs/200",
            ],
        )

    def test_repository_case_does_not_change_job_identity(self):
        url = "https://github.com/O/N/pull/7"
        gh = Gh(view([run_("pass"), self.check()]), api=ok(json.dumps(self.job())))
        self.assertEqual(self.go(gh, out=outcome(url=url)).status, GateStatus.PASSED)
        self.assertEqual(
            gh.calls[1], ["gh", "api", "--hostname", "github.com", "repos/O/N/actions/jobs/200"]
        )


class Superseded(Base):
    def check(self, job=200, run=100, **changes):
        return {
            **run_("Test scope", "CANCELLED" if job == 200 else "SUCCESS"),
            "workflowName": "CI",
            "detailsUrl": f"https://github.com/o/n/actions/runs/{run}/job/{job}",
            **changes,
        }

    def evidence(self):
        return {
            "repos/o/n/actions/jobs/200": {
                "id": 200,
                "run_id": 100,
                "run_attempt": 1,
                "head_sha": HEAD,
                "name": "Test scope",
                "runner_id": 9,
                "runner_name": "runner",
                "steps": [{"name": "Set up"}],
                "status": "completed",
                "conclusion": "cancelled",
            },
            "repos/o/n/actions/jobs/201": {
                "id": 201,
                "run_id": 101,
                "run_attempt": 1,
                "head_sha": HEAD,
                "name": "Test scope",
                "runner_id": 9,
            },
            "repos/o/n/actions/runs/100": {
                "id": 100,
                "head_sha": HEAD,
                "workflow_id": 50,
                "run_number": 1,
            },
            "repos/o/n/actions/runs/101": {
                "id": 101,
                "head_sha": HEAD,
                "workflow_id": 50,
                "run_number": 2,
            },
        }

    def go_checks(self, checks=None, *, evidence=None, out=None, draft=True):
        evidence = self.evidence() if evidence is None else evidence
        api = {path: ok(json.dumps(data)) for path, data in evidence.items()}
        gh = Gh(view(checks or [self.check(), self.check(201, 101)], draft=draft), api=api)
        return self.go(gh, out=out)

    def test_superseded_started_cancelled_failed_and_pending_checks_are_ignored(self):
        for conclusion, status in (
            ("CANCELLED", "COMPLETED"),
            ("FAILURE", "COMPLETED"),
            ("TIMED_OUT", "COMPLETED"),
            (None, "IN_PROGRESS"),
            (None, "QUEUED"),
        ):
            for reverse in (False, True):
                with self.subTest(conclusion=conclusion, status=status, reverse=reverse):
                    checks = [
                        self.check(conclusion=conclusion, status=status),
                        self.check(201, 101),
                    ]
                    gated = self.go_checks(checks[::-1] if reverse else checks)
                    self.assertEqual((gated.status, gated.outcome), (GateStatus.PASSED, outcome()))
                    self.assertEqual(self.gh.calls[-1], READY)
                    self.assertEqual(set(self.gh.timeouts), {SHORT})

    def test_latest_cancelled_failure_and_pending_still_fail_and_draft(self):
        for conclusion, status, reason in (
            ("CANCELLED", "COMPLETED", "CANCELLED"),
            ("FAILURE", "COMPLETED", "FAILURE"),
            (None, "QUEUED", "QUEUED"),
            (None, "IN_PROGRESS", "IN_PROGRESS"),
        ):
            with self.subTest(conclusion=conclusion, status=status):
                checks = [
                    self.check(conclusion="SUCCESS"),
                    self.check(201, 101, conclusion=conclusion, status=status),
                ]
                evidence = self.evidence()
                evidence["repos/o/n/actions/jobs/201"].update(
                    status=status.lower(),
                    conclusion=conclusion.lower() if conclusion else None,
                    runner_name="runner",
                    steps=[{"name": "Set up"}],
                )
                self.assert_failed(
                    self.go_checks(checks, evidence=evidence, draft=False),
                    f"G3: check Test scope {reason}",
                )
                self.assertEqual(self.gh.calls[-1], UNDO)

    def test_workflow_order_wins_over_job_times_and_workflow_display_names(self):
        checks = [
            self.check(startedAt="2026-10-26T20:51:00Z", completedAt="2026-10-26T20:52:00Z"),
            self.check(201, 101, startedAt=None, workflowName="renamed CI"),
        ]
        self.assertEqual(self.go_checks(checks).status, GateStatus.PASSED)

    def test_run_attempt_orders_reruns_of_the_same_workflow_run(self):
        evidence = self.evidence()
        evidence["repos/o/n/actions/jobs/201"].update(run_id=100, run_attempt=2)
        self.assertEqual(
            self.go_checks([self.check(), self.check(201, 100)], evidence=evidence).status,
            GateStatus.PASSED,
        )

    def test_equal_run_and_attempt_do_not_prove_supersession(self):
        evidence = self.evidence()
        evidence["repos/o/n/actions/jobs/201"]["run_id"] = 100
        self.assert_failed(
            self.go_checks([self.check(), self.check(201, 100)], evidence=evidence), "CANCELLED"
        )

    def test_same_display_name_in_a_different_workflow_does_not_supersede(self):
        evidence = self.evidence()
        evidence["repos/o/n/actions/runs/101"]["workflow_id"] = 51
        self.assert_failed(self.go_checks(evidence=evidence), "CANCELLED")

    def test_different_check_name_and_legacy_status_do_not_supersede(self):
        for check in (self.check(201, 101, name="Other check"), context("Test scope", "SUCCESS")):
            with self.subTest(check=check):
                self.assert_failed(self.go_checks([self.check(), check]), "CANCELLED")

    def test_metadata_must_identify_each_job_run_workflow_and_head(self):
        invalid = {
            "repos/o/n/actions/jobs/201": [
                {"id": 202},
                {"run_id": 102},
                {"head_sha": OLDER},
                {"name": "Other check"},
                {"run_attempt": None},
                {"run_attempt": True},
                {"run_attempt": 0},
            ],
            "repos/o/n/actions/runs/101": [
                {"id": 102},
                {"head_sha": OLDER},
                {"workflow_id": None},
                {"workflow_id": True},
                {"run_number": "2"},
                {"run_number": 0},
            ],
        }
        for path, changes in invalid.items():
            for change in changes:
                with self.subTest(path=path, change=change):
                    evidence = self.evidence()
                    evidence[path].update(change)
                    self.assert_failed(self.go_checks(evidence=evidence), "CANCELLED")

    def test_api_errors_and_unreadable_metadata_preserve_the_older_failure(self):
        for answer in (failed("HTTP 403"), ok("not json"), ok("[]"), ok("null"), *PROCESS_ERRORS):
            with self.subTest(answer=answer):
                api = {path: ok(json.dumps(data)) for path, data in self.evidence().items()}
                api["repos/o/n/actions/runs/101"] = answer
                gh = Gh(view([self.check(), self.check(201, 101)]), api=api)
                self.assert_failed(self.go(gh), "CANCELLED")

    def test_untrusted_links_are_not_followed_for_supersession(self):
        for link in (
            None,
            201,
            "https://evil.example/o/n/actions/runs/101/job/201",
            "https://github.com/other/n/actions/runs/101/job/201",
            "http://github.com/o/n/actions/runs/101/job/201",
            "https://[",
        ):
            with self.subTest(link=link):
                self.assert_failed(
                    self.go_checks([self.check(), self.check(201, 101, detailsUrl=link)]),
                    "CANCELLED",
                )
                for argv in self.gh.calls:
                    if argv[1] == "api":
                        self.assertIn(
                            argv[-1], ("repos/o/n/actions/jobs/200", "repos/o/n/actions/runs/100")
                        )

    def test_only_latest_checks_count_toward_all_skipped(self):
        checks = [self.check(conclusion="SUCCESS"), self.check(201, 101, conclusion="SKIPPED")]
        self.assert_failed(self.go_checks(checks), "all 1 checks skipped")

    def test_job_and_workflow_metadata_are_cached_across_checks(self):
        checks = [self.check(), self.check(201, 101)] * 2
        self.assertEqual(self.go_checks(checks).status, GateStatus.PASSED)
        api = [tuple(argv) for argv in self.gh.calls if argv[1] == "api"]
        self.assertEqual(len(api), 4)
        self.assertEqual(len(set(api)), 4)

    def test_enterprise_host_and_case_insensitive_repository_identity(self):
        url = "https://ghe.example.com/O/N/pull/7"
        checks = [self.check(), self.check(201, 101)]
        for check in checks:
            check["detailsUrl"] = check["detailsUrl"].replace("github.com", "ghe.example.com")
        evidence = {path.replace("o/n", "O/N"): data for path, data in self.evidence().items()}
        self.assertEqual(
            self.go_checks(checks, evidence=evidence, out=outcome(url=url)).status,
            GateStatus.PASSED,
        )
        for argv in self.gh.calls:
            if argv[1] == "api":
                self.assertEqual(argv[2:4], ["--hostname", "ghe.example.com"])


class Errors(Base):
    def test_a_failed_view_fails_without_touching_the_pr(self):
        for answer, reason in (
            (failed("HTTP 401: Bad credentials\n(https://api.github.com)"), "gh pr view: HTTP 401: Bad credentials (https://api.github.com)"),
            (PROCESS_ERRORS[1], "gh pr view: TimeoutExpired"),
            (PROCESS_ERRORS[0], "gh pr view: OSError: gh unavailable"),
            (ok("not json"), "gh pr view: "),
            (ok("[]"), "gh pr view: "),
            (ok(json.dumps({"state": "OPEN", "isDraft": "no", "headRefOid": HEAD, "statusCheckRollup": []})), "gh pr view: "),
            (ok(json.dumps({"state": "OPEN", "isDraft": True, "headRefOid": HEAD, "statusCheckRollup": {}})), "gh pr view: "),
            (ok(json.dumps({"state": "OPEN", "isDraft": True, "statusCheckRollup": []})), "gh pr view: "),
        ):  # fmt: skip
            with self.subTest(reason=reason, answer=type(answer).__name__):
                self.assert_failed(self.go(Gh(answer)), reason)
                self.assertEqual(self.gh.calls, [VIEW])

    def test_a_failed_ready_on_a_now_ready_pr_passes(self):
        for ready in (failed("GraphQL: forbidden"), PROCESS_ERRORS[1]):
            with self.subTest(ready=type(ready).__name__):
                gated = self.go(Gh([view(), view(draft=False)], ready=ready))
                self.assertEqual((gated.status, gated.outcome), (GateStatus.PASSED, outcome()))
                self.assertEqual(self.gh.calls, [VIEW, READY, VIEW])

    def test_a_failed_ready_on_a_draft_pr_undoes_and_fails(self):
        for ready, reason in (
            (failed("GraphQL: forbidden"), "gh pr ready: GraphQL: forbidden"),
            (PROCESS_ERRORS[1], TIMED_OUT),
        ):
            with self.subTest(reason=reason):
                gated = self.go(Gh([view(), view()], ready=ready))
                self.assert_failed_after_ready(gated, reason)
        gated = self.go(Gh([view(), view()], ready=PROCESS_ERRORS[1], undo=failed("nope")))
        self.assert_failed_after_ready(gated, f"{TIMED_OUT}; gh pr ready --undo: nope")

    def test_a_failed_ready_and_view_undoes_and_fails(self):
        gh = Gh([view(), failed("HTTP 502")], ready=PROCESS_ERRORS[1], undo=failed("nope"))
        self.assert_failed_after_ready(
            self.go(gh), f"{TIMED_OUT}; gh pr view: HTTP 502; gh pr ready --undo: nope"
        )

    def assert_failed_after_ready(self, gated, reason):
        self.assertEqual((gated.status, gated.outcome.status), (GateStatus.FAILED, "failed"))
        self.assertTrue(
            gated.outcome.summary.endswith(f"\nready gate: {reason}"), gated.outcome.summary
        )
        self.assertEqual(self.gh.calls, [VIEW, READY, VIEW, UNDO])
        self.assert_safe_calls()

    def test_a_failed_undo_is_added_to_the_reason(self):
        gated = self.go(Gh(view([run_("u", "FAILURE")], draft=False), undo=failed("nope")))
        self.assert_failed(gated, "G3: check u FAILURE; gh pr ready --undo: nope")
        self.assertEqual(self.gh.calls, [VIEW, UNDO])


class NotDone(Base):
    def test_failed_and_needs_input_draft_a_ready_pr_and_keep_their_outcome(self):
        for status in (OutcomeStatus.FAILED, OutcomeStatus.NEEDS_INPUT):
            for draft, calls, gate_status in (
                (False, [VIEW, UNDO], GateStatus.DRAFTED),
                (True, [VIEW], GateStatus.KEPT),
            ):
                with self.subTest(status=status, draft=draft):
                    given = outcome(status)
                    gated = self.go(Gh(view(draft=draft)), out=given)
                    self.assertEqual(
                        (gated.outcome, gated.status, self.gh.calls), (given, gate_status, calls)
                    )
                    self.assert_safe_calls()

    def test_a_closed_pr_is_left_alone(self):
        gated = self.go(Gh(view(draft=False, state="CLOSED")), out=outcome(OutcomeStatus.FAILED))
        self.assertEqual((gated.status, self.gh.calls), (GateStatus.KEPT, [VIEW]))

    def test_errors_keep_the_outcome_and_say_why(self):
        given = outcome(OutcomeStatus.FAILED)
        for gh, reason, calls in (
            (Gh(failed("boom")), "gh pr view: boom", [VIEW]),
            (Gh(view(draft=False), undo=failed("nope")), "gh pr ready --undo: nope", [VIEW, UNDO]),
        ):
            with self.subTest(reason=reason):
                gated = self.go(gh, out=given)
                self.assertEqual(
                    (gated.outcome, gated.status, gated.reason), (given, GateStatus.KEPT, reason)
                )
                self.assertEqual(self.gh.calls, calls)


class Skipped(Base):
    def test_compare_tree_and_empty_urls_skip_the_gate(self):
        for url in (
            "",
            "https://github.com/o/n/compare/main...b?expand=1",
            "https://github.com/o/n/tree/b",
        ):
            for status in OutcomeStatus:
                with self.subTest(url=url, status=status):
                    given = outcome(status, url=url)
                    gated = self.go(Gh(view()), out=given)
                    self.assertEqual(
                        (gated.outcome, gated.status, gated.reason), (given, GateStatus.SKIPPED, "")
                    )
                    self.assertEqual(self.gh.calls, [])

    def test_a_ghe_pr_is_gated_on_its_own_host(self):
        url = "https://ghe.example.com/o/n/pull/3"
        gated = self.go(Gh(view()), out=outcome(url=url))
        self.assertEqual(gated.status, GateStatus.PASSED)
        self.assertEqual(
            self.gh.calls,
            [
                ["gh", "pr", "view", url, "--json", "state,isDraft,headRefOid,statusCheckRollup"],
                ["gh", "pr", "ready", url],
            ],
        )


class Hosts(Case):
    def setUp(self):
        self.dir = scratch(self, real=False)

    def found(self, text=None, output=None, **env):
        if text is not None:
            with open(os.path.join(self.dir, "hosts.yml"), "wb") as f:
                f.write(text if isinstance(text, bytes) else text.encode())
        return hosts(output or {"type": "pull-request"}, {"GH_CONFIG_DIR": self.dir, **env})

    def test_github_com_and_a_logged_in_ghe_host_are_gh_hosts(self):
        text = (
            'github.com:\n    user: me\n"GHE.Example.com":\n    git_protocol: https\nghe.x:8443:\n'
        )
        self.assertEqual(self.found(text), {"github.com", "ghe.example.com"})

    def test_gh_host_env_is_a_gh_host(self):
        self.assertEqual(self.found(GH_HOST="GHE.corp"), {"github.com", "ghe.corp"})
        self.assertEqual(self.found(GH_HOST=""), {"github.com"})

    def test_the_pin_is_the_only_host(self):
        pinned = {"type": "pull-request", "host": "GHE.example.com"}
        self.assertEqual(self.found("other.example:\n", pinned, GH_HOST="x.y"), {"ghe.example.com"})

    def test_a_bad_or_missing_hosts_yml_adds_nothing(self):
        self.assertEqual(self.found(GH_HOST="ghe.corp"), {"github.com", "ghe.corp"})
        self.assertEqual(self.found(b"\xff\xfe:\n"), {"github.com"})
        os.remove(os.path.join(self.dir, "hosts.yml"))
        os.makedirs(os.path.join(self.dir, "hosts.yml"))
        self.assertEqual(self.found(), {"github.com"})

    def test_the_config_dir_falls_back_to_xdg_then_home(self):
        for sub, host in (("gh", "ghe.xdg"), (os.path.join(".config", "gh"), "ghe.home")):
            os.makedirs(os.path.join(self.dir, sub))
            with open(os.path.join(self.dir, sub, "hosts.yml"), "w") as f:
                f.write(f"{host}:\n")
        pr = {"type": "pull-request"}
        self.assertEqual(hosts(pr, {"XDG_CONFIG_HOME": self.dir}), {"github.com", "ghe.xdg"})
        with unittest.mock.patch("os.path.expanduser", return_value=self.dir):
            self.assertEqual(hosts(pr, {}), {"github.com", "ghe.home"})


if __name__ == "__main__":
    unittest.main()
