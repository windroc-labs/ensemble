"""A stand-in `claude` for integration tests, `-p --output-format stream-json --verbose` (print),
`-p /usage` (the usage probe) or neither (interactive); install() puts it on PATH. An option
src/ensemble/session/clients/claude/ never emits exits 2. Interactive: PROMPT, else the first stdin
line, is turn 1; each later line runs the next of [steps, *turns] between UserPromptSubmit and Stop
hooks. Stdin's end exits `exit`; stranded (orphaned, or its scenario file gone), 1. Interactive
readiness: SessionStart (source startup, or resume on --resume; matched on source) fires before
turn 1 and after each dialog's answer; input arriving from
then until READY s after it is dropped, as Claude drops keys before its input box. A stdin line
starting with / is a local command: logged to `commands`, never a prompt, an answer or a turn.

Scenario: the JSON object in $FAKE_CLAUDE_SCENARIO's file (bad: exit 1):
- steps: a string is assistant text; {"kind": "tool", "name": N} is a tool_use (interactive: then
PostToolUse). In print, {"kind": "limit", "resets_at": T} is a rejected rate_limit_event, then LIMIT
with error "rate_limit". {"kind": "write", "path": P, "text": T} writes T to P (relative to cwd).
{"kind": "git"} replies with git rev-parse --show-toplevel's stdout and exit code. {"kind": "run",
"argv": A, "path": P} runs A in the cwd and writes its {"returncode", "stdout", "stderr"} to P
(relative to cwd; returncode null when A could not start or ran over 60 s).
Interactive {"kind": "limit", "text": S} prints S, keeps it as a rate_limit API error and ends the
turn with StopFailure; {"kind": "dialog", "text": S} prints S and reads another line. Other steps: {"kind": "hook", "event": E, ...}; {"kind": "sleep", "seconds": X};
{"kind": "exit"} exits at once with `exit`.
{"kind": "recall"} replies with the text of its transcript's first user prompt, so a resumed
conversation can answer from the first generation's.
{"kind": "queued", "answer": A} runs a tool while the next stdin line arrives, which Claude Code
queues (an enqueue record) and joins to the turn once the tool returns (PostToolUse, then a
queued_command attachment), then answers A; with "prompted": true, UserPromptSubmit runs for the line
as the turn joins it, before its attachment, as Claude Code 2.1.312 does. {"kind": "notice", "text":
T} is a background task's completion Claude Code queues and joins to the turn: an enqueue record,
UserPromptSubmit for T, then a task-notification queued_command attachment; it reads no input. {"kind": "pause", "file": F} waits
for F to exist; {"kind": "input"} waits for any input to arrive (kept for the next line).
- turns: interactive steps of turn 2 on.
- dialog: interactive text printed at startup, before the first SessionStart (as Claude's workspace
trust dialog), awaiting its answer as a dialog step does.
- background: interactive background_tasks per Stop.
- resume: print steps run instead of `steps` on --resume.
- rate_limit: print a rate_limit_info after init.
- usage: `-p /usage`'s stdout, or an object mapping a CLAUDE_CONFIG_DIR ("": unset) to it; default
(and an unmapped CLAUDE_CONFIG_DIR's): an unused account's panel.
- exit: the exit code.
- hang: in print, true sleeps after steps until orphaned or HANG_CAP s, then exits 1.
- log: a file each start appends {"pid", "argv", "cwd", "config_dir"} to (config_dir: "" unset).
- gate: a file a --session-id or --resume run waits for after its log line, as hang does.
- commands: a file each local command appends {"pid", "line", "time", "dialog"} to; dialog: read
while a dialog step waited.
"""

import argparse
import itertools
import json
import os
import re
import select
import shlex
import subprocess
import sys
import termios
import time
import uuid
from dataclasses import dataclass

ENV = "FAKE_CLAUDE_SCENARIO"
HANG_CAP = 120
HOOK_TIMEOUT = 60
READY = 0.3
DEFAULTS = {
    "steps": [],
    "resume": None,
    "turns": [],
    "dialog": None,
    "background": 0,
    "rate_limit": None,
    "usage": "Current session: 0% used\n",
    "exit": 0,
    "hang": False,
    "log": None,
    "gate": None,
    "commands": None,
}
LIMIT = "You've hit your usage limit."


def install(bin_dir: str, tail: str = "/usr/bin:/bin") -> str:
    q = shlex.quote
    path = os.path.join(bin_dir, "claude")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec {q(sys.executable)} -I {q(os.path.abspath(__file__))} "$@"\n')
    os.chmod(path, 0o755)
    return f"{bin_dir}:{tail}"


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value) -> bool:
    return isinstance(value, str)


