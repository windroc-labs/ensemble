"""bin/ensemble-session as installed, run from an unrelated cwd with no PYTHONPATH (Q01): --help's
subcommands and each configured Host's display help; usage errors exit 2 on stderr alone and start
nothing; no default client (D1); the client sees the wrapper's bin dir first on PATH and gets
arguments with spaces verbatim; inside an agent session every start command exits 2. Its config
(Q02): a local overlay is honoured; a bad config or an unknown client exits 2 with the reason on
stderr before anything starts.
"""

import os
import re
import shlex

from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer import display as displays
from tests.session.case import REPO
from tests.session.cli_support import MANAGER, CliCase

SUBCOMMANDS = [
    "start",
    "restart",
    "resume",
    "reply",
    "status",
    "content",
    "show",
    "hide",
    "close",
    "ls",
    "send",
    "read",
    "tile",
    "hook",
    "client-resume",
    "resume-argv",
]
DISPLAY_HELP = "display (--display D), per configured Host:\n"
PROG = "ensemble-session"


def subcommands(help_text: str) -> list[str]:
    block = help_text.split("positional arguments:\n", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^    (\S+)", block, re.M)


class SessionCliCase(CliCase):
    def sessions(self) -> list[str]:
        result = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return sorted(result.stdout.split())

    def assert_nothing_started(self) -> None:
        self.assertEqual(self.sessions(), [MANAGER])
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "workers")))
        self.assertEqual(self.calls("w1"), [])

    def assert_refused(self, result, stderr: str) -> None:
        """Exit 2, stdout empty, stderr exactly `stderr`, nothing started."""
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertEqual(result.stderr, stderr)
        self.assert_nothing_started()

    def assert_usage_error(self, result, message: str) -> None:
        """argparse's refusal: its usage, then `message` as the last stderr line; exit 2."""
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertTrue(result.stderr.startswith(f"usage: {PROG}"), result.stderr)
        self.assertEqual(result.stderr.splitlines()[-1], message)
        self.assert_nothing_started()

    def start_w1(self):
        return self.session(
            "start", "w1", "--client", "claude", "--cwd", self.cwd, **self.scene("w1")
        )


class InsideAnAgentSessionTest(SessionCliCase):
    def test_each_start_command_exits_2_before_anything_starts_and_the_rest_run(self):
        refused = (
            f"{PROG}: refused inside an agent session ensemble started ($ENSEMBLE_AGENT_SESSION "
            "is set): only a client you start starts sessions and runs\n"
        )
        marked = {"ENSEMBLE_AGENT_SESSION": "1"}
        start = ("start", "w1", "--client", "claude", "--cwd", self.cwd)
        for argv in (start, ("restart", "w1"), ("resume", "w1"), ("client-resume", "claude", "x")):
            with self.subTest(argv=argv):
                self.assert_refused(self.session(*argv, **marked, **self.scene("w1")), refused)
        self.ok(self.session("--help", **marked))
        self.assertEqual(self.ok(self.session("ls", **marked)).stderr, "")


class HelpTest(SessionCliCase):
    ATTACH = False

    def elsewhere(self) -> str:
        path = os.path.join(self.root, "elsewhere")
        os.mkdir(path)
        return path

    def test_help_from_an_unrelated_cwd_lists_every_subcommand_and_the_tmux_display_help(self):
        self.assertNotIn("PYTHONPATH", self.env())
        result = self.ok(self.session("--help", cwd=self.elsewhere()))
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.startswith(f"usage: {PROG} [-h] COMMAND ..."), result.stdout)
        self.assertEqual(subcommands(result.stdout), SUBCOMMANDS)
        tmux = displays.display_help(TmuxHost("tmux"))
        self.assertTrue(tmux)
        self.assertTrue(
            result.stdout.endswith(f"{DISPLAY_HELP}  tmux: {tmux}\n  docker: {tmux}\n"),
            result.stdout,
        )

    def test_help_gives_a_second_configured_hosts_display_line_too(self):
        self.fake_host()
        result = self.ok(self.session("--help", cwd=self.elsewhere(), module=True))
        tmux = displays.display_help(TmuxHost("tmux"))
        self.assertEqual(subcommands(result.stdout), SUBCOMMANDS)
        self.assertTrue(
            result.stdout.endswith(
                f"{DISPLAY_HELP}  tmux: {tmux}\n  docker: {tmux}\n  fake: as given\n"
            ),
            result.stdout,
        )


class UsageTest(SessionCliCase):
    ATTACH = False

    def test_a_bad_command_line_or_no_client_d1_exits_2_on_stderr_alone_and_starts_nothing(self):
        for argv, scene, message in (
            (
                ("bogus", "w1"),
                {},
                f"{PROG}: error: argument COMMAND: invalid choice: 'bogus' (choose from "
                + ", ".join(f"'{cmd}'" for cmd in SUBCOMMANDS)
                + ")",
            ),
            (
                ("start",),
                {},
                f"{PROG} start: error: the following arguments are required: name, --client",
            ),
            (("read",), {}, f"{PROG} read: error: the following arguments are required: name"),
            (
                ("ls", "--display", "x", "--here"),
                {},
                f"{PROG} ls: error: argument --here: not allowed with argument --display",
            ),
            (
                ("start", "w1", "--cwd", self.cwd),
                self.scene("w1"),
                f"{PROG} start: error: the following arguments are required: --client",
            ),
        ):
            with self.subTest(argv=argv):
                self.assert_usage_error(self.session(*argv, **scene), message)

    def test_descendants_without_here_and_records_exits_2(self):
        for argv in (
            ("--descendants",),
            ("--here", "--descendants"),
            ("--records", "--descendants"),
        ):
            with self.subTest(argv=argv):
                result = self.session("ls", *argv)
                self.assert_refused(result, f"{PROG}: ls: --descendants needs --here --records\n")


