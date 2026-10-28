# Upgrading

## Credential rotation

Broker [command usage](../src/broker/main.go) owns login syntax; replacing a stored login is not upstream revocation.

| Credential | External action |
|---|---|
| Host `gh` login | Revoke GitHub CLI access in GitHub Settings › Applications; `gh auth logout` only removes local access |
| GitHub App key | Generate a replacement and delete the old key in the App settings, then import the replacement |
| OpenCode provider key / environment secret | Revoke or rotate at its source, then import the new value; close existing containers to replace their static copies |
| Claude setup-token / broker Codex login | Neither has a documented upstream revocation route; obtain a new token/login after suspected exposure |
| Linear app | Rotate its client secret in Linear API Applications; this invalidates all app tokens. Follow the configured writer's [renewal procedure](#linear-broker-renewal) |

## Broker protocol 34: credential requests

Before upgrading, close workers started before this release; they cannot be resumed after it.
Close running Docker sessions, then run `scripts/install.sh --docker` to install the protocol 34 broker.
Existing GitHub/env configuration stays in place; request kinds do not yet replace those grants.

### Linear container credentials

Ordinary issue runs now need a separate read-only app in `[orchestrator.linear.reader]`; run
`scripts/install.sh --board linear` to configure it, or set `client_id`, `secret_service`, `token_service`,
`user_id` and `renewal = "broker"` there yourself. Enable client credentials on that app. Enroll it through
`ensemble-broker login linear` with `name = reader`, `kind = reader` and those public app fields, following
[Linear broker renewal](#linear-broker-renewal). Its Client ID and Keychain services must differ from every
write app's: requesting different scopes on the same app revokes its existing tokens. Reader maintenance and
container issuance always use `read`. No client secret is delivered to a worker.

The orchestrator uses this reader by default; the `decompose` task keeps its existing explicit role write grant.
Restart existing workers after enrollment so their saved credential specs include access. A failed re-read
reports an error; the manager sends the current description and comments through the run's existing input channel.

Before declaring a role app in `linear`:

1. Enable client credentials on the existing role app; keep the same scopes for host and container.
2. Enroll it using [Linear broker renewal](#linear-broker-renewal), with that role's name and public app values,
   `kind = role`, and `renewal = "broker"` for its host transport. Complete the cutover before starting callers.

Changing the app's scopes invalidates its other tokens.

For request validation, delivery paths and modes, the maintenance swap, and revocation, see
[Broker credential implementation](../src/broker/internal/credential). The worker CLI consumes these files directly.

## Claude account selection is the session's

Account operation: [Session account configuration](../src/ensemble/session/config.toml). Before the next run:

1. Remove every `[delegate.accounts]`, `[delegate.roles.<role>.accounts]`, `[orchestrator.core.accounts]` and
   `[orchestrator.core.roles.<role>.accounts]` from `~/.ensemble/config.local.toml`. A left one is a configuration
   error: `unknown key 'accounts' …; moved to [session.clients.<client>] account`.
2. To fix one account for every start, set it in the session instead; leave it out to let each start pick:

   ```toml
   [session.clients.claude]
   account = "work"
   ```

`ensemble-crew reopen` no longer restores a run's recorded account: pass `--account` to choose one.

## Verifier reports on GitHub; orchestrator destination removed

The `verifier` now publishes its report with the `github` destination, to `Verification/` on `main` of a repo you name,
and its outcome carries only the verdict (`Pass:` or `Fail:` and one line) and the report's `url`. Before the next
verifier run:

1. Name the repo in `~/.ensemble/config.local.toml`; without it every verifier run and `ensemble-delegate run --list`
   stop with a configuration error naming `output.repo`. Install the ensemble bot GitHub App on that repo.

   ```toml
   [delegate.roles.verifier.output]
   repo = "<owner>/<name>"
   ```

2. Add exactly these two entries to `networks.mini-op.allow` in `~/.egress/config.local.yaml`, then run
   `~/.egress/current/start.sh`:

   ```yaml
       - github.com
       - api.github.com
   ```

3. Upgrade the broker as in [Broker protocol 31](#broker-protocol-31-output-repo-token-beside-prefetch): it gives the
   verifier's container a GitHub token for that repo beside its prefetched checkout, through
   `[session.hosts.docker.github] app`; without it the verifier cannot push its report.
4. Remove `deliverable = true` from a local `verifier` stage: the outcome has no deliverable, so write-back reports
   it cannot post one.

The `orchestrator` destination is removed: a local `[output] type = "orchestrator"` is a configuration error. Use
`local` or `github`.

## Broker protocol 33: short-lived credentials off the Keychain

Broker protocol 33 keeps no short-lived credential in the Keychain: GitHub tokens are issued and written per command
and revoked from memory or, at `close`, read back from the container; Codex logins keep no per-container record
([broker storage](../src/broker/internal/broker/)). `ensemble-broker status` loses `revocations`, and `status --cleanup` is
gone. Run `scripts/install.sh --docker`: before `broker re-trust` it deletes the leftover `ensemble-broker-tokens`,
`ensemble-broker-revocations` and `ensemble-broker-codex-grants` items and prints how many. Then close and resume every
running Docker session: one an older broker started fails its next renewal. A token an older broker recorded is no
longer revoked and expires within the hour.

## Broker protocol 32: the guard is the Docker container's main process

The broker now starts each worker container with the guard as its main process, and the harness reaches it over
`docker attach` ([Docker worker](../src/ensemble/session/hosts/docker/worker.py)), so a killed harness no longer leaves
its container running. Install this release's broker with its plugin (`scripts/install.sh` does both); the plugin
refuses an older broker. Close every running Docker session first (`ensemble-session ls`, then
`ensemble-session close NAME`): an older container has no guard for this release's harness to attach to.
Conversations stay: resume them after the upgrade.

## The delegate's drive: harness_host removed

A detached run's drive (`<agent>-drive`) now runs as a local process the delegate starts itself
([drive implementation](../src/ensemble/delegate/engine/harness.py)), never as a session on a harness Host, so
`[delegate.delegates] harness_host` is gone and any key under `[delegate.delegates]` is refused
(`[delegates] harness_host: the delegate takes no keys`). Delete that line, and the `[delegate.delegates]` table, from
`~/.ensemble/config.local.toml`; `scripts/install.sh --docker` no longer writes it. Let detached runs end first: a run an
older release detached reads as invalid; its other runs read as before.

## Session harness: close every session first

This release runs every tui and headless worker, on every Host, under its [session harness](../src/ensemble/session/harness/process.py)
and drops the in-session tmux start and the Docker relay and supervisor. An extension Host runs sessions only once it implements `Host.worker`
([Session Host contract](../src/ensemble/session/hosts/base.py)). It keeps no compatibility with sessions an
older release started, so before upgrading:

1. List them: `ensemble-session ls`.
2. Close each crew worker with `ensemble-crew close [--manager <manager>] NAME`, which also drops its roster entry,
   and every other session with `ensemble-session close NAME`.

Conversations stay: resume them after the upgrade. A session missed is not supported afterwards: on Docker it is not
listed, and the next Docker start removes its container as debris.

## Broker protocol 31: output-repo token beside prefetch

Broker protocol 31 adds the start manifest's optional `github.repo`: a [prefetching](../src/ensemble/delegate/config.toml) run
whose `[output]` is `type = "github"` (the `verifier`'s, once set so) gets one GitHub App token for that repo alone,
`contents: write`. Close every running Docker session, then run `scripts/install.sh --docker` to install the new broker;
the Docker Host refuses a protocol 30 broker. Install the GitHub App on that output repo, or the run's start fails
`github_failed`.

## Broker protocol 30: Keychain rename

Broker protocol 30 renames every broker keychain item to start with `ensemble-broker-` (`ensemble-claude` →
`ensemble-broker-claude`, `ensemble-bot` → `ensemble-broker-bot`, `ensemble-env/<name>` → `ensemble-broker-env/<name>`
and so on: [broker Keychain services](../src/broker/internal/broker/keychain.go)), and the static secret references with
them: a manifest or `[[delegate.environments.<name>.secrets]]` item names `ensemble-broker-env/<name>`, and one naming
`ensemble-env/<name>` is rejected (`manifest_rejected`). Upgrading from v0.4.1 or earlier, the maintainer gives you a
one-time rename step; in this order:

1. Close every running Docker session while the protocol 29 broker is still installed: it still reads the old names
   and revokes the sessions' GitHub tokens.
2. Run the one-time rename step: it renames the keychain items and the keys of `~/.ensemble/docker/static-secrets.json`.
3. Run `scripts/install.sh --docker`: it installs the new broker and runs `ensemble-broker retrust`, which asks the
   login keychain password once, then shows one macOS dialog per item an earlier broker wrote, once: click Allow.
   Until step 2 is complete, every broker command refuses to run and names that step.
4. Resume the sessions closed in step 1.

Close first: after step 2 a protocol 29 broker no longer finds its items, so its session's renewal or close revokes
nothing, and the protocol 30 broker rejects the old session's manifest. A session missed in step 1: close it with the
new broker right after step 3; `close` revokes its GitHub tokens from the renamed record, and its audit log lacks the
environment secrets' `Withdrawn` entries. Every later upgrade: `install.sh` runs `retrust` on each new broker, which
asks the login keychain password once and shows no dialog ([Broker installer](../src/broker/install.sh)).

A broker below protocol 30 cannot read the renamed items, so once a protocol 30 broker is installed,
`scripts/install.sh` refuses a `--ref` (an older release or branch) whose broker speaks below 30, before it replaces
the broker or `~/.egress/current`.

## Release changes

The built-in `verifier` now accepts every configured client, including Codex, OpenCode and Claude; board runs
use their Agent label. Rebuild `mini-op:env` from mini-op's plain-skill version (WDRC-195) before starting one:
it must contain `/opt/windroc/mini-op/skills/mini-op/SKILL.md`. Remove any local `fixed_client` role key and
any `mini-op` entry in an environment's `plugins`; the packaged environment no longer requests that plugin.
Update the broker with the plugin so its worker image includes the new client skill bootstrap.

The broker release asset no longer has a GitHub provenance attestation: the maintainer builds it and uploads it with
`ensemble-broker-darwin-arm64.tar.gz.sha256`, and `src/broker/install.sh` installs it only when the download matches
that sha256. Nothing to do: install or upgrade the broker as before (`gh` still downloads it).

Broker protocol 29 replaces `refresh` and `notices` with `maintain`: the broker now decides when each session's
credentials renew ([Docker maintenance](../src/ensemble/session/hosts/docker/maintenance.py)). Upgrade the broker with the
plugin, then restart (close and resume) every Docker session that was running: until then its old supervisor's
renewals fail, each asking for that restart, its GitHub tokens lapse within the hour and a Codex session closes at its
old deadline. The events `client_refresh_failed`, `github_refresh_failed` and `credential_notice_failed` become
`credential_refresh_failed` and `credential_maintenance_failed`. To go back to protocol 28, install its plugin and
broker, then close and resume every Docker session started under 29: a protocol 28 supervisor cannot read their
records. Pending GitHub revocations are kept either way.

Broker protocol 28 adds `notices` and `status`'s `static`, and records each Claude token's, OpenCode key's and
environment secret's expiry ([static credential registration](../src/broker/internal/credential/static.go)). Upgrade the
broker with the plugin. Existing items keep working with an unknown expiry; to record one, rerun the login with
`--fresh-token` (Claude, after `claude setup-token`), `--issued` or `--expires`.

Broker protocol 27 adds the start manifest's optional `gateway` (the caller network, default `workers`) and `prefetch`
([Environment configuration](../src/ensemble/delegate/config.toml)). Upgrade the broker with the plugin: sessions refuse a broker of
another protocol.

Broker protocol 26 drops `start`'s `client_update`: a start never updates Codex; the image carries Claude Code,
Codex and OpenCode at their latest releases and `ensemble-broker check-clients` rebuilds it when one moves
([Worker image implementation](../src/broker/internal/launcher/build.go)). Upgrade the broker with the plugin. Drop
`npm install -g @anthropic-ai/claude-code` (and `@openai/codex`) from `~/.ensemble/docker/worker/Dockerfile` to shrink
the image: the ensemble layer installs its own, and a base image no longer needs `claude`.

Broker protocol 25 makes `start` require the egress proxy ([Egress setup](upgrading.md#egress-proxy)): without it running and healthy, a start fails
`egress_unavailable` (exit 17) before any credential. Upgrade the broker with the plugin, then rerun
`scripts/install.sh --docker` to start the proxy ([Egress proxy](#egress-proxy)).

Broker protocol 24 adds the start manifest's optional `secrets` ([Environment configuration](../src/ensemble/delegate/config.toml)).
Upgrade the broker with the plugin: sessions refuse a broker of another protocol.

Broker protocol 23 makes `ensemble-broker status` read-only. Upgrade the broker with the
plugin, and change automation that relied on status retrying GitHub revocations to
`ensemble-broker status --cleanup`. The installer already uses this compatibility option.
Protocol 23 also carries optional `audit_warning: "audit_write_failed"` metadata independently
of success or failure; custom clients must display it while preserving the operation result.
Its `build` adds `--base-image IMAGE` and `--image-plugin NAME` ([Broker command help](../src/broker/main.go)); a build without them is unchanged.

## Client run limits

Move every entry in `[orchestrator.client_max_runs]` in `~/.ensemble/config.local.toml` to its client's session table, then delete the old table:

```toml
# Previously: [orchestrator.client_max_runs] with opencode = 1
[session.clients.opencode]
max_runs = 1
```

Zero or an omitted `max_runs` means unlimited. The old local table is now an unknown key: orchestrator commands refuse the configuration until it is deleted (only write-back ignores it, with a warning), and it no longer limits launches. Stop and restart headless/in-process runs started by the previous version before relying on the new limit, because they have no session-layer leases. Existing TUI sessions on configured Hosts are counted immediately. The limit now covers ad-hoc role runs, crew workers and `router --issue` too; a refused board start is a normal failed attempt. See [Session capacity implementation](../src/ensemble/session/engine/capacity.py) for counting and reconciliation.

## Egress proxy

After updating the plugin, rerun `scripts/install.sh --docker`: it installs the pinned private
`windroc-labs/egress-proxy` with your `gh` access, creates `~/.egress/config.local.yaml` only if absent, starts the
proxy and waits for healthy. Existing configs are preserved, so one without `networks.workers` stops the installer: add it first ([Egress setup](upgrading.md#egress-proxy)). Add Ollama and local/LAN provider exceptions before starting their workers
([Egress setup](upgrading.md#egress-proxy)).

The pinned proxy now builds the image as `egress-proxy`. After the installer succeeds, remove the
old image it leaves behind:

```sh
docker image rm egress-proxy-proxy
```

An already running worker stays on `bridge` during the upgrade; it is not forcibly migrated or stopped.
Its next start, restart or resume creates a container on the proxy's internal `workers` network.
List running broker workers still on `bridge`:

```sh
docker ps --filter network=bridge --filter label=ensemble.session
```

This filters out containers without the broker's session label. Restart or close/resume a listed worker when
its current work can be interrupted. Until then, it retains its old local/LAN network access.

## Unified source layout

After updating the plugin to the `src/ensemble/` layout, refresh the launchers once:
`<plugin dir>/skills/manage/scripts/ctmux --install`. Use the plugin path directly because an
older installed `ctmux` launcher still looks for `manager/skills/manage/scripts/ctmux`.
Rerunning `scripts/install.sh` after the plugin update does this automatically, including when
all five launchers already exist. Python imports and user configuration stay the same.

Docker-host users must rerun `scripts/install.sh` after the release containing this layout change
to reinstall the broker. An older broker fails on the new layout with `session/src unreadable`.

## Linear presets

What a Linear board needs in its team. `scripts/install.sh --board linear` then fills each id ([Installer](../scripts/install.sh)); by hand, put each in `~/.ensemble/config.local.toml` as a `linear.<key>` under
`[orchestrator.linear]` (`linear.states.todo` is `todo` in `[orchestrator.linear.states]`, `linear.roles.pm.client_id`
is `client_id` in `[orchestrator.linear.roles.pm]`).

| Create in Linear | Config key |
|---|---|
| the team | `linear.team` |
| workflow states Backlog, Todo, In Progress, In Review, Handoff, Done, Canceled | `linear.states.<state>`: `backlog`, `todo`, `in_progress`, `in_review`, `handoff`, `done`, `canceled` |
| a `Tasks` label group, its id from `issueLabels { nodes { id name isGroup } }` | `linear.task_label_group` |
| `decompose` under `Tasks`, required for PM promotion; other task labels (`build`, `light-build`, …) optional | `linear.task_labels.decompose`, `linear.task_labels.<task>` |
| an `Agent` label group and one label per client, optional; both or neither | `linear.agent_label_group`, `linear.agent_labels` |
| the harness's, manager's, reader's and each role's OAuth app (below) | `client_id`, `secret_service`, `token_service`, `user_id` of `linear.harness`, `linear.manager`, `linear.reader` and `linear.roles.<role>` |
| the humans subscribed on In Review | `linear.human_members` |
| the human PM and decompose questions are assigned to, optional: their Linear user id | `linear.assignee_id` |
| the Linear project of each product repo, optional | `linear.project_repos` |

Each OAuth app:

1. Linear › Settings › API › Applications › New: Client credentials on, Public and Webhooks off (the app page has no
   team-access setting). Its Client ID → `client_id`.
2. Store its client secret, then paste it (nothing echoes):

   ```sh
   security add-generic-password -s <secret_service> -a <secret_service> -w
   ```

3. Its Linear user id → `user_id`; runtime commands never fetch it. The first non-dry call as the app requests and
   stores its token.

The installer does steps 2 and 3 for each transport-owned app, with the services `linear-app-<slot>-secret` and
`linear-app-<slot>-token` unless configured, reuses a token with over five days left, and leaves an enrolled app's
token to the broker; cleanup removes only the items it created. A key the orchestrator does not know, in
`[orchestrator.linear]` or an app table, stops it ([Linear apps](#linear-apps)). Once every app has a usable token,
`bin/ensemble-orchestrator router --dry-run`, `bin/ensemble-orchestrator promote --dry-run` and
`bin/ensemble-orchestrator issue list` report no configuration error.

## Linear apps

The Linear board's harness, manager and roles are OAuth apps now; member accounts and personal API keys are gone.
On a Linear board, switch once, in this order:

1. Create the six apps (harness, manager, reader, one per role) in Linear › Settings › API › Applications: Client
   credentials on, Public and Webhooks off ([Linear presets](#linear-presets)).
2. Stop the board's launchd jobs and start no router by hand; wait until no run is left.
3. Write down the value of each old setting, `linear.harness_key`, `linear.manager_key` and each
   `[orchestrator.linear.roles.<role>]` `key`: it names the Keychain item holding that personal key. Unset, it
   was `linear-<slot>` (`linear-harness`, `linear-manager`, `linear-<role>`). Then delete those settings and each
   role's `account` from `~/.ensemble/config.local.toml`: the installer and the orchestrator refuse them as unknown keys.
4. Update ensemble, then rerun `scripts/install.sh --board linear`: it asks each app's Client ID and client secret.
5. Delegate every issue not Done, Canceled or Duplicate from a role's member account to that role's app, and
   from the harness's to the manager app: Linear's bulk edit, or `ensemble-orchestrator issue edit <ID> --role <role>`
   (`--role me` for the manager app).
6. Start the jobs again and let each role run one round ([Live checks](#live-checks)).
7. Revoke the old personal keys in Linear › Settings › Account › Security & Access, run
   `security delete-generic-password -s <service>` for each item written down in step 3, deactivate the old member
   accounts and remove their emails from `linear.human_members`. If the manager's old key was your own account's,
   revoke only that key: keep your account and email.

## Linear broker renewal

This optional cutover keeps existing identity/service names and fixed scopes. Upgrade the plugin and install its
broker first (`sh <plugin dir>/src/broker/install.sh`), even on hosts that do not run Docker workers.
Switch one configured identity at a time:

```mermaid
flowchart LR
    S[Stop host callers] --> E[Enroll identity]
    E --> C[Select broker writer]
    C --> M[Run host maintenance]
    M --> R[Restart callers]
```
Stop launchd router/promote jobs, manual host commands and active role write-back before enrollment so no old process
holds a root secret in memory. Docker workers receive Linear material only when their credential request declares it.

Copy the identity's six public values from its config table into a local `linear-identity.json` (no secret):

```json
{
  "name": "harness",
  "kind": "harness",
  "client_id": "<configured client_id>",
  "user_id": "<configured user_id>",
  "secret_service": "<configured secret_service>",
  "token_service": "<configured token_service>"
}
```

`kind` is `harness`, `manager`, `reader` (fixed `read` scope) or `role` (verifier included). The name and services are 1–128 ASCII letters, digits
or `._@+-`, not `.` or `..`; the client ID 1–256 printable ASCII characters without whitespace; the user ID a lowercase
UUID. The secret and token services differ and are no broker service; enrollment rejects an alias of another identity
before changing access. Enroll it:

```sh
/usr/local/libexec/ensemble/ensemble-broker login linear < linear-identity.json
```

It may ask the login Keychain password on the terminal, then open a macOS SecurityAgent dialog to change the client
secret item's access: enter the login Keychain password and click **Allow**. It records a pending binding before
changing access, so after a failure rerun the same enrollment.
Then set `renewal = "broker"` in its `[orchestrator.linear.harness]`, `[orchestrator.linear.manager]`, `[orchestrator.linear.reader]` or
`[orchestrator.linear.roles.<role>]` table in `~/.ensemble/config.local.toml` and, callers still stopped, run
`/usr/local/libexec/ensemble/ensemble-broker linear-maintain < linear-identity.json`; success contains version 1,
`renewed` and `expires_at`, never a token. Then restart callers. Repeat for the next identity, including verifier when configured. An unconfigured
verifier is not an error. If enrollment/maintenance fails, complete recovery while that identity's callers stay
stopped; do not change its writer back as an automatic fallback.

An interrupted enrollment keeps its reservation and pending record. Rerun the exact same enrollment to complete
root trust; a changed client ID, user ID or service is rejected. After broker reinstallation, rerun enrollment to
repair the registry, selected app record, host grant and root trust. This repair accepts only metadata restricted
to a prior broker code identity at the installed path; permissive or foreign-reader records stay refused. It asks
for the login Keychain password once per enrollment, preserves reservations and cached tokens, and releases the
registry transaction before interactive repair. Failed publication retains authoritative material; rerun maintenance. For a deliberate
identity replacement, stop callers and remove only that identity's reservation from `ensemble-broker-linear` / `registry`
and its `app/<token_service>` and host grant records through a controlled Keychain recovery; preserve every other
identity and all GitHub recovery material. There is no automatic rebinding command.

To rotate an enrolled app's secret, stop its host callers, rotate it at Linear, replace only its root item in Keychain
Access, and delete its published token, its `ensemble-broker-linear` / `app/<token_service>` and its
`ensemble-broker-linear-grants` / `host/<token_service>`, keeping `registry` and every other identity's items. Store
the new root at the configured service and account, rerun `login linear` and `linear-maintain`, then restart callers.
Deleting only the published token would republish the broker's cached access token.

Downgrade is explicit: stop the host callers, keep the broker's records, and remove `renewal` from each switched
local app table before loading an older plugin (which rejects the new key). In Keychain Access restore only that
identity's root item to its previous `/usr/bin/security` reader and partition settings, or recreate the root item
with the same service/account using the old installer's hidden prompt. Do not resume the old writer until its root
access is restored. Broker-only access never silently changes because a process selected the old writer. Re-enroll
before selecting broker again. This does not delete or migrate pending GitHub revocations.

## Live checks

Manual, for the manager and the user, through mini-op on the test Mac mini with Docker Desktop, never on a developer's
Mac; the `tests/session/live_docker.py` checks: [Docker acceptance entry](../tests/session/live_docker.py).

- [ ] Set up as in
  [Verifier reports on GitHub](#verifier-reports-on-github-orchestrator-destination-removed), one
  verifier run publishes its report under `Verification/` and returns only `status`, `title`, `summary` and `url`;
  its target checkout's `HEAD` and `git diff HEAD` are unchanged.
- [ ] Fresh `scripts/install.sh --docker` ends with `egress-proxy` healthy; rerunning keeps the proxy's ID and start
  time, pinned checkout, and config bytes, mode and modification time. Repeat with an existing config.
- [ ] After changing or recreating `~/.egress/config.local.yaml`, rerunning `scripts/install.sh --docker` makes the
  proxy serve the new config; with no change, the proxy keeps its start time.
- [ ] [Linear presets](#linear-presets) in a new Linear team, from only that section and the config: both dry-runs
  report no configuration error. Deferred until there is a new Linear team.
- [ ] [Linear apps](#linear-apps), before cutover: the harness app claims, moves, comments (the comment's `user` has
  `isMe` true), creates an issue, a relation and an attachment, archives, and subscribes the user.
- [ ] Before cutover: the manager app creates and edits issues, comments (`user` is the manager app user) and moves an
  issue (the move's actor is recognisable by its app user id).
- [ ] Before cutover: the harness and manager apps both fail to read or write another team's issue.
- [ ] After cutover: each role app fails on another team's issue and runs one round: claim, write-back, Handoff, next
  role's claim.
- [ ] After cutover: [Linear apps](#linear-apps) step 7 is done: the old credentials revoked in Linear and every
  Keychain item written down in its step 3 deleted.

A failed before-cutover check stops the cutover; report it to the user.

## With scripts/install.sh

On a machine already set up, by hand or by [`scripts/install.sh`](../README.md#install), run the [install command](../README.md#install) again
with the `<tag>` the `windroc` marketplace pins now, adding `--ref <ref>` for another branch or tag (OpenCode only: Claude Code and Codex take only `<tag>`): it reads your answers from its last run, else from `~/.ensemble/config.local.toml`, else
asks; skips what is installed and logged in; installs what the configuration lacks; and shows and asks before each
change to your config or shell profile. Flags change the configuration (`--clients`, `--docker`/`--no-docker`, the
`--opencode-*` flags); it removes nothing chosen before.

Each run refreshes the selected Claude Code/Codex marketplaces and updates their ensemble plugins to the served ref
before reading installed files. An update or version check failure stops with the client's name and reason.
For OpenCode, when `--ref` (default `<tag>`) names a newer commit than the one it last downloaded, it deletes
OpenCode's copy and downloads it again. Each successful install selects its plugin for all `~/.ensemble/bin` commands,
ignoring other clients’ caches and older OpenCode packages ([launcher](../skills/manage/scripts/launcher)).
Rerun the installer once to refresh older launchers and record that selection, even if their files already exist.
After a client plugin update removes the selected directory, the next command selects that same client's newest
installed ensemble copy, records its path and reports it on stderr; no installer rerun is needed. It never selects
another client's copy, nor an OpenCode copy of another ref (its package's `#<ref>`, none meaning `main`). If no such
copy is left, the command keeps the selected-plugin error and the selection: rerun the installer.
Equal versions use sorted paths to break ties. The installer also handles older refs whose launchers predate selection support.
After plugin refresh, when
the plugin's broker sources changed, or the installed broker speaks another protocol, it reinstalls the broker, reusing locally usable Claude and ready Codex
credentials and requesting any missing logins ([broker command usage](../src/broker/main.go)).
It does not edit config keys a release renames.

For OpenCode with `--docker`, rerunning the installer also ensures the local `auth.json` entry for a commander,
then imports it into the broker without a second key entry. A broker-only install needs one local login;
existing local API entries are reused, including after a broker reinstall. Both entries follow the install ledger's
cleanup ownership ([OpenCode sessions](customizing-roles-and-tasks.md#opencode-sessions)).

With OpenCode selected, an existing stable 1.x executable on `PATH` at or above the minimum version is reused.
Otherwise, the installer installs a private copy and offers a shell profile addition for `~/.ensemble/opencode/bin`;
accept it, then open a new terminal to run `opencode` directly. Existing executable paths, the selected provider
and credentials are kept on rerun.

## OpenCode plugin by hand

OpenCode installs the `#v<version>` tag of ensemble's `plugin` entry
([OpenCode operating limits](customizing-roles-and-tasks.md#opencode-operating-limits)) once, into
`${XDG_CACHE_HOME:-~/.cache}/opencode/packages/<spec>/node_modules/ensemble`, and never refreshes the same spec.
Installed commands keep their selected plugin path ([Install](../README.md#install)). For a release upgrade:

```mermaid
flowchart LR
    T[Set new #v&lt;version&gt;] --> R[Restart OpenCode]
    R --> D[Delete old spec directory]
    D --> C[Run an installed command]
```

When the old selected directory is gone, the next installed command selects and records the newest installed ensemble
copy from OpenCode and reports its path on stderr; no installer rerun is needed. To select a different ref while the
old directory remains, rerun the installer or the new plugin's `skills/manage/scripts/ctmux --install`; older refs
require the installer ([With scripts/install.sh](#with-scriptsinstallsh)).
