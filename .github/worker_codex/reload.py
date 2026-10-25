import io
import json
import os
import runpy
import selectors
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


class ReloadExchange:
    def __init__(self, before, after, publish, proactive=None):
        self.before, self.after, self.publish = before, after, publish
        self.proactive = proactive
        self.natural_complete = False
        self.stage = 1
        self.complete = False
        self.notifications = 0

    def request(self, method, params):
        return (json.dumps({"id": self.stage, "method": method, "params": params}) + "\n").encode()

    def start(self):
        return self.request(
            "initialize",
            {
                "clientInfo": {"name": "ensemble_ci_reload", "title": None, "version": "0.163.0"},
                "capabilities": {"experimentalApi": False, "explicitGatewayOauth": True},
            },
        )

    def auth_request(self, *, refresh=False):
        return self.request("getAuthStatus", {"includeToken": True, "refreshToken": refresh})

    def accept(self, message):
        def reject():
            raise ValueError("synthetic native reload failed")

        if not isinstance(message, dict):
            reject()
        if "method" in message:
            if "id" in message or not isinstance(message["method"], str):
                reject()
            self.notifications += 1
            if self.notifications > 1024:
                reject()
            return []
        if (
            "error" in message
            or type(message.get("id")) is not int
            or message["id"] != self.stage
            or self.complete
        ):
            reject()
        result = message.get("result")
        if not isinstance(result, dict):
            reject()
        if self.stage == 1:
            self.stage = 2
            return [b'{"method":"initialized"}\n', self.auth_request()]
        expected = self.before
        if self.stage in (4, 5):
            expected = self.after
        elif self.stage == 6:
            expected = self.proactive["near"]
        elif self.stage == 7:
            expected = self.proactive["token"]
        if (
            result.get("authMethod") != "chatgpt"
            or result.get("authToken") != expected
            or result.get("requiresOpenaiAuth") is not True
        ):
            reject()
        if self.stage in (4, 6):
            if self.stage == 6:
                self.proactive["publish_final"]()
                self.stage = 7
            else:
                self.stage = 5
            return [self.auth_request()]
        if self.stage == 2:
            self.publish()
            self.stage = 3
            return [self.auth_request()]
        if self.stage == 3:
            self.stage = 4
            return [self.auth_request(refresh=True)]
        if self.stage == 5 and self.proactive is not None:
            self.proactive["publish_near"]()
            self.stage = 6
            return [self.auth_request(refresh=True)]
        self.natural_complete = self.stage == 7
        self.complete = True
        return []

    def summary(self):
        return {
            "guarded_reload_completed": self.complete,
            "natural_auth_reload_completed": self.natural_complete,
            "notification_count": self.notifications,
        }


class BoundedLines:
    def __init__(self):
        self.pending = bytearray()
        self.total = 0

    def feed(self, data):
        self.total += len(data)
        if self.total > 1024 * 1024:
            raise ValueError("synthetic native output limit")
        self.pending.extend(data)
        messages = []
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = remaining
            if len(line) > 65536:
                raise ValueError("synthetic native line limit")
            messages.append(json.loads(line))
        if len(self.pending) > 65536:
            raise ValueError("synthetic native line limit")
        return messages

    def finish(self):
        if self.pending:
            raise ValueError("synthetic native incomplete output")


def stop_native(process):
    try:
        with suppress(OSError):
            process.stdin.close()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.25)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            with suppress(OSError):
                stream.close()


def run_reload(home, initial, replacement, near, adopted):
    documents = (initial, replacement, near, adopted)
    if any(
        document["auth_mode"] != "chatgpt"
        or document["tokens"]["refresh_token"] != ""
        or document["tokens"]["account_id"] != initial["tokens"]["account_id"]
        for document in documents
    ) or len({document["tokens"]["access_token"] for document in documents}) != len(documents):
        raise ValueError("synthetic managed same-account values required")
    bootstrap = runpy.run_path("/opt/ensemble/lib/bootstrap.py")

    def publish(document):
        bootstrap["inject_codex"](io.BytesIO(json.dumps(document).encode()), directory=home)

    exchange = ReloadExchange(
        initial["tokens"]["access_token"],
        replacement["tokens"]["access_token"],
        lambda: publish(replacement),
        proactive={
            "token": adopted["tokens"]["access_token"],
            "near": near["tokens"]["access_token"],
            "publish_near": lambda: publish(near),
            "publish_final": lambda: publish(adopted),
        },
    )
    process = subprocess.Popen(
        ["codex", "-c", 'cli_auth_credentials_store="file"', "app-server", "--listen", "stdio://"],
        cwd=home,
        env={
            "HOME": str(home),
            "CODEX_HOME": str(home),
            "PATH": "/opt/ensemble/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        process.stdin.write(exchange.start())
        process.stdin.flush()
        decoder = BoundedLines()
        stderr_size = 0
        deadline = time.monotonic() + 120
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            while not exchange.complete:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("synthetic native timeout")
                for key, _ in selector.select(timeout=min(0.2, remaining)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout":
                            decoder.finish()
                            if not exchange.complete:
                                raise ValueError("synthetic native closed before adoption")
                        continue
                    if key.data == "stderr":
                        stderr_size += len(data)
                        if stderr_size > 1024 * 1024:
                            raise ValueError("synthetic native stderr limit")
                        continue
                    for message in decoder.feed(data):
                        for request in exchange.accept(message):
                            process.stdin.write(request)
                            process.stdin.flush()
        return exchange.summary()
    finally:
        stop_native(process)


if __name__ == "__main__":
    try:
        payload = json.loads(sys.argv[1].encode()[:65537])
        result = run_reload(
            Path(os.environ["CODEX_HOME"]),
            payload["initial"],
            payload["replacement"],
            payload["near"],
            payload["adopted"],
        )
        assert result["guarded_reload_completed"] is True
        assert result["natural_auth_reload_completed"] is True
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, subprocess.SubprocessError):
        sys.exit("synthetic native guarded reload failed")
    print("Synthetic same-process guarded reload: PASS")
