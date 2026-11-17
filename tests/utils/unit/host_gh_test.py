"""The host gh wrapper's decisions, with the broker, gh and exec faked."""

import json
import os
import re
import signal
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.utils import host_gh
from tests.utils.case import REPO, Case
from tests.utils.sandbox import scratch


def done(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def reply(expires: float, **over) -> str:
    stamp = datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"version": 1, "account": "windroc-labs", "expires_at": stamp, **over})


class Base(Case):
    def setUp(self):
        self.root = scratch(self, real=False)
        self.state = os.path.join(self.root, "host-gh")
        os.mkdir(self.state, 0o700)
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.real = self.executable(self.bin, "gh")
        self.wrapper = self.executable(os.path.join(self.root, "ensemble-bin"), "gh")
        self.home = os.path.join(self.root, "home")
        self.broker = os.path.join(self.root, "ensemble-broker")

    def executable(self, folder, name):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def forget(self, path):
        if os.path.exists(path):
            os.remove(path)

    def stamp(self, ago):
        path = os.path.join(self.state, "refreshed")
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, (time.time() - ago, time.time() - ago))

    def runs(self, broker=None, version="2.102.0"):
        """A subprocess.run answering gh --version and the broker; records each argv."""
        calls = []

        def run(argv, **_):
            calls.append(list(argv))
            if argv[1:] == ["--version"]:
                return done(stdout=f"gh version {version} (2026-09-30)\n")
            if argv[0] == self.broker:
                return (
                    broker()
                    if callable(broker)
                    else broker or done(stdout=reply(time.time() + 3600))
                )
            raise AssertionError(argv)

        return calls, mock.patch.object(host_gh.subprocess, "run", run)

    def broker_calls(self, calls):
        return [c for c in calls if c[0] == self.broker]


class FindRealGh(Base):
    def test_the_first_gh_in_an_absolute_entry_that_is_not_the_wrapper(self):
        link = os.path.join(self.root, "link")
        os.mkdir(link)
        os.symlink(self.wrapper, os.path.join(link, "gh"))
        relative = self.executable(os.path.join(self.root, "rel"), "gh")
        path = os.pathsep.join(
            [
                "",
                "rel",
                os.path.dirname(self.wrapper),
                link,
                os.path.join(self.root, "none"),
                self.bin,
            ]
        )
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        self.assertTrue(os.access(os.path.join("rel", "gh"), os.X_OK), relative)
        self.assertEqual(host_gh.find_real_gh(path, self.wrapper), self.real)

    def test_none_when_only_the_wrapper_is_on_path(self):
        self.assertIsNone(host_gh.find_real_gh(os.path.dirname(self.wrapper), self.wrapper))


class Insecure(Base):
    def hosts(self, folder, text):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "hosts.yml"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_an_oauth_token_in_the_github_com_block_is_insecure(self):
        plain = "github.com:\n    users:\n        bot[bot]:\n            oauth_token: x\n    user: bot[bot]\n"
        keyring = "github.com:\n    users:\n        bot[bot]:\n    user: bot[bot]\n"
        other = "ghe.example:\n    oauth_token: x\ngithub.com:\n    user: me\n"
        default = os.path.join(self.home, ".config", "gh")
        for label, environ, folder, text, want in (
            ("plain text", {"HOME": self.home}, default, plain, True),
            ("keyring", {"HOME": self.home}, default, keyring, False),
            ("another host", {"HOME": self.home}, default, other, False),
            (
                "GH_CONFIG_DIR",
                {"HOME": "/none", "GH_CONFIG_DIR": self.root + "/c"},
                self.root + "/c",
                plain,
                True,
            ),
            (
                "XDG_CONFIG_HOME",
                {"HOME": "/none", "XDG_CONFIG_HOME": self.root + "/x"},
                self.root + "/x/gh",
                plain,
                True,
            ),
        ):
            with self.subTest(label):
                self.hosts(folder, text)
                self.assertIs(host_gh.insecure(environ), want)
        self.assertFalse(host_gh.insecure({"HOME": self.root + "/nobody"}))


