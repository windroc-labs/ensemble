import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.case import REPO, Case

PROTOCOL = Path(REPO, "src/broker/protocol/docker-broker")


class DockerBrokerProtocol(Case):
    def test_status_reports_only_default_claude_usability(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        validator = Draft202012Validator({**schema, "$ref": "#/$defs/status_output"})
        for ready in (True, False):
            self.assertTrue(validator.is_valid({"claude": {"default": ready}}))
        for value in ({}, {"default": "token"}, {"default": True, "token": "secret"}):
            self.assertFalse(validator.is_valid({"claude": value}))
        self.assertFalse(validator.is_valid({"revocations": []}))

    def test_plugin_sources_require_full_commits(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        validator = Draft202012Validator({**schema, "$ref": "#/$defs/plugin"})
        self.assertTrue(validator.is_valid("x@owner/repo#" + "a" * 40))
        source = "widget@custom/plugin#" + "a" * 40
        self.assertTrue(validator.is_valid(source + "::different/catalogue#" + "b" * 40))
        for value in (
            "updraft",
            "x@owner/repo",
            "x@owner/repo#main",
            "x@owner/repo#v1.0",
            "x@owner/repo#" + "a" * 39,
            "x@owner/repo#" + "A" * 40,
            source + "::",
            source + "::different/catalogue",
            source + "::different/catalogue#main",
            source + "::different/catalogue#" + "b" * 39,
            source + "::different/catalogue#" + "B" * 40,
            source + "::../catalogue#" + "b" * 40,
        ):
            with self.subTest(value=value):
                self.assertFalse(validator.is_valid(value))

    def test_build_base_image_and_image_plugin_names(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        for definition, valid, invalid in (
            (
                "base_image",
                (
                    "dummy-env:test",
                    "sha256:" + "c" * 64,
                    "registry.local/team/env:1.0",
                    "a__b",
                    "a" * 255,
                ),
                (
                    "-x",
                    "Dummy:test",
                    "env:",
                    "env@sha256:" + "c" * 64,
                    "localhost:5000/env",
                    "a" * 256,
                    "",
                ),
            ),
            (
                "plugin_name",
                ("dummy-plugin", "a.b_c", "a" * 64),
                ("../x", ".x", "-x", "a/b", "a" * 65, ""),
            ),
        ):
            validator = Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})
            for value in valid:
                with self.subTest(definition=definition, value=value):
                    self.assertTrue(validator.is_valid(value))
            for value in invalid:
                with self.subTest(definition=definition, value=value):
                    self.assertFalse(validator.is_valid(value))

    def test_every_shared_fixture_has_its_expected_schema_outcome(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        index = json.loads((PROTOCOL / "fixtures/index.json").read_text())
        self.assertTrue(index["cases"])
        self.assertEqual(index["protocol_version"], "34")
        self.assertEqual(
            index["protocol_version"],
            schema["$defs"]["version_output"]["properties"]["protocol_version"]["const"],
        )
        names = [case["file"] for case in index["cases"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            {p.name for p in (PROTOCOL / "fixtures").glob("*.json")}
            - {"index.json", "credential-dirs.json"},
        )
        covered = set()
        for case in index["cases"]:
            with self.subTest(fixture=case["file"]):
                self.assertEqual(Path(case["file"]).name, case["file"])
                definition = case["definition"]
                self.assertIn(definition, schema["$defs"])
                validator = Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})
                data = json.loads((PROTOCOL / "fixtures" / case["file"]).read_text())
                errors = list(validator.iter_errors(data))
                self.assertEqual(not errors, case["valid"], [e.message for e in errors])
                covered.add((definition, case["valid"]))
        for definition in (
            "manifest",
            "credential_spec",
            "version_input",
            "version_output",
            "build_input",
            "build_output",
            "start_input",
            "start_output",
            "lock_query_input",
            "lock_query_output",
            "check_clients_input",
            "check_clients_output",
            "maintain_input",
            "maintain_output",
            "close_input",
            "close_output",
            "probe_provider_input",
            "probe_provider_output",
            "status_input",
            "status_output",
            "workspace_rm_input",
            "workspace_rm_output",
            "error",
        ):
            for valid in (True, False):
                self.assertIn((definition, valid), covered)

    def test_credential_size_limits(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        validator = Draft202012Validator({**schema, "$ref": "#/$defs/credential_spec"})
        for count, valid in ((0, False), (1, True), (16, True), (17, False)):
            with self.subTest(count=count):
                spec = {
                    "env": [
                        {"item": "ensemble-broker-env/a", "file": f"file-{i}"} for i in range(count)
                    ]
                }
                self.assertEqual(validator.is_valid(spec), valid)
        for length, valid in ((255, True), (256, False)):
            with self.subTest(length=length):
                spec = {"env": [{"item": "ensemble-broker-env/a", "file": "a" * length}]}
                self.assertEqual(validator.is_valid(spec), valid)

    def test_a_broker_only_fixture_kind_fits_the_manifest_envelopes(self):
        fixture = json.loads((PROTOCOL / "fixtures/credential-spec-test-kind.json").read_text())
        extension = json.loads((PROTOCOL / "test-kind.schema.json").read_text())
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        production = Draft202012Validator({**schema, "$ref": "#/$defs/credential_spec"})
        for value in fixture.values():
            self.assertTrue(Draft202012Validator(extension).is_valid(json.loads(value)))
            self.assertFalse(production.is_valid(json.loads(value)))
        manifest = json.loads((PROTOCOL / "fixtures/manifest-valid.json").read_text())
        self.assertTrue(Draft202012Validator(schema).is_valid({**manifest, **fixture}))

    def test_manifest_required_fields_and_rejected_unknown_fields_are_exercised(self):
        index = json.loads((PROTOCOL / "fixtures/index.json").read_text())
        cases = {case["file"]: case for case in index["cases"]}
        for field in (
            "name",
            "generation",
            "image",
            "cwd",
            "workspace",
            "claude_dir",
            "client",
            "codex_dir",
            "opencode_dir",
            "github",
            "labels",
        ):
            self.assertFalse(cases[f"manifest-missing-{field}.json"]["valid"])
        self.assertFalse(cases["manifest-unknown-field.json"]["valid"])
        self.assertTrue(cases["manifest-no-hook.json"]["valid"])
