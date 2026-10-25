"""delegate/engine/container.py: the input's Reference `Repo:` as a github.com owner/name, a local
clone's from its origin read with GUARD; ENSEMBLE_USERS; the Docker Host test.
"""

import json
import os

from ensemble.delegate import ConfigError
from ensemble.delegate.engine import container
from ensemble.utils import GUARD
from tests.delegate.plans import Base
from tests.delegate.repo import fail, ok


def origin(url, code=0):
    calls = []

    def run(argv, _timeout):
        calls.append(argv)
        return fail("fatal: no remote") if code else ok(url)

    return run, calls


def refuse(argv, _timeout):
    raise AssertionError(f"ran {argv}")


class ReferenceRepo(Base):
    def test_only_the_leading_reference_blocks_repo_line_counts(self):
        for text, want in (
            ("Reference: TASK-1\nRepo: acme/ensemble\nCheckout: TASK-1\n\nBody", "acme/ensemble"),
            ("\n\nReference: TASK-1\nRepo: github.com/Acme/x.y\n", "Acme/x.y"),
            ("Reference: TASK-1\nRepo: https://github.com/acme/ensemble.git\n", "acme/ensemble"),
            ("Reference: TASK-1\nTitle: t\n\nRepo: acme/other\n", None),
            ("Reference: TASK-1\nRepo: acme/a\n\n## Brief\nRepo: acme/other\n", "acme/a"),
            ("Brief\nRepo: acme/ensemble\n", None),
            ("Repo: acme/ensemble\n", None),
            ("Reference: TASK-1\nRepo: ghe.example.com/acme/ensemble\n", None),
            ("Reference: TASK-1\nRepo: not a repo\n", None),
            ("Reference: TASK-1\nRepo: acme/a\nRepo: acme/a\n", "acme/a"),
            ("", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(container.reference_repo(text, run=refuse), want)

    def test_two_different_repo_lines_are_refused(self):
        with self.assertRaises(ConfigError) as cm:
            container.reference_repo("Reference: T\nRepo: acme/a\nRepo: acme/b\n", run=refuse)
        self.assertIn("Repo:", str(cm.exception))

    def test_a_local_clone_is_named_by_its_origin_read_with_guard(self):
        clone = os.path.join(self.tmp, "clone")
        os.mkdir(clone)
        for url, want in (
            ("git@github.com:acme/ensemble.git\n", "acme/ensemble"),
            ("https://github.com/acme/ensemble\n", "acme/ensemble"),
            ("git@ghe.example.com:acme/ensemble.git\n", None),
            ("/srv/plain.git\n", None),
        ):
            with self.subTest(url=url):
                run, calls = origin(url)
                got = container.reference_repo(f"Reference: T\nRepo: {clone}\n", run=run)
                self.assertEqual(got, want)
                self.assertEqual(
                    calls, [["git", *GUARD, "-C", clone, "remote", "get-url", "origin"]]
                )

    def test_a_local_path_without_an_origin_is_none(self):
        run, _ = origin("", code=2)
        self.assertIsNone(container.reference_repo("Reference: T\nRepo: ~/gone\n", run=run))


class UsersEnv(Base):
    def test_the_users_without_bots_as_a_json_array(self):
        got = container.users_env(["octocat", "ensemble-bot[bot]", 'q"uote'])
        self.assertEqual(got, {"ENSEMBLE_USERS": '["octocat", "q\\"uote"]'})
        self.assertEqual(json.loads(got["ENSEMBLE_USERS"]), ["octocat", 'q"uote'])
        for users in ("octocat", [1]):
            with self.subTest(users=users), self.assertRaises(ConfigError):
                container.users_env(users)

    def test_a_docker_run_whose_reference_names_two_repos_is_refused_before_any_write(self):
        text = "Reference: T\nRepo: acme/a\nRepo: acme/b\n\nResearch X."
        got, _ = self.planned(input=text)
        self.assertIsNone(got.repo)
        self.driver.docker = True
        self.assertIn("Repo:", self.refused(input=text, mode="tui", workdir=self.tmp + "/w2"))
        self.assertFalse(os.path.exists(self.tmp + "/w2"))


class OnDocker(Base):
    def test_the_agents_host_is_docker_by_its_implementation(self):
        hosts = {
            "default": "tmux",
            "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"},
            "box": {"implementation": "ensemble.session.hosts.docker.host:DockerHost"},
        }
        for host, want in ((None, False), ("tmux", False), ("box", True), ("absent", False)):
            with self.subTest(host=host):
                self.assertIs(container.on_docker({"hosts": hosts}, host), want)
        self.assertTrue(container.on_docker({"hosts": hosts | {"default": "box"}}, None))
        with self.assertRaises(ConfigError):
            container.on_docker({"hosts": {"default": "x"}}, None)
