# Customizing roles and tasks

- The built-in roles, tasks and templates live in `src/ensemble/delegate/team/`; don't edit them.
- A role's run settings (model, client, destination, commands) go in `~/.ensemble/config.local.toml`.
- Your own roles, tasks and templates go in a team dir. A team dir only adds: it cannot change or replace a built-in role, task or template, so a new task needs a new role.
- Your own roles run through `/ensemble:act-as`, `/ensemble:manage` and `ensemble-delegate run`, and on the board as a workflow stage ([On the board](#on-the-board)).

## See what exists

`ensemble-delegate run --list` prints each role with its destination type and source file, then its tasks with theirs. It checks every team file and renders every role's prompt, writing nothing; exit 2 names the bad file or key.

## Change a role's run settings

Set run keys under `[delegate]` (every role) or `[delegate.roles.<role>]` (one role) in `~/.ensemble/config.local.toml`. Each key, its values and the order they resolve in: the comments of `src/ensemble/delegate/config.toml`.

```toml
[delegate.roles.engineer]
client = "codex"          # a [clients.<name>] of src/ensemble/session/config.toml
tier = 1

[delegate.roles.researcher.output]
type = "github"
repo = "<owner>/<name>"
branch = "main"
dir = "Research/"
```

- **Client and policy:** choose the client with this config or `ensemble-delegate run --client`; [session settings](sessions.md#settings-and-trust) covers permissions and flag overrides.
- **Claude account:** [selection and login alignment](docker-host.md#claude-accounts).
- **Destination:** `[output]` (`local`, `github` or `pull-request`); a lower level's `[output]` replaces the whole table.
- **Access:** `read`, `write` and `commands` widen what the agent run may read, write and run; `templates` inlines templates into its prompt.
- **Board runs:** the same keys under `[orchestrator.core]` (e.g. `[orchestrator.core.roles.engineer]`) apply to board runs only, after the `[delegate]` ones.

## Add roles, tasks and templates

1. Make a team dir, e.g. `~/.ensemble/team/`, holding any of `roles/`, `tasks/`, `templates/` and `tools/`. Keep it out of every dir a run can write (below).
2. List it in `~/.ensemble/config.local.toml` (only there: the packaged `config.toml` refuses `team_dirs`):
   ```toml
   [delegate]
   team_dirs = ["~/.ensemble/team"]
   ```
3. Write the role and task files (formats below), named `<name>.md` with `<name>` of `[a-z0-9-]`.
4. Check with `ensemble-delegate run --list`, then [run it](#run-and-check).

Rules a team dir follows; each break stops the run with an error naming the file:

- **Add-only:** a role, task or template name that core or an earlier team dir already defines.
- **Own tasks:** a role lists only tasks in its own team dir's `tasks/`; a built-in role only built-in tasks.
- **Templates:** any role may name any template, built-in or yours, in `templates`.
- **Text only:** no front matter (`---`) and no `{{…}}` placeholder; a file may name a method only as a built-in `<team>/methods/<m>.md`.
- **Tools:** a file in `tools/` runs as `{{role_team}}/tools/<file>` in the role's `commands` (written `<role team>/tools/<file>` in its text).
- **Out of reach:** the team dir, its files and its `tools/` tree must not overlap the run's workdir, cwd, a write dir, a dir the run adds or a dir the client can write (Codex: its `CODEX_HOME`). Codex's default `danger-full-access` ignores this: [Client policy](sessions.md#settings-and-trust).

A team dir's role needs no `[delegate.roles.<role>]` table: unset keys take the `[delegate]` values.

### Role file: `roles/<role>.md`

A `# ` title, an intro, then `## Tasks`, the role's only task list. Its first line names the default task as `unsure → `<task>` (`<tasks>/<task>.md`)`; then one line per task. Each path is exactly `<tasks>/<task>.md`, and the default is a listed task.

```markdown
# Reviewer

You review a change against its requirement.

## Tasks

Pick the task below that fits the input; unsure → `review` (`<tasks>/review.md`).

- `review` (`<tasks>/review.md`): review a pull request against its issue.
```

### Task file: `tasks/<task>.md`

It starts with a `# ` heading, has exactly one `[ensemble-progress:start] <what to report>` line and a `## Resume` section, outside code fences. The smallest built-in example: `src/ensemble/delegate/team/tasks/echo.md`.

## Run and check

| Command | Does |
|---|---|
| `ensemble-delegate run --list` | Lists and checks every role and task. |
| `ensemble-delegate run --role <role> [--task <task>] --input <text> --dry-run` | Prints the resolved client, tier, effort, cwd, dirs, commands and prompt as JSON; starts nothing. |
| `ensemble-delegate prompt --role <role> [--task <task>] --input <text> --workdir <dir> --out <file>` | Writes the role's prompt to `<file>`; starts nothing. |
| `/ensemble:act-as <role>[:<task>] <input>` | Runs the role in this conversation. |

## On the board

The board's stages (order, input layout, title prefix, comment leads, whether a Handoff needs instructions, the state a failed first run goes to) are a workflow file. The packaged one, `src/ensemble/orchestrator/engine/flow/workflow.toml`, has researcher, pm and engineer; its header lists every key and its default. To change a stage, or to put your own role on the board, copy it (e.g. into your team dir), edit the copy and name it in `~/.ensemble/config.local.toml`; it replaces the packaged one whole:

```toml
[orchestrator]
workflow = "~/team/workflow.toml"

[orchestrator.linear.roles.analyst]   # each stage role's Linear OAuth app
client_id = "<analyst Client ID>"
secret_service = "linear-app-analyst-secret"
token_service = "linear-app-analyst-token"
user_id = "<analyst app user UUID>"
```

A stage needs only `role` and `input` (`research`, `design` or `build`: its input layout):

```toml
[[stage]]
role = "analyst"
input = "research"
next = "pm"           # promote hands its Handoff issues to pm
```

- The role's charter (`roles/<role>.md`, here in a team dir) says where its output goes. A done run or a question goes to In Review; a failure follows `first_failure`, and a failure after resume goes to In Review. Engineer `decompose` uses local ticket-table output and completes itself and its source PRD on done; questions and failures leave the PRD unchanged.
- `[orchestrator.roles.<role>] max_runs` is optional: no table → 1.
- Every `ensemble-orchestrator` command checks the file when it loads it and exits 2 naming the file and stage: an unknown or ill-typed key, a role the Delegate does not know, a `next` that names no other stage, a `next` chain with a cycle.
- At least one stage's role must still publish documents to GitHub (its `[output]`), as the packaged researcher and pm do.
- A `[orchestrator.roles.<role>]` or `[orchestrator.linear.roles.<role>]` table must name a stage's role (one for a packaged stage your workflow leaves out is ignored), and a `[orchestrator.linear.task_labels]` or `[orchestrator.task_tiers]` entry a task of one. Board runs' run keys: `[orchestrator.core]`, above.

### A standalone verifier stage

To run `verifier` checks from the board, add a stage no other stage's `next` names, with its `[orchestrator.linear.roles.verifier]` app as above:

```toml
[[stage]]
role = "verifier"
input = "build"       # the target repo is checked as for engineer
ref = true            # reads the description's optional Ref: line (a branch or tag)
mapped_only = true    # Repo: must be the project's mapped repo
start = "Verification started:"
done = "Verification done:"
failed = "Verification failed:"
```

- Leave `next` and `deliverable` unset: the verifier publishes its report itself ([Upgrading](upgrading.md#verifier-reports-on-github-orchestrator-destination-removed) has its setup), and write-back links the report's `url` on the issue.
- Its issue, delegated to the verifier app: an optional `Repo:` line (the project's mapping), an optional `Ref:` line, then what to test or the steps to run.

## OpenCode sessions

OpenCode sessions run on every Host, but outside an `isolated` Host (tmux, or `[hosts.docker] isolated = false`) only in a trusted cwd (a role run's: under `trusted_dirs`, so given by `--cwd`, as its default cwd, the workdir's `agent/`, never is): OpenCode 1.x has no project trust, so in any cwd it runs `.opencode/plugins/*.js` and installs the npm plugins `opencode.json` lists, and `OPENCODE_DISABLE_PROJECT_CONFIG` does not stop it. A start, TUI or headless, and a resume in an untrusted cwd there fail before OpenCode starts: run it on the Docker Host or in a trusted cwd. On the tmux Host both modes run `[clients.opencode] executable` (default `opencode`) and refuse to start when its `--version` is not 1.x: where PATH finds a 2.x (Homebrew's `opencode` is 2.x), set it to a 1.x install's absolute path in `~/.ensemble/config.local.toml`'s `[session.clients.opencode]`. On the Docker Host they run the image's `/opt/ensemble/bin/opencode` instead (`executable` is ignored; the broker checks its version), in yolo mode, with Ollama at `host.docker.internal`. A long message shows as `[Pasted ~N lines]` in the TUI but is sent in full.

Each launch enables only the `ollama` provider and the `[clients.opencode.providers.<name>]` ones: OpenCode's built-in providers, OpenCode Zen's included, stay off, so `opencode models` under that config lists only these. Every launch passes `--model`: its tier's (`[clients.opencode.tiers]`), else `[clients.opencode] model` (default `ollama/qwen3.6:35b-mlx`), never OpenCode's own pick. Each of these names `ollama/<model>` or `<name>/<model>` of a providers table, else the client refuses its config. A provider is any OpenAI-compatible server, such as one on your LAN:

```toml
# ~/.ensemble/config.local.toml
[session.clients.opencode.providers.strata]
base_url = "http://<LAN host>:8080/v1"
models = ["qwen3.8-flash-next"]   # beside the models tiers and model map to it
context = 204800                          # its context window in tokens; default 65536

[session.clients.opencode.tiers]
1 = "strata/qwen3.8-flash-next"
```

Its API key, on the tmux Host, stays OpenCode's: run `opencode auth login` once, pick Other and enter the provider's name (`strata`), and OpenCode keeps the key in its `auth.json`. The availability probe reads the selected provider's API entry and sends it only in the HTTP Authorization header; it returns no key or response body. On the Docker Host the broker keeps it: `ensemble-broker login opencode strata` once, for every configured provider ([Docker host › OpenCode on Docker](docker-host.md#opencode-on-docker)). A name is up to 64 letters, digits, `.`, `_` or `-`.

`scripts/install.sh --docker` with OpenCode selected configures both local OpenCode (including a commander) and
the broker from one key entry: paste it once at `opencode auth login`, or pass one line with `--opencode-key-stdin`;
it stores it in `${XDG_DATA_HOME:-~/.local/share}/opencode/auth.json`, then imports that API entry into the broker.
Reruns skip a usable local API entry and a broker item whose recorded broker binary fingerprint still matches; a
missing broker item or a changed broker binary imports the local entry again without asking. An older broker-only
install needs one local login: the broker's key is never exported. Existing entries are not compared or synchronized.
The install ledger records each entry it adds, `opencode-auth` for the local provider and `keychain` for
`ensemble-broker-opencode` / `<name>`; `scripts/install-cleanup.sh` removes only those, keeping preexisting
credentials and other providers, and revokes nothing at the provider.

With OpenCode selected, the installer also adds the provider's URL, model and context to the global
`$XDG_CONFIG_HOME/opencode/opencode.jsonc` (default `~/.config/opencode/opencode.jsonc`), or the existing
`opencode.json` when there is no JSONC file, so direct `opencode models <provider>` and
`opencode run --model <provider>/<model>` use it without an ensemble session. An existing provider in either file is
left as-is, reruns included, and the default model is unchanged; adjust a provider filter that excludes the name
first. Edits keep symlinks and JSONC syntax (comments, trailing commas, BOM; a duplicate key's last value wins).
Cleanup removes the provider it added if unchanged, then restores the backups taken before the first plugin install
(the configuration, `package.json`, `package-lock.json`, `.gitignore`; later edits revert), and removes what this
installation created: a configuration, the plugin's added files, and new OpenTUI data, gh cache/state, uv cache and
`~/.homebrew/trust.json` (under `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` and `UV_CACHE_DIR`).

Set each provider's `context` and `[clients.opencode] ollama_context` to the server's real context window (Ollama's `OLLAMA_CONTEXT_LENGTH`): a smaller value compacts early, a larger one lets a request overflow. Both default to 65536.

A crew worker's cwd is always trusted (you pick it), so `ensemble-crew worker NAME --client opencode --cwd DIR` starts on tmux in any `DIR`. Board client prerequisites: [Task and client labels](board.md#task-and-client-labels).

On the tmux Host OpenCode needs the host's Ollama for `ollama/` models ([Docker host › OpenCode on Docker](docker-host.md#opencode-on-docker)) and an OpenCode 1.x. With none on PATH, `npm install -g opencode-ai@1.19.3`; ignore npm's warning that its postinstall did not run. Beside a 2.x, install 1.x under its own prefix, leaving the 2.x as it is, and point `executable` at it:

```bash
npm install -g --prefix ~/.local/opencode-1 opencode-ai@1.19.3
~/.local/opencode-1/bin/opencode --version   # 1.19.3
```

```toml
# ~/.ensemble/config.local.toml
[session.clients.opencode]
executable = "<home>/.local/opencode-1/bin/opencode"   # absolute: ~ is not expanded
```

TUI runs deliver every completed main-thread assistant text to the Delegate, including intermediate progress reports; child sessions' replies are excluded. The final text also ends the turn, and the Delegate records it once.

OpenCode keeps no jsonl transcript: its sessions are in `opencode.db` (tmux: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`; a Docker Host session's: `~/.ensemble/docker-state/<name>/opencode/opencode/opencode.db`, which a resume needs). To see what a session holds, or whether a `send` reached it:

```bash
python3 -I src/ensemble/session/clients/opencode/transcript.py <ses_id> [--db <path>] [--limit N]
```

It prints the session's newest `N` (default 100) user and assistant texts, oldest first, one JSON line each: `{"time": "2026-10-18T12:00:00.123Z", "role": "user", "text": "…"}`. A missing db or unknown session: exit 1 and one `opencode transcript: …` line on stderr. The `ses_…` id is the last `client_session` line's `client_id` in the run's `<workdir>/run/run.jsonl`.
