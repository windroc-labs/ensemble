"""Test-only stdlib transport fake mounted into the worker's Python startup path."""

import io
import json
from pathlib import Path
from unittest import mock


def respond(request, **_kwargs):
    assert request.full_url == "https://api.linear.app/graphql"
    assert request.get_header("Authorization") == "Bearer IMAGE-TEST-TOKEN"
    payload = json.loads(request.data)
    assert payload["query"].lstrip().startswith("query")
    assert payload["variables"]["i"] == "TASK-2"
    return io.BytesIO(Path("/tmp/worker-issue/response.json").read_bytes())


mock.patch("urllib.request.urlopen", side_effect=respond).start()
