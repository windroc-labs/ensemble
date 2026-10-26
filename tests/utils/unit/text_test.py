"""Printable text, exception messages and ISO timestamps."""

import re
import unittest
from datetime import UTC, datetime, timedelta, timezone

from ensemble.utils import one_line, parse_time, sid_error, timestamp
from ensemble.utils.text import last_lines, printable
from tests.utils.case import Case


class Printable(Case):
    def test_drops_control_characters_but_tabs(self):
        self.assertEqual(printable("a\x1b]52;c;ZXZpbA==\x07b\tc\r\n"), "a]52;c;ZXZpbA==b\tc")


class LastLines(Case):
    def test_the_last_non_blank_lines_stripped_on_one_line(self):
        text = "a\n\n  b  \nc\nd\ne\nf\n\n"
        self.assertEqual(last_lines(text), "b | c | d | e | f")
        self.assertEqual(last_lines(text, 2), "e | f")
        self.assertEqual(last_lines(" \n\n"), "")


class SidError(Case):
    def test_only_an_invalid_identifier_gets_an_error_naming_it(self):
        error = 'sid must be a non-empty printable string without "/", got '
        for sid, want in (
            *((sid, None) for sid in ("session-1", "thread_2", "space allowed", "é", "a" * 200)),
            *(
                (sid, f"{error}{sid!r:.80}")
                for sid in (None, 1, b"sid", "", "a/b", "a\tb", "a\nb", "a\0b")
            ),
            ("a" * 200 + "\n", error + "'" + "a" * 79),
        ):
            with self.subTest(sid=sid):
                self.assertEqual(sid_error(sid), want)


class OneLine(Case):
    def test_any_value_becomes_one_line(self):
        error = RuntimeError("two\nlines")
        error.credential = "private attribute"
        for value, expected in (
            ("a\n\n  b\tc ", "a b c"),
            (" \t\n", ""),
            (42, "42"),
            (None, "None"),
            (error, "RuntimeError: two lines"),
        ):
            with self.subTest(value=value):
                self.assertEqual(one_line(value), expected)


class ParseTime(Case):
    def test_iso_times_parse_without_loss(self):
        utc = datetime(2026, 10, 14, 12, 34, 56, tzinfo=UTC)
        for value, expected in (
            ("2026-10-14T12:34:56Z", utc),
            ("2026-10-14T12:34:56+00:00", utc),
            ("2026-10-14T08:34:56-04:00", utc),
            (
                "2026-10-14T12:34:56.123456Z",
                datetime(2026, 10, 14, 12, 34, 56, 123456, tzinfo=UTC),
            ),
            ("2026-10-14T12:34:56", datetime(2026, 10, 14, 12, 34, 56)),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_time(value), expected)

    def test_invalid_timestamps_raise_value_error(self):
        for value in ("", "not a time", "2026-13-14T12:34:56Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time(value)


class Timestamp(Case):
    def test_default_timestamp_is_local_time_in_seconds(self):
        self.assertRegex(timestamp(), r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\Z")

    def test_local_timestamp_has_seconds_precision_and_preserves_the_instant(self):
        instant = datetime(2026, 10, 14, 8, 34, 56, 987654, tzinfo=timezone(timedelta(hours=-4)))
        value = timestamp(now=lambda: instant)
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", value))
        self.assertEqual(parse_time(value), instant.replace(microsecond=0))


if __name__ == "__main__":
    unittest.main()
