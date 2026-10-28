# Sessions

[Architecture](architecture.md) · [Docker operations](docker-host.md) · [Credential access](credentials.md)

## Settings and trust

For settings to change on this machine, use the comments and `# local:` examples in
[`session/config.toml`](../src/ensemble/session/config.toml) and
[`delegate/config.toml`](../src/ensemble/delegate/config.toml). List overrides replace the whole list:
when changing client flags or role policy, preserve the entries you still need.
Permission modes belong to session config; role flags belong to delegate config.

Project instructions load only in a trusted cwd. Select the role's `cwd` in delegate configuration, then include
that directory in `trusted_dirs`; trusting it alone does not select it. A workdir's `agent/` is intentionally
untrusted. Put task files it must edit in its writable dirs
rather than waiting for an approval that cannot grant a new path. Codex's packaged `auto` mode has no filesystem
sandbox: directory grants are not an isolation boundary. Use Docker for untrusted work.

Worker sessions do not load ensemble's own plugin; start the commander yourself to use `/ensemble:manage`.
Commands that would start more sessions refuse `ENSEMBLE_AGENT_SESSION=1`; this prevents accidental recursion,
not a hostile client's actions. Role runs also disable configured MCP servers. Codex plugins or managed servers
can still supply tools; inspect the effective client configuration before trusting it with outside services.

To re-enable a packaged Codex tool, edit `[session.clients.codex] flags` in `~/.ensemble/config.local.toml`,
copying the current packaged list and removing only the entries you intend to change. Session flags are reread
on resume; Codex's role policy and writable grants instead come from the original launch. Claude resumes take
the current role policy. A native `client-resume` runs here, with this machine's permission mode, outside ensemble's hooks.

OpenCode's permission rules are not filesystem isolation. A pre-approved command permits redirections too:
an allowed `git status` also allows `git status > FILE`. External-directory reads cover aliases and resolved paths;
write grants cover resolved paths only, so use those for writes. Except for `.git`, path matching is case-sensitive:
on a case-insensitive disk, a differently cased read directory or `OPENCODE.JSON` can bypass edit denies.
Its GPT `apply_patch` tool checks only a moved file's source, not its destination. Use Docker containment for
untrusted work rather than relying on these rules to prevent writes.

Commander monitoring and acknowledgement belong to
[`/ensemble:manage`](../skills/manage/SKILL.md). After installing or updating the Codex plugin, open `/hooks`
and review and trust its SessionStart command; a changed hook needs trust again. Installation alone does not
enable that prompt. See [Codex plugin hooks](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).

## Working with a session

`ensemble-session --help` is the command reference. Use `ls` to find a session's name, state and sid;
`read`, `send` and `press` act on its running worker. Text sent during a turn joins that turn: Codex takes it
immediately, Claude after its current tool, OpenCode after its current step. For Codex and Claude,
`ensemble-crew pending --content` exposes intermediate replies when the session has an events file.

| Need | Action |
|---|---|
| Start without a pane | `ensemble-session start NAME --display none`; `show NAME` makes it visible |
| Keep a session hidden | Hide it or close its pane; hidden state survives restart/resume until `show` |
| Recover a lost display | `ensemble-session resume NAME`; a live owner reopens its display without starting a second worker |
| Continue after a crash | The same resume command, once the old generation is confirmed gone |
| End it | `ensemble-session close NAME`; a failed close retains ownership records for retry |

An attempt ending does not close its TUI. Close idle retained sessions when they should release capacity or an
environment lock. Detaching a display or stopping an event reader leaves a TUI running; a headless run ends
with its caller. A live owner can accept follow-up input even after an outcome, including `needs_input`.

## Capacity and recovery

Configure a client's `max_runs` using session config's examples; board role limits and usage gates are separate
([board operation](board.md#operating-the-board)). If admission names holding sessions, close those no longer
needed. A failed inventory or unknown worker stays counted. Reconcile using the original Host configuration;
close its sessions before changing that configuration, even an unrelated Host setting.

A resume refuses a live or unknown previous generation rather than starting a duplicate. Wait for reaping,
then retry. For a permanently unknown tmux generation, first confirm no client or descendant remains, then
remove only its guard directory; its location is defined by
[`managed.location`](../src/ensemble/session/hosts/tmux/managed.py). Missing native history cannot be repaired
by resume; recover the history or start a new conversation explicitly. OpenCode can recover a headless session
whose id was not yet bound through its saved title; if it never saved a conversation, it may start fresh.

On macOS, detached or signal-ignoring descendants can survive even a live guard: there is no process namespace
or subreaper. Inspect and end escapees yourself before removing recovery state. Docker's containment ends only
when its container stops; see [security limits](credentials.md#security-limits).

Any process that can write your harness's 0700 directory can stop or close its worker: well-formed control
requests have no caller authentication. Treat that directory like your tmux socket, not as isolation from
other processes running as you.

## Recall a closed run

From the commander's `ensemble-crew hold` shell (else add `--manager MANAGER`):

```sh
ensemble-crew reopen ID
```

This recalls the latest run's conversation as a plain worker; it does not resume the role, change the board,
or write an outcome. For a nondefault board work directory, add `--work-dir PATH`.
To recall an earlier conversation by its native session id:

```sh
ensemble-crew worker NAME --resume SID --client CLIENT --cwd CWD
```

`ensemble-crew resume ID` requires a roster entry, removed by `close`.
`ensemble-orchestrator resume ID` continues an interrupted role run, not one with an outcome.
A run comment's `client-resume` runs on this machine and cannot find Docker-only history.

Reopen needs the run record, cwd and client history. Prune can remove them
([cleanup](board.md#state-cleanup)); a removed Docker workspace returns empty. If a run/session still lives or
its status is unknown, close it first. `forget` discards a roster entry only when its work is already gone.
Choose a Claude account with `--account` if needed; current account selection is described in
[Docker operations](docker-host.md#claude-accounts).

## OpenCode commander

Install the ensemble plugin as in [README](../README.md#install), or use
`"plugin": ["file://<checkout>/.opencode-plugin/ensemble.js"]` for a checkout. `opencode plugin` installs npm
packages only. Start OpenCode 1.x in the `ctmux` shell, then run `/ensemble:manage`.

OpenCode monitoring is manual: no Monitor tool, background shell or completion notification wakes an idle
commander. The plugin supplies the three ensemble commands and shell PATH; its re-arm prompt applies only
under `ensemble-crew hold`, not subagent sessions. OpenCode's own permission settings govern commands;
skill `allowed-tools` and `disable-model-invocation` have no effect. Its display has no mod hiding tool calls.
The plugin disables itself inside workers. Provider, version and transcript operations live in
[OpenCode sessions](customizing-roles-and-tasks.md#opencode-sessions).

## Implementation contracts

For extension work, use the definitions rather than a second API catalogue:

| Contract | Source |
|---|---|
| Session public surface and launch/event types | [`session/__init__.py`](../src/ensemble/session/__init__.py), [`engine/types.py`](../src/ensemble/session/engine/types.py) |
| Host managed handle and crash admission | [`hosts/base.py`](../src/ensemble/session/hosts/base.py) |
| Harness ownership, event dispatch and handler calls | [`harness/`](../src/ensemble/session/harness/), [`harness/types.py`](../src/ensemble/session/harness/types.py) |
| Guard framing, displays and process shutdown | [`harness/guard.py`](../src/ensemble/session/harness/guard.py) |
| Delegate attempt ownership | [`engine/handler.py`](../src/ensemble/delegate/engine/handler.py), [`engine/record.py`](../src/ensemble/delegate/engine/record.py) |
