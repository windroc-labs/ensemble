import json
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="codex-tools-") as temporary:
    work = Path(temporary)
    marker = uuid.uuid4().hex
    (work / "input.txt").write_text(marker)
    observed = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append(body)
            if len(observed) == 1:
                item = {
                    "type": "custom_tool_call",
                    "call_id": "tool-1",
                    "name": "exec",
                    "input": 'text(await tools.exec_command({cmd:"cat input.txt",login:false}));',
                }
            else:
                item = {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg-1",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            events = [
                {"type": "response.created", "response": {"id": "resp-" + str(len(observed))}},
                {"type": "response.output_item.done", "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-" + str(len(observed)),
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    },
                },
            ]
            data = "".join(
                "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = [
            'model_provider="fixture"',
            'model_providers.fixture={name="Fixture",base_url="http://127.0.0.1:'
            + str(server.server_port)
            + '/v1",wire_api="responses",requires_openai_auth=false}',
            "features.code_mode=true",
            "features.code_mode_only=true",
            "features.code_mode_host={enabled=true,disable_in_process_fallback=true}",
            "features.code_mode_host_grpc=false",
        ]
        command = ["codex", "--no-daemon", "--dangerously-bypass-approvals-and-sandbox"]
        for value in config:
            command.extend(["-c", value])
        command.extend(
            [
                "exec",
                "--skip-git-repo-check",
                "--json",
                "-m",
                "gpt-5.4",
                "Read input.txt using the command tool.",
            ]
        )
        (work / "home").mkdir()
        env = {
            "HOME": temporary,
            "CODEX_HOME": str(work / "home"),
            "PATH": "/opt/ensemble/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        result = subprocess.run(command, cwd=work, env=env, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode(errors="replace")[-2000:]
        assert len(observed) == 2, "expected tool call and follow-up"
        outputs = [
            item for item in observed[1]["input"] if item.get("type") == "custom_tool_call_output"
        ]
        if marker not in json.dumps(outputs):
            print(json.dumps(outputs), file=sys.stderr)
            print(result.stderr.decode(errors="replace")[-4000:], file=sys.stderr)
        assert marker in json.dumps(outputs), "command did not read the fixture"
        assert b"failed to spawn code-mode host" not in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
print("Offline Codex code-mode command turn: PASS")
