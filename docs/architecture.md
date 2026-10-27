# Core architecture

Five layers in one source tree (`src/ensemble/`): utils < session < delegate < orchestrator < manager. Import rules: `AGENTS.md` › Import rules. The code, by package; terms in `CONTEXT.md`.

The session harness's tui and headless workers run under the standalone `harness/guard.py`: as managed local workers on tmux
(`hosts/tmux/managed.py`), over one `docker exec -i` per container on Docker (`hosts/docker/worker.py`).
The harness owns the lifecycle pipe; the guard directly parents the client and owns a tui client's PTY;
tmux runs only the display attachment. Handle readers drain the framed channel continuously.
Reclamation limits and the migration from legacy mode launchers are in
[Session harness interfaces](#session-harness-interfaces).

```mermaid
flowchart TB
  subgraph EN[entry points]
    LD[launchd]
    HU[the user]
    CC[Starter, control and resume commands]
  end
  subgraph MG[ensemble.manager]
    MC["cli.py<br>ensemble-crew"] --> W["engine/crew.py"] --> RS["engine/roster.py"]
    MC --> HO["engine/holder.py<br>lease while a command runs"] --> RS
  end
  subgraph LO[ensemble.orchestrator]
    LCLI["cli.py<br>ensemble-orchestrator"]
    R["engine/router.py<br>pick · usage gate · trigger"]
    LR["engine/linear_run.py<br>Delegate ownership · queries"]
    WB["engine/writeback.py<br>one event's write-back"]
    PP["engine/promote.py → prune.py"]
    LT["trackers/linear/linear_tracker.py<br>LinearTracker"]
    LB["trackers/local/<br>LocalTracker · issue files · board lock"]
    FL["engine/flow/<br>Tracker · IssueState · Handoff grammar · workflow"]
    LC["engine/config.py<br>[core] overlay"]
  end
  subgraph DL[ensemble.delegate]
    D["cli.py<br>ensemble-delegate"]
    HD["engine/harness_delegate.py<br>HarnessDelegate · state.py RunState · record.py RunRecord"]
    H["engine/harness.py<br>detached run"]
    E["engine/: base · plan · drivers · runner · handler · callback · types · config · ready_gate"]
    C["composer/<br>prompt · run spec"]
    EV["event/<br>reply · outcome check · callback line"]
    CF["config.py<br>package settings"]
    subgraph TM[team/]
      T["roles · tasks · templates"]
      AT[tools/]
    end
  end
  subgraph SE[ensemble.session]
    SC["cli.py<br>ensemble-session"]
    DR["engine/: base · hosted_session · worker · types · records · tmux_cli<br>modes/: headless · tui · in_process<br>modes/client_adapters: Headless · JsonHeadless · Tui · InProcess<br>viewer/: viewer · display · placement · grid · iterm · hidden<br>hosts/base: Host · Process · Interactive · InteractiveProcess<br>hosts/types: SessionView · CommandView<br>hosts/reclaimed: ReclaimedGroup<br>hosts/tmux/: host · hook<br>hosts/docker/: host · worker · activity · guard_hook · hooks · maintenance · broker · daemon · mounts · stub · types"]
    subgraph AD[clients/]
      CL[claude]
      CX[codex]
    end
  end
  U[ensemble.utils]
  subgraph RT[clients]
    P1[Claude Code]
    P2[Codex CLI]
  end
  LD -.->|"router · promote"| LCLI
  HU -.->|"router --issue"| LCLI
  HU -.->|"act-as · CLI"| D
  HU -.->|"ctmux · CLI"| MC
  HU -.-> SC
  SC --> DR
  LCLI --> R & PP & WB & LR
  R & PP & WB --> LR
  PP & R --> LT --> FL
  LCLI --> LB --> FL
  PP & R --> FL
  WB --> LT
  WB --> FL
  R -->|"writeback.replay"| WB
  LR -->|"ensemble.delegate: detach, views"| HD
  H -.->|"each event: ensemble-orchestrator writeback"| LCLI
  LC -->|"Delegate class methods"| HD
  W -.-> CC
  D & H --> HD
  D --> C
  HD -.->|"detach on the harness Host"| H
  HD --> E & C
  E --> C
  DL -->|ensemble.session| SE
  C -->|reads| T
  DR --> AD
  CL -.-> P1
  CX -.-> P2
  RT -.-> AT
  classDef shared stroke-dasharray: 4 3
  class EV,U,CF shared
```

Solid arrow: an import, the only direction allowed, or a file read (`reads`); its label names the package imported, by its `__all__`. Dotted: runs a command or process. Dashed: shared modules, their imports not drawn.

## Modules and replacement boundaries

Layer interactions are shown above; import contracts are in `AGENTS.md` › Import rules.
Keeping a module assumes Python, the clients and their command protocols remain available.
Each Host implementation supplies its process capabilities.

| Path | Responsibility | Dependencies | Environment change | Replacement boundary |
|---|---|---|---|---|
| `src/ensemble/orchestrator/` | Board scheduling, state and write-back | delegate, utils | Keep | Public delegate API; tracker contract in `engine/flow/tracker.py` |
| `src/ensemble/delegate/` | Prompts, run plans, harness and result protocol | session, utils | Keep | Public session API and `event/` progress and outcome formats |
| `src/ensemble/session/engine/`, `modes/`, `clients/` | Session lifecycle, run modes and client adapters | Host, utils, client commands | Keep session and client logic | Host, Process, Interactive and InteractiveProcess capabilities in `hosts/base.py` and data types in `engine/types.py`; tmux operations remain in `engine/tmux_cli.py` and `viewer/` |
| `src/ensemble/session/harness/` | The session harness process that owns a built-in client's tui or headless worker on tmux or Docker, or an in_process adapter, its dir, handler dispatch and control view | session engine, utils | Keep | `Spec.handlers`, `EventCall` and the files in [Session harness](#session-harness) |
| `src/ensemble/session/hosts/tmux/` | Local processes and tmux sessions through `TmuxHost` | Host, `engine/tmux_cli.py`, utils, local tmux | Replace as a unit if local tmux is unavailable | Host start, enumeration, status, input and close semantics; display boundary shown above |
| `src/ensemble/session/hosts/docker/`, `src/broker/`, `src/broker/protocol/` | Docker adaptation, the guarded worker, worker image, containers and credential injection | Host, Docker Desktop, broker; broker uses macOS Keychain | Replace as a unit | Preserve Host for callers; retaining the broker also requires its protocol JSON schemas, versions, exit codes and shared fixtures |
| `src/ensemble/manager/`, `skills/manage/`, `hooks/` | Crew, Roster, event cursors and commander sessions | utils; commands to other layers | Keep scheduling logic; adapt local display and client hooks | Record ctl and resume commands, CLI output; import relationships shown above |
| `src/ensemble/utils/` | Configuration overlays, shared types, paths and process control | Python standard library | Keep, with the required OS process capabilities | Public exports and shared data formats |
| External `windroc/egress-proxy` | Egress service, outside this repository | Docker labels and health, read by broker start ([`launcher/egress.go`](../src/broker/internal/launcher/egress.go)); `scripts/install.sh` fetches a pinned commit and starts the egress proxy | Replace the independent service as a unit | Versions, label meanings and health semantics in [docs/label-contract.md](https://github.com/windroc/egress-proxy/blob/122d7454d738db4a9b7e0c4a89d2cfa5ff564339/docs/label-contract.md) |
| `src/broker/internal/broker/keychain_darwin.go` | Read credentials from macOS Keychain for broker injection into containers | macOS Security; storage interface in `keychain.go` | Replace the credential source as a unit | Credentials, CodexCredentials, ProviderKeys and Tokens in `launcher/host.go`; credential readers and lifecycle in [Credentials](credentials.md) |
| `src/broker/internal/credential/` | Provider-neutral token lifecycle (records, renewal, release, revocation backlog) and the container delivery every credential shares | audit, docker, protocol; a Store and a Provider from its caller | Keep | The `Provider` contract and record JSON in `contract.go`; GitHub is its provider (`broker/github.go`), Codex its shared one (`broker/codex_grants.go`: each container's grant is withdrawn, never revoked) |

The **Host boundary** is defined by [`hosts/base.py`](../src/ensemble/session/hosts/base.py).
In [`config.toml`](../src/ensemble/session/config.toml), `[hosts.<name>] implementation = "module.path:Class"`
selects an implementation and `[hosts] default` selects the host. Preserving its capabilities,
return types and error semantics keeps upper session, delegate and orchestrator code unchanged.
Client adaptation remains in `clients/`.

The **egress boundary** is defined by the external [docs/label-contract.md (122d745)](https://github.com/windroc/egress-proxy/blob/122d7454d738db4a9b7e0c4a89d2cfa5ff564339/docs/label-contract.md).
Callers depend only on the proxy's `windroc.egress.*` labels and Docker health, selecting an internal
network and proxy address from the network key's `.name` and `.proxy`. A replacement proxy preserves
the consumed contract version, label meanings and health semantics; the contract lists the complete
startup validation rules. [`launcher/egress.go`](../src/broker/internal/launcher/egress.go) applies them for `workers` before
every [`launcher/start.go`](../src/broker/internal/launcher/start.go) start.

```mermaid
flowchart TB
  M[manager] -. command .-> O[orchestrator]
  O --> D[delegate] --> S[session core and clients]
  M -. command .-> D
  M -. command .-> S
  S --> H[Host interface]
  H --> T[TmuxHost]
  H --> A[Docker host adapter]
  A --> P[protocol]
  P --> B[broker]
  B --> K[macOS Keychain]
  B -->|labels and health| E[external egress service]
```

Solid arrows show calls or data contracts; dotted arrows show commands.

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

## Run limits

Set a client's simultaneous session limit in `~/.ensemble/config.local.toml`:

```toml
[session.clients.opencode]
max_runs = 1

[session.clients.claude]
max_runs = 3
```

`max_runs` is a whole number >= 0. Zero (the packaged default) or an omitted key means unlimited. The setting belongs to the session layer's `[clients.<client>]` table in `src/ensemble/session/config.toml`, and is removed before configuration reaches the client adapter.

Every `Session.start` shares admission for that client across processes using the same user state directory, regardless of caller, Host, mode, name or resume flag. Board runs, `router --issue`, ad-hoc role runs and crew workers all count. The check and start hold one client admission lock; a full client raises the existing `SessionError` before launching or recording a new session, naming the client, limit and holding names (session ids for unnamed runs). There is no capacity queue. Preflight, inspect and attaching to an existing session reserve nothing.

TUI sessions count while their client is alive, including `done` between turns and `blocked`; an unknown state keeps its slot. Close a retained TUI when it should stop using capacity. Headless and in-process modes hold a lease until their mode ends or is stopped/closed, even when nobody reads events. A failed start releases its reservation once shutdown is confirmed; an unconfirmed TUI shutdown retains its reservation until matching Host inventory proves the client is gone. TUI ownership survives its launcher; transient leases belong to their caller process, or to its session harness for a harnessed headless run. An abnormal caller exit does not add a recovery protocol for orphaned children.

Admission checks all configured Hosts; an unavailable inventory fails closed. Shared TUI reservations from a Host configuration the caller does not have remain counted until a caller with the matching configuration confirms the session ended. Close the original session or retry using its original Host configuration to reconcile it. Changing an unrelated Host setting changes that configuration's identity, so close its sessions before changing its settings.

The orchestrator's [role limits](board.md#run-limits) and usage gate remain separate. Direct client commands outside `Session.start`, including `client-resume`'s native client handoff, do not create ensemble sessions.

## Session harness

A built-in Claude, Codex or OpenCode tui or headless session, with or without `Spec.environment`, runs under a session harness of its own on the `tmux` and `docker` Hosts when the session class is configured with its default callables. An unowned start (a test that injects a class loader, probe, Host factory or viewer factory; an extension client whose `Tui` or `Headless` keeps `_owned` false, or whose `Tui` keeps `_remote_owned` false on Docker; a start on an extension Host whose `_harnessed` is false) runs the earlier in-session way (a headless one still through its Host's `worker`) only on a Host whose `_unowned` is true (`tmux`, extension Hosts), and refuses non-empty `Spec.handlers` (`... has no session harness yet`); the `docker` Host refuses it before anything is written: `ConfigError("<client> <mode> on host <host>: runs only under a session harness")`. `Session.preflight` finds each handler module without importing it. An `in_process` session runs under one too (no Host).

`Session.start` writes the start to `~/.ensemble/harness/<32 hex>/request.json` (mode 0600; it holds `Spec.env`, and the harness deletes it once read), runs `python3 -P -s -m ensemble.session.harness.process [--caller FD] <dir>` detached (the caller's interpreter), and returns a control view once the harness runs the worker. A start error in the harness, or an interrupt at any point after the harness is spawned, is raised in the caller, the harness ended and its dir removed; SIGHUP, SIGTERM and SIGINT are held across the spawn itself, so one that arrives during it is handled the same way. The tui session's `session.json` names the dir as `owner`; a headless run, the harness's own child, has no `session.json` and stays nameless unless its spec names it. A harness that does not start is sent SIGTERM (headless) or killed.

```mermaid
flowchart LR
  E[Session.start] -->|request.json| H[session harness]
  H -->|holds harness.lock| W[tui or headless worker]
  H -->|events.jsonl, harness.json| V[control views]
  V -->|requests/| H
  H -->|on_event| D[handlers]
```

The harness holds `harness.lock` for life. It records each event in `events.jsonl` before calling each handler's `on_event`; ids are `<generation>:<seq>` and turns `<generation>:t<n>`. A turn started while one is open fails that one (`TurnFailed("superseded")`); an exit mid-turn fails it (`TurnFailed("exited")`) before the one `Exited`. A handler that raises is retried with the same ids after 1 s, doubling to 30 s, while the others go on; `deliveries.jsonl` keeps failed attempts 1, 2, 4, … and the delivered one. A handler still running when the harness exits records nothing more. `Stderr` and `ClientBound` stay in the log. After the worker's exit the harness waits up to 30 s for its handlers, then writes `exited`, answers the waiting `stop` and `close` requests, and exits; `harness.json` says `starting`, `running`, `draining` (the worker ended, its handlers still called; with `returncode`), `exited` (with `returncode` and `pending`) or `failed` (with the start `error`), `opening` while the client's opening line is unsent, `up` once its guard reported the client up ([Guard protocol](#guard-protocol)), and `dialog`, the dialog the client waits on (the first `Blocked`'s reason until any event but `Blocked`, `Stderr`, `Reply` or `ClientBound`); each pump of the worker writes its status to `harness.json` before its events are logged, so a view acting on an event reads the status it came with, and a start ending on that status still says a dialog whose `Blocked` it has not read yet. SIGTERM and SIGHUP close the worker, as does EOF on a headless start's caller pipe (`--caller FD`); the harness unblocks the signals its caller held across the spawn.

The control view never owns the worker: for a tui session, its caller exiting, its `events()` iterator closing or a display detaching leaves the harness supervising. A headless run dies with its caller: the starting view holds the write end of a pipe whose read end only the harness has, so once the view is dropped or its process ends, even by SIGKILL, the harness closes the worker. `events()` reads `events.jsonl`, accepting only the worker's generation in order. `stop`, `close` and `Session.retire` (also once its terminal is gone, its sid then checked against `session.json`) write a request to `requests/` and return once the harness confirms the worker ended and logged `Exited` and, past its handlers' drain, lets go of its lock (a handler's own request returns at the confirmed exit, since the drain waits for that handler), else raise `SessionError` (`Unanswered` when it cannot tell). `Session.retire` asks a bound owner before any Host probe but a listed terminal of another sid, so a worker whose tmux session is gone is still closed through it. A refused or unconfirmed stop or close leaves the harness supervising and sets `harness.json` `close_failed` (`op`, `error`, `at`); `ensemble-session close` then exits 1, even for a Record that looks stale on its Host. `send`, `press`, `screen`, `show` and `status` still reach a tmux worker's display, the tmux session, directly; every tui `send` and `press` holds the session's send lock (`send.lock` in its tui dir) over its whole input, so senders in any thread or process never interleave; the harness types the opening under it, never waiting for it, and only once no turn runs, a launch prompt's turn has started, and every send went in before the hook of a turn's start or end began (its `hooks.jsonl` record carries the sends seen as that hook began, a Docker worker's as its harness took the hook from the guard), so it never lands inside a turn: a start with a prompt gets it once its first turn ends. A send failing after its first action raises `Unanswered`, never retried. A Docker tui worker is remote (`harness.json` `"remote": true`): only its harness reaches it, so `send` (`{"text"}`, up to 1 MiB), `press` (`{"keys"}`, 1 to 64 key names) and `screen` (`{"lines"}`, null or 1 to 10000) are requests in `requests/` the harness serves through the worker; a bad field or a failure is a refusal that leaves the worker up, and after the exit they are refused. `display` (any worker, no fields), served the same way, opens the worker's display again where it is gone, answering `{"reopened"}`; a session of its name it does not own is refused, left alone. A headless view has no terminal: `send` and `press` raise `SessionError`, `screen`, `show` and `content` raise `NotShowable`; `alive` and `status` come from `harness.json`, `info` and `session_status` from the run's record, and an exception in its `events()` (not closing the iterator) stops the worker, as an unharnessed headless run does. `Session.attach` gives a control view only while the `owner` harness holds its lock and its `harness.json` names that session, sid and generation; any other `owner` is ignored, and a worker whose harness exited or died is attached and closed through its Host (on Docker a view of its guarded container, removed once its owner lock is free).

A tui worker runs under the guard ([Guard protocol](#guard-protocol)), and only its harness holds the guard's lifecycle pipe: `kill -9` of the harness ends guard and client, and its display (a tmux worker's: the tmux session of its name) ends no worker when killed. Its `session.json`, display and dir go only with their own worker generation. Resume (`ensemble-session resume`, a crew or delegate resume) of a worker whose harness runs returns its Record and starts nothing; it first asks that harness to open its display again where it was killed (a tmux worker's tmux session, or the `guard.py --attach` in it), on the same guard and generation, and shows a reopened one unless hidden; a refusal is said on stderr, the Record still returned; after a crash it starts a new harness, guard and client on the same sid, transcript, cwd, client, account and flags, waiting up to 10 s for a reaping guard. It is refused while the old generation is unknown (busy, exit 75; a guard killed before it confirmed its client's exit) and when the client keeps no transcript of the conversation (exit 2), never starting an empty one. `ensemble-session close` of such a worker without a running harness removes its record only once its generation is gone: it waits out reaping and fails busy, the record kept, while unknown. A tmux worker's way out of a permanent unknown: once no client of it is left, remove its guard dir, `<ENSEMBLE_CONTROL_ROOT, else the temp dir>/eg-<uid>/<key>` (`hosts/tmux/managed.py` `location`).

A headless worker runs under the guard too (`Host.worker(name, launch, terminal=False)`, named by `Spec.name`, else `headless-<generation>`; Docker refuses a nameless one: [Harness-owned workers](docker-host.md#harness-owned-workers)), its input ended at start (an empty `i`), so `kill -9` of its harness ends its client as well (on Docker: [Harness-owned workers](docker-host.md#harness-owned-workers)). Its output is read as lines (UTF-8, undecodable bytes replaced, universal newlines); a line over 8 MiB (`modes/headless.py` `LINE_MAX`) fails the run. A Host without a managed worker (the base `Host.worker` raises `SessionError("host <host> runs no managed worker")`) runs no headless session. On tmux its guard dir is a one-shot dir, `<ENSEMBLE_CONTROL_ROOT, else the temp dir>/eg-<uid>/o<15 hex>` keyed by host and generation (`hosts/tmux/managed.py` `one_shot`), never a tui's: a headless run named like a tui leaves that tui's dir alone, and two of one name may run at once. Its starting process holds `owner.lock` there and removes the dir at the confirmed close; after that process died (`kill -9`), the next tmux worker start removes each one-shot dir over 60 s old whose owner lock and leases are free (`sweep`).

An `in_process` adapter runs on a thread of the harness, so it ends with the harness; its control view has no terminal (`send`, `press`, `screen`: `SessionError`; `show`, `hide`, `attach_command`: `NotShowable`) and is alive while its harness holds its lock and has not logged `Exited`.

Any process of the user that can write the 0700 harness dir can stop or close its worker, the same trust as the user's tmux socket: the harness serves every well-formed request and authenticates none.

## Session harness interfaces

Frozen (WDRC-199, WDRC-226): a session harness owns each worker from start to close and calls the handlers its caller registers. Every TUI and headless adapter yields `TurnStarted`. [Session harness](#session-harness) names the starts it owns. The harness launches a tui worker through `TmuxHost.worker` or `DockerHost.worker`, the managed transport below ([Harness-owned workers](docker-host.md#harness-owned-workers)), a headless one with `terminal=False`; an unowned start keeps the in-session path on the tmux Host and is refused on Docker. No other client, Host or mode reads `Spec.handlers` yet.

**Handlers.** `Spec.handlers` is an ordered tuple of `HookCallback(module, state, roots)`, default `()`. The harness records each event before it calls each handler module's `on_event(EventCall)` in event order; a failure is retried with the same ids and never stops supervision. `EventCall` carries `session` (an `ensemble.session.Session` to send, stop or close through), `event`, `sid`, `generation` (the worker's start), `event_id`, `turn` (`None` outside a turn) and the handler's `state`. `on_event` returns `None`; returning or failing never closes the worker, only a close request through `session` does. Every event field (reply text, Blocked reason, TurnFailed detail) is untrusted worker output.

**Events.** `SessionEvent` is `TurnStarted | Activity | Reply | TurnEnd | TurnFailed | Blocked | LimitHit | Exited`. `TurnStarted` marks a turn its client accepted (the first prompt, a `send` or text typed into its terminal), never a replayed record. Tui adapters normalize `HookKind.TURN_STARTED`; headless adapters derive acceptance from native structured records. In-process `run()` will yield it when that adapter is migrated. `TurnEnd(reply, pending)` carries the turn's full last reply; `TurnFailed` and `TurnEnd` exclude each other within a turn; `Exited` comes once, last.

Claude and Codex use accepted-prompt hooks; Codex also observes native turn records, including failures without Stop. Codex turn ids reconcile the two sources and reject late old-turn terminal hooks. OpenCode starts a turn on a new root user-message id; busy/tool steps do not start turns, and asynchronous replies retain that id. Replay restores state without publishing a new `TurnStarted`.

Headless streams replies and diagnostics while retaining terminal candidates until both output streams drain and the exit code is known. Success requires native completion and exit 0. Nonzero exit, missing completion, native failure or an uninteractive permission request produces `TurnFailed` before `Exited`; a partial reply or EOF alone cannot succeed. Claude checks result subtype, error and permission-denial fields; Codex checks turn completion/failure; OpenCode requires a final `step_finish` with reason `stop`. A permission/question request stops the headless worker rather than waiting for an answer.

**Managed workers.** `Host.worker(name, launch, *, terminal, display=None, events=None, metadata=None) -> InteractiveProcess` replaces the worker-launch paths `terminal`, `detach` and `pipe`. It evolves the existing `InteractiveProcess`, which already inherits `send(action: Paste | Key)`, `press(keys: Sequence[KeyPress])`, `screen(lines=None)`, `state() -> Liveness`, `close() -> None` and `returncode() -> int | None`; there is no parallel `WorkerHandle`. Managed `close()` returns only after confirmed worker exit, else raises `SessionError` (`Unanswered` when unknown); `returncode()` supplies the confirmed exit code, never a guessed SIGHUP code. Headless handles reject `send`, `press` and `screen` with `NotShowable`; their input uses `write`.

Only the channel has new members: `generation: str` identifies the worker start; `read(timeout: float) -> list[WorkerMessage]` returns ordered messages, `[]` on timeout and nothing after the last `WorkerExit`; a broken transport raises `Unanswered` until exit is confirmed. `write(data: bytes) -> None` writes headless stdin; `write(b"")` ends it, and a write after the exit is refused. `tick(now: float) -> float | None` runs the Host's periodic duty and returns its next due time (the default `None` means no duty). The local handle drains stdout continuously into an 8 MiB queue. Overflow closes its lifecycle pipe, continues draining, and reports `Unanswered`; it never silently drops output and reports success. The guard ([Guard protocol](#guard-protocol)) is the client's direct parent and owns its PTY when `terminal=True`.

The new channel records have no previous public owner: `WorkerHook(label: str, payload: Mapping[str, object], delivery_id: str)` replaces the Docker relay's hook envelope and retry identity, `WorkerOutput(data: bytes, stream: str="stdout")` replaces separate headless stdout/stderr pipe readers, and `WorkerExit(returncode: int)` replaces host exit probes. `WorkerMessage` is their union. All payloads are untrusted; retain size/shape, sid and generation binding checks (FR-36). `Host.worker_liveness(name, generation) -> Liveness` replaces attach/reclaim probes after a harness dies: `LIVE` or `UNKNOWN` refuses a second owner, `ABSENT` permits resume. Local admission serializes starts and checks the prior generation's lease and completion record. An unconfirmed guard death remains `UNKNOWN`, even after its known PIDs disappear. Missing, damaged or mismatched existing records also remain `UNKNOWN`; there is no live-process adoption. The session harness must validate the original transcript and preserve sid, workspace, client and account before constructing a new launch.

| Host | Channel | Display |
|---|---|---|
| tmux | the guard's stdin and stdout over local pipes | a tmux session running `guard.py --attach` on its display socket |
| Docker | `docker exec -i` into the container, the guard there | `docker exec -it … guard.py --attach`; `tick` returns the next time its maintenance thread calls the broker, never calling it |

A Docker handle reports `UNKNOWN` from a broken channel until its container's removal is confirmed, then `ABSENT`; `returncode()` is the guard's `x` code, `None` when none came; after the removal `read()` returns `[]`.

**Extension migration.** The old methods other than `pipe` (deleted in WDRC-312) remain callable only during this implementation transition. The final Host must implement `worker` and `worker_liveness`; without `worker` it already runs no headless session ([Session harness](#session-harness)); there is no permanent unowned fallback. Adapt an existing `InteractiveProcess` subclass to the guard channel and confirmed-close contract. Move status/Record publication to the harness and display targeting to `Host.attach_target`; remove the obsolete methods below. A Tui maps its accepted-prompt hook to `HookKind.TURN_STARTED`; headless `events` and in-process `run` yield `TurnStarted` on accepted input. Native `on_hook(HookCall)` stays separate from registered `on_event(EventCall)`: the former converts client payloads, the latter receives normalized events. `EventCall` lives in `harness/types.py`, depending on `Session`; shared `engine.types` never imports the session API. Session users keep the default empty handlers tuple and handle the added event in exhaustive matches.

### End-state removals

These are approved-list candidates for deletion, not optional deprecations; each goes once no kept code calls it. `InteractiveProcess` survives as the sole managed handle. Its existing `Process`/`Interactive` bases are folded into it, preserving the reused declarations rather than duplicating them.

| Removed interface | Replacement | Deletion owner |
|---|---|---|
| `Host.terminal`, `Host.detach`, `Host.pipe` | `Host.worker` (`terminal=True` or `False`) | `Host.pipe`: deleted (WDRC-312); `Host.terminal`: the tmux in-session loop's removal; `Host.detach`: the same, once every delegate start is owned (the `-drive` still uses it) |
| `Host.attach`, `Host.reclaim` | `Session.attach` control view and `Host.worker_liveness` for crash-resume admission | the tmux in-session loop's removal |
| `Host.events_path`, `Host.prepare_events`, `Host.hook_callback` | harness-owned records/dispatch and `WorkerHook` on the guard channel | the tmux in-session loop's removal |
| public `Process` (both session surfaces), `Interactive` (extension), their exported members | fold preserved `name`, `send`, `press`, `screen`, `state`, `close`, `returncode` into existing `InteractiveProcess` | the tmux in-session loop's removal (the tmux terminal is a `Process`) |
| `InteractiveProcess.alive`, `.wait_exit`, `.kill`, `.identity`, `.token`, `.session_identity` | channel `state`, `generation`, `read`/`WorkerExit`, confirmed `close`; no caller-owned kill | the tmux in-session loop's removal |
| `InteractiveProcess.activity`, `.status`, `.mark`, `.row`, `.record`, `.publish`, `.display` | harness event/state/Record ownership; `Host.attach_target` retains display targeting | the tmux in-session loop's removal |
| extension `Piped`, `Lines`, all their exported members; internal `LocalPiped` | `InteractiveProcess.read`/`write`, `WorkerOutput`/`WorkerExit` | deleted (WDRC-312) |
| Docker `supervisor.py` (`Supervisor`, `main`) and Host `_spawn_supervisor` | harness dispatch/lifecycle; the Docker handle's maintenance thread (`hosts/docker/maintenance.py`) and `hooks.HookWorker` | deleted (WDRC-211) |
| Docker `relay.py` (`Queue`, `Daemon`, `answer`, `pump`, `connect`, `main`, relay request/reply protocol) and Host `_serve_relay` | guard hook socket and the same worker's long-lived `h`/output/exit channel | deleted (WDRC-211) |
| delegate harness worker ownership, runner-driven supervision/attempt lifetime | session `Spec.handlers`/`EventCall`; attempt-only Result and delegation policy | WDRC-208; WDRC-209 consumes attempt events |

`InteractiveProcess.tick` has no caller but is an approved addition (FR-26) and stays. "The tmux in-session loop's removal" is the follow-up that ends unowned starts on the tmux Host.

**Ownership.** `Session.start` will create the owner; `Session.attach` gets a control view, never a second owner; `events()` only observes, so closing its iterator, the caller exiting or a display detaching leaves a tui worker supervised (a headless run dies with its caller: [Session harness](#session-harness)); `stop`, `close` and `Session.retire` ask the owner and return only once it confirmed the worker ended, else raise `SessionError` (`Unanswered` when it cannot tell), so a caller releases a lock or removes a roster entry only after a confirmed end (FR-16, FR-23, FR-24).

**Delegate.** `Delegate.run`, `alive`, `status`, `resume` and `close` keep their signatures and the `Result` and `Outcome` formats. `run()` returns once a task attempt has its Result while the session and its handler go on; `alive()` and `Status.alive` say whether an attempt is active, so a run waiting on `needs_input` is not alive. `resume(input=...)` (CLI `--input`) accepts an answer or continuation even while the unlocked TUI/harness lives; the active-attempt check must not reject it just because the owner lives. If the worker died, resume creates new processes from the original transcript and refuses a live/unknown old generation. A directly accepted input after a terminal attempt opens the next attempt through `TurnStarted`; resume has already counted its attempt, so the same event does not count twice. In-attempt nudges/repair and replay never open another attempt.

Each attempt preserves `Begun`/`Progress`/one `Finished`, the existing Result/Outcome formats and Record `ctl`/`resume` control routing; subsequent attempts do not reuse the previous outcome or its completion marker. `close()` ends an active attempt once, never emits another failure for an ended attempt, and asks the session owner to close. Locks and roster entries remain until confirmed resource termination; close failures remain traceable. Delegate persisted `plan.session.handlers` lists are restored to tuples only when the key exists; old records default to `()`.

Implemented (WDRC-208) for **owned runs**, the tui runs a session harness owns: the Delegate preflights the start with the handler `ensemble.delegate.engine.handler` registered as `{"workdir", "worker": n}` (`n`, the attempt whose call starts the worker); a refused preflight plans the run without it, and the run keeps the earlier caller loop (every other mode, and an unowned tui on the tmux Host: [Session harness](#session-harness); on Docker an unowned start is refused). Each attempt has one owner: the call that starts a worker owns it while opening, that worker's first handled call binds it to the handler, and the handler's `TurnStarted` after an ending opens the next attempt (`delegate.json` keeps the earlier one's Result as `previous`); calls of another sid or worker are ignored. An ending stores the Result, then writes its notes (the attempt's own and `result`/`end`) and the run's outcome line, then sends `Finished`; an ending another owner stored first writes and sends nothing; for a locked run, it first closes it through the owner's view and claims the release only once that close returns. `run()` (a detached run's `-drive`) starts the worker and waits for its attempt's Result; it ends the attempt itself only while opening past `idle_timeout` (a give-up) or once its worker has been gone 35 s. A signal before it sends `Begun` (its TUI start) stores the stopped ending, then closes the worker; a signal after leaves the attempt to the handler. A stale out it cannot set aside is stored as the attempt's Result, with `closing` set, before its worker is stopped. A completed run is not alive from its `Finished` on, though its drive holds the run lock until it returns; meanwhile `status` reports it `working`, and `resume()` (and `resume(input=...)` unless a live worker takes the input) waits up to 30 s for that lock before it sweeps and starts. `resume(input=...)` on a live owned worker only sends the input; `resume()` and a replacing `run` first retire a gone worker outside the run mutex, under the run lock (which every start takes) and only while `delegate.json` still names the attempt they checked, so they follow its harness's end (its handler's last write-back) before the callback sweep. `close()` holds a shared lock, `run/delegate.close.lock`, from before it attaches the worker until the owner's close returns: meanwhile a `TurnStarted` opens no follow-up, and the client's exit the close causes (`TurnFailed("exited")` or `TurnFailed("process_exit")`, `Exited`) ends an active attempt as a stop (`stopped: Stopped: 143`, its `Finished` sent once), never as a failure, while other endings keep their own Result. Without a close, such an exit failure of an open turn waits for its `Exited`: a client killed by signal n ends the attempt interrupted (`stopped: Stopped: <128+n>`, left In Progress: [Board](board.md)), a nonzero exit as `the client exited <code>`. Once the close is confirmed, or its worker is gone, `delegate.json` `closing` is set (no follow-up of that worker until `resume(input=...)` or a new run clears it) and an attempt still active ends as that stop (a locked run's `release` first). A refused close drops the lock and leaves `closing` unset; input taken on an ended attempt during it opens no follow-up, the next does. `delegate.json` must hold `previous`, `bound` and the bool `closing`, and the callback lock both pids (`<child> <parent>`); anything else is invalid. Board write-back of each attempt: `docs/board.md` › Using the board.

### Guard protocol

`src/ensemble/session/harness/guard.py` is the standard-library-only guard program, run by path. `guard.py [--terminal [--display-grace]] --hook-socket PATH [--display-socket PATH] -- ARGV...` runs ARGV, with the cwd and environment it is given, in a session of its own; with `--terminal` the guard owns its PTY. `--display-grace`, which the tmux Host passes because it starts the display at launch (Docker starts one only when someone attaches), keeps a guard whose client exited before any display connected up for the display grace (2 s), so its first display still gets the final screen. `guard.py --attach PATH` connects the calling terminal to a running guard's display socket: a display only, so detaching or killing it never ends the worker; an unreachable socket prints one line (`guard: display PATH: <reason>`) and exits 1. `--lease PATH`, for a guard `docker exec` starts (which passes no descriptors), makes PATH's directory (0700), holds PATH locked for life and writes `state.json` beside it; a held lease exits 1 (`guard: lease held`). `guard.py --probe DIR` prints `live` (a guard holds `DIR/lease`), `exited` (free, `state.json` confirms the exit), `absent` (no DIR) or `dead`. Local managed launches expose the hook endpoint as `ENSEMBLE_GUARD_HOOK_SOCKET`. Only the harness holds the lifecycle pipe's write end; the guard's client launch closes all other descriptors, including the generation lease. Display clients receive raw terminal output, while the screen request returns current terminal cells, with bounded scrollback for an explicit line count; erasing a dialog removes it from the current screen. New displays receive a bounded snapshot before live output.

The channel is the guard's stdin (from its harness) and stdout (to it): frames of one kind byte, a 4-byte big-endian payload length (at most 64 MiB) and the payload; a JSON payload is one UTF-8 object.

| From | Kind | Payload |
|---|---|---|
| harness | `i` input | raw bytes for the PTY, else the client's stdin; empty: end of input (the client's stdin closes once the bytes before it are written; ignored with a PTY) |
| harness | `q` screen | `{"lines": int or null}` |
| harness | `c` close | `{}`: end the client, then `x` |
| harness | `k` hook taken | `{}`: the oldest `h` not yet taken; the guard then acks that hook to its client |
| guard | `s` started | `{"pid": int}`, once, after exec |
| guard | `u` up | `{}`, once, when its client still runs 5 s after exec (checked right after reaping it), so never for a client that exited sooner |
| guard | `h` hook | `{"label": str, "payload": object, "delivery_id": str}`, retry-stable identity |
| guard | `o`, `e` output | raw bytes a headless client wrote to stdout, stderr |
| guard | `v` view | `{"text": str}`: the PTY's last `q` lines, control sequences removed |
| guard | `x` exit | `{"returncode": int}`, last; -N for signal N |

EOF on its stdin is a close: its harness closed the channel or died. The guard tracks descendants by PID and kernel start time, sends SIGHUP, escalates to SIGTERM after 0.5 s and SIGKILL after 1.5 s, and reaps adopted children. Linux requires `PR_SET_CHILD_SUBREAPER` and uses pidfds for signaling. Its stdout is non-blocking, with an 8 MiB limit; pressure stops client/hook reads without stopping lifecycle monitoring. After confirmed tree exit, available output precedes one final `x`. Unconfirmed exit produces a broken channel, never a guessed successful exit. No frame starts a host process.

**macOS limitation (approved WDRC-203 decision, 2026-10-26).** Closing the guard's PTY hangs up the foreground client. There is no subreaper or PID namespace: own-process-group, HUP-ignoring and setsid descendants can survive a killed guard; a fast setsid daemon can escape even a live guard. The macOS display's `pane-died` hook runs `tmux kill-session`, best effort; a surviving harness also closes the matching display on channel failure. This does not guarantee removal of escapees. No uid-per-run scheme is used. Docker's final backstop is stopping its container (WDRC-207); the Linux guard alone cannot replace that containment after it is killed. These decisions use the WDRC-203 mini report; real-client crash/resume acceptance on the mini is WDRC-212's.

A hook invocation creates one `delivery_id` (32 lowercase hex characters) before its first send and keeps it through retries; a different invocation has a fresh id even with identical label/payload. It connects to `--hook-socket`, writes one `{"label", "payload", "delivery_id"}` line and gets `{"ok": true, "delivery_id": str}` once its harness took that `h` frame (`k`, sent right after the harness read it, so a Docker hook is stamped before its client goes on), or, while the guard closes, once the frame is written. A missing or mismatched acknowledgement retries the same request/id. The harness deduplicates by the trusted channel's generation and delivery_id; the Docker handle journals each id once, its first content winning. Identity is never inferred from label/payload equality. This replaces the relay's generation/sequence acknowledgement contract; it does not add dispatch behavior here.

## Recall a closed run

`ensemble-crew close <ID>` ends a board run's sessions and drops its roster entry once each session's owner confirmed its end; its conversation stays. A failed close keeps the entry, exits 1 and appends one JSON line (`at`, `name`, `sid`, `ctl`, `reason`) to `~/.ensemble/managers/<manager>/close-failures.jsonl`. To continue it:

```sh
ensemble-crew reopen [--work-dir <work_dir>] <ID> [--display D]
```

It reads the run record, `<work_dir>/work/<ID>/run/delegate.json` (`--work-dir` only when the board's `work_dir` is not `~/.ensemble`), and resumes its session id as worker `<agent name>` on the run's Host, client, cwd and Claude account: a Docker run comes back in a new container with its client history and [workspace volume](docker-host.md#workspace-volumes). The worker is tracked like any other; `ensemble-crew close <agent name>` ends it. It brings back the conversation, not the run: where its start is owned (every built-in client's tui, on tmux or Docker), it runs under a [session harness](#session-harness) of its own with no handlers, so its events are only logged: no outcome or write-back, and the board is untouched. A conversation known only by its session id: `ensemble-crew worker <name> --resume <sid> --client <client> --cwd <cwd> [--host <host>]`.

These do not recall it:

- `ensemble-crew resume <ID>` resumes roster entries only, and `close` removed it: `not in roster; a closed issue run: ensemble-crew reopen --manager=<manager> <ID>`.
- `ensemble-orchestrator resume <ID>` continues an interrupted run only; a run with an outcome prints its Record, starts nothing and says so on stderr.
- The run comment's `ensemble-session client-resume …` runs the client on this machine, outside ensemble; a Docker run's history is not there.

`reopen` fails when:

- the issue finished and prune cleaned it: prune deletes the run's agent files, its cwd among them, at once ([State cleanup](board.md#state-cleanup)), and the run record 7 days later;
- `work/<ID>/run/delegate.json` is gone (pruned, or deleted by hand): `no run record` (exit 1);
- the run, or a session holding its session id, is still alive, or its liveness is unknown (a status or the session listing failed): exit 2; close it first, or `ensemble-crew forget` an entry whose work is gone;
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

## OpenCode commander

A commander can run in OpenCode 1.x (checked against 1.19.3), with ensemble's `plugin` entry in `~/.config/opencode/opencode.json` ([Install](../README.md#install); upgrading it: [Upgrading › OpenCode plugin by hand](upgrading.md#opencode-plugin-by-hand)), or a checkout's `"plugin": ["file://<checkout>/.opencode-plugin/ensemble.js"]`. `opencode plugin` installs npm packages only. Start `opencode` in the `ctmux` shell, then run `/ensemble:manage`.

The plugin adds:

- `/ensemble:act-as`, `/ensemble:board` and `/ensemble:manage`: commands with the corresponding skill text.
- this plugin's `bin/` first on the shell tool's `PATH`.
- the SessionStart re-arm ([Session settings](#session-settings)): for a commander under `ensemble-crew hold`, a prompt at startup, resume and after compaction. Sub-agent sessions get none.

It turns itself off, adding nothing, under `ENSEMBLE_AGENT_SESSION`: an OpenCode worker or role run on the tmux Host loads your global `opencode.json`, and `[clients.opencode] flags` cannot turn a plugin off.

Limits:

- Manual monitoring only (`/ensemble:manage` › Start): OpenCode has no Monitor, and its shell tool has no background mode and no completion notification. The commander reads events with `ensemble-crew pending --content` at the start of each active turn; nothing wakes an idle commander.
- The skills' `allowed-tools` and `disable-model-invocation` mean nothing to OpenCode: its own `permission` settings decide which commands ask. The three are commands, never skills the model loads itself.
- No mod hides the `ensemble-crew` tool calls.