class Refusal(Base):
    def test_token_variables_insecure_storage_and_an_unverified_gh_are_refused(self):
        hosts = os.path.join(self.home, ".config", "gh")
        os.makedirs(hosts)
        environ = {"HOME": self.home}
        for label, extra, version, want in (
            ("GH_TOKEN", {"GH_TOKEN": "x"}, "2.102.0", "GH_TOKEN is set"),
            ("GITHUB_TOKEN", {"GITHUB_TOKEN": "x"}, "2.102.0", "GITHUB_TOKEN is set"),
            ("another gh", {}, "2.103.0", "gh 2.103.0 is not 2.102.0"),
        ):
            with self.subTest(label):
                self.forget(os.path.join(self.state, "gh-version"))
                _, run = self.runs(version=version)
                with run:
                    self.assertIn(
                        want, host_gh.refusal(self.real, self.state, {**environ, **extra}) or ""
                    )
        with open(os.path.join(hosts, "hosts.yml"), "w", encoding="utf-8") as f:
            f.write("github.com:\n    oauth_token: x\n")
        _, run = self.runs()
        with run:
            self.assertIn("plain text", host_gh.refusal(self.real, self.state, environ) or "")

    def test_an_empty_token_variable_and_the_verified_gh_pass(self):
        _, run = self.runs()
        with run:
            self.assertIsNone(
                host_gh.refusal(self.real, self.state, {"HOME": self.home, "GH_TOKEN": ""})
            )

    def test_the_version_is_read_once_per_gh_file(self):
        calls, run = self.runs()
        with run:
            for _ in range(3):
                self.assertEqual(host_gh.gh_version(self.real, self.state), "2.102.0")
            with open(self.real, "a", encoding="utf-8") as f:
                f.write("# upgraded\n")
            host_gh.gh_version(self.real, self.state)
        self.assertEqual(calls, [[self.real, "--version"]] * 2)


class Refresh(Base):
    def test_due_only_after_fifty_minutes(self):
        for ago, due in ((None, True), (2999, False), (3001, True)):
            with self.subTest(ago=ago):
                if ago is None:
                    self.forget(os.path.join(self.state, "refreshed"))
                else:
                    self.stamp(ago)
                calls, run = self.runs()
                with run:
                    self.assertIsNone(host_gh.ensure_fresh(self.real, self.state, self.broker))
                self.assertEqual(
                    self.broker_calls(calls),
                    [[self.broker, "host-gh-login", "--gh", self.real]] if due else [],
                )

    def test_the_stamp_follows_the_tokens_expiry(self):
        expires = time.time() + 3000
        _, run = self.runs(done(stdout=reply(expires)))
        with run:
            self.assertIsNone(host_gh.ensure_fresh(self.real, self.state, self.broker))
        mtime = os.stat(os.path.join(self.state, "refreshed")).st_mtime
        self.assertAlmostEqual(mtime, int(expires) - 3600, delta=1)
        self.assertGreaterEqual(host_gh.age(self.state), 599)

    def test_a_failed_or_unusable_refresh_names_why_and_keeps_the_stamp(self):
        for label, answer, want in (
            (
                "broker error",
                done(1, stderr="noise\nensemble-broker: login_required: run x\n"),
                "login_required: run x",
            ),
            ("silent failure", done(3), "exited 3"),
            ("not JSON", done(stdout="{"), "unusable"),
            ("a list", done(stdout="[]"), "unusable"),
            ("a token", done(stdout=reply(time.time() + 3600, token="ghs_x")), "unusable"),
            ("bad expiry", done(stdout=reply(0, expires_at="soon")), "unusable"),
            ("too long", done(stdout=reply(time.time() + 3600) + " " * 5000), "unusable"),
        ):
            with self.subTest(label):
                self.forget(os.path.join(self.state, "refreshed"))
                _, run = self.runs(answer)
                with run:
                    failed = host_gh.ensure_fresh(self.real, self.state, self.broker)
                self.assertIn(want, failed or "")
                self.assertNotIn("ghs_", failed or "")
                self.assertFalse(os.path.exists(os.path.join(self.state, "refreshed")))

    def test_concurrent_wrappers_make_one_broker_call(self):
        def slow():
            time.sleep(0.3)
            return done(stdout=reply(time.time() + 3600))

        calls, run = self.runs(slow)
        results = []
        with run:
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        host_gh.ensure_fresh(self.real, self.state, self.broker)
                    )
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual((results, len(self.broker_calls(calls))), ([None] * 5, 1))

    def test_the_checker_refreshes_while_the_child_runs(self):
        self.stamp(0)
        calls, run = self.runs()
        stop = threading.Event()
        with run:
            checker = threading.Thread(
                target=host_gh.keep_fresh,
                args=(self.real, self.state, self.broker, stop),
                kwargs={"every": 0.05, "after": 0.2},
            )
            checker.start()
            time.sleep(0.5)
            stop.set()
            checker.join(5)
        self.assertGreaterEqual(len(self.broker_calls(calls)), 1)
        self.assertFalse(checker.is_alive())


