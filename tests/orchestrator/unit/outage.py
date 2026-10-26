import io
import os
import urllib.request
from unittest import mock

from ensemble.orchestrator.trackers.linear import client
from tests.orchestrator.unit.board_ids import HARNESS_IDENTITY
from tests.orchestrator.unit.keychain_fixtures import Keychain, record

FAILURES = {  # factories: a fresh exception per use
    "5xx": lambda: urllib.error.HTTPError(client.URL, 503, "Service Unavailable", {}, io.BytesIO()),
    "timeout": lambda: TimeoutError("timed out"),
    "URLError": lambda: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
}
FIELDS = {
    "5xx": "status=503",
    "timeout": "reason=TimeoutError: timed out",
    "URLError": "reason=ConnectionRefusedError: [Errno 61] Connection refused",
}


def failing(error, gql=None, ops=None):
    failed = []

    def call(query, **v):
        if ops is not None and client.operation(query) not in ops:
            return gql(query, **v)
        failed.append((query, v))
        v = {"identity": HARNESS_IDENTITY, **v}
        token = record("lin_api_outage", days=20)
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(urllib.request, "urlopen", side_effect=error()),
            mock.patch("subprocess.run", Keychain({v["identity"].token_service: token})),
        ):
            return client.linear_gql(query, **v)

    call.failed = failed
    return call
