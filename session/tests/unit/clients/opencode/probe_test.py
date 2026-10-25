"""The Ollama listing probe over a fake HTTP connection."""

import http.client
import json
import os
from unittest import mock

from ensemble.session.clients.opencode import probe

from session.tests.case import Case


class Response:
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def connection(status=200, body=b'{"models": []}', error=None):
    found = mock.Mock()
    found.request.side_effect = error
    found.getresponse.return_value = Response(status, body)
    return found


class ProbeTest(Case):
    def test_provider_rejects_bad_urls_without_opening_the_store(self):
        for base in (
            "file:///tmp/key",
            "http://",
            "https://user:secret@host/v1",
            "https://host/v1?x",
            "https://host/v1#",
            "https://host/\n",
        ):
            with (
                self.subTest(base=base),
                mock.patch("builtins.open", side_effect=AssertionError("read store")),
            ):
                self.assertIsNone(probe.provider("strata", base))

    def test_provider_unknown_auth_and_network_failures_are_opaque(self):
        directory = os.path.join(self.home, "data", "opencode")
        os.makedirs(directory)
        path = os.path.join(directory, "auth.json")
        for auth in (
            [],
            {},
            {"strata": {}},
            {"strata": {"type": "oauth", "key": "x"}},
            {"strata": {"type": "api", "key": "bad\nkey"}},
            {"strata": {"type": "api", "key": "x" * probe.LIMIT}},
        ):
            with open(path, "w", encoding="utf-8") as file:
                json.dump(auth, file)
            with (
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.dirname(directory)}),
                mock.patch.object(probe.http.client, "HTTPSConnection") as network,
            ):
                self.assertIsNone(probe.provider("strata", "https://strata.test/v1"))
                network.assert_not_called()
        with open(path, "w", encoding="utf-8") as file:
            json.dump({"strata": {"type": "api", "key": "TEST-SECRET"}}, file)
        for error in (OSError("TEST-SECRET"), http.client.HTTPException("TEST-SECRET")):
            made = connection(error=error)
            with (
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.dirname(directory)}),
                mock.patch.object(probe.http.client, "HTTPConnection", return_value=made),
            ):
                self.assertIsNone(probe.provider("strata", "http://strata.test/v1"))
                made.close.assert_called_once()

    def test_provider_reads_auth_json_and_returns_only_availability(self):
        directory = os.path.join(self.home, "data", "opencode")
        os.makedirs(directory)
        with open(os.path.join(directory, "auth.json"), "w", encoding="utf-8") as file:
            json.dump({"strata": {"type": "api", "key": "TEST-SECRET"}}, file)
        for status in (200, 204):
            made = connection(status=status, body=b"TEST-SECRET")
            response = made.getresponse.return_value
            with (
                self.subTest(status=status),
                mock.patch.object(
                    response, "read", side_effect=AssertionError("read response body")
                ) as read,
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.dirname(directory)}),
                mock.patch.object(
                    probe.http.client, "HTTPSConnection", return_value=made
                ) as network,
            ):
                self.assertEqual(
                    probe.provider("strata", "https://strata.test/v1/"), {"available": True}
                )
                read.assert_not_called()
            network.assert_called_once_with("strata.test", None, timeout=5)
            made.request.assert_called_once_with(
                "GET", "/v1/models", headers={"Authorization": "Bearer TEST-SECRET"}
            )
            made.close.assert_called_once()

    def test_provider_failures_do_not_read_a_response_body_or_follow_redirects(self):
        directory = os.path.join(self.home, "data", "opencode")
        os.makedirs(directory)
        path = os.path.join(directory, "auth.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump({"strata": {"type": "api", "key": "TEST-SECRET"}}, file)
        for status in (302, 401, 500):
            made = connection(status=status)
            with (
                self.subTest(status=status),
                mock.patch.object(
                    made.getresponse.return_value,
                    "read",
                    side_effect=AssertionError("read response body"),
                ) as read,
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.dirname(directory)}),
                mock.patch.object(probe.http.client, "HTTPSConnection", return_value=made),
            ):
                self.assertIsNone(probe.provider("strata", "https://strata.test/v1"))
                read.assert_not_called()
            self.assertEqual(made.request.call_count, 1)
            made.close.assert_called_once()
        with open(path, "w", encoding="utf-8") as file:
            file.write("not JSON TEST-SECRET")
        with (
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.dirname(directory)}),
            mock.patch.object(probe.http.client, "HTTPSConnection") as network,
        ):
            self.assertIsNone(probe.provider("strata", "https://strata.test/v1"))
            network.assert_not_called()

    def listing(self, port="11434", **response):
        made = connection(**response)
        with mock.patch.object(probe.http.client, "HTTPConnection", return_value=made) as kind:
            found = probe.listing(port)
        return found, kind, made

    def test_a_listing_is_its_json_object(self):
        found, kind, made = self.listing()
        self.assertEqual(found, {"models": []})
        kind.assert_called_once_with("127.0.0.1", 11434, timeout=probe.TIMEOUT)
        made.request.assert_called_once_with("GET", "/api/tags")
        made.close.assert_called_once_with()

    def test_anything_else_is_none(self):
        for response in (
            {"status": 404},
            {"error": OSError("refused")},
            {"error": http.client.HTTPException("bad")},
            {"body": b"not json"},
            {"body": b"[]"},
            {"body": json.dumps({"m": "x" * probe.LIMIT}).encode()},
        ):
            with self.subTest(response=str(response)[:60]):
                self.assertIsNone(self.listing(**response)[0])

    def test_a_bad_port_asks_nothing(self):
        for port in ("0", "65536", "x", "-1"):
            with self.subTest(port=port):
                found, kind, _ = self.listing(port)
                self.assertIsNone(found)
                kind.assert_not_called()