class Main(Base):
    def main(self, *argv, state=None, environ=None, polls=(0,)):
        """main's exit code or message. `self.popen` is the faked child's Popen; its poll gives
        `polls` in turn, a signal's byte reaching the wakeup fd before each None."""
        env = {
            "PATH": os.pathsep.join([os.path.dirname(self.wrapper), self.bin]),
            "HOME": self.home,
        }
        self.wakeup = mock.Mock()
        codes = iter(polls)

        def poll():
            if (code := next(codes)) is None:
                os.write(self.wakeup.call_args.args[0], bytes([signal.SIGTERM]))
            return code

        self.popen = mock.Mock(return_value=mock.Mock(poll=poll))
        with (
            mock.patch.dict(os.environ, environ or env, clear=True),
            mock.patch.object(host_gh.subprocess, "Popen", self.popen),
            mock.patch.object(host_gh.signal, "signal") as self.signal,
            mock.patch.object(host_gh.signal, "set_wakeup_fd", self.wakeup),
            mock.patch.object(host_gh.os, "kill") as self.kill,
            self.assertRaises(SystemExit) as raised,
        ):
            host_gh.main(
                list(argv), state=state or self.state, wrapper=self.wrapper, broker=self.broker
            )
        return raised.exception.code

    def test_without_the_state_dir_gh_runs_unchanged(self):
        missing = os.path.join(self.root, "off")
        calls, run = self.runs()
        execv = mock.patch.object(host_gh.os, "execv", side_effect=SystemExit(0))
        with run, execv as execv:
            self.main("pr", "view", "1", state=missing)
        execv.assert_called_once_with(self.real, ["gh", "pr", "view", "1"])
        self.assertEqual((calls, os.path.exists(missing), self.popen.called), ([], False, False))

    def test_no_real_gh_exits_with_its_reason(self):
        code = self.main("pr", environ={"PATH": os.path.dirname(self.wrapper)})
        self.assertEqual(code, "gh (ensemble): no gh on PATH besides the ensemble wrapper")

    def test_a_refused_environment_runs_no_broker_and_no_child(self):
        calls, run = self.runs()
        with run:
            code = self.main("pr", environ={"PATH": self.bin, "HOME": self.home, "GH_TOKEN": "x"})
        self.assertTrue(str(code).startswith("gh (ensemble): GH_TOKEN is set"))
        self.assertEqual((self.broker_calls(calls), self.popen.called), ([], False))

    def test_a_failed_refresh_stops_gh_only_once_the_token_expired(self):
        failing = done(1, stderr="ensemble-broker: github_failed: down\n")
        for ago, code in (
            (3100, 0),
            (3700, "gh (ensemble): token refresh failed: ensemble-broker: github_failed: down"),
        ):
            with self.subTest(ago=ago):
                self.stamp(ago)
                _, run = self.runs(failing)
                with run:
                    self.assertEqual(self.main("pr"), code)
                self.assertIs(self.popen.called, code == 0)

    def test_a_fresh_token_runs_the_child_without_the_broker(self):
        self.stamp(10)
        calls, run = self.runs()
        with run:
            self.assertEqual(self.main("run", "watch", "7"), 0)
        self.popen.assert_called_once_with(["gh", "run", "watch", "7"], executable=self.real)
        self.assertEqual(self.broker_calls(calls), [])

    def test_each_wakeup_polls_gh_again_until_it_exits(self):
        self.stamp(10)
        _, run = self.runs()
        with run:
            self.assertEqual(self.main("run", "watch", polls=(None, None, 3)), 3)
        self.assertFalse(os.get_blocking(self.wakeup.call_args.args[0]))

    def test_a_child_killed_by_a_signal_kills_the_wrapper_by_it(self):
        self.stamp(10)
        for signum, reset in ((signal.SIGTERM, [signal.SIGTERM]), (signal.SIGKILL, [])):
            with self.subTest(signum.name):
                _, run = self.runs()
                with run:
                    self.assertEqual(self.main("api", polls=(-signum,)), 128 + signum)
                self.kill.assert_called_once_with(os.getpid(), signum)
                defaults = [
                    c.args[0] for c in self.signal.call_args_list if c.args[1] == signal.SIG_DFL
                ]
                self.assertEqual(defaults, reset)


class Verified(Case):
    def test_the_verified_gh_is_the_one_the_live_runbook_checks(self):
        """Raising VERIFIED_GH needs the live runbook rerun with that gh: they name one version."""
        with open(
            os.path.join(REPO, ".mini-op/runbooks/host-gh-refresh.md"), encoding="utf-8"
        ) as f:
            runbook = f.read()
        pinned = re.search(r"`gh --version` printing `gh version ([^ `]+)", runbook)
        self.assertEqual(pinned and pinned[1], host_gh.VERIFIED_GH)
