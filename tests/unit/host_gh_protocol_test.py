"""The host gh login's reply names the installation and expiry, never the token."""

import json
from pathlib import Path

import jsonschema

from tests.case import REPO, Case


class HostGHProtocol(Case):
    def test_metadata_only_reply(self):
        path = Path(REPO, "src/broker/protocol/host-gh/protocol.schema.json")
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            {"$defs": schema["$defs"], "$ref": "#/$defs/logged_in"},
            format_checker=jsonschema.FormatChecker(),
        )
        reply = {"version": 1, "account": "windroc-labs", "expires_at": "2026-11-15T13:00:00Z"}
        validator.validate(reply)
        validator.validate({**reply, "audit_warning": "audit_write_failed"})
        for bad in (
            {**reply, "token": "SENTINEL"},
            {**reply, "version": 2},
            {**reply, "expires_at": "bad"},
            {**reply, "account": "not a login"},
            {k: v for k, v in reply.items() if k != "account"},
        ):
            with self.subTest(keys=tuple(bad)), self.assertRaises(jsonschema.ValidationError):
                validator.validate(bad)
