"""The layout of mini-op's project runbooks in .mini-op/runbooks/ (WDRC-108 FR-43, FR-211, FR-213,
FR-214, FR-218). Failures name path:line only, never the file's text.
"""

import os
import re
from pathlib import Path

from tests.case import REPO, Case

RUNBOOKS = Path(REPO, ".mini-op/runbooks")
SCRIPTS = {
    "install-ensemble": {"install-test", "storekey-stdin"},
    "clean-ensemble": {"clean-test"},
    "test-accounts": {"importkey", "lq"},
}
DATA = {"install-ensemble": {"strata.conf.example", "test-board.toml"}}
NAMES = {"install-ensemble", "clean-ensemble", "manager-on-mini", "test-accounts", "gotchas"}
SECRET_READERS = (
    "install-ensemble/storekey-stdin",
    "test-accounts/importkey",
    "test-accounts/lq",
)
RETIRED = (
    "~/apm/runbooks/",
    "/run/secrets/",
    "status.md",
    "${0:A:h}/../..",
    "windroc/ensemble/",
    "/home/agent/keychain-backup",
)
APM = re.compile(r"~/apm/\S*")
APM_OK = re.compile(r"~/apm/(?:bin/[A-Za-z0-9_-]+|lib/[A-Za-z0-9_-]+\.zsh)(?![\w/.-])")
TOOLS = re.compile(r"(?<![\w/-])(linearlogin|maillink)(?![\w-])")
PROJECT = re.compile(r"~/mini-op-projects/ensemble/([^/\s`'\"]+)/")


def files():
    return sorted(path for path in RUNBOOKS.rglob("*") if path.is_file())


def lines(path):
    rel = path.relative_to(REPO).as_posix()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        yield f"{rel}:{number}", line


class Layout(Case):
    def test_every_runbook_has_when(self):
        books = {path.stem: path for path in RUNBOOKS.glob("*.md")}
        self.assertEqual(set(books), NAMES)
        bad = [
            name
            for name, path in books.items()
            if not re.match(r"---\nwhen: \S[^\n]*\n---\n", path.read_text())
        ]
        self.assertEqual(bad, [])

    def test_script_dirs(self):
        dirs = {path.name for path in RUNBOOKS.iterdir() if path.is_dir()}
        self.assertEqual(dirs, set(SCRIPTS))
        for name, scripts in SCRIPTS.items():
            with self.subTest(name=name):
                found = {path.name for path in (RUNBOOKS / name).iterdir()}
                self.assertEqual(found, scripts | DATA.get(name, set()))
                for script in scripts:
                    path = RUNBOOKS / name / script
                    self.assertTrue(os.access(path, os.X_OK), script)
                    self.assertTrue(path.read_text().startswith("#!/usr/bin/env zsh\n"), script)
                for data in DATA.get(name, ()):
                    self.assertFalse(os.access(RUNBOOKS / name / data, os.X_OK), data)

    def test_no_retired_paths(self):
        found = [
            where
            for path in files()
            for where, line in lines(path)
            if any(r in line for r in RETIRED)
        ]
        self.assertEqual(found, [])
        for path in RUNBOOKS.glob("*.md"):
            with self.subTest(runbook=path.name):
                other = [
                    where
                    for where, line in lines(path)
                    if any(dir_ != path.stem for dir_ in PROJECT.findall(line))
                ]
                self.assertEqual(other, [])

    def test_secrets_come_only_from_the_env_secret_files(self):
        allowed = {f"/run/ensemble/secrets/env/{name}" for name in ("mini-password", "strata-key")}
        found = [
            where
            for path in files()
            for where, line in lines(path)
            if any(match not in allowed for match in re.findall(r"/run/[\w./-]*[\w-]", line))
        ]
        self.assertEqual(found, [])

    def test_apm_paths_allowlisted(self):
        found = []
        for path in files():
            for where, line in lines(path):
                apm = [m.start() for m in APM.finditer(line) if not APM_OK.match(line, m.start())]
                tools = [
                    m.start()
                    for m in TOOLS.finditer(line)
                    if not line[: m.start()].endswith("~/apm/bin/")
                ]
                found += [where] * bool(apm or tools)
        self.assertEqual(found, [])

    def test_downloads_and_strata_conf(self):
        for script in ("install-ensemble/install-test", "clean-ensemble/clean-test"):
            text = (RUNBOOKS / script).read_text()
            self.assertIn(
                "https://raw.githubusercontent.com/windroc-labs/ensemble/$ref/scripts/", text
            )
        install = (RUNBOOKS / "install-ensemble/install-test").read_text()
        self.assertIn("conf=${STRATA_CONF:-$HOME/.mini-op-local/ensemble/strata.conf}", install)
        self.assertNotIn("${0:A:h}", install)
        example = (RUNBOOKS / "install-ensemble/strata.conf.example").read_text().splitlines()
        settings = [line for line in example if line and not line.startswith("#")]
        self.assertEqual(settings, ["url=", "model=", "context="])

    def test_strata_key_route(self):
        text = (RUNBOOKS / "install-ensemble.md").read_text()
        self.assertIn(
            "{ cat /run/ensemble/secrets/env/mini-password; printf '\\n'; "
            "cat /run/ensemble/secrets/env/strata-key; } | ssh mini "
            "'~/mini-op-projects/ensemble/install-ensemble/storekey-stdin ",
            text,
        )
        found = [
            where
            for path in RUNBOOKS.glob("*.md")
            for where, line in lines(path)
            if re.search(r"\bsecurity\b[^|\n]*\s-w\b", line)
        ]
        self.assertEqual(found, [])

    def test_no_secret_env(self):
        found = []
        for rel in SECRET_READERS:
            for where, line in lines(RUNBOOKS / rel):
                code = line.split("#", 1)[0] if not line.startswith("#!") else ""
                if re.search(r"\bexport\b", code):
                    found.append(where)
                for call in re.finditer(r"\bkeychain_(?:read|has)\b((?:\s+[^\s|;&)]+)*)", code):
                    args = call[1].split()[:2]
                    if any(not re.fullmatch(r'"\$\w+"', arg) for arg in args):
                        found.append(where)
                if re.search(r"(?<![|\w-])security\b(?! -i)[^|\n]*\s-[wp]\b", code):
                    found.append(where)
        self.assertEqual(found, [])
        usage = (RUNBOOKS / "test-accounts/lq").read_text()
        self.assertIn("lq <service> <account> <json-body-file>", usage)

    def test_lq_sends_the_manager_app_token(self):
        text = (RUNBOOKS / "test-accounts/lq").read_text()
        self.assertIn("linear-app-manager-token", text)
        self.assertIn("access_token", text)
        self.assertIn("Authorization: Bearer ", text)
