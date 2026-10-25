"""Prints Ollama's model listing (GET /api/tags) on 127.0.0.1:PORT as JSON, or null when it cannot
be read. Standalone: the standard library only.
"""

import http.client
import json
import os
import sys
from urllib.parse import urlsplit

TIMEOUT = 5  # seconds
LIMIT = 1 << 20  # bytes of the listing


def valid_url(base: str) -> bool:
    try:
        url = urlsplit(base)
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme in ("http", "https")
        and bool(url.hostname)
        and url.username is None
        and url.password is None
        and "?" not in base
        and "#" not in base
        and not any(ord(c) < 33 or ord(c) == 127 for c in base)
        and (port is None or 0 < port < 65536)
    )


def provider(name: str, base: str) -> object:
    """Host OpenCode's stored API key; no redirects, response body or raw error escapes."""
    connection = None
    try:
        if not valid_url(base):
            return None
        url = urlsplit(base)
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        with open(os.path.join(root, "opencode", "auth.json"), "rb") as file:
            body = file.read(LIMIT + 1)
        if len(body) > LIMIT:
            return None
        auth = json.loads(body)
        entry = auth.get(name) if isinstance(auth, dict) else None
        key = entry.get("key") if isinstance(entry, dict) and entry.get("type") == "api" else None
        if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) == 127 for c in key):
            return None
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = kind(url.hostname or "", url.port, timeout=TIMEOUT)
        connection.request(
            "GET",
            (url.path or "").rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        response = connection.getresponse()
        available = 200 <= response.status < 300
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None
    finally:
        if connection is not None:
            connection.close()
    return {"available": True} if available else None


def listing(port: str) -> object:
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=TIMEOUT)
    try:
        connection.request("GET", "/api/tags")
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(LIMIT + 1)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    if len(body) > LIMIT:
        return None
    try:
        found = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--provider":
        result = provider(sys.argv[2], sys.argv[3])
    else:
        result = listing(sys.argv[1]) if len(sys.argv) == 2 else None
    print(json.dumps(result))