PRINT = {
    "git": {},
    "tool": {"name": _text},
    "limit": {"resets_at": lambda v: v is None or _number(v)},
    "write": {"path": lambda v: _text(v) and v != "", "text": _text},
    "run": {
        "argv": lambda v: isinstance(v, list) and v != [] and all(_text(a) for a in v),
        "path": lambda v: _text(v) and v != "",
    },
}
OPTIONAL = {"queued": {"prompted": lambda v: isinstance(v, bool)}}  # tui keys a step may leave out
TUI = {
    "tool": {"name": _text},
    "limit": {"text": _text},
    "dialog": {"text": _text},
    "sleep": {"seconds": lambda v: _number(v) and 0 <= v <= HANG_CAP},
    "exit": {},
    "queued": {"answer": _text},
    "notice": {"text": _text},
    "pause": {"file": lambda v: _text(v) and v != ""},
    "input": {},
    "recall": {},
}


def _step(step, *, tui: bool) -> bool:
    if isinstance(step, str):
        return True
    kind = step.get("kind") if isinstance(step, dict) else None
    if tui and kind == "hook":
        return isinstance(step.get("event"), str) and step["event"] != ""
    shape = (TUI if tui else PRINT).get(kind) if isinstance(kind, str) else None
    if shape is None:
        return False
    optional = OPTIONAL.get(kind, {}) if tui else {}
    return {"kind", *shape} <= set(step) <= {"kind", *shape, *optional} and all(
        ok(step[key]) for key, ok in {**shape, **optional}.items() if key in step
    )


CHECKS = {
    "steps": lambda v: isinstance(v, list),
    "resume": lambda v: (
        v is None or isinstance(v, list) and all(_step(step, tui=False) for step in v)
    ),
    "turns": lambda v: isinstance(v, list) and all(isinstance(t, list) for t in v),
    "dialog": lambda v: v is None or _text(v),
    "background": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "rate_limit": lambda v: v is None or isinstance(v, dict),
    "usage": lambda v: (
        isinstance(v, str)
        or isinstance(v, dict)
        and all(isinstance(x, str) for x in (*v, *v.values()))
    ),
    "exit": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "hang": lambda v: isinstance(v, bool),
    "log": lambda v: v is None or isinstance(v, str),
    "gate": lambda v: v is None or isinstance(v, str),
    "commands": lambda v: v is None or isinstance(v, str),
}


def scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_claude.py: {ENV} is unset")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_claude.py: {ENV}: {e}")
    if not isinstance(data, dict):
        sys.exit("fake_claude.py: the scenario is no JSON object")
    if extra := sorted(set(data) - set(DEFAULTS)):
        sys.exit(f"fake_claude.py: unknown scenario key {extra[0]!r}")
    data = {**DEFAULTS, **data}
    if bad := [key for key, ok in CHECKS.items() if not ok(data[key])]:
        sys.exit(f"fake_claude.py: bad scenario {bad[0]}")
    return data


def settings_hooks(a):
    try:
        settings = json.loads(a.settings)
    except ValueError:
        settings = None
    if not isinstance(settings, dict):
        sys.exit("Error: --settings is no JSON object")
    return settings.get("hooks")


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="claude", allow_abbrev=False)
    for flag in ("--verbose", "--strict-mcp-config", "--no-session-persistence"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("-p", "--print", action="store_true")
    for opt in (
        "--session-id",
        "--resume",
        "--name",
        "--model",
        "--effort",
        "--permission-mode",
        "--setting-sources",
        "--output-format",
        "--settings",
        "--mcp-config",
        "--append-system-prompt",
    ):
        ap.add_argument(opt)
    for opt in ("--add-dir", "--allowedTools"):
        ap.add_argument(opt, nargs="+", action="extend")
    ap.add_argument("prompt", nargs="?")
    a = ap.parse_args(argv)
    if a.output_format != ("stream-json" if a.print and a.prompt != "/usage" else None):
        ap.error(
            "this fake models -p with --output-format stream-json, and interactive mode without either"
        )
    if a.print and (not a.prompt):
        sys.exit("Error: no prompt")
    if a.session_id and a.resume:
        sys.exit("Error: --session-id and --resume exclude each other")
    a.hooks = None
    if a.settings is not None:
        a.hooks = settings_hooks(a)
    if a.output_format and (not a.verbose):
        sys.exit("Error: When using --print, --output-format=stream-json requires --verbose")
    return a


def _matches(matcher, field: str) -> bool:
    if matcher in ("", "*"):
        return True
    try:
        return isinstance(matcher, str) and re.search(matcher, field) is not None
    except re.error:
        return False


def run_hooks(hooks, event: str, payload: dict, field: str) -> None:
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        items = (
            entry.get("hooks")
            if isinstance(entry, dict) and _matches(entry.get("matcher", ""), field)
            else None
        )
        for hook in items if isinstance(items, list) else []:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
            ):
                try:
                    subprocess.run(
                        ["/bin/sh", "-c", hook["command"]],
                        input=json.dumps(payload),
                        text=True,
                        stdout=subprocess.DEVNULL,
                        timeout=HOOK_TIMEOUT,
                    )
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"fake_claude.py: {event} hook: {e}", file=sys.stderr, flush=True)


