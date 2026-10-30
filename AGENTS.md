# AGENTS.md

Test commands and tool pins: [verification workflow](.github/workflows/test.yml); Python test selection: `python3 .github/run_tests.py --help`.

## Gotchas

- Every real-machine test (a real Keychain, real OS services, anything beyond fakes) is a mini-op runbook run on the test Mac mini (`.mini-op/runbooks/`), never on a developer's or user's Mac; `keychain_free_tests` (`tests/guard_checks/keychain.py`) fails a test file that calls a real credential store.
- This file never loads in an agent run outside a trusted cwd ([Delegate configuration](src/ensemble/delegate/config.toml)). An agent run reaches its cwd, `<work_dir>/work/<ID>/agent/`, the core and role team directories (read-only) and the config's extra `read`/`write` dirs: give a task a new writable path there, or it stalls on a permission nobody can grant.
- Input rendering and write-back changes: `tests/orchestrator/unit/engine/inputs_test.py` and `tests/orchestrator/unit/engine/writeback_test.py` pin input text and Linear calls.
- Every task has exactly one `[ensemble-progress:start]` line and no `budget` one, which start replaced (`tests/delegate/unit/team_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in the team text (`src/ensemble/delegate/team/`), in `/ensemble:act-as` and `/ensemble:manage`, or in `/ensemble:board` (`skills/board/SKILL.md`) outside its `## Linear` section: only that section, the Linear layer's, does.
- Skills and `config.toml` comments name a file by its path and a skill by its slash command (`/ensemble:act-as`), never "the act-as skill" or `ensemble:act-as`.
- Code, comments, messages and skills never reference a document: state the fact in place, or drop the pointer. Documents may reference code.
- A layer's packaged `config.toml` (`src/ensemble/<package>/config.toml`) holds only its keys, each a real line (a local-only one its empty default, its comment saying what empty means); a `# local:` line only shows a value for `~/.ensemble/config.local.toml` (`tests/guard_checks/configuration.py` `explicit_keys`). Layer sections and merge rules: `src/ensemble/utils/config_file.py`'s docstring.
- Identify Linear entities by id, never name; role app user ids come from configuration.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`src/ensemble/orchestrator/trackers/linear/ops.py:move` does).
- Tmux implementation: `TmuxHost` owns local guard workers and their inventory (`src/ensemble/session/hosts/tmux/`); `Viewer` owns every Host's worker panes through `Grid`, `Placement` and `TmuxCli` (`src/ensemble/session/viewer/`, `src/ensemble/session/engine/tmux_cli.py`). A worker pane runs the Host's `CommandView` itself and carries its `@worker` name and `@worker-opener`; no tmux session is made for a worker, and a pane carries no lifecycle. `/ensemble:manage` (`skills/manage/SKILL.md`) reaches it only through `ensemble-session`.
- Docker implementation: `src/ensemble/session/hosts/docker/` (`DockerHost`: `host.py`; a tui or headless worker's `DockerWorker`, the guard, its container's main process, over one `docker attach`: `worker.py`; maintenance, hooks, the broker client, daemon, mounts and stub beside them).
- The session's and delegate's public interfaces are frozen: `tests/guard_checks/exports.py` `session_interface_snapshot` and `delegate_interface_snapshot` (who may change `tests/session_interface.json` and `tests/delegate_interface.json`: their docstrings).
- Keep `<work_dir>/logs/runs.jsonl`: deleting it breaks resume, Recover, write-back and first-claim search.
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Merging

