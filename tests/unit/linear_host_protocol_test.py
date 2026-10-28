"""Linear's host-only metadata protocol never permits credential output."""

import json
from pathlib import Path

import jsonschema

from tests.case import REPO, Case


class LinearHostProtocol(Case):
    def test_metadata_only_reply(self):
        path = Path(REPO, "src/broker/protocol/linear-host/protocol.schema.json")
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            {"$defs": schema["$defs"], "$ref": "#/$defs/maintained"},
            format_checker=jsonschema.FormatChecker(),
        )
        reply = {"version": 1, "renewed": True, "expires_at": "2026-11-26T12:00:00Z"}
        validator.validate(reply)
        validator.validate({**reply, "audit_warning": "audit_write_failed"})
        for bad in (
            {**reply, "access_token": "SENTINEL"},
            {**reply, "client_secret": "SENTINEL"},
            {**reply, "version": 2},
            {**reply, "expires_at": "bad"},
            {**reply, "renewed": "true"},
        ):
            with self.subTest(keys=tuple(bad)), self.assertRaises(jsonschema.ValidationError):
                validator.validate(bad)