def waited(done, parent: int) -> bool:
    until = time.monotonic() + HANG_CAP
    while not done():
        if os.getppid() != parent or time.monotonic() > until:
            return False
        time.sleep(0.05)
    return True


def stranded(parent: int, scenario: str) -> bool:
    """Orphaned, or its scenario file gone: the test that started it has ended."""
    return os.getppid() != parent or not os.path.exists(scenario)


class Stranded(Exception):
    """Orphaned, or its scenario file gone."""


class Stdin:
    """By os.read: select can't see what a buffered reader holds."""

    def __init__(self, parent: int, scenario: str, commands: str | None = None):
        self.parent, self.scenario, self.buf, self.eof = parent, scenario, b"", False
        self.commands, self.dialog = commands, False

    def read(self, wait: float) -> bytes | None:
        """What arrived within `wait` s (b"" at its end; None: nothing)."""
        try:
            chunk = os.read(0, 65536) if select.select([0], [], [], wait)[0] else None
        except (OSError, ValueError):
            chunk = b""
        self.eof = self.eof or chunk == b""
        return chunk

    def keep(self) -> None:
        """Buffers what has arrived so far."""
        while not self.eof and (chunk := self.read(0)):
            self.buf += chunk

    def arrival(self) -> None:
        """Waits, at most HANG_CAP s, until something arrived; it stays buffered."""
        until = time.monotonic() + HANG_CAP
        while not self.buf and not self.eof and time.monotonic() < until:
            self.buf += self.read(0.1) or b""

    def drop(self, until: float) -> None:
        """Discards what arrives before monotonic `until`."""
        while not self.eof and (left := until - time.monotonic()) > 0:
            self.read(left)

    def line(self) -> str:
        """The next line that is no local command; each local command logged on the way."""
        while True:
            line = self.raw()
            if not line.startswith("/"):
                return line
            if self.commands:
                entry = {
                    "pid": os.getpid(),
                    "line": line,
                    "time": time.time(),
                    "dialog": self.dialog,
                }
                with open(self.commands, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")

    def raw(self) -> str:
        while b"\n" not in self.buf and not self.eof:
            if stranded(self.parent, self.scenario):
                raise Stranded
            self.buf += self.read(0.1) or b""
        if not self.buf:
            raise EOFError
        line, _, self.buf = self.buf.partition(b"\n")
        # A managed worker's guard sends a paste bracketed, as Claude reads it.
        return line.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"").decode(errors="replace")


@dataclass(frozen=True, kw_only=True)
class InteractiveRequest:
    scenario: dict
    turns: list
    hooks: dict | None
    payload: dict
    keep: object
    record: object
    stdin: Stdin
    prompt: str | None
    source: str


