import base64
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.case import REPO, Case

PROTOCOL = Path(REPO, "src/broker/protocol/docker-relay")
BROKER = Path(REPO, "src/broker/protocol/docker-broker")
REPLY = {"stub_request": "stub_reply", "relay_request": "relay_reply"}
MALFORMED_REPLY = {"stub_request": {"queued": False}, "relay_request": {"ok": False}}


def _index():
    return json.loads((PROTOCOL / "fixtures/index.json").read_text())


def _fixture(case):
    data = json.loads((PROTOCOL / "fixtures" / case["file"]).read_text())
    if "payload_bytes" in case:
        data["payload"] = base64.b64encode(bytes(case["payload_bytes"])).decode()
    return data


def _validator(schema, definition):
    return Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})


class DockerRelayProtocol(Case):
    def test_every_shared_fixture_has_its_expected_schema_outcome(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        broker = json.loads((BROKER / "protocol.schema.json").read_text())
        index = _index()
        self.assertTrue(index["cases"])
        self.assertEqual(
            index["broker_protocol_version"],
            broker["$defs"]["version_output"]["properties"]["protocol_version"]["const"],
        )
        names = [case["file"] for case in index["cases"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            {p.name for p in (PROTOCOL / "fixtures").glob("*.json")} - {"index.json"},
        )
        covered = set()
        for case in index["cases"]:
            with self.subTest(fixture=case["file"]):
                self.assertEqual(Path(case["file"]).name, case["file"])
                definition = case["definition"]
                self.assertIn(definition, schema["$defs"])
                errors = list(_validator(schema, definition).iter_errors(_fixture(case)))
                self.assertEqual(not errors, case["valid"], [e.message[:200] for e in errors])
                covered.add((definition, case["valid"]))
        for definition in ("stub_request", "stub_reply", "relay_request", "relay_reply"):
            for valid in (True, False):
                self.assertIn((definition, valid), covered)

    def test_expected_replies_follow_the_reply_contract(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        for case in _index()["cases"]:
            with self.subTest(fixture=case["file"]):
                definition, expected = case["definition"], case["expected"]
                if definition not in REPLY:
                    self.assertEqual(expected, {"stage": "response", "reply": None})
                    continue
                self.assertEqual(expected["stage"], "schema")
                reply = expected["reply"]
                self.assertFalse(list(_validator(schema, REPLY[definition]).iter_errors(reply)))
                if not case["valid"]:
                    self.assertEqual(reply, MALFORMED_REPLY[definition])
                    continue
                request = json.loads((PROTOCOL / "fixtures" / case["file"]).read_text())
                if definition == "stub_request":
                    self.assertEqual(reply, {"id": request["id"], "queued": True})
                else:
                    self.assertEqual(
                        reply, {"gen": request["gen"], "seq": request["seq"], "ok": True}
                    )

    def test_required_fields_labels_and_payload_limit_are_exercised(self):
        cases = {case["file"]: case for case in _index()["cases"]}
        fields = {
            "stub-request": ("id", "argv", "payload"),
            "stub-reply": ("id", "queued"),
            "relay-request": ("id", "gen", "seq", "argv", "payload"),
            "relay-reply": ("gen", "seq", "ok"),
        }
        for message, required in fields.items():
            self.assertTrue(cases[f"{message}.json"]["valid"])
            self.assertFalse(cases[f"{message}-extra-field.json"]["valid"])
            for field in required:
                self.assertFalse(cases[f"{message}-missing-{field}.json"]["valid"])
        for message in ("stub-request", "relay-request"):
            self.assertFalse(cases[f"{message}-label-invalid.json"]["valid"])
            over = cases[f"{message}-payload-over-limit.json"]
            self.assertEqual((over["payload_bytes"], over["valid"]), (64 * 2**20 + 1, False))
        self.assertTrue(cases["stub-request-label-max.json"]["valid"])
        self.assertFalse(cases["stub-request-label-too-long.json"]["valid"])
        limit = cases["stub-request-payload-at-limit.json"]
        self.assertEqual((limit["payload_bytes"], limit["valid"]), (64 * 2**20, True))
