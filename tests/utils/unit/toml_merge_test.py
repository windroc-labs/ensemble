"""A Docker config merges under the file the image's bootstrap wrote, the bootstrap's keys winning."""

import datetime
import io
import math
import os
import stat
import subprocess
import sys
import tomllib
from unittest import mock

from ensemble.utils import toml_merge
from tests.utils.case import Case
from tests.utils.sandbox import scratch

BOOTSTRAP = """cli_auth_credentials_store = "file"
check_for_update_on_startup = false
[projects."/work/a \\"b\\"\\n"]
trust_level = "trusted"
[plugins."p@m"]
enabled = true
"""


class DumpsTest(Case):
    def test_tomllib_reads_back_what_it_writes(self):
        utc = datetime.UTC
        for table in (
            {},
            {"a": 1, "b": -2.5, "c": True, "d": "x", "e": [1, 2], "f": []},
            {"s": 'q"\\\n\t\x00\x7f雪', "k y": 1, 'q"': 2, "": 3, "\x7f": 4},
            {"t": {"u": {"v": 1}, "w": 2}, "x": {}, "y": {"z": {}}},
            {"arr": [{"a": 1, "b": {"c": [1]}}, {}], "mixed": [[1], ["a"]]},
            {"dt": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=utc)},
            {"d": datetime.date(2026, 1, 2), "t": datetime.time(3, 4, 5), "n": 10**20},
            {"f": float("inf"), "g": float("-inf"), "h": 1e100, "i": 0.1},
        ):
            with self.subTest(table=table):
                self.assertEqual(tomllib.loads(toml_merge.dumps(table)), table)

    def test_nan_reads_back_as_nan(self):
        self.assertTrue(math.isnan(tomllib.loads(toml_merge.dumps({"n": math.nan}))["n"]))

    def test_a_bootstrap_key_with_quotes_and_a_newline_stays_one_key(self):
        table = tomllib.loads(BOOTSTRAP)
        self.assertEqual(tomllib.loads(toml_merge.dumps(table)), table)
        self.assertEqual(list(table["projects"]), ['/work/a "b"\n'])


class MergeTest(Case):
    def setUp(self):
        self.home = scratch(self)
        self.path = os.path.join(self.home, "config.toml")

    def run_main(self, text, argv=None):
        with mock.patch.object(sys, "stdin", io.StringIO(text)):
            return toml_merge.main([self.home, "config.toml"] if argv is None else argv)

    def bootstrap(self):
        with open(self.path, "w") as f:
            f.write(BOOTSTRAP)

    def merged(self):
        with open(self.path, "rb") as f:
            return tomllib.load(f)

    def test_the_bootstrap_file_wins_key_by_key(self):
        self.bootstrap()
        docker = """service_tier = "fast"
check_for_update_on_startup = true
[projects."/work/a \\"b\\"\\n"]
trust_level = "untrusted"
note = "kept"
[plugins."p@m"]
enabled = false
"""
        self.assertEqual(self.run_main(docker), 0)
        expected = tomllib.loads(BOOTSTRAP)
        expected["service_tier"] = "fast"
        expected["projects"]['/work/a "b"\n']["note"] = "kept"
        self.assertEqual(self.merged(), expected)

    def test_without_a_bootstrap_file_the_docker_config_is_the_file(self):
        self.assertEqual(self.run_main('model = "m"\n'), 0)
        self.assertEqual(self.merged(), {"model": "m"})

    def test_the_merged_file_is_a_private_regular_file_and_nothing_else_is_left(self):
        self.bootstrap()
        self.assertEqual(self.run_main("# only a comment\n"), 0)
        found = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(found.st_mode))
        self.assertEqual(stat.S_IMODE(found.st_mode), 0o600)
        self.assertEqual(os.listdir(self.home), ["config.toml"])
        self.assertEqual(self.merged(), tomllib.loads(BOOTSTRAP))

    def test_a_link_in_place_of_the_bootstrap_file_is_refused(self):
        target = os.path.join(scratch(self), "elsewhere.toml")
        with open(target, "w") as f:
            f.write("a = 1\n")
        os.symlink(target, self.path)
        with self.assertRaises(OSError):
            self.run_main("b = 2\n")
        with open(target) as f:
            self.assertEqual(f.read(), "a = 1\n")
        self.assertTrue(os.path.islink(self.path))

    def test_a_failed_replace_leaves_no_staged_file(self):
        self.bootstrap()
        with (
            mock.patch("os.replace", side_effect=OSError("full")),
            self.assertRaisesRegex(OSError, "full"),
        ):
            self.run_main("a = 1\n")
        self.assertEqual(os.listdir(self.home), ["config.toml"])
        self.assertEqual(self.merged(), tomllib.loads(BOOTSTRAP))

    def test_bad_arguments_or_toml_exit_2_and_write_nothing(self):
        for argv, text in (
            ([self.home], ""),
            ([self.home, "config.json"], ""),
            ([self.home, "../config.toml"], ""),
            ([self.home, ".config.toml"], ""),
            (["relative", "config.toml"], ""),
            (None, "not = = toml"),
        ):
            with self.subTest(argv=argv, text=text), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(self.run_main(text, argv), 2)
                self.assertEqual(os.listdir(self.home), [])

    def test_it_runs_as_a_script_on_stdin(self):
        script = os.path.abspath(toml_merge.__file__)
        done = subprocess.run(
            [sys.executable, "-I", script, self.home, "config.toml"],
            input='a = "雪"\n',
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.merged(), {"a": "雪"})
