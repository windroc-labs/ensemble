# Credentials

Every credential ensemble uses: who logs in, what each is for, where it is stored, who can read it, how long it lives
and how to revoke it. The local Host (`tmux`) and the Docker Host (`[session.hosts] default = "docker"`,
[Docker host](docker-host.md)) differ for the model clients; their columns are separate. `<B>` is `/usr/local/libexec/ensemble/ensemble-broker`.

**Three-way readers**: a credential the broker hands to a container (the Claude setup-token copy, GitHub installation
tokens, the Codex access token, OpenCode provider API keys) can be read by the broker, by every process in that container, and by any process
running as the user: `<B> start --manifest <file>` does not check its caller, and the Docker
socket is the user's, so such a process can start a container or
`docker exec` into one and read `/run/ensemble/secrets/`. Only the App private key and the Codex refresh token stay
with the broker; a provider API key is the same long-lived key in every container that lists it. The user accepted this on 2026-10-18; the broker is unchanged ([Risks](#risks)).

## Who logs in

| Host | Client | Who logs in, how | Login state | Container gets | The copy goes |
|---|---|---|---|---|---|
| `tmux` | Claude Code | the user, beforehand: `claude`, then `/login` ([Claude auth]) | macOS Keychain; `~/.claude/.credentials.json` (0600) if the Keychain refuses the write ([Claude auth]) | no container | — |
| `tmux` | Codex | the user, beforehand: `codex login` ([Codex auth]) | `~/.codex/auth.json`, or the OS credential store per `cli_auth_credentials_store` ([Codex auth]) | no container | — |
| Docker | Claude Code | the user, once per broker install and [account](docker-host.md#claude-accounts): `claude setup-token` in its subscription, pasted into `<B> login claude [<name>]` ([Log in](docker-host.md#log-in)) | `ensemble-claude` / account name | the selected account's token alone, `/run/ensemble/secrets/claude`, 0400 on tmpfs, given to the client only as `CLAUDE_CODE_OAUTH_TOKEN` | deleted with the container (close, restart, resume start a new one); never revoked |
| Docker | Codex | an admin installs the pinned Codex (`sudo <B> install-codex <version>`); the user, once per broker install: `<B> login codex` in a browser ([Codex](docker-host.md#codex)) | `ensemble-codex` / `auth` | `/run/ensemble/secrets/codex/auth.json`, 0400 on tmpfs, with no refresh token; the broker publishes each renewal | deleted with the container; never revoked |
| `tmux` | OpenCode | no one for the host's Ollama on `127.0.0.1` ([Docker host › OpenCode](docker-host.md#opencode)); a configured provider's API key: the user, beforehand: `opencode auth login` ([OpenCode sessions](sessions.md#opencode-sessions)) | OpenCode's `auth.json`; ensemble reads the selected API entry for availability probes and explicit broker import | no container | — |
| Docker | OpenCode | no one for the host's Ollama (through `host.docker.internal`); each configured provider's API key: `scripts/install.sh --docker` imports the local entry, or the user runs `<B> login opencode <name>` per broker install and provider ([Log in](docker-host.md#log-in)) | `ensemble-opencode` / `<name>` | the GitHub tokens and each configured provider's key, `/run/ensemble/secrets/opencode/<name>.key`, 0400 on tmpfs, read by OpenCode through `{file:…}` in its inline config, never env or argv | deleted with the container; never revoked |

GitHub on the Docker Host needs no per-run login: `<B> login github ensemble-bot` once per broker install
([Log in](docker-host.md#log-in)). The local Host uses the host `gh` login below.

## Credentials

### Host gh login

| Field | Value |
|---|---|
| Purpose | the orchestrator's target-repo check, docs fetch and duplicate search; the Delegate's ready gate (`gh pr ready`, `--undo`); `repo.py` clone, PR and API calls; the manager's PR reads |
| Minimum permission | an account that can push to and open pull requests on every target repo; `gh auth login`'s scopes (a classic token needs at least `repo`, `read:org`, `gist`) |
| Stored at | the system credential store, else a plain-text file; `gh auth status` says which |
| Readers | any process running as the user (`gh auth token`); never mounted into a container |
| Lifetime | until revoked |
| Revoke | `gh auth logout` removes it locally only; revoke at github.com › Settings › Applications › GitHub CLI › Revoke access (every token gh ever made) |

### GitHub App private key

| Field | Value |
|---|---|
| Purpose | signs the 9-minute JWT that issues installation tokens; never leaves the broker |
| Minimum permission | one private App, installed only on repos agents may push to; the broker requests `[session.hosts.docker.github] permissions`, by default contents, pull requests and workflows write, checks, actions and statuses read |
| Stored at | login keychain, service `ensemble-bot` (`[session.hosts.docker.github] app`), account the App ID, created by `<B> login github ensemble-bot --key-file <pem> --app-id <ID>`; delete the downloaded `.pem` once the item holds it |
| Readers | the broker only: an item `--key-file` creates trusts only it for reads from the start, and the login finishes its partition list; an item made otherwise trusts its creator (`/usr/bin/security`: any process running as the user) until `<B> login github ensemble-bot`; a `.pem` left on disk, any process running as the user |
| Lifetime | until the key is deleted in the App's settings |
| Revoke | generate a new key and delete the old one in the App's settings; delete the item (`security delete-generic-password -s ensemble-bot`) and rerun `<B> login github ensemble-bot --key-file <new pem> --app-id <ID>` |

### GitHub installation tokens

| Field | Value |
|---|---|
| Purpose | git (credential helper) and `gh` (wrapper setting `GH_TOKEN` for its own process) inside a Docker container |
| Minimum permission | one token per installation, covering every repo of it, with the permissions above |
| Stored at | broker items `ensemble-tokens` (per container) and `ensemble-revocations` (backlog); the container's `/run/ensemble/secrets/github/<owner>` (0400, tmpfs) |
| Readers | **three-way readers** |
| Lifetime | 1 hour; each session renews them 10 minutes before the first expires |
| Revoke | `close` revokes them before removing the container; a failed revoke is retried at the next start, close or status until it expires; `<B> status` lists pending ones |

### Linear harness app

The harness is a Linear OAuth app, `[linear.harness]`: its client secret and its token are two Keychain items.

| Field | Value |
|---|---|
| Purpose | the harness's access for router (claims, bounces), promote (creates the next role's issue, relates it, moves to Done), prune (archives), a run's issue read and run comments |
| Minimum permission | an OAuth app with Client credentials on, its team access limited to the team; each token is requested with the scopes `read,write`, fixed in code |
| Stored at | the client secret: a login keychain item whose service is `linear.harness.secret_service`; the token and its expiry (JSON `{"access_token", "expires_at"}`): one whose service is `linear.harness.token_service`, written by the commands |
| Readers | any process running as the user: read through `/usr/bin/security`, which an item made by `security add-generic-password` without `-T` trusts |
| Lifetime | the token 30 days; any Linear call as the harness (router, promote, prune, write-back) first requests a new one with `client_credentials` when it is missing, undated, expired or has under 5 days left. A failed request with the old token still valid logs `token-error` and uses it; with it expired the command exits 2 naming the fix. `--dry-run` requests none and logs a `token` line with the days left |
| Revoke | rotate the client secret in Linear › Settings › API › Applications (this revokes every token of the app), store the new secret with `security add-generic-password -U -s <secret service> -a <secret service> -w`, then `security delete-generic-password -s <token service>` so the next call requests a token |

### Linear manager app

The manager is a Linear OAuth app, `[linear.manager]`, with the harness app's four keys. Any of them unset, the `issue`
commands exit 2 naming it; router, promote and prune run without it.

| Field | Value |
|---|---|
| Purpose | the manager's `ensemble-orchestrator issue` commands (reads, comments, moves, creates, relations, labels, archives); its comments and moves count as other (instructions; a move to Todo restarts the attempt count), whether or not `linear.manager.user_id` is set; the id only matters for a manager move that carries a `botActor` |
| Minimum permission | an OAuth app with Client credentials on, its team access limited to the team; each token is requested with the scopes `read,write,app:assignable`, fixed in code |
| Stored at | the client secret: a login keychain item whose service is `linear.manager.secret_service`; the token and its expiry: one whose service is `linear.manager.token_service`, written by the `issue` commands |
| Readers | any process running as the user: read through `/usr/bin/security`, which an item made by `security add-generic-password` without `-T` trusts |
| Lifetime | the token 30 days; any `issue` command first requests a new one when it is missing, undated, expired or has under 5 days left. A failed request with the old token still valid prints `token-error: manager: <reason>` on stderr and uses it; with it expired the command exits 2 naming the fix |
| Revoke | as the harness app, with `linear.manager`'s services |

Before this app, the manager used a personal API key (`manager_key`, now an unknown key). Once the app works, revoke
that key in Linear › Settings › Account › Security & Access, then `security delete-generic-password -s <its service>`
(the installer's default: `linear-manager`).

### Linear role apps

Each `[orchestrator.linear.roles.<role>]` holds `client_id`, `secret_service`, `token_service` and
`user_id`. All four are required; `account` and `key` are unknown keys. `user_id` is the installed app's
Linear user id; runtime role discovery needs no role token or email lookup.

| Field | Value |
|---|---|
| Purpose | role write-back: comments, state moves, subscriptions, attachments and retitles; resume and Recover resolve the run's recorded role against current configuration, ignoring old start-row credentials |
| Permission | fixed `read,write,app:assignable,app:mentionable`; restrict each app to the team |
| Stored at | client secret in `secret_service`; token and expiry in `token_service`, both login Keychain entries |
| Readers | any process running as the user |
| Lifetime | renewed through the shared transport when missing, undated, expired or under 5 days remain; dry-run never renews |
| Failure | an expired token whose renewal fails skips only that role's claims and resumes; write-back reports refusal for Recover |
| Revoke | revoke the app's tokens in Linear, then remove its token and secret entries from Keychain |

Changing an app's client secret revokes its tokens: store the new secret and delete the token entry so
the next call requests one. New runs record the role, never a credential service; existing run rows stay unchanged.
Deterministic comments use the current app user id as their namespace. If an old member's comment landed
before its write-back ledger was saved, Recover leaves it intact and posts the missing step as the app.

### Claude Code login

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | the user's own Claude Code | worker and role-run Claude Code in a container |
| Minimum permission | the user's subscription login | a setup-token: subscription, model requests only |
| Stored at | macOS Keychain (fallback `~/.claude/.credentials.json`) | `ensemble-claude` / account name, one item per [account](docker-host.md#claude-accounts); one copy per running container |
| Readers | Claude Code and any process running as the user that the Keychain lets read it | the item: the broker; the copies: **three-way readers** |
| Lifetime | refreshed by Claude Code; `/login` renews | one year |
| Revoke | `/logout` | Unknown: no official route. `claude.ai/settings/claude-code`, cited by the community, is unverified. On a leak: run `claude setup-token` again and rerun `<B> login claude [<name>]` |

### Stored broker credentials

`ensemble-broker status` reports local usability of the default Claude token as `claude.default`:
the item must trust this broker, allow a read without a dialog, and hold a nonempty, well-formed token.
This does not contact Claude to check expiry or revocation. The installer checks this status before
requesting `claude setup-token`, even without its saved login state. Codex already skips login when
its status is `ready`.

If a broker login reports `keychain status -25244` (`errSecInvalidOwnerEdit`), another process
owns the item's access controls. In Keychain Access, select the login keychain and delete only
the named service/account, such as `ensemble-opencode` / your provider or `ensemble-codex` / `auth`,
then rerun `scripts/install.sh` (or the broker login command). The installer stops retrying this
error. For a Codex `login_unavailable` failure with an existing item, it also names that item and
this recovery when another process created it. The installer leaves deletion to you.

### Codex login

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | the user's own Codex | worker and role-run Codex in a container |
| Minimum permission | the user's ChatGPT login | the broker's own ChatGPT login; the container gets an access token without a refresh token |
| Stored at | `~/.codex/auth.json` or the OS store | the full login, refresh token included: `ensemble-codex` / `auth`; the access token: `/run/ensemble/secrets/codex/auth.json` per container |
| Readers | Codex and any process running as the user | refresh token: the broker only, except that during `login codex` and each renewal the full login also sits in a 0700 temp dir under `/private/tmp` that any process running as the user can read; access token: **three-way readers** |
| Lifetime | refreshed by Codex during use | access token renewed by the broker 10 minutes before it expires; refresh token: not documented |
| Revoke | `codex logout` clears it locally | Unknown upstream: no documented route. Rerun `<B> login codex` to replace the login, or delete the `ensemble-codex` item |

The broker's temporary Codex login directory is removed on success, child failure and normal cancellation; SIGKILL or host failure can leave it behind. Other processes running as you can read it meanwhile. Personal `~/.codex` is neither read nor changed.

### OpenCode provider API keys

One per `[session.clients.opencode.providers.<name>]` table, such as a LAN Strata server.

The installer also registers the selected provider in local OpenCode's global config so a directly started
commander can use the key; existing definitions are preserved ([OpenCode sessions](sessions.md#opencode-sessions)).

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | OpenCode's requests to the provider | the same, from a container |
| Minimum permission | whatever the provider's key grants; issue one for ensemble alone | the same |
| Stored at | OpenCode's `auth.json` (`opencode auth login`); ensemble reads the selected API entry for provider availability probes and explicit broker import | `ensemble-opencode` / `<name>`; per container `/run/ensemble/secrets/opencode/<name>.key`; Docker installs also keep the local entry for commanders |
| Readers | OpenCode and any process running as the user | item: the broker only, once `login opencode <name>` has made it trust only the broker; container copy: **three-way readers**; on the network: anyone between the container and an `http://` `base_url` ([Risks](#risks)) |
| Lifetime | until the provider revokes it | the same; a container's copy until the container goes |
| Revoke | revoke it at the provider; `opencode auth logout` removes it locally | revoke it at the provider, then `<B> login opencode <name>` with a new one, which replaces the item; running containers keep the old copy until closed |

`scripts/install.sh --docker` with OpenCode selected stores the key locally first, then imports that provider's
API entry into the broker ([Log in](docker-host.md#log-in)). Paste once at `opencode auth login`, or supply one
line with `--opencode-key-stdin`. The local store is `${XDG_DATA_HOME:-~/.local/share}/opencode/auth.json`.
Reruns skip an existing usable local API entry and a broker item whose recorded binary fingerprint still matches.
A missing broker item or changed broker binary imports the local entry without asking for the key again. An older
broker-only install needs one local login to fill the missing entry; the broker key is never exported. Existing
entries are not compared or synchronized when both are skipped.

The install ledger records each entry it adds: `opencode-auth` for the local provider and `keychain` for
`ensemble-opencode` / `<name>`. `scripts/install-cleanup.sh` removes both recorded entries, preserving preexisting
credentials and other providers. This deletes the stored copies; it does not revoke the key at the provider.

## Keychain items

All in the login keychain. "Readers" is by design; [Measured readers](#measured-readers) records what a live read
shows.

| Service | Account | Config key | Holds | Written by | Readers |
|---|---|---|---|---|---|
| `ensemble-claude` | account name (`default` or `<name>`) | `[session.clients.claude.accounts.<name>]` | that Claude account's setup-token | `<B> login claude [<name>]` | the broker |
| `ensemble-bot` | App ID | `[session.hosts.docker.github] app` | App private key | the user; access set by `<B> login github ensemble-bot` | the broker, once logged in |
| `ensemble-github-users` | `ensemble-bot` | — | the App's slug and its bot user's ID, for git identity (not a secret) | `<B> login github ensemble-bot` | the broker |
| `ensemble-tokens` | container name | — | that container's installation tokens and expiries | `<B> start`, `refresh`; removed by `close` | the broker |
| `ensemble-revocations` | `pending` | — | tokens awaiting revocation | `<B> close`, `refresh` failures | the broker |
| `ensemble-codex` | `auth` | — | the full Codex login | `<B> login codex`; renewals at `start`, `refresh` | the broker |
| `ensemble-opencode` | provider name | `[session.clients.opencode.providers.<name>]` | that OpenCode provider's API key | `<B> login opencode <name>` | the broker |
| the `linear.harness.secret_service` value | any | `linear.harness.secret_service` | harness app client secret | installer or the user ([Linear presets](#linear-presets)) | any process running as the user |
| the `linear.harness.token_service` value | the existing item's, else the service | `linear.harness.token_service` | harness app token and expiry | installer, router, promote, prune, write-back | any process running as the user |
| the `linear.manager.secret_service` value | any | `linear.manager.secret_service` | manager app client secret | installer or the user ([Linear presets](#linear-presets)) | any process running as the user |
| the `linear.manager.token_service` value | the existing item's, else the service | `linear.manager.token_service` | manager app token and expiry; its comments and moves count as other whether or not `linear.manager.user_id` is set (the id only matters for a manager move that carries a `botActor`) | installer, the `issue` commands | any process running as the user |
| each `linear.roles.<role>.secret_service` value | any | `linear.roles.<role>.secret_service` | role app client secret | installer or the user | any process running as the user |
| each `linear.roles.<role>.token_service` value | the existing item's, else the service | `linear.roles.<role>.token_service` | role app token and expiry | installer, router, write-back, Recover | any process running as the user |
| Claude Code's own | — | — | local Claude login | Claude Code `/login` | Claude Code; service name not documented |
| `gh`'s own | — | — | host `gh` token | `gh auth login` | `gh`; see `gh auth status` |
| Codex's own, keyring mode only | — | — | local Codex login | `codex login` | Codex; service name not documented |

Claude setup-token, container copy: `/run/ensemble/secrets/claude` in every running Claude container; readable by
every process in the container and every process that can `docker exec` (**three-way readers**).

### Measured readers

Each broker item is read twice: from a `/ensemble:manage` session's shell tool (manager) and from Terminal (shell);
both read through `security`, which is how a process reads a generic password, so they cover the manager, a shell and
`security`. For each item the loop first checks it exists (attributes only, no value), then reads it; neither prints
the value, and `perl … alarm 10` stands for `timeout 10`, which macOS lacks. Paste it into Terminal for the shell
column, and after `!` in the manager's Claude Code prompt for the manager column:

```sh
for s in ensemble-claude ensemble-bot ensemble-github-users ensemble-tokens ensemble-revocations ensemble-codex ensemble-opencode; do security find-generic-password -s "$s" >/dev/null 2>&1; e=$?; r=$(bash -c 'perl -e "alarm 10; exec @ARGV" security find-generic-password -s "$0" -w 2>/dev/null | wc -c | tr -d " "; echo "exit=${PIPESTATUS[0]}"' "$s" | tr '\n' ' '); echo "$s exists=$e bytes/$r"; done
```

An item must exist to be measured (`exists=0`); `exists=44` means absent: record `absent, not measured` and make it
first. `ensemble-claude`, `ensemble-github-users`, `ensemble-codex` and `ensemble-opencode` exist after their `login`; `ensemble-bot` once
the user stores the key; `ensemble-tokens` while a Docker session runs; `ensemble-revocations` after a `close` with the
network off, until the next start, close or status online.

Pass: `exists=0`, `exit=` non-zero (142 when the alarm stops it, 128 when the dialog is denied) and a byte count of `0`. Click Deny on any dialog, never Always
Allow; if Always Allow was clicked, `/usr/bin/security` now reads the item: rerun the `login` that writes it
(`login github ensemble-bot` for `ensemble-bot` and `ensemble-github-users`), or for `ensemble-tokens` and
`ensemble-revocations` delete the item in Keychain Access (its tokens expire within an hour), and note it here. Record only
`exists=<n>, exit=<n>, bytes=<n>`.
A read that returns bytes fails this check unless the user accepts it: mark the row `silently readable, accepted <date>`.

| Service | Manager | Shell |
|---|---|---|
| `ensemble-claude` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-bot` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-github-users` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-tokens` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-revocations` | absent, not measured | absent, not measured |
| `ensemble-codex` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-opencode` | pending | pending |

## What a container reaches

- Its workspace volume `ensemble-ws-<name>` at its cwd.
- Its own client state: Claude and OpenCode's read-write binds; Codex's volatile home and bound `sessions/` history.
- Its credentials on tmpfs ([Credentials](#credentials)).
- The network, LAN providers included, and through `host.docker.internal` Mac services listening only on `127.0.0.1` ([accepted risk](docker-host.md#risks)).
- Every repo of every App installation.

Every container runs as `agent`, with all capabilities dropped and `no-new-privileges`.

## What a container cannot reach

Run each with `C` a running worker container (`docker ps --filter label=ensemble.session --format '{{.Names}}'`),
once for each client's worker: Claude, Codex, OpenCode. Each prints counts only.

| Resource | Command | Pass | Claude | Codex | OpenCode |
|---|---|---|---|---|---|
| host keychain | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/Library/Keychains*" -print 2>/dev/null \| wc -l; command -v security \| wc -c'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| Linear keys | `docker exec "$C" sh -c 'env \| grep -ci linear; find / \( -path /proc -o -path /sys \) -prune -o -name config.local.toml -print 2>/dev/null \| wc -l'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| host `gh` login | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/gh/hosts.yml" -print 2>/dev/null \| wc -l; env \| grep -c "^GH_TOKEN="'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| Docker socket | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -type s -name "*docker*" -print 2>/dev/null \| wc -l; command -v docker \| wc -c; env \| grep -c "^DOCKER_HOST="'` | `0`, `0`, `0` | `0`, `0`, `0` | `0`, `0`, `0` | pending |

Each search covers every mount (the workspace volume, the binds, the tmpfs), skipping only `/proc` and `/sys`. The
container's own tmux socket is not Docker's, so the socket search names `docker`.

## Linear presets

With `--board linear`, `scripts/install.sh` asks for each harness/role app's Client ID and hidden client secret,
stores its secret and first token with expiry in Keychain, and fills the app user ids ([Setup](board.md#setup)).
Default services are `linear-app-<slot>-secret` and `linear-app-<slot>-token`; configured services are preserved.
Reruns reuse valid tokens with over five days left. Cleanup removes only the credential items this installer created.
The manager still supplies its separate API key. The table
lists what it needs in Linear. By hand: create these in the Linear team, then put each id in `~/.ensemble/config.local.toml`: a `linear.<key>` under
`[orchestrator.linear]` (`linear.states.todo` is `todo` in `[orchestrator.linear.states]`, `linear.roles.pm.client_id` is
`client_id` in `[orchestrator.linear.roles.pm]`), `local_clones` under `[orchestrator]` ([Setup](board.md#setup)).

| Create in Linear | Config key |
|---|---|
| the team | `linear.team` |
| workflow states Backlog, Todo, In Progress, In Review, Handoff, Done, Canceled | `linear.states.backlog`, `linear.states.todo`, `linear.states.in_progress`, `linear.states.in_review`, `linear.states.handoff`, `linear.states.done`, `linear.states.canceled` |
| a `Tasks` label group, its id from `issueLabels { nodes { id name isGroup } }` | `linear.task_label_group` |
| one label per task under `Tasks` (`build`, `light-build`, …), optional | `linear.task_labels` |
| an `Agent` label group and one label per client, optional; set both or neither | `linear.agent_label_group`, `linear.agent_labels` |
| one OAuth app per role, Client credentials on, Public and Webhooks off, access restricted to the team | `linear.roles.<role>.client_id`, `secret_service`, `token_service`, `user_id` |
| the humans subscribed on In Review | `linear.human_members` |
| the Linear project of each product repo, optional | `linear.project_repos` |
| — (local clones of target repos, optional) | `local_clones` |
| the harness's OAuth app (below): its Client ID, its client secret's and token's Keychain items, its Linear user id | `linear.harness.client_id`, `linear.harness.secret_service`, `linear.harness.token_service`, `linear.harness.user_id` |
| the manager's OAuth app (below), for the `issue` commands: its Client ID, its client secret's and token's Keychain items, its Linear user id; its comments and moves count as yours either way, the id mattering only for a move carrying a `botActor` | `linear.manager.client_id`, `linear.manager.secret_service`, `linear.manager.token_service`, `linear.manager.user_id` |

The harness app:

1. Linear › Settings › API › Applications › New: Public and Webhooks off, Client credentials on; on its page limit team
   access to the team. Its Client ID → `linear.harness.client_id`.
2. Store its client secret, then paste it (nothing echoes):

   ```sh
   security add-generic-password -s <linear.harness.secret_service> -a <linear.harness.secret_service> -w
   ```

3. Its Linear user id → `linear.harness.user_id`; when configuring by hand, the first non-dry command as the harness
   requests and stores its token. The installer obtains both the token and user id during setup.

For each role app, use the same secret-storage steps as the harness with its role table's service
names. Fill `user_id` from the app's viewer during setup; runtime commands do not fetch it. The first
non-dry call requests its token. The manager app takes the same steps with `linear.manager`'s keys; its
scopes are `read,write,app:assignable`.

`bin/ensemble-orchestrator router --dry-run`, `bin/ensemble-orchestrator promote --dry-run` and
`bin/ensemble-orchestrator issue list` should report no configuration error once every app has a usable token.

## Live checks

For the manager and the user, on macOS with Docker Desktop; write each result in this page. Last run 2026-10-18.

- [x] [Measured readers](#measured-readers): six services, manager and shell.
- [x] [What a container cannot reach](#what-a-container-cannot-reach): four commands, a Claude and a Codex worker
  (run by `live_docker.py worker`).
- [ ] [Linear presets](#linear-presets): in a new Linear team, only this section and the config; both dry-runs report
  no config error. Deferred until there is a new Linear team (the user, 2026-10-18).
- [x] [Live acceptance](docker-host.md#live-acceptance): `worker` for Claude and Codex, `role`, and `pr` on an unattended
  board run's PR, each exits 0.
- [ ] [Live acceptance](docker-host.md#live-acceptance): `worker --client opencode` exits 0, with a configured provider
  logged in (`login opencode <name>`).
- [ ] [Measured readers](#measured-readers): `ensemble-opencode`.

## Risks

- **Three-way readers** of handed-out credentials: accepted by the user on 2026-10-18; the broker is unchanged.
- The Linear key items trust `/usr/bin/security`: any process running as the user reads them without a dialog.
- Each broker release changes its ad-hoc code identity: rerun `login claude`, each `login claude <name>`,
  `login github ensemble-bot`, `login codex` and each `login opencode <name>` ([Log in](docker-host.md#log-in)).
- Config picks a Docker Claude session's account ([Claude accounts](docker-host.md#claude-accounts)); it is no
  access control: any process running as the user can start a container with any logged-in account (`<B> start`
  does not check its caller).
- `<B> probe-provider --provider NAME --base-url URL` also does not check its caller: any process running as
  the user can choose an endpoint and have the broker send that provider's stored API key to it.
- An OpenCode provider whose `base_url` is `http://` gets its API key in plaintext: anyone on the LAN path, or on the
  Docker Desktop VM's network, can capture it and the prompts. Prefer `https://`, or a key that reaches only that
  server.
- Neither the setup-token nor the Codex login has a documented upstream revocation.

[Claude auth]: https://code.claude.com/docs/en/authentication#credential-management
[Codex auth]: https://learn.chatgpt.com/docs/auth#login-caching
[gh auth login]: https://cli.github.com/manual/gh_auth_login
[gh auth logout]: https://cli.github.com/manual/gh_auth_logout
[GitHub private keys]: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps
[GitHub installation tokens]: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
[GitHub revoke]: https://docs.github.com/en/rest/apps/installations#revoke-an-installation-access-token
[Linear API keys]: https://linear.app/docs/api-and-webhooks#api-keys