Once an admin enables **Require merge queue** for `main`, add a reviewed, green PR with
GitHub's **Merge when ready** button or `gh pr merge <number> --auto --squash`.
The queue tests current `main` plus preceding queued PRs on `merge_group` commits and
squash-merges automatically when required `CI` passes; no manual branch updates are needed.
`Worker image` also reports on queue groups, building when their combined diff changes image inputs.
Queue groups run every CI project, including diff coverage against the group's base SHA.
Once every unit shard passes (none run when only docs or the version changed), the `Coverage` job combines their data into artifact `unit-coverage-merged-<run id>-<attempt>` (`coverage-data` for coverage.py 7.10.7, `total.md`; kept 30 days; a pull request's covers only the projects it tests) and writes total line coverage to its job summary.
A unit shard that cannot collect coverage warns and keeps its tests' result.
Pull requests and queue groups gate coverage: `Coverage` fails on a shard without data or low diff coverage.
On pushes to `main` coverage is advisory: the unit jobs' coverage install and upload steps and the whole `Coverage` job may fail without failing `CI`, and diff coverage is not measured.
Latest `main` coverage, from the newest of the last 20 push runs that has it (a run lacks it when it changed only docs or the version, a unit test failed, or a coverage step failed; its Unit and `Coverage` jobs show which): `for run in $(gh run list --repo windroc-labs/ensemble --workflow CI --branch main --event push --limit 20 --json databaseId --jq '.[].databaseId'); do gh run download "$run" --repo windroc-labs/ensemble --pattern 'unit-coverage-merged-*' && break; done`.

`CI` requires every applicable job and, when broker scope is selected, `Broker ubuntu-24.04`.
The main CI workflow has no macOS job. Installer PRs run the separate `Installer macOS`
workflow with fake credentials. The independent `Broker macOS` workflow
(`.github/workflows/broker-macos.yml`) runs on pushes to `main` and pull requests that change
`src/broker/` or the workflow, and on manual dispatch, preserving macOS checks (gofmt runs on
ubuntu only) and the darwin/arm64 build artifact without blocking required CI.
Require `CI` and `mini-op PR text` (`.github/workflows/mini-op-pr-text.yml`, the PR title, body and commits scan, which a title or body edit reruns alone), never `Broker macos-15` separately.
A base change alone reruns no CI: push to the branch, or close and reopen the PR.

## Releasing

`<version>` is `.claude-plugin/plugin.json`'s `version`, `X.Y.Z`. In this order: bump the version and merge it, publish the Release, upload and confirm its broker asset, then pin it in the marketplace. Installs follow the pin, so a pin ahead of its asset breaks every new Docker install.

1. Bump `version` to the same `X.Y.Z` in `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` and `package.json` (nothing else), open a PR titled `ensemble X.Y.Z` and merge it at once with `gh pr merge <number> --squash --admin`, without waiting for CI (rulesets reject a direct push to `main`).
2. Create and publish Release `v<version>`: `gh release create vX.Y.Z --target <that commit's full sha> --title 'vX.Y.Z: <summary>' --notes-file <notes>`. Notes format: first line `Changes on top of v<previous> (ensemble X.Y.Z).`; one bullet per user-facing change with its PR numbers; internal PRs in one `Internal` bullet; last an **Upgrading** section with every step a user must take (config keys, CLI verbs, paths, data the release deletes), checked against each PR body, or "Nothing to do beyond v<previous>'s steps." Steps that stay needed also go into `docs/upgrading.md`.
3. On a Mac with Apple silicon, Go and the Xcode command line tools, in a clone of this repo, run `sh scripts/release-broker.sh v<version>` (no workflow builds the broker): it fetches the tag, builds the broker from its commit with that commit's `src/broker/build.sh`, packs `ensemble-broker-darwin-arm64.tar.gz`, writes its sha256 to `ensemble-broker-darwin-arm64.tar.gz.sha256` and uploads both to the Release; `src/broker/install.sh` installs the asset only when it matches that sha256. Then confirm both: `gh release view v<version> --repo windroc-labs/ensemble --json assets` lists them.
4. Only after step 3, the manager pins it in the `windroc` marketplace: in `windroc-labs/agent-plugins-marketplace`, replace ensemble's existing `source.ref` with `v<version>` in `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json` (a PR there, merged with `gh pr merge <number> --repo windroc-labs/agent-plugins-marketplace --squash --admin` without waiting for CI; never in the release PR). Each source object must contain exactly one `ref`: duplicate keys make Codex skip ensemble. Keep `source.source` as `url` and `source.url` as `https://github.com/windroc-labs/ensemble.git`; [Codex's official marketplace format](https://developers.openai.com/plugins/build/plugins#marketplace-metadata) supports a git tag in `source.ref` for this source. Before merging the marketplace PR, register its checkout with `codex plugin marketplace add <checkout>` in an isolated home, confirm `codex plugin list` includes `ensemble@windroc`, run `codex plugin add ensemble@windroc`, and check the installed commit matches `v<version>` and the plugin manifest version is `<version>`. `scripts/install.sh` and the clients install the tag it pins, with that release's broker; a `sha` pin stops the installer.
5. Only then update the installed plugin (`skills/manage/SKILL.md` › Git and reviews): `claude plugin marketplace update windroc`, then `claude plugin update ensemble@windroc` (a running Claude Code applies it after `/reload-plugins` or a restart); `codex plugin marketplace upgrade windroc`; OpenCode: its `plugin` entry's `#v<version>` (`docs/upgrading.md` › OpenCode plugin by hand).

## Import rules

Layer contracts live in `pyproject.toml` and run with `lint-imports`: manager > orchestrator > delegate > session > utils. Manager imports only itself and utils; it reaches other layers through commands. Cross-layer imports use the lower layer's public package; exact test exceptions for removed exports are listed in `pyproject.toml`. Orchestration reaches session only through Delegate: its contract ignores no import. Orchestrator `engine/` imports nothing under `trackers/` (one subpackage per tracker; Linear: `trackers/linear/`) but the router's Linear transport its contract lists, and `trackers/` imports none of the board rules (`router`, `promote`, `prune`, `writeback`); `engine/flow/`, its tracker-agnostic core, names no Linear concept. Project tests may import their project and lower layers, never the root `tests` package or a higher project.

Session and Delegate internal units use the longest matching package; their dependency rules and shared-module purity are declared in `pyproject.toml`'s unit contracts. Session owns no config reader, reply-protocol tag or team text; `tests/guard_checks/` enforces those names and literal dynamic imports.

Repository design rules live in `tests/guard_checks/`, one module per concern: namespace and subsystem placement, unit test placement, shared types, client-name ownership (`client_names`), configuration and import side effects. A rule's exceptions and a layer's constants live in `tests/guard_data/<package>.py` of the layer owning the path or package they name. A helper used by one file stays there as a private function; otherwise, a helper independent of its layer's own classes goes in `utils/`. Shared data types live in their layer's types module; standalone scripts keep theirs local. The `standalone_imports` guard owns their inventory and enforces standard-library-only imports.

No embedded program strings: `tests/guard_checks/embedded_programs.py`; use standalone files covered by lint and type checks, passed by path or stdin.

Python suppressions require a matching `NOQA_ALLOWED` entry in its layer's `tests/guard_data/<package>.py`; Pylint disables are paired with block-scoped enables.

Vulture scans `src/`, so code only tests use is dead: delete it. A name it cannot see used goes in `.github/vulture_whitelist.py` (rules: its docstring).

## Domain docs

Single-context: root `CONTEXT.md`. User docs: `docs/*.md`, indexed in `README.md` › Docs. `docs/adr/`, `docs/agents/`, `docs/handoffs/` and `docs/grill-engineering-comparison.md` are gitignored: local only.
