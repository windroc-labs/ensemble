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

A built-in Claude, Codex or OpenCode tui or headless session started on the `tmux` Host runs under a session harness of its own, when the session class is configured with its default callables (a test that injects a class loader, probe, Host factory or viewer factory keeps the earlier path). Every other client, Host and mode (in-process, Docker) starts as before, and refuses non-empty `Spec.handlers`. `Session.preflight` finds each handler module without importing it.

`Session.start` writes the start to `~/.ensemble/harness/<32 hex>/request.json` (mode 0600; it holds `Spec.env`, and the harness deletes it once read), runs `python3 -P -s -m ensemble.session.harness.process <dir>` detached (the caller's interpreter), and returns a control view once the harness runs the worker. A start error in the harness is raised in the caller with its class and message, and its dir is removed. The tui session's `session.json` names the dir as `owner`; a headless run, the harness's own child, has no `session.json` and stays nameless unless its spec names it. A harness that does not start is sent SIGTERM (headless) or killed.

```mermaid
flowchart LR
  E[Session.start] -->|request.json| H[session harness]
  H -->|holds harness.lock| W[tui or headless worker]
  H -->|events.jsonl, harness.json| V[control views]
  V -->|requests/| H
  H -->|on_event| D[handlers]
```

The harness holds `harness.lock` for life. It records each event in `events.jsonl` before calling each handler's `on_event`; ids are `<generation>:<seq>` and turns `<generation>:t<n>`. A turn started while one is open fails that one (`TurnFailed("superseded")`); an exit mid-turn fails it (`TurnFailed("exited")`) before the one `Exited`. A handler that raises is retried with the same ids after 1 s, doubling to 30 s, while the others go on; `deliveries.jsonl` keeps failed attempts 1, 2, 4, … and the delivered one. `Stderr` and `ClientBound` stay in the log. After the worker's exit the harness waits up to 30 s for its handlers, then exits; `harness.json` says `starting`, `running`, `exited` (with `returncode` and `pending`) or `failed` (with the start `error`), and `opening` while the client's opening line is unsent. SIGTERM and SIGHUP close the worker.

The control view never owns the worker: its caller exiting, its `events()` iterator closing or a display detaching leaves the harness supervising. `events()` reads `events.jsonl`, accepting only the worker's generation in order. `stop`, `close` and `Session.retire` write a request to `requests/` and return once the harness confirms the worker ended and logged `Exited`, else raise `SessionError` (`Unanswered` when it cannot tell). `send`, `press`, `screen`, `show` and `status` still reach the tmux session directly; a `send` while `opening_pending` is set can interleave with the harness typing the opening line. A headless view has no terminal: `send` and `press` raise `SessionError`, `screen`, `show` and `content` raise `NotShowable`; `alive` and `status` come from `harness.json`, `info` and `session_status` from the run's record, and an exception in its `events()` (not closing the iterator) stops the worker, as an unharnessed headless run does. `Session.attach` gives a control view only while the `owner` harness holds its lock and its `harness.json` names that session, sid and generation; any other `owner` is ignored, and a worker whose harness exited or died is attached and closed the earlier way.

Any process of the user that can write the 0700 harness dir can stop or close its worker, the same trust as the user's tmux socket: the harness serves every well-formed request and authenticates none.

## Session harness interfaces

Frozen for WDRC-199 (WDRC-226): a session harness will own each worker from start to close and call the handlers its caller registers. Every TUI and headless adapter yields `TurnStarted`. [Session harness](#session-harness) runs it for the built-in Claude, Codex and OpenCode tui and headless sessions on tmux, calling their handlers. `TmuxHost.worker` implements the managed transport below, but that harness still launches its worker on the legacy tui path or `Host.pipe`, as every other start does. No other start reads `Spec.handlers` yet. The tickets that implement each part change none of these signatures.

**Handlers.** `Spec.handlers` is an ordered tuple of `HookCallback(module, state, roots)`, default `()`. The harness records each event before it calls each handler module's `on_event(EventCall)` in event order; a failure is retried with the same ids and never stops supervision. `EventCall` carries `session` (an `ensemble.session.Session` to send, stop or close through), `event`, `sid`, `generation` (the worker's start), `event_id`, `turn` (`None` outside a turn) and the handler's `state`. `on_event` returns `None`; returning or failing never closes the worker, only a close request through `session` does. Every event field (reply text, Blocked reason, TurnFailed detail) is untrusted worker output.

**Events.** `SessionEvent` is `TurnStarted | Activity | Reply | TurnEnd | TurnFailed | Blocked | LimitHit | Exited`. `TurnStarted` marks a turn its client accepted (the first prompt, a `send` or text typed into its terminal), never a replayed record. Tui adapters normalize `HookKind.TURN_STARTED`; headless adapters derive acceptance from native structured records. In-process `run()` will yield it when that adapter is migrated. `TurnEnd(reply, pending)` carries the turn's full last reply; `TurnFailed` and `TurnEnd` exclude each other within a turn; `Exited` comes once, last.

Claude and Codex use accepted-prompt hooks; Codex also observes native turn records, including failures without Stop. Codex turn ids reconcile the two sources and reject late old-turn terminal hooks. OpenCode starts a turn on a new root user-message id; busy/tool steps do not start turns, and asynchronous replies retain that id. Replay restores state without publishing a new `TurnStarted`.

Headless streams replies and diagnostics while retaining terminal candidates until both output streams drain and the exit code is known. Success requires native completion and exit 0. Nonzero exit, missing completion, native failure or an uninteractive permission request produces `TurnFailed` before `Exited`; a partial reply or EOF alone cannot succeed. Claude checks result subtype, error and permission-denial fields; Codex checks turn completion/failure; OpenCode requires a final `step_finish` with reason `stop`. A permission/question request stops the headless worker rather than waiting for an answer.

**Managed workers.** `Host.worker(name, launch, *, terminal, display=None, events=None, metadata=None) -> InteractiveProcess` replaces the worker-launch paths `terminal`, `detach` and `pipe`. It evolves the existing `InteractiveProcess`, which already inherits `send(action: Paste | Key)`, `press(keys: Sequence[KeyPress])`, `screen(lines=None)`, `state() -> Liveness`, `close() -> None` and `returncode() -> int | None`; there is no parallel `WorkerHandle`. Managed `close()` returns only after confirmed worker exit, else raises `SessionError` (`Unanswered` when unknown); `returncode()` supplies the confirmed exit code, never a guessed SIGHUP code. Headless handles reject `send`, `press` and `screen` with `NotShowable`; their input uses `write`.

Only the channel has new members: `generation: str` identifies the worker start; `read(timeout: float) -> list[WorkerMessage]` returns ordered messages, `[]` on timeout and nothing after the last `WorkerExit`; a broken transport raises `Unanswered` until exit is confirmed. `write(data: bytes) -> None` writes headless stdin. `tick(now: float) -> float | None` runs the Host's periodic duty and returns its next due time (the default `None` means no duty). The local handle drains stdout continuously into an 8 MiB queue. Overflow closes its lifecycle pipe, continues draining, and reports `Unanswered`; it never silently drops output and reports success. The guard ([Guard protocol](#guard-protocol)) is the client's direct parent and owns its PTY when `terminal=True`.

The new channel records have no previous public owner: `WorkerHook(label: str, payload: Mapping[str, object], delivery_id: str)` replaces the Docker relay's hook envelope and retry identity, `WorkerOutput(data: bytes, stream: str="stdout")` replaces separate headless stdout/stderr pipe readers, and `WorkerExit(returncode: int)` replaces host exit probes. `WorkerMessage` is their union. All payloads are untrusted; retain size/shape, sid and generation binding checks (FR-36). `Host.worker_liveness(name, generation) -> Liveness` replaces attach/reclaim probes after a harness dies: `LIVE` or `UNKNOWN` refuses a second owner, `ABSENT` permits resume. Local admission serializes starts and checks the prior generation's lease and completion record. An unconfirmed guard death remains `UNKNOWN`, even after its known PIDs disappear. Missing, damaged or mismatched existing records also remain `UNKNOWN`; there is no live-process adoption. The session harness must validate the original transcript and preserve sid, workspace, client and account before constructing a new launch.

| Host | Channel | Display |
|---|---|---|
| tmux | the guard's stdin and stdout over local pipes | a tmux session running `guard.py --attach` on its display socket |
| Docker | `docker exec -i` into the container, the guard there | `docker exec -it … guard.py --attach`; `tick` runs the broker's credential maintenance |

**Extension migration.** The old methods remain callable only during this implementation transition. The final Host must implement `worker` and `worker_liveness`; there is no permanent unowned fallback. Adapt an existing `InteractiveProcess` subclass to the guard channel and confirmed-close contract. Move status/Record publication to the harness and display targeting to `Host.attach_target`; remove the obsolete methods below. A Tui maps its accepted-prompt hook to `HookKind.TURN_STARTED`; headless `events` and in-process `run` yield `TurnStarted` on accepted input. Native `on_hook(HookCall)` stays separate from registered `on_event(EventCall)`: the former converts client payloads, the latter receives normalized events. `EventCall` lives in `harness/types.py`, depending on `Session`; shared `engine.types` never imports the session API. Session users keep the default empty handlers tuple and handle the added event in exhaustive matches.

### End-state removals

These are approved-list candidates for deletion, not optional deprecations. This interface-only PR leaves used legacy paths executable; the implementation tickets remove them after their replacements work. `InteractiveProcess` survives as the sole managed handle. Its existing `Process`/`Interactive` bases are folded into it, preserving the reused declarations rather than duplicating them.

| Removed interface | Replacement | Deletion owner |
|---|---|---|
| `Host.terminal`, `Host.detach`, `Host.pipe` | `Host.worker` (`terminal=True` or `False`) | WDRC-204/206 local, WDRC-205 headless, WDRC-207 Docker; WDRC-208 removes delegate callers; WDRC-211 removes ABCs |
| `Host.attach`, `Host.reclaim` | `Session.attach` control view and `Host.worker_liveness` for crash-resume admission | WDRC-206/207; WDRC-210 crew callers; WDRC-211 ABCs |
| `Host.events_path`, `Host.prepare_events`, `Host.hook_callback` | harness-owned records/dispatch and `WorkerHook` on the guard channel | WDRC-204/207; WDRC-211 ABCs |
| public `Process` (both session surfaces), `Interactive` (extension), their exported members | fold preserved `name`, `send`, `press`, `screen`, `state`, `close`, `returncode` into existing `InteractiveProcess` | WDRC-211 after WDRC-204..210 migrate callers |
| `InteractiveProcess.alive`, `.wait_exit`, `.kill`, `.identity`, `.token`, `.session_identity` | channel `state`, `generation`, `read`/`WorkerExit`, confirmed `close`; no caller-owned kill | WDRC-206/207 implementations; WDRC-211 base cleanup |
| `InteractiveProcess.activity`, `.status`, `.mark`, `.row`, `.record`, `.publish`, `.display` | harness event/state/Record ownership; `Host.attach_target` retains display targeting | WDRC-204/207; WDRC-211 base cleanup |
| extension `Piped`, `Lines`, all their exported members; internal `LocalPiped` | `InteractiveProcess.read`/`write`, `WorkerOutput`/`WorkerExit` | WDRC-205/207; WDRC-211 exports/ABC cleanup |
| Docker `supervisor.py` (`HookWorker`, `Supervisor`, `main`, maintenance/deadline helpers) and Host `_supervise` | harness dispatch/lifecycle; Docker handle `tick` reuses broker maintenance policy | WDRC-207 |
| Docker `relay.py` (`Queue`, `Daemon`, `answer`, `pump`, `connect`, `main`, relay request/reply protocol) and Host `_serve_relay` | guard hook socket and the same worker's long-lived `h`/output/exit channel | WDRC-207; retain equivalent input validation, no second relay daemon |
| delegate harness worker ownership, runner-driven supervision/attempt lifetime | session `Spec.handlers`/`EventCall`; attempt-only Result and delegation policy | WDRC-208; WDRC-209 consumes attempt events |

For WDRC-211, the cumulative snapshot diff against `wdrc-199-session-harness` must be this freeze's additions/changes **plus** the removals above: delete `ensemble.session.Process` and every `Process.*` key; delete extension `Process`, `Interactive`, `Piped`, `Lines` and all their member keys; delete the eight Host methods above on both surfaces; delete the listed obsolete `InteractiveProcess` members; change its base to `abc.ABC` after folding the preserved members. No other exports, fields, defaults or signatures change. `tests/delegate_interface.json` stays byte-for-byte unchanged. These removals are not claimed as already implemented in this PR.

**Ownership.** `Session.start` will create the owner; `Session.attach` gets a control view, never a second owner; `events()` only observes, so closing its iterator, the caller exiting or a display detaching leaves the worker supervised; `stop`, `close` and `Session.retire` ask the owner and return only once it confirmed the worker ended, else raise `SessionError` (`Unanswered` when it cannot tell), so a caller releases a lock or removes a roster entry only after a confirmed end (FR-16, FR-23, FR-24).

**Delegate.** `Delegate.run`, `alive`, `status`, `resume` and `close` keep their signatures and the `Result` and `Outcome` formats. `run()` returns once a task attempt has its Result while the session and its handler go on; `alive()` and `Status.alive` say whether an attempt is active, so a run waiting on `needs_input` is not alive. `resume(input=...)` (CLI `--input`) accepts an answer or continuation even while the unlocked TUI/harness lives; the active-attempt check must not reject it just because the owner lives. If the worker died, resume creates new processes from the original transcript and refuses a live/unknown old generation. A directly accepted input after a terminal attempt opens the next attempt through `TurnStarted`; resume has already counted its attempt, so the same event does not count twice. In-attempt nudges/repair and replay never open another attempt.

Each attempt preserves `Begun`/`Progress`/one `Finished`, the existing Result/Outcome formats and Record `ctl`/`resume` control routing; subsequent attempts do not reuse the previous outcome or its completion marker. `close()` ends an active attempt once, never emits another failure for an ended attempt, and asks the session owner to close. Locks and roster entries remain until confirmed resource termination; close failures remain traceable. This is the target contract, not behavior implemented by this freeze. Delegate persisted `plan.session.handlers` lists are restored to tuples only when the key exists; old records default to `()` and no callbacks are reconstructed or invoked here.

### Guard protocol

`src/ensemble/session/harness/guard.py` is the standard-library-only guard program, run by path. `guard.py [--terminal] --hook-socket PATH [--display-socket PATH] -- ARGV...` runs ARGV, with the cwd and environment it is given, in a session of its own; with `--terminal` the guard owns its PTY. `guard.py --attach PATH` connects the calling terminal to a running guard's display socket: a display only, so detaching or killing it never ends the worker. Local managed launches expose the hook endpoint as `ENSEMBLE_GUARD_HOOK_SOCKET`. Only the harness holds the lifecycle pipe's write end; the guard's client launch closes all other descriptors, including the generation lease. Display clients receive raw terminal output, while the screen request returns current terminal cells, with bounded scrollback for an explicit line count; erasing a dialog removes it from the current screen. New displays receive a bounded snapshot before live output.

The channel is the guard's stdin (from its harness) and stdout (to it): frames of one kind byte, a 4-byte big-endian payload length (at most 64 MiB) and the payload; a JSON payload is one UTF-8 object.

| From | Kind | Payload |
|---|---|---|
| harness | `i` input | raw bytes for the PTY, else the client's stdin |
| harness | `q` screen | `{"lines": int or null}` |
| harness | `c` close | `{}`: end the client, then `x` |
| guard | `s` started | `{"pid": int}`, once, after exec |
| guard | `h` hook | `{"label": str, "payload": object, "delivery_id": str}`, retry-stable identity |
| guard | `o`, `e` output | raw bytes a headless client wrote to stdout, stderr |
| guard | `v` view | `{"text": str}`: the PTY's last `q` lines, control sequences removed |
| guard | `x` exit | `{"returncode": int}`, last; -N for signal N |

EOF on its stdin is a close: its harness closed the channel or died. The guard tracks descendants by PID and kernel start time, sends SIGHUP, escalates to SIGTERM after 0.5 s and SIGKILL after 1.5 s, and reaps adopted children. Linux requires `PR_SET_CHILD_SUBREAPER` and uses pidfds for signaling. Its stdout is non-blocking, with an 8 MiB limit; pressure stops client/hook reads without stopping lifecycle monitoring. After confirmed tree exit, available output precedes one final `x`. Unconfirmed exit produces a broken channel, never a guessed successful exit. No frame starts a host process.

**macOS limitation (approved WDRC-203 decision, 2026-10-26).** Closing the guard's PTY hangs up the foreground client. There is no subreaper or PID namespace: own-process-group, HUP-ignoring and setsid descendants can survive a killed guard; a fast setsid daemon can escape even a live guard. The macOS display's `pane-died` hook runs `tmux kill-session`, best effort; a surviving harness also closes the matching display on channel failure. This does not guarantee removal of escapees. No uid-per-run scheme is used. Docker's final backstop is stopping its container (WDRC-207); the Linux guard alone cannot replace that containment after it is killed. These decisions use the WDRC-203 mini report; the current implementation still needs composed WDRC-204 harness and real-client crash/resume acceptance on the mini.

A hook invocation creates one `delivery_id` (32 lowercase hex characters) before its first send and keeps it through retries; a different invocation has a fresh id even with identical label/payload. It connects to `--hook-socket`, writes one `{"label", "payload", "delivery_id"}` line and gets `{"ok": true, "delivery_id": str}` once the guard has written its `h` frame. A missing or mismatched acknowledgement retries the same request/id. The harness deduplicates by the trusted channel's generation and delivery_id, rejects an id reused with different content, and reuses the recorded normalized EventCall ids on redelivery. Identity is never inferred from label/payload equality. This replaces the relay's generation/sequence acknowledgement contract; it does not add dispatch behavior here.

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
