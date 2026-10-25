import re
from pathlib import Path

from tests.case import REPO, Case

WORKFLOWS = Path(REPO, ".github/workflows")
STATICCHECK = "sh with-go-state.sh go run honnef.co/go/tools/cmd/staticcheck@v0.8.2 ./..."
ASSET = "ensemble-broker-darwin-arm64.tar.gz"


def job(workflow, name):
    text = (WORKFLOWS / workflow).read_text()
    found = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text)
    assert found, f"{workflow} has no job {name}"
    return found[1]


def steps(block):
    return dict(re.findall(r"(?ms)^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", block))


class BrokerJob(Case):
    def test_broker_job_runs_pinned_staticcheck_unswallowed(self):
        found = steps(job("test.yml", "broker"))
        names = list(found)
        for name in (
            "Gofmt",
            "Go vet",
            "Go test",
            "Go build",
            "Build darwin/arm64 binary with cgo",
        ):
            self.assertIn(name, names)
        self.assertEqual(names.index("Staticcheck"), names.index("Go vet") + 1)
        self.assertIn(f"        run: {STATICCHECK}\n", found["Staticcheck"])
        self.assertNotIn("continue-on-error", job("test.yml", "broker"))

    def test_broker_jobs_report_without_running_go_outside_scope(self):
        block = job("test.yml", "broker")
        self.assertIn("    needs: [changes]\n", block)
        self.assertNotRegex(block, r"(?m)^    if:")
        self.assertIn("os: [ubuntu-24.04, macos-15]", block)
        self.assertIn("broker: ${{ steps.scope.outputs.broker }}", job("test.yml", "changes"))
        found = steps(block)
        for name, body in found.items():
            if name in ("Check out", "Require unchanged checkout"):
                continue
            with self.subTest(step=name):
                self.assertIn("needs.changes.outputs.broker == 'true'", body)
        for name, runner in (
            ("Go build", "Linux"),
            ("Build darwin/arm64 binary with cgo", "macOS"),
            ("Upload darwin/arm64 binary", "macOS"),
        ):
            self.assertIn(f"&& runner.os == '{runner}'", found[name])

    def test_macos_build_step_builds_and_signs_with_build_script(self):
        step = steps(job("test.yml", "broker"))["Build darwin/arm64 binary with cgo"]
        self.assertIn('run: sh build.sh "$RUNNER_TEMP/ensemble-broker-darwin-arm64"', step)
        self.assertIn("GOOS: darwin", step)
        self.assertIn("GOARCH: arm64", step)


class Release(Case):
    def setUp(self):
        self.text = (WORKFLOWS / "release.yml").read_text()
        self.job = job("release.yml", "broker")
        self.steps = steps(self.job)

    def test_release_trigger_runner_and_permissions(self):
        self.assertRegex(self.text, r"(?m)^on:\n  release:\n    types: \[published\]\n\n")
        self.assertRegex(self.text, r"(?m)^permissions: \{\}$")
        self.assertIn("    runs-on: macos-15\n", self.job)
        granted = re.findall(r"(?m)^      ([a-z-]+): write$", self.job)
        self.assertEqual(sorted(granted), ["attestations", "contents", "id-token"])
        self.assertEqual(len(re.findall(r": write$", self.text, re.M)), 3)
        self.assertEqual(re.findall(r"(?m)^  (\S+):$", self.text.split("\njobs:\n")[1]), ["broker"])

    def test_every_action_is_pinned_by_sha(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_release_builds_signs_attests_and_uploads_the_asset(self):
        names = list(self.steps)
        self.assertLess(
            names.index("Require the tag to be the plugin version"), names.index("Build and sign")
        )
        self.assertIn(
            'test "$TAG" = "v$version"', self.steps["Require the tag to be the plugin version"]
        )
        build = self.steps["Build and sign"]
        self.assertIn('run: sh src/broker/build.sh "$RUNNER_TEMP/release/ensemble-broker"', build)
        self.assertIn("GOOS: darwin", build)
        self.assertIn("GOARCH: arm64", build)
        self.assertIn(
            f'tar -czf "$RUNNER_TEMP/{ASSET}" -C "$RUNNER_TEMP/release" ensemble-broker',
            self.steps["Pack"],
        )
        attest = self.steps["Attest provenance"]
        self.assertIn("uses: actions/attest-build-provenance@", attest)
        self.assertIn(f"subject-path: ${{{{ runner.temp }}}}/{ASSET}", attest)
        upload = self.steps["Upload to the release"]
        self.assertIn(
            f'gh release upload "$TAG" "$RUNNER_TEMP/{ASSET}" --repo "$GITHUB_REPOSITORY" --clobber',
            upload,
        )
        self.assertIn("GH_TOKEN: ${{ github.token }}", upload)
        self.assertLess(names.index("Pack"), names.index("Attest provenance"))
        self.assertLess(names.index("Attest provenance"), names.index("Upload to the release"))
        self.assertIn("TAG: ${{ github.event.release.tag_name }}", self.job)

    def test_release_run_blocks_hold_no_expressions(self):
        run = re.compile(r"(?m)^        run: ")
        runs = [run.split(body, maxsplit=1)[1] for body in self.steps.values() if run.search(body)]
        self.assertGreaterEqual(len(runs), 4)
        for run in runs:
            self.assertNotIn("${{", run)
