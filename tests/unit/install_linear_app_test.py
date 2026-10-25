"""Installer app credentials, at process and terminal boundaries."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import install_linear_app as app
from tests.case import Case

USER = "00000000-0000-4000-8000-000000000123"


class LinearAppInstall(Case):
    def setUp(self):
        self.config = Path(self.home, "config.toml")
        self.config.write_text('[orchestrator.linear.roles.pm]\nclient_id = "client-pm"\n')
        self.output = Path(self.home, "apps.json")
        self.output.write_text("{}")
        self.secrets = {"secret-pm": "hidden-secret"}
        self.requests = []
        self.oauth = {"access_token": "hidden-token", "expires_in": 2592000}
        self.viewer = USER
        self.reject_write = False
        self.runner = mock.patch("subprocess.run", side_effect=self.run_process)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def run_process(self, args, **kwargs):
        self.requests.append((args, kwargs))
        out, code = "", 0
        if args[:2] == ["security", "find-generic-password"]:
            service = args[args.index("-s") + 1]
            code = 0 if service in self.secrets else 44
            if "-w" in args:
                out = self.secrets.get(service, "")
        elif args == ["security", "-i"]:
            if not self.reject_write:
                words = kwargs["input"].split()
                self.secrets[words[words.index("-s") + 1]] = bytes.fromhex(words[-1]).decode()
        elif args[0] == "curl":
            out = json.dumps(
                self.oauth
                if args[-1].endswith("/oauth/token")
                else {"data": {"viewer": {"id": self.viewer}}}
            )
        else:
            self.fail(f"unexpected process: {args}")
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    def prepare(self):
        return app.prepare(str(self.config), "pm", "token-pm", "secret-pm", str(self.output))

    def cached(self, **changes):
        record = {
            "access_token": "cached-token",
            "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
            **changes,
        }
        self.secrets["token-pm"] = json.dumps(record)
        with self.config.open("a") as file:
            file.write(f'user_id = "{USER}"\n')

    def test_new_token_is_requested_stored_verified_and_app_id_is_planned(self):
        self.prepare()
        planned = json.loads(self.output.read_text())["pm"]
        self.assertEqual(
            planned,
            {
                "client_id": "client-pm",
                "secret_service": "secret-pm",
                "token_service": "token-pm",
                "user_id": USER,
            },
        )
        held = json.loads(self.secrets["token-pm"])
        self.assertEqual(held["access_token"], "hidden-token")
        self.assertGreater(datetime.fromisoformat(held["expires_at"]), datetime.now(UTC))
        calls = [call for call in self.requests if call[0][0] == "curl"]
        self.assertIn("client_secret=hidden-secret", calls[0][1]["input"])
        self.assertIn(
            "scope=read%2Cwrite%2Capp%3Aassignable%2Capp%3Amentionable", calls[0][1]["input"]
        )
        self.assertEqual(calls[1][1]["input"], 'header = "Authorization: Bearer hidden-token"\n')
        for args, _ in self.requests:
            self.assertNotIn("hidden-secret", " ".join(args))
            self.assertNotIn("hidden-token", " ".join(args))
        self.assertNotIn("hidden-", self.output.read_text())

    def test_valid_configured_token_is_reused_without_network_or_write(self):
        self.cached()
        self.prepare()
        self.assertFalse(
            [args for args, _ in self.requests if args[0] == "curl" or args == ["security", "-i"]]
        )

    def test_due_token_is_replaced(self):
        self.cached(expires_at=(datetime.now(UTC) + timedelta(days=4)).isoformat())
        self.prepare()
        self.assertEqual(json.loads(self.secrets["token-pm"])["access_token"], "hidden-token")

    def test_malicious_token_and_invalid_expiry_are_rejected_without_write(self):
        for value in ('x"\nurl = "https://evil.invalid', "x\\y", "x y", "x\x1b", ""):
            with self.subTest(token=value):
                self.oauth["access_token"] = value
                with self.assertRaises(app.InstallError):
                    self.prepare()
                self.assertNotIn("token-pm", self.secrets)
        self.oauth["access_token"] = "safe"
        for value in (True, 0, -1, "100", 10**100):
            with self.subTest(expiry=value):
                self.oauth["expires_in"] = value
                with self.assertRaises(app.InstallError):
                    self.prepare()
                self.assertNotIn("token-pm", self.secrets)

    def test_invalid_viewer_and_configured_id_mismatch_are_rejected(self):
        self.viewer = "bad-id"
        with self.assertRaises(app.InstallError):
            self.prepare()
        self.viewer = USER
        self.secrets.pop("token-pm", None)
        self.config.write_text(
            '[orchestrator.linear.roles.pm]\nclient_id = "client-pm"\nuser_id = "00000000-0000-4000-8000-000000000999"\n'
        )
        with self.assertRaises(app.InstallError):
            self.prepare()

    def test_failed_write_is_detected_by_readback(self):
        self.reject_write = True
        with self.assertRaisesRegex(app.InstallError, "Keychain write"):
            self.prepare()

    def test_unsafe_service_is_rejected_before_any_process(self):
        for service in ("x\nremove", "x y", 'x"', "../x"):
            with self.subTest(service=service), self.assertRaises(app.InstallError):
                app.prepare(str(self.config), "pm", service, "secret-pm", str(self.output))
        self.assertEqual(self.requests, [])

    def test_duplicate_app_is_rejected(self):
        self.output.write_text(json.dumps({"researcher": {"user_id": USER}}))
        with self.assertRaisesRegex(app.InstallError, "same Linear app"):
            self.prepare()
