import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid

from session.tests.case import PLUGIN_BIN, Case

COMMAND = os.path.join(PLUGIN_BIN, "ensemble-session")
WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}


def install(folder: str, text: str) -> str:
    os.mkdir(folder)
    path = os.path.join(folder, "tmux")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def state(root: str, events) -> dict:
    return {
        "client": "fake",
        "sid": str(uuid.uuid4()),
        "workdir": root,
        "cwd": root,
        "project": False,
        "transcript": "",
        "resume": "",
        "events": events,
        "words": dict(WORDS),
        "host_hook": {
            "module": "ensemble.session.hosts.tmux.hook",
            "state": {
                "executable": os.path.join(root, "bin", "tmux"),
                "target": "w1",
                "generation": uuid.uuid4().hex,
                "environment": {"TMUX_TMPDIR": root},
            },
            "roots": [],
        },
    }


def descriptor(module: str, state: dict | None = None, roots: tuple[str, ...] = ()) -> dict:
    """A session.json callback descriptor, as HookCallback.fields() writes it."""
    return {"module": module, "state": {} if state is None else state, "roots": list(roots)}


def identity(value: dict) -> str:
    return f"{value['sid']}\t{value['host_hook']['state']['generation']}"


def write_state(folder: str, value) -> None:
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    fd = os.open(os.path.join(folder, "session.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


PANE = "%3"
PROCESS = "4242 $3 %3 4343"  # its launch's @process: PANE
LINE = r"\A[0-9]{2}:[0-9]{2}:[0-9]{2} w1 %s\n\Z"  # one event line
TS = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z"
LOOKUP = ["display-message", "-p", "-t", "=w1:", "#{@process}\t#{@sid}\t#{@generation}"]
TMUX = """#!{python} -I
import json, os, sys
with open(os.environ["TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("TMUX_FAIL"):
    sys.exit("boom\\nagain")
if sys.argv[1] == "display-message":
    if "@process" in sys.argv[-1]:
        print("" if os.environ.get("TMUX_TARGET_ABSENT") else "{process}\\t" + os.environ["TMUX_TARGET_IDENTITY"])
    else:
        print("w1")
"""
DECOY = """#!{python} -I
open({log!r}, "a").close()
"""


class HookBase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        self.events = os.path.join(self.root, "events")
        os.close(os.open(self.events, os.O_WRONLY | os.O_CREAT, 0o600))
        self.tmux = install(
            os.path.join(self.root, "bin"), TMUX.format(python=sys.executable, process=PROCESS)
        )
        self.decoy = os.path.join(self.root, "decoy.log")
        install(
            os.path.join(self.root, "path"), DECOY.format(python=sys.executable, log=self.decoy)
        )
        self.log = os.path.join(self.root, "tmux.log")
        self.env = {
            "PATH": os.path.join(self.root, "path"),
            "TMUX_PANE": PANE,
            "TMUX_LOG": self.log,
            "TMUX_TMPDIR": self.root,
            "HOME": self.root,
        }
        self.state = state(self.root, self.events)
        self.env["TMUX_TARGET_IDENTITY"] = identity(self.state)
        write_state(self.dir, self.state)

    def hook(self, label, payload=None, args=None, **env):
        """Runs `ensemble-session hook DIR LABEL` (env: changes to self.env, None drops a key;
        args replaces DIR LABEL); checks exit 0 and no stdout; returns stderr.
        """
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps({} if payload is None else payload).encode()
        )
        environ = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        environ["PATH"] = os.pathsep.join(
            (environ.get("PATH", ""), os.path.dirname(sys.executable), "/usr/bin", "/bin")
        )
        res = subprocess.run(
            [COMMAND, "hook", *([self.dir, label] if args is None else args)],
            input=data,
            capture_output=True,
            env=environ,
            timeout=60,
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()

    def callback(self, module: str, state: dict | None = None) -> dict:
        """Its descriptor, self.root declared."""
        return descriptor(module, state, (self.root,))

    def assertError(self, err, text):
        """err is one stderr line holding text."""
        self.assertRegex(err, r"\Atui_hook: [^\n]*\n\Z")
        self.assertIn(text, err)

    def assertNothingWritten(self):
        self.assertFalse(os.path.lexists(self.hooks))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.lines(), "")

    def records(self):
        with open(self.hooks, "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(lines[-1], b"")
        return [json.loads(line) for line in lines[:-1]]

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def lines(self):
        with open(self.events) as f:
            return f.read()

    def reset(self):
        open(self.events, "w").close()
        if os.path.exists(self.log):
            os.unlink(self.log)


class MarkingCases:
    def test_a_failed_mark_is_one_stderr_line_after_the_record(self):
        self.assertError(self.hook("stop", TMUX_FAIL="1"), "tmux: boom again")
        os.unlink(self.events)
        self.assertError(self.hook("ask"), "events file")
        self.assertFalse(os.path.exists(self.events))
        self.assertEqual([r["label"] for r in self.records()], ["stop", "ask"])


class PayloadCases:
    def test_strs_over_4096_chars_are_cut_unless_the_word_is_done(self):
        long = "\u00e9" * 5000
        key = "k" * 5000
        payload = {"a": long, "b": [{"c": "y" * 4097}, "z" * 4096], "n": 5, key: "v"}
        cut = {"a": long[:4096], "b": [{"c": "y" * 4096}, "z" * 4096], "n": 5, key: "v"}
        for label in ("tool", "ask", "other", "stop"):
            self.assertEqual(self.hook(label, payload), "")
        self.assertEqual([r["payload"] for r in self.records()], [cut, cut, cut, payload])

    def test_bad_stdin_is_an_empty_payload_and_a_stderr_line(self):
        cases = (
            b"",
            b"not json",
            b"[1]",
            b'"text"',
            b"\xff\xfe\xfd",
            b"[" * 100000,
        )
        for data in cases:
            with self.subTest(data[:20]):
                self.assertError(self.hook("tool", data), "payload")
        self.assertEqual([r["payload"] for r in self.records()], [{}] * len(cases))


class RecordCases:
    def test_each_call_appends_one_record_line_ts_label_payload(self):
        payload = {"session_id": "s1", "text": "a\nb\u2028c\U0001f600", "n": [1, None, True, 2.5]}
        self.assertEqual(self.hook("stop", payload), "")
        self.assertEqual(self.hook("tool", {"tool_name": "Bash"}), "")
        records = self.records()
        self.assertEqual([list(r) for r in records], [["ts", "label", "payload"]] * 2)
        self.assertEqual(
            [(r["label"], r["payload"]) for r in records],
            [("stop", payload), ("tool", {"tool_name": "Bash"})],
        )
        for record in records:
            self.assertRegex(record["ts"], TS)
        self.assertEqual(stat.S_IMODE(os.stat(self.hooks).st_mode), 0o600)

    def test_each_word_marks_the_pane_done_and_blocked_with_an_event_line(self):
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                calls = [LOOKUP, ["set-option", "-t", PANE, "@state", word]]
                if word == "working":
                    self.assertEqual(self.lines(), "")
                else:
                    calls.append(["display-message", "-p", "-t", PANE, "#S"])
                    self.assertRegex(self.lines(), LINE % word)
                self.assertEqual(self.calls(), calls)
        self.assertEqual([r["label"] for r in self.records()], list(WORDS))

    def test_the_tmux_session_json_names_runs_never_one_on_path(self):
        self.assertEqual(self.hook("ask"), "")
        self.assertEqual(
            self.calls(),
            [
                LOOKUP,
                ["set-option", "-t", PANE, "@state", "blocked"],
                ["display-message", "-p", "-t", PANE, "#S"],
            ],
        )
        self.assertRegex(self.lines(), LINE % "blocked")
        self.assertFalse(os.path.exists(self.decoy))

    def test_a_label_without_a_word_is_recorded_and_marks_nothing(self):
        self.assertEqual(self.hook("other", {"x": 1}), "")
        self.assertEqual(
            [(r["label"], r["payload"]) for r in self.records()], [("other", {"x": 1})]
        )
        self.assertEqual(self.calls(), [])

    def test_no_events_file_marks_the_state_with_no_line(self):
        write_state(self.dir, {**self.state, "events": None})
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                self.assertEqual(self.calls(), [LOOKUP, ["set-option", "-t", PANE, "@state", word]])
        self.assertEqual(self.lines(), "")
        self.assertEqual(len(self.records()), 3)

    def test_target_lookup_works_without_client_pane_environment(self):
        for pane in (None, ""):
            self.assertEqual(self.hook("stop", TMUX_PANE=pane), "")
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(
            self.calls(),
            [
                LOOKUP,
                ["set-option", "-t", PANE, "@state", "done"],
                ["display-message", "-p", "-t", PANE, "#S"],
            ]
            * 2,
        )

    def test_missing_target_journals_without_marking(self):
        self.assertEqual(self.hook("stop", TMUX_TARGET_ABSENT="1"), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((self.calls(), self.lines()), ([LOOKUP], ""))
