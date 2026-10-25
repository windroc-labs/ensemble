"""OpenCode usage through the probe script against a local HTTP server standing in for Ollama."""

import http.server
import json
import os
import shlex
import socket
import sys
import threading

from ensemble.session.engine.types import Usage
from tests.session.case import Case
from tests.session.integration.clients.opencode import fake_opencode

TAGS = {"models": [{"name": "qwen3.6:35b-mlx"}]}


class Tags(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(TAGS).encode() if self.path == "/api/tags" else b"{}"
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UsageIntegrationTest(Case):
    def usage(self, port: int) -> Usage | None:
        executable = os.path.join(self.home, "opencode")
        with open(executable, "w", encoding="utf-8") as file:
            file.write(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_opencode.__file__)} "$@"\n'
            )
        os.chmod(executable, 0o755)
        self.configure_session(
            {
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "ollama_port": port,
                    "model": "ollama/local",
                    "executable": executable,
                }
            }
        )
        return self.session_cls.usage("opencode")

    def test_a_running_ollama_allows_runs(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Tags)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.assertEqual(self.usage(server.server_address[1]), Usage(None, None))

    def test_a_stopped_ollama_is_unknown_usage(self):
        self.assertIsNone(self.usage(free_port()))
