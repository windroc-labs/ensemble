# Testing

[Architecture](architecture.md) · [Repository rules](../AGENTS.md)

## Contributor checks

From the repo root, set `PYTHONPATH=.:.github:src`. Run Python tests through the supervised
[`run_tests.py`](../.github/run_tests.py) entry; its `--help` owns selection, sharding and coverage options.
Unit tests use fakes; process/tool cases belong to integration tests. Test isolation rules are enforced by
[`tests/checks.py`](../tests/checks.py), [`tests/unit_imports.py`](../tests/unit_imports.py) and
[`isolation.py`](../.github/isolation.py), with integration fixtures in [`sandbox.py`](../tests/utils/sandbox.py).

| Need | Entry |
|---|---|
| One project's unit tests | `python3 .github/run_tests.py 'Session unit' tests/session/unit` (replace project) |
| One file / test id | `python3 .github/run_tests.py Focused FILE_OR_ID` |
| All unit tests / a shard | `python3 .github/run_tests.py Unit --unit [--shard 1/8]` |
| Named integration shard | `python3 .github/integration_jobs.py N`; inventory check: `--check` |
| Go broker | In `src/broker/`, use `sh with-go-state.sh` before Go tools; commands/pins in [broker workflow](../.github/workflows/test.yml) |
| Lint and required tooling | [`test.yml`](../.github/workflows/test.yml) and [`pyproject.toml`](../pyproject.toml) |

Enable the staged Python formatting hook once per clone with `git config core.hooksPath .githooks`.
Its implementation and version pin are in [`.githooks/pre-commit`](../.githooks/pre-commit).
For work confined to docs, CI's focused reference check can be run directly:

```sh
python3 .github/run_tests.py 'Docs references' tests/manager/unit/skills tests/orchestrator/unit/skills
```

CI selection belongs to [`ci_scope.py`](../.github/ci_scope.py); workflow jobs, not a copied scope table,
define what runs. Diff-coverage thresholds/exemptions belong to
[`coverage_gate.py`](../.github/coverage_gate.py) and each layer's [`guard_data`](../tests/guard_data/).

## Live acceptance

Follow [AGENTS.md › Gotchas](../AGENTS.md#gotchas) for the real-machine test boundary. Runbooks under [`.mini-op/runbooks/`](../.mini-op/runbooks/) own the steps.
CI uses fake credentials. Do not confuse a fake-backed integration pass with live acceptance.

For Docker worker/role/PR acceptance, run [`tests/session/live_docker.py`](../tests/session/live_docker.py)
through mini-op, with a healthy proxy, broker installed and logged in, Docker as the default Host and a
test repo with the App installed. Its CLI owns the cases. Guard kill/lock/log cases are in
[`docker-guard-main-process.md`](../.mini-op/runbooks/docker-guard-main-process.md);
installer/Linear/proxy checks in [Upgrading › Live checks](upgrading.md#live-checks).
OpenCode with host Ollama avoids quota only for checks needing a running client; it cannot substitute for
reply-protocol-specific acceptance.

## Integration timings

Let an entire PR CI workflow finish. Collect each run attempt into its own empty directory:

```sh
gh run download RUN_ID --repo windroc-labs/ensemble --pattern 'integration-RUN_ID-ATTEMPT-*' --dir /tmp/integration-samples/ATTEMPT
gh api repos/windroc-labs/ensemble/actions/runs/RUN_ID/attempts/ATTEMPT > /tmp/integration-samples/ATTEMPT/run.json
python3 .github/integration_timings.py /tmp/integration-samples --output .github/integration_durations.json
python3 .github/integration_jobs.py --check
```
