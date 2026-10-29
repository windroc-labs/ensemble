"""The shared session record fixtures against src/ensemble/manager/engine/session.schema.json."""

import json

import jsonschema

from tests.manager import session_records
from tests.manager.case import Case


def _validator() -> jsonschema.Draft202012Validator:
    with open(session_records.SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


class SessionRecords(Case):
    def test_each_client_has_a_valid_record(self):
        validator = _validator()
        self.assertEqual(set(session_records.VALID), {"claude", "codex", "opencode"})
        for client, record in session_records.VALID.items():
            with self.subTest(client=client):
                self.assertEqual(record["client"], client)
                validator.validate(record)

    def test_invalid_records_fail(self):
        validator = _validator()
        for case, text in session_records.INVALID.items():
            with self.subTest(case=case):
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                self.assertFalse(validator.is_valid(record))
