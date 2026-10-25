"""PR URL host rule, check summary and report cells from `gh pr view` JSON."""

from ensemble.manager.engine.pr import cells, checks, pr_url, unknown
from tests.manager.case import Case

URL = "https://github.com/o/r/pull/7"


def _run(conclusion, status="COMPLETED"):
    return {"__typename": "CheckRun", "status": status, "conclusion": conclusion}


def _context(state):
    return {"__typename": "StatusContext", "state": state}


class PrUrl(Case):
    def test_accepts_only_https_urls_on_github_or_gh_host(self):
        for text, env, expected in (
            (f"TASK-1 {URL} draft", {}, URL),
            ("see https://GitHub.com/o/r/pull/7.", {}, "https://GitHub.com/o/r/pull/7"),
            ("https://ghe.corp/o/r/pull/3", {"GH_HOST": "GHE.corp"}, "https://ghe.corp/o/r/pull/3"),
            ("https://ghe.corp/o/r/pull/3", {}, None),
            ("https://evil.example/o/r/pull/1", {}, None),
            ("http://github.com/o/r/pull/1", {}, None),
            ("https://x@github.com/o/r/pull/1", {}, None),
            ("https://github.com:8443/o/r/pull/1", {}, None),
            ("https://github.com/o/r/issues/1", {}, None),
            ("PR #283", {}, None),
            ("see https://[x/o/r/pull/1", {}, None),
            (f"https://[x/o/r/pull/1 {URL}", {}, URL),
        ):
            with self.subTest(text=text, env=env):
                self.assertEqual(pr_url(text, env=env), expected)

    def test_first_valid_url_across_texts_wins_and_none_is_skipped(self):
        evil = "https://evil.example/o/r/pull/1"
        self.assertEqual(pr_url(None, evil, f"{evil} {URL}", env={}), URL)
        self.assertEqual(pr_url("note", URL.replace("7", "8"), URL, env={}), URL.replace("7", "8"))
        self.assertIsNone(pr_url(None, None, env={}))


class Checks(Case):
    def test_summary_words(self):
        for rollup, expected in (
            ([], "none"),
            ([_run("SUCCESS"), _context("SUCCESS")], "pass"),
            ([_run("NEUTRAL"), _run("SKIPPED"), _run("STALE"), _run(None)], "pass"),
            ([_run("SUCCESS"), _run(None, "IN_PROGRESS")], "pending 1/2"),
            ([_context("PENDING"), _context("EXPECTED"), _run("SUCCESS")], "pending 2/3"),
            ([_run(None, "QUEUED"), _run("FAILURE")], "fail 1/2"),
            (
                [
                    _run("CANCELLED"),
                    _run("TIMED_OUT"),
                    _run("ACTION_REQUIRED"),
                    _run("STARTUP_FAILURE"),
                    _context("FAILURE"),
                    _context("ERROR"),
                    _run("SUCCESS"),
                ],
                "fail 6/7",
            ),
        ):
            with self.subTest(rollup=rollup):
                self.assertEqual(checks(rollup), expected)


class Cells(Case):
    def test_gh_view_becomes_pr_checks_mergeable_and_review(self):
        view = {
            "number": 7,
            "mergeable": "CONFLICTING",
            "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [_run("SUCCESS")],
        }
        self.assertEqual(cells(view), ("#7", "pass", "conflicting", "changes_requested"))
        empty = {**view, "reviewDecision": "", "statusCheckRollup": None}
        self.assertEqual(cells(empty), ("#7", "none", "conflicting", "-"))

    def test_unknown_keeps_the_number_from_the_url(self):
        self.assertEqual(unknown(URL), ("#7", "?", "?", "?"))

    def test_malformed_view_raises_value_error(self):
        for view in (None, [], {"number": "7"}, {"number": 7, "mergeable": None}):
            with self.subTest(view=view), self.assertRaises(ValueError):
                cells(view)
