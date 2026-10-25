"""delegate/event/delegate_event.py: the callback line, round trips and refusals; its exports."""

import json
import unittest

from ensemble import delegate
from ensemble.delegate import event as event_package
from ensemble.delegate.event import DelegateEvent, DelegateEventError, DelegateEventKind
from ensemble.delegate.event.types import Outcome
from ensemble.utils import OutcomeStatus
from tests.delegate.case import Case

SID = "1234abcd-0000-4000-8000-000000000000"
BEGUN = DelegateEvent("begun", sid=SID, attempt=1)
PROGRESS = DelegateEvent(
    "progress", sid=SID, attempt=2, name="start", text="planning: 修复\nthe work"
)
FINISHED = DelegateEvent(
    "finished",
    sid=SID,
    attempt=1,
    returncode=0,
    outcome=Outcome(OutcomeStatus.DONE, "T", "S", files=["a"]),
    limit={"resets_at": 1760000000.5},
)
FAILED = DelegateEvent(
    "finished", sid=SID, attempt=3, returncode=1, error="boom", limit={"resets_at": None}
)
BARE = DelegateEvent("finished", sid=SID, attempt=1, returncode=2)
RUN = {"sid": SID, "attempt": 1}
MISSING = object()
STAMPS = [
    *(("sid", bad, "sid: want a non-empty str") for bad in (MISSING, "", None, 1, [SID])),
    *(
        ("attempt", bad, "attempt: want an int >= 1")
        for bad in (MISSING, 0, -1, True, "1", 1.0, None)
    ),
]
LIMIT = 'limit: want null or {"resets_at": a number or null}'
OUTCOME = "outcome: want the fields deliverable, files, questions, status, summary, title, url"


def line(**data) -> str:
    return json.dumps(data)


