"""Claude `/usage` text parsing."""

import re
from datetime import datetime

from ensemble.session.clients.claude.usage import NO_PANEL, parse, reset_at
from ensemble.session.engine.types import Usage, Window
from tests.session.case import Case
from tests.session.unit.clients.claude.support import epoch, fixture

NY = "America/New_York"
NOW = epoch("2026-10-17 01:07", NY)


def panel(*lines) -> str:
    return "".join(f"{line}\n" for line in lines)


class PanelTest(Case):
    def test_real_outputs(self):
        for name, now, want in (
            (
                "usage-default.txt",
                NOW,
                Usage(
                    Window(17.0, epoch("2026-10-17 04:39", NY)),
                    Window(1.0, epoch("2026-10-24 00:59", NY)),
                ),
            ),
            (
                "usage-config-dir.txt",
                epoch("2026-10-17 12:00", NY),
                Usage(
                    Window(13.0, epoch("2026-10-17 14:40", NY)),
                    Window(14.0, epoch("2026-10-24 01:00", NY)),
                ),
            ),
        ):
            with self.subTest(name):
                self.assertEqual(parse(fixture(name), now), want)

    def test_used_and_left_wording(self):
        for line, used in (
            ("Current session: 17% used", 17.0),
            ("Current session: 0% used", 0.0),
            ("Current session: 40% left", 60.0),
            ("Current session: 12.4 % remaining", 88.0),
            ("Current session: 150% used", 100.0),
        ):
            with self.subTest(line):
                self.assertEqual(parse(panel(line), NOW), Usage(Window(used, None), None))

    def test_a_percent_on_a_later_line_before_the_next_label(self):
        text = panel("Current session", "  ███ 30% used", "  Resets 4:39am (America/New_York)")
        self.assertEqual(parse(text, NOW), Usage(Window(30.0, epoch("2026-10-17 04:39", NY)), None))

    def test_weekly_is_the_most_used_weekly_window(self):
        text = panel(
            "Current session: 1% used",
            "Current week (all models): 10% used · resets Oct 24 at 1am (America/New_York)",
            "Current week (Opus): 90% used · resets Oct 20 at 2pm (America/New_York)",
            "Current week (Fable): 50% used",
        )
        self.assertEqual(parse(text, NOW).weekly, Window(90.0, epoch("2026-10-20 14:00", NY)))
        fable = text.replace("Opus): 90%", "Opus): 9%")
        self.assertEqual(parse(fable, NOW).weekly, Window(50.0, epoch("2026-10-24 01:00", NY)))

    def test_a_garbled_all_models_label_still_counts(self):
        text = panel("Current session: 1% used", "Current week (all modls): 30% used")
        self.assertEqual(parse(text, NOW).weekly, Window(30.0, None))

    def test_no_weekly_panel_has_no_weekly_window(self):
        self.assertIsNone(parse(panel("Current session: 1% used"), NOW).weekly)

    def test_status_lines_and_the_insights_section_are_ignored(self):
        text = panel(
            "Current session: 5% used",
            "Opus | 0% context",
            "What's contributing to your limits usage?",
            "Current week (Opus): 99% used",
        )
        self.assertEqual(parse(text, NOW), Usage(Window(5.0, None), None))

    def test_a_label_free_panel_falls_back_to_the_first_percent(self):
        text = panel("Current session", "Current week", "7% used")
        self.assertEqual(parse(text, NOW).five_hour, Window(7.0, None))

    def test_no_panel_says_why(self):
        rows = (
            ("", "no output"),
            ("Sure! Here is your usage summary: you are doing great.\n", NO_PANEL),
            ("Current session: 40%\n", NO_PANEL),
            ("Loading usage data…\nCurrent session\n", "still loading"),
            ("Failed to load usage data\n", "could not load usage data"),
            ("Failed to load usage data: token_expired\n", "token expired"),
            ("Failed to load usage data: authentication_error\n", "authentication error"),
            ("Failed to load usage data: rate limited\n", "usage endpoint rate limited"),
            (
                'Failed to load usage data: {"error": {"type": "rate_limit_error"}}',
                "usage endpoint rate limited",
            ),
            (
                'Failed to load usage data: {"error": {"type": "x", "message": "Nope",'
                ' "details": {"error_code": "bad_org"}}}',
                "Nope (bad_org)",
            ),
            (
                "Failed to load usage data: " + "{" * 100000 + "}" * 100000,
                "could not load usage data",
            ),
            (
                "You are currently using your subscription to power your Claude Code usage\n",
                "subscription notice without quota data",
            ),
        )
        for text, reason in rows:
            with self.subTest(text), self.assertRaisesRegex(ValueError, f"^{re.escape(reason)}$"):
                parse(text, NOW)


