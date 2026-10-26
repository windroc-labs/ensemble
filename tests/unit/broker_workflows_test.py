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
        for workflow in ("test.yml", "broker-macos.yml"):
            with self.subTest(workflow=workflow):
                block = job(workflow, "broker")
                found = steps(block)
                names = list(found)
                for name in ("Gofmt", "Go vet", "Go test"):
                    self.assertIn(name, names)
                self.assertEqual(names.index("Staticcheck"), names.index("Go vet") + 1)
                self.assertIn(f"        run: {STATICCHECK}\n", found["Staticcheck"])
                self.assertNotIn("continue-on-error", block)

    def test_broker_jobs_skip_runners_and_gate_outside_scope(self):
        block = job("test.yml", "broker")
        self.assertIn("    needs: [changes]\n", block)
        self.assertIn("    if: ${{ needs.changes.outputs.broker == 'true' }}", block)
        self.assertRegex(job("test.yml", "ci"), r"needs:.*\bbroker\b")
        self.assertIn("    runs-on: ubuntu-24.04", block)
        self.assertIn("broker: ${{ steps.scope.outputs.broker }}", job("test.yml", "changes"))
        found = steps(block)
        self.assertIn(
            'run: sh with-go-state.sh go build -trimpath -o "$RUNNER_TEMP/ensemble-broker" .',
            found["Go build"],
        )

    def test_macos_never_appears_in_pr_or_merge_group_workflows(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text()
            if re.search(r"(?m)^  (pull_request|pull_request_target|merge_group):", text):
                with self.subTest(workflow=path.name):
                    self.assertNotIn("macos", text.lower())
        self.assertFalse((WORKFLOWS.parent / "ci_broker.py").exists())
        self.assertFalse((WORKFLOWS.parent / "ci_broker_test.py").exists())

    def test_macos_coverage_runs_only_on_main_push_or_manual_dispatch(self):
        text = (WORKFLOWS / "broker-macos.yml").read_text()
        events = re.search(r"(?ms)^on:\n(.*?)(?=^\S|\Z)", text)[1]
        self.assertIn("  push:\n    branches: [main]", events)
        self.assertIn("  workflow_dispatch:", events)
        self.assertEqual(re.findall(r"(?m)^  (\w+):", events), ["push", "workflow_dispatch"])
        self.assertIn("    runs-on: macos-15", job("broker-macos.yml", "broker"))
        self.assertIn("Upload darwin/arm64 binary", steps(job("broker-macos.yml", "broker")))

    def test_macos_build_step_builds_and_signs_with_build_script(self):
        step = steps(job("broker-macos.yml", "broker"))["Build darwin/arm64 binary with cgo"]
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