class EventLine(Case):
    def test_parse_produces_a_complete_typed_outcome(self):
        outcome = {
            "status": "needs_input",
            "title": "T",
            "summary": "S",
            "questions": ["Q"],
            "url": "",
            "files": [],
            "deliverable": "",
        }
        parsed = DelegateEvent.parse(
            line(v=1, kind="finished", returncode=0, outcome=outcome, **RUN)
        )
        self.assertIsInstance(parsed.outcome, Outcome)
        self.assertIs(parsed.outcome.status, OutcomeStatus.NEEDS_INPUT)
        self.assertEqual(json.loads(parsed.to_line())["outcome"], outcome)

    def test_parse_rejects_incomplete_or_mistyped_outcomes(self):
        outcome = {
            "status": "done",
            "title": "T",
            "summary": "S",
            "questions": [],
            "url": "",
            "files": [],
            "deliverable": "",
        }
        invalid = [
            {key: value for key, value in outcome.items() if key != missing} for missing in outcome
        ]
        invalid += [
            {**outcome, key: value}
            for key, value in (
                ("status", "x"),
                ("title", 1),
                ("title", "T" * 201),
                ("title", "two\nlines"),
                ("summary", None),
                ("summary", "S" * 4001),
                ("questions", [1]),
                ("questions", ["Q"] * 5),
                ("url", False),
                ("files", "a"),
                ("deliverable", []),
                ("extra", "x"),
            )
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(DelegateEventError):
                DelegateEvent.parse(line(v=1, kind="finished", returncode=0, outcome=data, **RUN))

    def test_a_finished_summary_over_300_characters_is_refused(self):
        # A run begun before the limit fell from 4000 may still send one.
        outcome = {
            "status": "done",
            "title": "T",
            "summary": "S" * 300,
            "questions": [],
            "url": "",
            "files": [],
            "deliverable": "",
        }
        parsed = DelegateEvent.parse(
            line(v=1, kind="finished", returncode=0, outcome=outcome, **RUN)
        )
        self.assertEqual(parsed.outcome.summary, "S" * 300)
        self.refused(
            line(
                v=1, kind="finished", returncode=0, outcome={**outcome, "summary": "S" * 301}, **RUN
            ),
            "summary is longer than 300 characters",
        )

    def refused(self, text, message):
        with self.assertRaises(DelegateEventError) as caught:
            DelegateEvent.parse(text)
        self.assertEqual(str(caught.exception), message)

    def test_each_kind_round_trips(self):
        for event in (BEGUN, PROGRESS, FINISHED, FAILED, BARE):
            with self.subTest(event=event):
                text = event.to_line()
                self.assertEqual(text.splitlines(), [text])
                self.assertEqual(DelegateEvent.parse(text), event)
                self.assertEqual(DelegateEvent.parse(text + "\n"), event)

    def test_the_line_holds_v_kind_and_only_the_kinds_fields(self):
        self.assertEqual(BEGUN.to_line(), f'{{"v":1,"kind":"begun","sid":"{SID}","attempt":1}}')
        self.assertEqual(
            BARE.to_line(),
            f'{{"v":1,"kind":"finished","sid":"{SID}","attempt":1,"returncode":2,'
            '"outcome":null,"error":"","limit":null}',
        )
        self.assertEqual(
            list(json.loads(PROGRESS.to_line())), ["v", "kind", "sid", "attempt", "name", "text"]
        )
        self.assertEqual(list(DelegateEventKind), ["begun", "progress", "finished"])
        self.assertEqual(DelegateEvent(DelegateEventKind.BEGUN, **RUN), BEGUN)
        self.assertEqual(DelegateEvent(DelegateEventKind.BEGUN, **RUN).to_line(), BEGUN.to_line())

    def test_a_finished_line_without_the_optional_fields_takes_their_defaults(self):
        self.assertEqual(DelegateEvent.parse(line(v=1, kind="finished", returncode=2, **RUN)), BARE)

    def test_kind_must_be_known(self):
        for kind in ("started", None, ["begun"]):
            with self.subTest(kind=kind):
                self.refused(
                    line(v=1, kind=kind, **RUN), "kind: want one of begun, progress, finished"
                )
        self.refused(line(v=1, **RUN), "kind: want one of begun, progress, finished")

    def test_v_must_be_the_int_one(self):
        self.refused(line(kind="begun", **RUN), "v: want 1")
        for v in (2, True, "1", 1.0, None):
            with self.subTest(v=v):
                self.refused(line(v=v, kind="begun", **RUN), "v: want 1")

    def test_a_field_of_another_kind_is_refused(self):
        self.refused(line(v=1, kind="begun", text="x", **RUN), "text: not a begun field")
        self.refused(
            line(v=1, kind="progress", name="n", text="t", returncode=0, **RUN),
            "returncode: not a progress field",
        )
        self.refused(
            line(v=1, kind="finished", returncode=0, extra=1, **RUN), "extra: not a finished field"
        )
        for kwargs, field in (
            ({"text": "x"}, "text"),
            ({"error": "x"}, "error"),
            ({"outcome": {}}, "outcome"),
        ):
            with self.subTest(field=field), self.assertRaises(DelegateEventError) as caught:
                DelegateEvent("begun", **RUN, **kwargs)
            self.assertEqual(str(caught.exception), f"{field}: not a begun field")

    def test_every_kind_requires_the_runs_sid_and_attempt(self):
        own = {"begun": {}, "progress": {"name": "n", "text": "t"}, "finished": {"returncode": 0}}
        for kind, fields in own.items():
            for field, bad, message in STAMPS:
                data = {**RUN, **fields}
                if bad is MISSING:
                    del data[field]
                else:
                    data[field] = bad
                with self.subTest(kind=kind, field=field, bad=bad):
                    self.refused(line(v=1, kind=kind, **data), message)
                    with self.assertRaises(DelegateEventError) as caught:
                        DelegateEvent(kind, **data)
                    self.assertEqual(str(caught.exception), message)
            event = DelegateEvent.parse(line(v=1, kind=kind, sid=SID, attempt=7, **fields))
            self.assertEqual((event.sid, event.attempt), (SID, 7))

    def test_each_kinds_fields_are_checked(self):
        for data, message in (
            ({"kind": "progress", "text": "t"}, "name: want a str"),
            ({"kind": "progress", "name": "n", "text": 1}, "text: want a str"),
            ({"kind": "finished"}, "returncode: want an int"),
            ({"kind": "finished", "returncode": True}, "returncode: want an int"),
            ({"kind": "finished", "returncode": 0.0}, "returncode: want an int"),
            ({"kind": "finished", "returncode": 0, "error": None}, "error: want a str"),
            (
                {"kind": "finished", "returncode": 0, "outcome": {"title": "T"}},
                OUTCOME,
            ),
            (
                {"kind": "finished", "returncode": 0, "outcome": {"status": 1}},
                OUTCOME,
            ),
            (
                {"kind": "finished", "returncode": 0, "outcome": ["done"]},
                OUTCOME,
            ),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": 1, "at": 2}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": True}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": "soon"}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": 5}, LIMIT),
        ):
            with self.subTest(data=data):
                self.refused(line(v=1, **RUN, **data), message)

    def test_not_a_json_object_is_refused(self):
        self.refused("[1]", "DelegateEvent line: want a JSON object")
        with self.assertRaises(DelegateEventError) as caught:
            DelegateEvent.parse("{nope")
        self.assertTrue(str(caught.exception).startswith("DelegateEvent line: not JSON ("))
        self.refused(b"{}", "DelegateEvent line: want a str")


class Exports(Case):
    def test_event_and_delegate_export_the_callback_event(self):
        for name in ("DelegateEvent", "DelegateEventError", "DelegateEventKind"):
            with self.subTest(name=name):
                self.assertIn(name, event_package.__all__)
        self.assertNotIn("KINDS", event_package.__all__)
        self.assertIn("DelegateEvent", delegate.__all__)
        self.assertIs(delegate.DelegateEvent, DelegateEvent)


if __name__ == "__main__":
    unittest.main()