class InteractiveSession:
    def __init__(self, request: InteractiveRequest):
        self.request = request

    def hook(self, event: str, fields: dict) -> None:
        payload = {**self.request.payload, "hook_event_name": event, **fields}
        key = {"Notification": "notification_type", "SessionStart": "source"}.get(
            event, "tool_name"
        )
        field = payload.get(key)
        run_hooks(self.request.hooks, event, payload, field if isinstance(field, str) else "")

    def ready(self) -> None:
        """SessionStart; input that arrives from then until READY s after it is dropped."""
        stdin = self.request.stdin
        stdin.keep()
        self.hook("SessionStart", {"source": self.request.source})
        stdin.drop(time.monotonic() + READY)

    def turn(self, text: str, steps: list) -> None:
        self.hook("UserPromptSubmit", {"prompt": text})
        self.request.keep("user", text)
        last = ""
        for k, step in enumerate(steps):
            kind = "text" if isinstance(step, str) else step["kind"]
            if kind == "text":
                print(step, flush=True)
                self.request.keep("assistant", [{"type": "text", "text": step}])
                last = step
            elif kind == "tool":
                self.request.keep(
                    "assistant",
                    [{"type": "tool_use", "id": f"toolu_{k}", "name": step["name"], "input": {}}],
                )
                self.hook("PostToolUse", {"tool_name": step["name"]})
            elif kind == "hook":
                self.hook(
                    step["event"],
                    {key: value for key, value in step.items() if key not in ("kind", "event")},
                )
            elif kind == "sleep":
                time.sleep(step["seconds"])
            elif kind == "queued":
                last = self.queued(k, step["answer"], prompted=step.get("prompted", False))
            elif kind == "notice":
                queued = {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "content": step["text"],
                }
                self.request.record(queued)
                self.joined(step["text"], "task-notification")
            elif kind == "pause":
                until = time.monotonic() + HANG_CAP
                while not os.path.exists(step["file"]) and time.monotonic() < until:
                    time.sleep(0.05)
            elif kind == "input":
                self.request.stdin.arrival()
            elif kind == "exit":
                sys.exit(self.request.scenario["exit"])
            elif kind == "recall":
                last = first_prompt(self.request.payload["transcript_path"])
                print(last, flush=True)
                self.request.keep("assistant", [{"type": "text", "text": last}])
            else:
                print(step["text"], flush=True)
                if kind == "limit":
                    error = {"isApiErrorMessage": True, "error": "rate_limit"}
                    self.request.keep(
                        "assistant", [{"type": "text", "text": step["text"]}], **error
                    )
                    self.hook(
                        "StopFailure",
                        {"error": "rate_limit", "last_assistant_message": step["text"]},
                    )
                    return
                self.answer()
        self.hook(
            "Stop",
            {
                "stop_hook_active": False,
                "last_assistant_message": last,
                "background_tasks": [{}] * self.request.scenario["background"],
            },
        )

    def queued(self, k: int, answer: str, *, prompted: bool) -> str:
        request = self.request
        request.keep("assistant", [{"type": "tool_use", "id": f"toolu_{k}", "name": "Bash"}])
        line = request.stdin.line()
        request.record({"type": "queue-operation", "operation": "enqueue", "content": line})
        request.keep("user", [{"type": "tool_result", "tool_use_id": f"toolu_{k}"}])
        self.hook("PostToolUse", {"tool_name": "Bash"})
        if prompted:
            self.joined(line, "prompt")
        else:
            attachment = {"type": "queued_command", "prompt": line, "commandMode": "prompt"}
            request.record({"type": "attachment", "attachment": attachment})
        print(answer, flush=True)
        request.keep("assistant", [{"type": "text", "text": answer}])
        return answer

    def joined(self, text: str, mode: str) -> None:
        """`text` joined to the running turn with its UserPromptSubmit, as Claude Code 2.1.312."""
        self.hook("UserPromptSubmit", {"prompt": text})
        attachment = {"type": "queued_command", "prompt": text, "commandMode": mode}
        self.request.record({"type": "attachment", "attachment": attachment})

    def answer(self) -> None:
        stdin = self.request.stdin
        stdin.dialog = True
        try:
            stdin.line()
        finally:
            stdin.dialog = False
        self.ready()

    def run(self):
        try:
            if self.request.scenario["dialog"] is None:
                self.ready()
            else:
                print(self.request.scenario["dialog"], flush=True)
                self.answer()  # SessionStart follows its answer
            prompt = self.request.prompt or self.request.stdin.line()
            for k in itertools.count():
                self.turn(prompt, self.request.turns[k] if k < len(self.request.turns) else [])
                prompt = self.request.stdin.line()
        except EOFError:
            return self.request.scenario["exit"]
        except Stranded:
            return 1


def invocation(argv: list[str]):
    parent = os.getppid()
    s = scenario()
    if s["log"]:
        with open(s["log"], "a", encoding="utf-8") as f:
            config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
            entry = {"pid": os.getpid(), "argv": argv, "cwd": os.getcwd(), "config_dir": config_dir}
            f.write(json.dumps(entry) + "\n")
    a = parse(argv)
    turns = [s["steps"]] if a.print else [s["steps"], *s["turns"]]
    if not all(_step(step, tui=not a.print) for steps in turns for step in steps):
        sys.exit("fake_claude.py: bad scenario steps")
    return (parent, s, a, turns)


def quiet_controls() -> None:
    """A terminal stdin echoes no ^[ for a paste's bracket, as Claude's input box shows none."""
    if os.isatty(0):
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHOCTL
        termios.tcsetattr(0, termios.TCSANOW, attrs)