class CostSummaryTest(Case):
    """An API key or gateway login has no subscription usage panel: `/usage` prints only what
    the session cost.
    """

    def test_a_cost_summary_alone_has_no_panel(self):
        for text in (
            "Total cost: $0.00",
            "Usage: 0 input, 0 output",
            "Usage: 1,234 input, 56 output\n",
            panel(
                "Total cost:            $0.00",
                "Total duration (API):  0s",
                "Total duration (wall): 1.2s",
                "Total code changes:    0 lines added, 0 lines removed",
                "Usage:                 0 input, 0 output, 0 cache read, 0 cache write",
            ),
            panel(
                "",
                "Total cost:            $0.0123",
                "Usage by model:",
                "    claude-haiku:  10 input, 2 output, 0 cache read, 0 cache write ($0.0001)",
                "",
            ),
        ):
            with self.subTest(text):
                self.assertEqual(parse(text, NOW), Usage(None, None))

    def test_anything_else_is_still_a_failed_read(self):
        rows = (
            ("Total cost: $0.00\nSure, here is your usage.", NO_PANEL),
            ("Usage: none", NO_PANEL),
            ("Total cost: unknown", NO_PANEL),
            ("", "no output"),
            ("Loading usage data…\n", "still loading"),
            ("Hi! It looks like you typed /usage. How can I help?\n", NO_PANEL),
            ("@@ ## %% !!\n", NO_PANEL),
            ("Failed to load usage data\nTotal cost: $0.00\n", "could not load usage data"),
        )
        for text, reason in rows:
            with self.subTest(text), self.assertRaisesRegex(ValueError, f"^{re.escape(reason)}$"):
                parse(text, NOW)


class ResetTest(Case):
    def test_forms(self):
        for text, want in (
            ("resets 4:39am (America/New_York)", epoch("2026-10-17 04:39", NY)),
            ("Resets 12:30am (America/New_York)", epoch("2026-10-18 00:30", NY)),
            ("resets 4pm (America/New_York)", epoch("2026-10-17 16:00", NY)),
            ("resets 16:05 (America/New_York)", epoch("2026-10-17 16:05", NY)),
            ("resets Oct 24 at 12:59am (America/New_York)", epoch("2026-10-24 00:59", NY)),
            ("Resets Oct24 at 1am (America/New_York)", epoch("2026-10-24 01:00", NY)),
            ("resets Oct 24, 3.15 pm (America/New_York)", epoch("2026-10-24 15:15", NY)),
            ("resets Jan 2 at 9am (America/New_York)", epoch("2027-01-02 09:00", NY)),
            ("resets Oct 24, 2027, 1am (America/New_York)", epoch("2027-10-24 01:00", NY)),
            ("resets Oct 1, 2025 10 (America/New_York)", epoch("2025-10-01 10:00", NY)),
            ("resets Oct 24 at 1am (Europe/London)", epoch("2026-10-24 01:00", "Europe/London")),
        ):
            with self.subTest(text):
                self.assertEqual(reset_at(text, NOW), want)

    def test_an_unknown_zone_is_local_time(self):
        want = datetime(2026, 10, 24, 1, 0).timestamp()
        self.assertEqual(reset_at("resets Oct 24 at 1am (Mars/Base)", NOW), want)
        self.assertEqual(reset_at("resets Oct 24 at 1am", NOW), want)

    def test_unreadable_is_none(self):
        for text in (
            None,
            "",
            "soon",
            "resets Oct 24 at 13",
            "resets 13pm",
            "resets Foo 3 1am",
            "resets 25:00",
        ):
            with self.subTest(text):
                self.assertIsNone(reset_at(text, NOW))

    def test_daylight_saving(self):
        self.assertIsNone(reset_at("resets Mar 8, 2026 2:30am (America/New_York)", NOW))
        self.assertEqual(
            reset_at("resets Mar 8 at 2:30am (America/New_York)", NOW),
            epoch("2027-03-08 02:30", NY),
        )
        first = datetime.fromisoformat("2026-11-01 01:30-04:00").timestamp()
        self.assertEqual(reset_at("resets Nov 1 at 1:30am (America/New_York)", NOW), first)

    def test_a_just_passed_reset_stays_in_its_window(self):
        passed = epoch("2026-10-17 00:59", NY)
        self.assertEqual(
            reset_at("resets Oct 17 at 12:59am (America/New_York)", NOW, 5 * 3600), passed
        )
        self.assertEqual(
            reset_at("resets Oct 17 at 12:59am (America/New_York)", NOW),
            epoch("2027-10-17 00:59", NY),
        )
