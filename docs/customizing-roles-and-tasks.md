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

- **Client and policy:** choose the client with this config or `ensemble-delegate run --client`; [Delegate configuration](../src/ensemble/delegate/config.toml) covers permissions and flag overrides.
- **Claude account:** [Session account configuration](../src/ensemble/session/config.toml).
- **Destination:** `[output]` (`local`, `github` or `pull-request`); a lower level's `[output]` replaces the whole table.
- **Access:** Delegate always grants reads of the core team directory, the role's team directory and its tasks directory, even when a role or overlay replaces `read`. `read` adds only extra directories; `write` and `commands` widen writes and commands; `templates` inlines templates into the prompt.
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
- **Out of reach:** the team dir, its files and its `tools/` tree must not overlap the run's workdir, cwd, a write dir, a dir the run adds or a dir the client can write (Codex: its `CODEX_HOME`). Codex's default `danger-full-access` ignores this: [Delegate configuration](../src/ensemble/delegate/config.toml).

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

- The role's charter (`roles/<role>.md`, here in a team dir) says where its output goes. A done run or a question goes to In Review; a failure follows `first_failure`, and a failure after resume goes to In Review. Engineer `decompose` returns no deliverable; write-back posts `Decompose done: Created <ids>; reused <ids>`, built from its summary's identifiers alone (a summary not of that form posts `Decompose done: no Created/reused identifiers in the run's summary`), and completes itself and its source PRD on done; questions and failures leave the PRD unchanged.
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

## Docker worker client settings

Each client's Docker workers start from a **Docker config**, a file you edit: yours under `~/.ensemble/docker-config/`, else the packaged default. Your file replaces the default whole; start from a copy of it. Your personal client settings (`~/.claude/settings.json`, `~/.codex/config.toml`) never reach a container.

| Client | Your file | Packaged default | Worker copy | Wins over it |
|---|---|---|---|---|
| Claude | `claude/settings.json` | [`docker-settings.json`](../src/ensemble/session/clients/claude/docker-settings.json) | `$CLAUDE_CONFIG_DIR/settings.json` | `skipWorkflowUsageWarning`, `autoCompactEnabled`, `autoContinueAtUsageLimit`, `promptSuggestionEnabled`, which an unattended worker needs; run flags; project settings |
| Codex | `codex/config.toml` | [`docker-config.toml`](../src/ensemble/session/clients/codex/common/docker-config.toml) | `$CODEX_HOME/config/config.toml`, which `$CODEX_HOME/config.toml` links to | `cli_auth_credentials_store`, `forced_login_method`, `check_for_update_on_startup` and the cwd's project trust, passed as `-c` flags; run flags; project config |
| OpenCode | `opencode/opencode.json` | [`docker-opencode.json`](../src/ensemble/session/clients/opencode/docker-opencode.json) | `$XDG_DATA_HOME/opencode.json`, named by `OPENCODE_CONFIG` | ensemble's inline config (providers, permissions, plugin); `--model`; project config |

- **When it applies:** each new worker start copies it, so an edit needs no release and reaches later workers only. A resume keeps the worker's copy, with every edit made to it, for every client; only the worker's next new start replaces it.
- **One worker:** edit its copy in place, inside the container (`docker exec -it <container> vi "$CODEX_HOME/config.toml"`) or on the host under `~/.ensemble/docker-state/<worker>/<client>/` (Codex: `codex/config/config.toml`). Copies are independent: other workers keep theirs. A Codex worker also keeps what Codex itself saves there, such as a service tier set in its TUI.
- **Codex plugins** the image installs are enabled in the image's `/etc/codex/config.toml`, below the copy, so the Docker config can turn one off.
- **Model and effort** come from the run's tier and effort flags (`[clients.<name>.tiers]`, `[clients.<name>.efforts]`); the copy only sets what a run leaves unset. The packaged defaults set Claude `opus` / `medium` (`model`, `effortLevel`) and Codex `gpt-6.1-sol` / `high` (`model`, `model_reasoning_effort`). It is a user-level file, never a managed layer.
- **Trust:** the Docker config applies to every role's Docker workers. Only the keys above are overridden: others, such as Claude `permissions` or `defaultMode`, OpenCode `permission`, or Codex `chatgpt_base_url`, `model_provider` and `[model_providers]` (which can send the broker's ChatGPT login elsewhere), can widen a run's Access. Any change to a worker's copy lasts across that worker's resumes, including one its agent makes, such as an approved hook, an MCP server or a provider URL. A run's `-c mcp_servers={}` also turns off the MCP servers the copy Codex loads names: the Docker config on a new start, the worker's copy on a resume. Keep credentials out of it; the copy is plaintext in every worker's home.
- An invalid Claude (not a JSON object) or Codex (not TOML) Docker config fails a new Docker start, naming the file; a resume never reads it. OpenCode reports its own parse errors.

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

Its API key, on the tmux Host, stays OpenCode's: run `opencode auth login` once, pick Other and enter the provider's name (`strata`), and OpenCode keeps the key in its `auth.json`. The availability probe reads the selected provider's API entry and sends it only in the HTTP Authorization header; it returns no key or response body. On the Docker Host the broker keeps it: `ensemble-broker login opencode strata` once, for every configured provider ([OpenCode operating limits](customizing-roles-and-tasks.md#opencode-operating-limits)). A name is up to 64 letters, digits, `.`, `_` or `-`.

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

A crew worker's cwd is always trusted (you pick it), so `ensemble-crew worker NAME --client opencode --cwd DIR` starts on tmux in any `DIR`. Board client prerequisites: [Board configuration](../src/ensemble/orchestrator/config.toml).

On the tmux Host OpenCode needs the host's Ollama for `ollama/` models ([OpenCode operating limits](customizing-roles-and-tasks.md#opencode-operating-limits)) and an OpenCode 1.x. With none on PATH, `npm install -g opencode-ai@1.19.3`; ignore npm's warning that its postinstall did not run. Beside a 2.x, install 1.x under its own prefix, leaving the 2.x as it is, and point `executable` at it:

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

### OpenCode operating limits

OpenCode's pre-approved commands permit redirections: allowing `git status` also allows `git status > FILE`.
Except for `.git`, permission paths are case-sensitive: on a case-insensitive disk a differently cased read
directory or `OPENCODE.JSON` can bypass edit denies. Its GPT `apply_patch` checks only a moved file's source,
not its destination. Use Docker containment for untrusted work rather than relying on these rules to constrain writes.
Write through a directory alias using its resolved path; read grants cover both spellings, write grants only the resolved one.

OpenCode's own permissions apply to ensemble commands; skill `allowed-tools` and `disable-model-invocation`
do not. Commander monitoring is manual: OpenCode has no Monitor, background shell or completion notification
to wake an idle commander. Follow `/ensemble:manage`'s monitoring contract.

To set host Ollama's context window, use `launchctl setenv OLLAMA_CONTEXT_LENGTH 65536` (or the required
larger window), reopen the app, and pull every configured model.
Docker access to local/LAN providers needs each `host:port` (Ollama: `host.docker.internal:11434`) in
`networks.workers.exceptions` of `~/.egress/config.local.yaml`, then `~/.egress/current/start.sh`.
The installer does not add these exceptions; host-side `probe-provider` bypasses the proxy and can pass while
a worker still lacks access. macOS Local Network permission may also be needed. An `http://` provider URL
sends its key in plaintext.