class ClientEnvTest(SessionCliCase):
    def path_logging_claude(self) -> tuple[str, str]:
        """A bin dir whose `claude` appends its PATH to a log, then runs the fake; (dir, log)."""
        bin_dir = os.path.join(self.root, "path-bin")
        os.mkdir(bin_dir)
        log = os.path.join(self.root, "path.log")
        claude = os.path.join(bin_dir, "claude")
        with open(claude, "w", encoding="utf-8") as f:
            f.write(
                f"#!/bin/sh\nprintf '%s\\n' \"$PATH\" >> {shlex.quote(log)}\n"
                f'exec {shlex.quote(os.path.join(self.server.bin, "claude"))} "$@"\n'
            )
        os.chmod(claude, 0o755)
        return bin_dir, log

    def test_the_client_gets_the_wrappers_bin_first_on_path_and_spaced_arguments_verbatim(self):
        bin_dir, log = self.path_logging_claude()
        given = [p for p in self.env()["PATH"].split(os.pathsep) if p != os.path.join(REPO, "bin")]
        caller = os.pathsep.join((bin_dir, *given))
        record = self.record(
            self.start("w1", "--", "--model", "big  model", prompt="a b  c", PATH=caller)
        )
        self.assertEqual(record.detail, "claude")
        with open(log, encoding="utf-8") as f:
            (path,) = f.read().splitlines()
        self.assertEqual(
            path.split(os.pathsep), [os.path.join(REPO, "bin"), *caller.split(os.pathsep)]
        )
        (call,) = self.calls("w1")
        self.assertEqual(call["argv"][-4:], ["--model", "big  model", "--", "a b  c"])


class ConfigTest(SessionCliCase):
    ATTACH = False

    def test_a_local_overlay_sets_the_tmux_hosts_status_line(self):
        self.record(self.start("w0"))
        self.assertEqual(self.server.option("w0", "status"), "off")
        before = self.server.tmux("show-options", "-g").stdout
        keys = ("status", "status-left", "status-left-length")
        other = [self.server.option("w0", key) for key in keys]
        self.local_config("[session.hosts.tmux]\nstatus_line = true\n")
        name = "engineer-WDRC-281-502d679e"
        self.record(self.start(name))
        self.assertEqual(self.server.option(name, "status"), "on")
        self.assertEqual(self.server.option(name, "status-left-length"), str(len(name) + 3))
        self.assertEqual(self.server.tmux("show-options", "-g").stdout, before)
        self.assertEqual([self.server.option("w0", key) for key in keys], other)

    def test_a_bad_config_or_host_or_client_override_exits_2_with_its_reason_first(self):
        local = "config: ~/.ensemble/config.local.toml"
        for text, reason in (
            (
                "status_line = true\n",
                f"{local}: unknown key 'status_line': flat local configuration; no supplied "
                "layer has it; move it under its owning layer's section",
            ),
            ("[session]\nbogus = 1\n", f"{local}: unknown key 'bogus' in [session]"),
            (
                '[session.hosts]\ndefault = "nope"\n',
                "config: [hosts] default: 'nope' is not a Host here; want one of docker, tmux",
            ),
            ('[session.hosts.tmux]\nstatus_line = "yes"\n', "status_line: want true or false"),
            (
                '[session.viewer]\ngrid_retile = "never"\n',
                "grid_retile: want one of all, open-close, off",
            ),
            (
                "[session.hosts.tmux]\nworkers_per_column = 2\n",
                "unknown key 'workers_per_column' in [hosts.tmux]",
            ),
            (
                "[session.hosts.tmux]\nno_such_key = 2\n",
                "unknown key 'no_such_key' in [hosts.tmux]",
            ),
            (
                '[session.clients.claude]\nmodel = "opus"\n',
                "unknown key 'model' in ClaudeClient's config",
            ),
            (
                '[session.clients.claude]\nflags = "--x"\n',
                "[clients.claude].flags must be a list of strings",
            ),
        ):
            with self.subTest(config=text):
                self.local_config(text)
                self.assert_refused(self.start_w1(), f"{PROG}: {reason}\n")

    def test_malformed_toml_exits_2_with_its_reason_before_anything_starts(self):
        self.local_config("[session\n")
        result = self.start_w1()
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertRegex(result.stderr, rf"\A{PROG}: config: \S[^\n]*\(at line 1, column \d+\)\n\Z")
        self.assert_nothing_started()

    def test_an_unknown_client_exits_2_before_anything_starts(self):
        result = self.session("start", "w1", "--client", "nope", "--cwd", self.cwd)
        self.assert_refused(result, f"{PROG}: no [clients.nope] table in the configured clients\n")

    def test_an_unimportable_host_implementation_exits_2_before_anything_starts(self):
        self.local_config('[session.hosts.tmux]\nimplementation = "nope.mod:Nope"\n')
        result = self.start_w1()
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("[hosts.tmux].implementation", result.stderr)
        self.assert_nothing_started()
