# Sessions and client policy

How the sessions ensemble starts are set up, how to list and show them, and the flags role runs pass their client.

## Session settings

Every session ensemble starts, agent runs and workers, loads your user settings with every plugin except `ensemble` (`[clients.<client>] flags` in `src/ensemble/session/config.toml`; OpenCode's plugin turns itself off: [OpenCode commander](#opencode-commander)): only a client you start, the commander, has `/ensemble:manage`. Each such session's client also carries `ENSEMBLE_AGENT_SESSION=1`, under which `ensemble-crew` `run`/`worker`/`reopen`/`issue`/`role`/`hold`/`resume`, `ensemble-session` `start`/`restart`/`resume`/`client-resume`, `ensemble-delegate` `run`/`resume` and `ensemble-orchestrator` `router`/`resume` exit 2 (their forms that start nothing, such as `--dry-run`, `--list` and `--brake`, still run), even run by a checkout's `bin/` path: a guard against slips, not isolation (`env -u` clears it). Agent runs load no MCP servers your Claude or Codex config names, user or project, so none can write past the outcome: `--strict-mcp-config` in `[policy.claude]`, `-c mcp_servers={}` in `[policy.codex]` (`src/ensemble/delegate/config.toml`; [Client policy](#client-policy)); workers keep them. A trusted cwd adds its project settings (`trusted_dirs` in `src/ensemble/delegate/config.toml`).

Codex sessions also start without its built-in browser, computer use, Apps and plugin suggestions, so a run cannot open a web page or a third-party service to act outside its outcome; web search, image generation, subagents and your own plugins stay. Checked against Codex 0.163.0, whose plugin ids can change with a release:

| Tool | Packaged `[clients.codex] flags` entry |
|---|---|
| Browser and Chrome (MCP server `node_repl`) | `-c plugins.<id>@openai-bundled.enabled=false`, `<id>` each of `browser`, `chrome`, `chrome-dev`, `chrome-internal` |
| Computer use (`node_repl`, `cua_repl`) | the same, `<id>` each of `computer-use`, `unified-computer-use` |
| Apps (`codex_apps` connectors, the browser connector among them) | `--disable apps` |
| Plugin suggestions, which can install a plugin | `--disable tool_suggest` |

The feature flags `browser_use`, `computer_use` and `in_app_browser` do not turn these off, and `--disable plugins` would turn off your plugins too. To get a tool back, drop its entry from a `[session.clients.codex] flags` list in `~/.ensemble/config.local.toml`: a local list replaces the packaged one whole, so copy the rest. Every launch reads this list, a resume and `client-resume` included, so a change reaches sessions started before it.

A Codex resume keeps the run's own flags (`[policy.codex]` in `src/ensemble/delegate/config.toml`) from its start, where Claude's takes the current ones. Codex's recovery state records each session's launch, its writable grants among them, and a resume replays that record rather than take new grants or a new run policy from its caller. The list above is read again because it is the client's own setting, not the run's; it refuses `--add-dir` and `--`, so it can neither widen a session's grants nor pass literal arguments.

In the commander's Claude Code session the plugin's mod always hides Monitor tool-call rows and notices, plus Bash tool-call rows whose command contains `ensemble-crew next-event` or `ensemble-crew ack`. Other tools, Bash commands and notices draw as usual; the model's content is unchanged. Ctrl+o shows Monitor notices; the hidden tool-call rows stay hidden.

`ensemble-crew next-event` (including `--follow`) exits 1 on a malformed complete event line and reports its file path and physical line number. Inspect the file with `pending`; reads never acknowledge handling. A final line without a newline waits until completed.

A commander under `ensemble-crew hold` gets a re-arm prompt at startup, resume, compact and clear. Claude Code uses Monitor. Codex uses Manual mode: run `ensemble-crew pending --content` at the beginning of every active turn before status reports or new launches; there are no idle notifications. Only a background tool whose runtime contract guarantees notifications to this conversation may run `attach`'s `next-event:` command, without `--follow`; after handling and acking each event, run `attach` again and use its newly printed command. A commander restarted outside its `hold`, or one giving `--manager` to each command without `hold`, gets no prompt. OpenCode uses Manual mode too ([OpenCode commander](#opencode-commander)).

After installing or updating the Codex plugin, open `/hooks`, review and trust the ensemble SessionStart command before relying on this prompt; installation alone does not trust plugin hooks. A changed hook definition needs review and trust again. [Codex plugin hooks](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks) describes this trust requirement.

## ensemble-session

`bin/ensemble-session --help` lists its subcommands. `ls` prints a header, then one tab-separated row per session: `name`, `state` (`working`, `done`, `blocked`, `dead`, `unknown` when its host could not tell), `client`, `sid`, `display`, with `-` for an empty value.

Sessions run on `[session.hosts] default`: `tmux`, or `docker` ([Docker host](docker-host.md)).

`start --account NAME` (also `ensemble-crew worker`'s) runs a Claude session on a registered Claude account
([Docker host › Claude accounts](docker-host.md#claude-accounts)). Restart and resume keep the account; `--env
ENSEMBLE_ACCOUNT=…` and a `[delegate.policy.<client>] env` naming it are refused (exit 2).

`start --display none` starts a worker without a pane, still opened by the caller, and hidden. Hidden (from that, `hide`, or closing its pane by hand; not from losing the manager's window or detaching its viewer) lasts through `restart` and `resume` until `show`.

Text sent to a Codex or Claude tui session mid-turn joins that turn. Codex takes it at once; Claude queues it until the running tool returns. Text sent after a Claude turn ended starts a new turn. When the session has an events file, `ensemble-crew pending --content` and `next-event --content` print mid-turn replies; reply text stays for 30 days.

A Codex or Claude turn that fails ends a role run's attempt at once. A usage limit stays a usage limit.

## Recall a closed run

`ensemble-crew close <ID>` ends a board run's sessions and drops its roster entry; its conversation stays. To continue it:

```sh
ensemble-crew reopen [--work-dir <work_dir>] <ID> [--display D]
```

It reads the run record, `<work_dir>/work/<ID>/run/delegate.json` (`--work-dir` only when the board's `work_dir` is not `~/.ensemble`), and resumes its session id as worker `<agent name>` on the run's Host, client, cwd and Claude account: a Docker run comes back in a new container with its client history and [workspace volume](docker-host.md#workspace-volumes). The worker is tracked like any other; `ensemble-crew close <agent name>` ends it. It brings back the conversation, not the run: no harness, outcome or write-back, and the board is untouched. A conversation known only by its session id: `ensemble-crew worker <name> --resume <sid> --client <client> --cwd <cwd> [--host <host>]`.

These do not recall it:

- `ensemble-crew resume <ID>` resumes roster entries only, and `close` removed it: `not in roster; a closed issue run: ensemble-crew reopen --manager=<manager> <ID>`.
- `ensemble-orchestrator resume <ID>` continues an interrupted run only; a run with an outcome prints its Record, starts nothing and says so on stderr.
- The run comment's `ensemble-session client-resume …` runs the client on this machine, outside ensemble; a Docker run's history is not there.

`reopen` fails when:

- the issue finished and prune cleaned it: prune deletes the run's agent files, its cwd among them, at once ([State cleanup](board.md#state-cleanup)), and the run record 7 days later;
- `work/<ID>/run/delegate.json` is gone (pruned, or deleted by hand): `no run record` (exit 1);
- the run, or a session holding its session id, is still alive: exit 2; close it first;
- its client history is gone: a Docker run's is in `~/.ensemble/docker-state/<agent name>/` (`no native history to resume`). A removed workspace volume comes back empty.

`reopen` brings back the issue's latest run; an earlier run's conversation: by its session id, above.

To check a recall, from the commander's `ensemble-crew hold` shell (elsewhere add `--manager <manager>` to each `ensemble-crew` command): close a finished dummy-tester run with `ensemble-crew close <ID>`, run `ensemble-crew reopen <ID>`, then `ensemble-session ls` shows `<agent name>` with the run's sid, and `ensemble-session send <agent name> 'What was your input?'` gets an answer from the earlier conversation (`ensemble-session read <agent name>`).

## Client policy

Role runs' client flags are `[policy.<client>]` in `src/ensemble/delegate/config.toml` (`[policy.claude]`, `[policy.codex]`; none packaged for OpenCode, so none unless a local `[delegate.policy.opencode]` sets them), after the session's `[clients.<client>] flags` and permission mode. Their system-prompt flag (`--append-system-prompt`, Codex's `developer_instructions`) tells a role run its final reply is the outcome block alone; OpenCode has no such flag, its config being the session's. A local list replaces the default whole: copy that flag into it, and the MCP one.

A Codex launch fails, naming the file, when a config layer is unreadable or invalid, or names an MCP server outside `[A-Za-z0-9_-]`: rename it. Servers defined by a `-c` flag, bundled in your Codex plugins, or managed by MDM still load. Turning a plugin off also drops its skills.

A role run's TUI turn end without an outcome gets one nudge, and later ones count toward giving up. Claude Code waits for reported background work; Codex reports none.

A Claude or Codex session's `client-resume` (its resume command, on this machine, in `auto` mode) replays its launch, these flags included. Codex's replays its snapshot, the config flags of its launch with them. Claude's replays the run's flags (`--strict-mcp-config`, the system prompt), Access (setting sources and the `--mcp-config` beside them, dirs, allowed tools), model, effort, name and env with this machine's current `[clients.claude] flags` and `allow`, since it runs here, not on the launch's Host; it opens the TUI without ensemble's hooks, which serve only an ensemble session.

Every session, crew workers and role runs, gets one permission mode from the Host it runs on: an `isolated` Host (`[hosts.docker] isolated = true`) `yolo`, every other Host `auto`. Each mode's flags are the client's, in `[clients.<client>.modes]` in `src/ensemble/session/config.toml`, the only home of permission flags; a start, restart and resume each get the mode of the Host they run on, and `client-resume` (this machine) `auto`:

| Client | `auto` | `yolo` |
|---|---|---|
| Claude | `--permission-mode auto` | `--dangerously-skip-permissions --settings '{"skipDangerousModePermissionPrompt": true}'` (no first-start dialog) |
| Codex | `-s danger-full-access -c approval_policy="on-request" -c approvals_reviewer="auto_review"` | `--dangerously-bypass-approvals-and-sandbox` |
| OpenCode | none: its config's `permission.bash = "ask"` asks for every shell call but the run's pre-approved commands; headless refuses the call, the TUI shows the ask and the session is `blocked` | `--auto`, plus backend `allow` for `bash`, `read`, `external_directory` and `doom_loop`; edit denies stay |

OpenCode's pre-approved commands match a command's text without its redirections, so an allowed `git status` also allows `git status > <file>`. An OpenCode run edits only its cwd and Access dirs, never `.git` or OpenCode's own config, and reads its Access reads without editing them; a refused call does not end its turn. External-directory grants cover both supplied symlink paths and resolved targets, so installed-plugin tasks under `~/.ensemble/links/` remain readable; read-only edit denies cover both spellings. Write-directory edit grants remain resolved-only: use the resolved path for writes through a supplied directory alias. OpenCode matches these paths case-sensitively: on a case-insensitive disk (macOS's default) only `.git` is denied in every spelling, so `OPENCODE.JSON` or a read dir spelled in another case stays editable, and its `apply_patch` tool (models named `gpt-…`) checks only a moved file's source.

`yolo` asks for nothing: no permission prompt, approval or sandbox; the container is the boundary ([Docker host](docker-host.md#permissions)). OpenCode approves tools at the backend, so toggling its TUI approval mode cannot restore those prompts; question dialogs still wait for an answer. When the selected OpenCode mode includes `--auto`, permission requests emit no `blocked`; questions still do, as do permission requests in modes without `--auto`. Claude refuses it as root; the container's clients run as `agent`. `danger-full-access` ignores writable roots and team dirs; a Docker container cannot run Codex's sandbox ([Permissions](docker-host.md#permissions)). For `workspace-write` outside the Docker Host:

```toml
[session.clients.codex.modes]
auto = ["-s", "workspace-write", "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"', "-c", "sandbox_workspace_write.network_access=false", "-c", "sandbox_workspace_write.writable_roots=[]"]
```

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

Its API key, on the tmux Host, stays OpenCode's: run `opencode auth login` once, pick Other and enter the provider's name (`strata`), and OpenCode keeps the key in its `auth.json`. The availability probe reads the selected provider's API entry and sends it only in the HTTP Authorization header; it returns no key or response body. On the Docker Host the broker keeps it: `ensemble-broker login opencode strata` once, for every configured provider ([Docker host › OpenCode](docker-host.md#opencode)). A name is up to 64 letters, digits, `.`, `_` or `-`.

`scripts/install.sh --docker` with OpenCode selected configures both local OpenCode (including a commander) and
the broker from one key entry; it imports the local API entry instead of asking for the key twice
([Credentials](credentials.md#opencode-provider-api-keys)).

With OpenCode selected, the installer also adds the provider's URL, model and context to the global
`$XDG_CONFIG_HOME/opencode/opencode.jsonc` (default `~/.config/opencode/opencode.jsonc`), or the existing
`opencode.json` when there is no JSONC file. Direct `opencode models <provider>` and
`opencode run --model <provider>/<model>` then use it without an ensemble session. An existing provider
in either file is left as-is, including on reruns; the default model is unchanged. Provider filters that
exclude the selected name must be adjusted by the user before adding it. Config edits preserve symlinks
and support JSONC comments, trailing commas, UTF-8 BOM and duplicate keys (last value wins). Installer cleanup removes
the provider it added if its definition is unchanged. Plugin cleanup then restores the configuration
backup from before the first plugin install, including the user's original providers and settings;
edits made after that backup revert. A configuration created by this installation stays recorded as
created across upgrades and is removed with the plugin's added files (`node_modules`, `package.json`,
`package-lock.json`, `.gitignore`), including after repeated install/cleanup cycles. Preexisting
`package.json`, `package-lock.json` and `.gitignore` are also backed up before the first plugin install
and restored by cleanup.
The cleanup list also includes newly created OpenTUI data, gh cache/state, uv cache and
`~/.homebrew/trust.json`; it respects `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` and
`UV_CACHE_DIR` for those tool directories. Existing tool directories and trust files are kept.

Set each provider's `context` and `[clients.opencode] ollama_context` to the server's real context window (Ollama's `OLLAMA_CONTEXT_LENGTH`): a smaller value compacts early, a larger one lets a request overflow. Both default to 65536.

A crew worker's cwd is always trusted (you pick it), so `ensemble-crew worker NAME --client opencode --cwd DIR` starts on tmux in any `DIR`; a role run only under `trusted_dirs`; a board run never (its cwd is the workdir's `agent/`): run OpenCode board runs on the Docker Host ([Linear board › OpenCode](board.md#opencode)).

On the tmux Host OpenCode needs the host's Ollama for `ollama/` models ([Docker host › OpenCode](docker-host.md#opencode) steps 1–3) and an OpenCode 1.x. With none on PATH, `npm install -g opencode-ai@1.19.3`; ignore npm's warning that its postinstall did not run. Beside a 2.x, install 1.x under its own prefix, leaving the 2.x as it is, and point `executable` at it:

```bash
npm install -g --prefix ~/.local/opencode-1 opencode-ai@1.19.3
~/.local/opencode-1/bin/opencode --version   # 1.19.3
```

```toml
# ~/.ensemble/config.local.toml
[session.clients.opencode]
executable = "/Users/<you>/.local/opencode-1/bin/opencode"   # absolute: ~ is not expanded
```

TUI runs deliver every completed main-thread assistant text to the Delegate, including intermediate progress reports; child sessions' replies are excluded. The final text also ends the turn, and the Delegate records it once.

OpenCode keeps no jsonl transcript: its sessions are in `opencode.db` (tmux: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`; a Docker Host session's: `~/.ensemble/docker-state/<name>/opencode/opencode/opencode.db`, which a resume needs). To see what a session holds, or whether a `send` reached it:

```bash
python3 -I src/ensemble/session/clients/opencode/transcript.py <ses_id> [--db <path>] [--limit N]
```

It prints the session's newest `N` (default 100) user and assistant texts, oldest first, one JSON line each: `{"time": "2026-10-18T12:00:00.123Z", "role": "user", "text": "…"}`. A missing db or unknown session: exit 1 and one `opencode transcript: …` line on stderr. The `ses_…` id is the last `client_session` line's `client_id` in the run's `<workdir>/run/run.jsonl`.

## OpenCode commander

A commander can run in OpenCode 1.x (checked against 1.19.3). Install ensemble in `~/.config/opencode/opencode.json`, `<version>` a release:

```json
{"plugin": ["ensemble@git+https://github.com/windroc-labs/ensemble.git#v<version>"]}
```

OpenCode installs that tag once, into `${XDG_CACHE_HOME:-~/.cache}/opencode/packages/<spec>/node_modules/ensemble`, and never refreshes the same spec: to update, set the new release's `#v<version>`, restart OpenCode, and delete the old spec's directory. A checkout instead: `"plugin": ["file://<checkout>/.opencode-plugin/ensemble.js"]`. `opencode plugin` installs npm packages only. `ctmux --install`'s commands find the installed copy ([Install](../README.md#install)); start `opencode` in the `ctmux` shell, then run `/ensemble:manage`.

The plugin adds:

- `/ensemble:act-as`, `/ensemble:board` and `/ensemble:manage`: commands with the corresponding skill text.
- this plugin's `bin/` first on the shell tool's `PATH`.
- the SessionStart re-arm ([Session settings](#session-settings)): for a commander under `ensemble-crew hold`, a prompt at startup, resume and after compaction. Sub-agent sessions get none.

It turns itself off, adding nothing, under `ENSEMBLE_AGENT_SESSION`: an OpenCode worker or role run on the tmux Host loads your global `opencode.json`, and `[clients.opencode] flags` cannot turn a plugin off.

Limits:

- Manual monitoring only (`/ensemble:manage` › Start): OpenCode has no Monitor, and its shell tool has no background mode and no completion notification. The commander reads events with `ensemble-crew pending --content` at the start of each active turn; nothing wakes an idle commander.
- The skills' `allowed-tools` and `disable-model-invocation` mean nothing to OpenCode: its own `permission` settings decide which commands ask. The three are commands, never skills the model loads itself.
- No mod hides the `ensemble-crew` tool calls.
