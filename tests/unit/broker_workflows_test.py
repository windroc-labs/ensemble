import re
from pathlib import Path

from tests.case import REPO, Case

WORKFLOWS = Path(REPO, ".github/workflows")
STATICCHECK = "sh with-go-state.sh go run honnef.co/go/tools/cmd/staticcheck@v0.8.2 ./..."


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

    def test_broker_jobs_skip_runners_and_gate_outside_scope(self):
        block = job("test.yml", "broker")
        self.assertIn("    needs: [changes]\n", block)
        self.assertIn("    if: ${{ needs.changes.outputs.broker == 'true' }}", block)
        aggregate = steps(job("test.yml", "ci"))
        self.assertIn(
            "always() && steps.gate.outcome == 'success' && needs.changes.outputs.broker == 'true'",
            aggregate["Require broker results or confirmed unstarted macOS cancellation"],
        )
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
    def test_no_workflow_builds_or_attests_a_release(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text()
            with self.subTest(workflow=path.name):
                self.assertNotRegex(text, r"(?m)^  release:")
                self.assertNotIn("attest", text)