def first_prompt(path: str) -> str:
    """The text of the first user prompt `path` keeps, "" when none."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            content = record.get("message", {}).get("content")
            if record.get("type") == "user" and isinstance(content, str):
                return content
    return ""


@dataclass(frozen=True)
class Transcript:
    sid: str
    cwd: str
    path: str

    def say(self, record: dict) -> None:
        print(json.dumps({**record, "session_id": self.sid}), flush=True)

    def record(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**record, "sessionId": self.sid, "cwd": self.cwd}) + "\n")

    def keep(self, role: str, content, **extra) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": role,
                        "sessionId": self.sid,
                        "cwd": self.cwd,
                        "message": {"role": role, "content": content},
                        **extra,
                    }
                )
                + "\n"
            )

    def assistant(self, content: list, **extra) -> None:
        self.keep("assistant", content)
        self.say(
            {"type": "assistant", "message": {"role": "assistant", "content": content}, **extra}
        )


def printed(s, a, journal, parent):
    last = ""
    journal.say({"type": "system", "subtype": "init", "cwd": journal.cwd})
    if s["rate_limit"] is not None:
        journal.say({"type": "rate_limit_event", "rate_limit_info": s["rate_limit"]})
    journal.keep("user", a.prompt)
    steps = s["resume"] if a.resume and s["resume"] is not None else s["steps"]
    for k, step in enumerate(steps):
        if isinstance(step, str):
            last = step
            journal.assistant([{"type": "text", "text": step}])
        elif step["kind"] == "tool":
            journal.assistant(
                [{"type": "tool_use", "id": f"toolu_{k}", "name": step["name"], "input": {}}]
            )
        elif step["kind"] == "write":
            os.makedirs(os.path.dirname(os.path.abspath(step["path"])), exist_ok=True)
            with open(step["path"], "w", encoding="utf-8") as f:
                f.write(step["text"])
        elif step["kind"] == "run":
            try:
                done = subprocess.run(
                    step["argv"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                ran = {"returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
            except (OSError, subprocess.TimeoutExpired) as e:
                ran = {"returncode": None, "stdout": "", "stderr": str(e)}
            os.makedirs(os.path.dirname(os.path.abspath(step["path"])), exist_ok=True)
            with open(step["path"], "w", encoding="utf-8") as f:
                json.dump(ran, f)
        elif step["kind"] == "git":
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "rev-parse",
                    "--show-toplevel",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            last = json.dumps({"returncode": result.returncode, "stdout": result.stdout.strip()})
            journal.assistant([{"type": "text", "text": last}])
        else:
            resets = {} if step["resets_at"] is None else {"resetsAt": step["resets_at"]}
            journal.say(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        **resets,
                    },
                }
            )
            last = LIMIT
            journal.assistant([{"type": "text", "text": LIMIT}], error="rate_limit")
    if s["hang"]:
        waited(lambda: False, parent)
        return 1
    ok = s["exit"] == 0
    journal.say(
        {
            "type": "result",
            "subtype": "success" if ok else "error_during_execution",
            "is_error": not ok,
            **({"result": last} if ok else {}),
        }
    )
    return s["exit"]


def main(argv: list[str]) -> int:
    parent, s, a, turns = invocation(argv)
    if a.print and a.prompt == "/usage":
        usage = s["usage"]
        if isinstance(usage, dict):
            usage = usage.get(os.environ.get("CLAUDE_CONFIG_DIR", ""), DEFAULTS["usage"])
        print(usage, end="", flush=True)
        return s["exit"]
    sid, cwd = (a.resume or a.session_id or str(uuid.uuid4()), os.getcwd())
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    project = re.sub("[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    transcript = os.path.join(config, "projects", project, f"{sid}.jsonl")
    if a.resume and (not os.path.isfile(transcript)):
        sys.exit(f"No conversation found with session ID: {sid}")
    gate = s["gate"]
    if (
        gate is not None
        and (a.session_id or a.resume)
        and (not waited(lambda: os.path.exists(gate), parent))
    ):
        print(f"fake_claude.py: gate {gate}: never created", file=sys.stderr, flush=True)
        return 1
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    journal = Transcript(sid, cwd, transcript)
    if not a.print:
        quiet_controls()
        return InteractiveSession(
            InteractiveRequest(
                scenario=s,
                turns=turns,
                hooks=a.hooks,
                payload={"session_id": sid, "transcript_path": transcript, "cwd": cwd},
                keep=journal.keep,
                record=journal.record,
                stdin=Stdin(parent, os.environ[ENV], s["commands"]),
                prompt=a.prompt,
                source="resume" if a.resume else "startup",
            )
        ).run()
    return printed(s, a, journal, parent)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
