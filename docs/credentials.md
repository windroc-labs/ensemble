# Credentials

Every credential ensemble uses: who logs in, what each is for, where it is stored, who can read it, how long it lives
and how to revoke it. The local Host (`tmux`) and the Docker Host (`[session.hosts] default = "docker"`,
[Docker host](docker-host.md)) differ for the model clients; their columns are separate. `<B>` is `/usr/local/libexec/ensemble/ensemble-broker`.

**Three-way readers**: a credential the broker hands to a container (the Claude setup-token copy, GitHub installation
tokens, the Codex access token, OpenCode provider API keys, environment secrets) can be read by the broker, by every process in that container, and by any process
running as the user: `<B> start --manifest <file>` does not check its caller, and the Docker
socket is the user's, so such a process can start a container or
`docker exec` into one and read `/run/ensemble/secrets/`. The App private key, Codex refresh token and enrolled Linear client secrets stay
with the broker; a provider API key or an environment secret is the same long-lived value in every container that lists it. The user accepted this on 2026-10-18; the broker is unchanged ([Risks](#risks)).

## Who logs in

| Host | Client | Who logs in, how | Login state | Container gets | The copy goes |
|---|---|---|---|---|---|
| `tmux` | Claude Code | the user, beforehand: `claude`, then `/login` ([Claude auth]) | macOS Keychain; `~/.claude/.credentials.json` (0600) if the Keychain refuses the write ([Claude auth]) | no container | — |
| `tmux` | Codex | the user, beforehand: `codex login` ([Codex auth]) | `~/.codex/auth.json`, or the OS credential store per `cli_auth_credentials_store` ([Codex auth]) | no container | — |
| Docker | Claude Code | the user, once per [account](docker-host.md#claude-accounts): `claude setup-token` in its subscription, pasted into `<B> login claude [<name>]` ([Log in](docker-host.md#log-in)) | `ensemble-broker-claude` / account name | the selected account's token alone, `/run/ensemble/secrets/claude`, 0400 on tmpfs, given to the client only as `CLAUDE_CODE_OAUTH_TOKEN` | deleted with the container (close, restart, resume start a new one); never revoked |
| Docker | Codex | an admin installs the pinned Codex (`sudo <B> install-codex <version>`); the user, once: `<B> login codex` in a browser ([Codex](docker-host.md#codex)) | `ensemble-broker-codex` / `auth` | `/run/ensemble/secrets/codex/auth.json`, 0400 on tmpfs, with no refresh token; the broker publishes each renewal | deleted with the container; never revoked |
| `tmux` | OpenCode | no one for the host's Ollama on `127.0.0.1` ([Docker host › OpenCode](docker-host.md#opencode)); a configured provider's API key: the user, beforehand: `opencode auth login` ([OpenCode sessions](sessions.md#opencode-sessions)) | OpenCode's `auth.json`; ensemble reads the selected API entry for availability probes and explicit broker import | no container | — |
| Docker | OpenCode | no one for the host's Ollama (through `host.docker.internal`); each configured provider's API key: `scripts/install.sh --docker` imports the local entry, or the user runs `<B> login opencode <name>` per provider ([Log in](docker-host.md#log-in)) | `ensemble-broker-opencode` / `<name>` | the GitHub tokens and each configured provider's key, `/run/ensemble/secrets/opencode/<name>.key`, 0400 on tmpfs, read by OpenCode through `{file:…}` in its inline config, never env or argv | deleted with the container; never revoked |

GitHub on the Docker Host needs no per-run login: `<B> login github ensemble-broker-bot` once
([Log in](docker-host.md#log-in)); a new broker keeps every broker item through `<B> retrust`
([Re-trust after an upgrade](docker-host.md#re-trust-after-an-upgrade)). The local Host uses the host `gh` login below.

## Credentials

### Host gh login

| Field | Value |
|---|---|
| Purpose | the orchestrator's target-repo check, docs fetch and duplicate search; the Delegate's ready gate (`gh pr ready`, `--undo`, Actions job and workflow-run reads); `repo.py` clone, PR and API calls; the manager's PR reads |
| Minimum permission | an account that can push to and open pull requests on every target repo; `gh auth login`'s scopes (a classic token needs at least `repo`, `read:org`, `gist`); fine-grained tokens need Actions read to verify superseded checks and unstarted cancellations |
| Stored at | the system credential store, else a plain-text file; `gh auth status` says which |
| Readers | any process running as the user (`gh auth token`); never mounted into a container |
| Lifetime | until revoked |
| Revoke | `gh auth logout` removes it locally only; revoke at github.com › Settings › Applications › GitHub CLI › Revoke access (every token gh ever made) |

The ready gate ignores a superseded Actions check only when job and workflow-run API evidence
proves a newer run or attempt of the same check name and workflow on the PR head.
Missing evidence keeps the check. Latest cancelled, failed or pending checks still block,
except that a cancelled Actions check is treated as skipped when its job API response
matches the PR head and confirms no runner or steps. Missing evidence still blocks;
all skipped or unstarted-cancelled checks together still mean CI did not run.

### GitHub App private key

| Field | Value |
|---|---|
| Purpose | signs the 9-minute JWT that issues installation tokens; never leaves the broker |
| Minimum permission | one private App, installed only on repos agents may push to; the broker requests `[session.hosts.docker.github] permissions`, by default contents, pull requests and workflows write, checks, actions and statuses read |
| Stored at | login keychain, service `ensemble-broker-bot` (`[session.hosts.docker.github] app`), account the App ID, created by `<B> login github ensemble-broker-bot --key-file <pem> --app-id <ID>`; delete the downloaded `.pem` once the item holds it |
| Readers | the broker only: an item `--key-file` creates trusts only it for reads from the start, and the login finishes its partition list; an item made otherwise trusts its creator (`/usr/bin/security`: any process running as the user) until `<B> login github ensemble-broker-bot`; a `.pem` left on disk, any process running as the user |
| Lifetime | until the key is deleted in the App's settings |
| Revoke | generate a new key and delete the old one in the App's settings; delete the item (`security delete-generic-password -s ensemble-broker-bot`) and rerun `<B> login github ensemble-broker-bot --key-file <new pem> --app-id <ID>` |

### GitHub installation tokens

| Field | Value |
|---|---|
| Purpose | git (credential helper) and `gh` (wrapper setting `GH_TOKEN` for its own process) inside a Docker container |
| Minimum permission | one token per installation, covering every repo of it, with the permissions above |
| Stored at | broker items `ensemble-broker-tokens` (per container) and `ensemble-broker-revocations` (backlog); the container's `/run/ensemble/secrets/github/<owner>` (0400, tmpfs) |
| Readers | **three-way readers** |
| Lifetime | 1 hour; each session renews them 10 minutes before the first expires |
| Revoke | `close` releases the container's grant: it attempts revocation before removing the container, even if revocation fails; backlog entries retry at the next start, close or `status --cleanup` until revoked or expired; `<B> status` lists pending backlog entries without changing them |

The `ensemble-broker-tokens` record holds each token's state: prepared (issued, not yet confirmed in the container),
delivered, or retiring (replaced, awaiting revocation). Start and maintain report a token only once it is delivered;
a renewal or close interrupted at any step leaves the record consistent, and the next maintain or close finishes it.

If the backlog cannot be read or written and direct revocation fails, the broker retains the original
`ensemble-broker-tokens` record and reports pending cleanup. Retry `<B> close --container <container>` with the original container
name, even after the container is gone. `status` and `status --cleanup` do not discover these retained records. Unreadable token
records also stay in place for recovery; fix their access and retry close. A record is removed only after
confirmed revocation, confirmed expiry or a successful durable transfer to the backlog.

### GitHub prefetch token

| Field | Value |
|---|---|
| Purpose | one [prefetch](docker-host.md#prefetch) clone into a new workspace |
| Minimum permission | `contents: read` on the prefetched repo alone |
| Stored at | broker item `ensemble-broker-tokens` (the starting container's record) while in use; the prefetch helper's tmpfs `HOME`, never the workspace, the clone's `.git/config` or the run container |
| Readers | the broker; the helper container's processes and any process that can `docker exec` into it, for the length of the clone |
| Lifetime | until the helper exits, at most 1 hour |
| Revoke | the broker revokes it as soon as the helper exits; an unconfirmed revocation goes to the backlog, as installation tokens |

### Linear harness app

The harness is a Linear OAuth app, `[linear.harness]`: its client secret and its token are two Keychain items.
The following transport-owned defaults apply until [broker renewal](#linear-broker-renewal) is enabled.

| Field | Value |
|---|---|
| Purpose | the harness's access for router (claims, bounces), promote (creates the next role's issue, relates it, moves to Done), prune (archives), a run's issue read and run comments |
| Minimum permission | an OAuth app with Client credentials on; each token is requested with the scopes `read,write`, fixed in code |
| Stored at | the client secret: a login keychain item whose service is `linear.harness.secret_service`; the token and its expiry (JSON `{"access_token", "expires_at"}`): one whose service is `linear.harness.token_service`, written by the commands |
| Readers | any process running as the user: read through `/usr/bin/security`, which an item made by `security add-generic-password` without `-T` trusts |
| Lifetime | the token 30 days; any Linear call as the harness (router, promote, prune, write-back) first requests a new one with `client_credentials` when it is missing, undated, expired or has under 5 days left. A failed request with the old token still valid logs `token-error` and uses it; with it expired the command exits 2 naming the fix. `--dry-run` requests none and logs a `token` line with the days left |
| Revoke | rotate the client secret in Linear › Settings › API › Applications (this revokes every token of the app), store the new secret with `security add-generic-password -U -s <secret service> -a <secret service> -w`, then `security delete-generic-password -s <token service>` so the next call requests a token |

### Linear manager app

The manager is a Linear OAuth app, `[linear.manager]`, with the harness app's four keys. Any of them unset, the `issue`
commands exit 2 naming it; router, promote and prune run without it. Its transport-owned defaults below can also
switch to [broker renewal](#linear-broker-renewal).

| Field | Value |
|---|---|
| Purpose | the manager's `ensemble-orchestrator issue` commands (reads, comments, moves, creates, relations, labels, archives); its comments and moves count as other (instructions; a move to Todo restarts the attempt count), whether or not `linear.manager.user_id` is set; the id only matters for a manager move that carries a `botActor` |
| Minimum permission | an OAuth app with Client credentials on; each token is requested with the scopes `read,write,app:assignable`, fixed in code |
| Stored at | the client secret: a login keychain item whose service is `linear.manager.secret_service`; the token and its expiry: one whose service is `linear.manager.token_service`, written by the `issue` commands |
| Readers | any process running as the user: read through `/usr/bin/security`, which an item made by `security add-generic-password` without `-T` trusts |
| Lifetime | the token 30 days; any `issue` command first requests a new one when it is missing, undated, expired or has under 5 days left. A failed request with the old token still valid prints `token-error: manager: <reason>` on stderr and uses it; with it expired the command exits 2 naming the fix |
| Revoke | as the harness app, with `linear.manager`'s services |

### Linear role apps

Each `[orchestrator.linear.roles.<role>]` holds `client_id`, `secret_service`, `token_service` and
`user_id`, plus optional `renewal` (`transport`, the default, or `broker`). All four identity fields are required;
other keys are refused. `user_id` is the installed app's
Linear user id; runtime role discovery reads it from configuration and needs no role token.

| Field | Value |
|---|---|
| Purpose | role write-back: comments, state moves, subscriptions, attachments and retitles; resume and Recover resolve the run's recorded role against current configuration, ignoring old start-row credentials |
| Permission | fixed `read,write,app:assignable,app:mentionable` |
| Stored at | client secret in `secret_service`; token and expiry in `token_service`, both login Keychain entries |
| Readers | any process running as the user until broker renewal restricts the root item |
| Lifetime | renewed through the shared transport when missing, undated, expired or under 5 days remain; dry-run never renews |
| Failure | an expired token whose renewal fails skips only that role's claims and resumes; write-back reports refusal for Recover |
| Revoke | revoke the app's tokens in Linear, then remove its token and secret entries from Keychain |

With transport ownership, changing an app's client secret revokes its tokens: store the new secret and delete the
token entry so the next call requests one. Enrolled apps instead follow the secret-rotation procedure under
[broker renewal](#linear-broker-renewal). New runs record the role, never a credential service; existing run rows stay unchanged.
Deterministic comments use the current app user id as their namespace. If a comment posted before the switch to apps
landed before its write-back ledger was saved, Recover leaves it intact and posts the missing step as the app.

### Linear broker renewal

**Broker renewal** is an opt-in per app: `linear.harness.renewal`, `linear.manager.renewal` or
`linear.roles.<role>.renewal` is `broker`. Omitted or `transport` retains the existing transport writer.
The same role path supports a configured verifier; an absent verifier needs no enrollment.

| Material | Readers and owner |
|---|---|
| Client secret, existing `secret_service` / existing account | only the installed broker after enrollment; Python never reads it on this path |
| Authoritative access token and enrollment, `ensemble-broker-linear` / `app/<token_service>` | broker only; version 1 binds the complete identity and preserves access material before publication |
| Identity reservations, `ensemble-broker-linear` / `registry` | broker only; prevents client ID, user ID and root/token service collisions |
| Host grant, `ensemble-broker-linear-grants` / `host/<token_service>` | broker only; prepared/delivered revision bookkeeping |
| Published token, existing `token_service` / existing account | installed broker and `/usr/bin/security`; any process running as the user can invoke that reader; contains only access token and expiry |

Enrollment and recovery order: [Upgrading › Linear broker renewal](upgrading.md#linear-broker-renewal).
`login linear` and `linear-maintain` take one public identity object on stdin:

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

Copy the six values into a local `linear-identity.json` (no secret). `kind` is `harness`, `manager`, or `role`
for every role, including verifier. Identity/service names are 1–128 ASCII letters, digits or `._@+-`, excluding
`.` and `..`; client ID is 1–256 printable ASCII characters without whitespace, user ID a lowercase UUID. Root/token
services must differ and cannot be broker internal services. Enrollment rejects cross-identity aliases before
changing access. A different binding requires explicit recovery rather than silent replacement.

```sh
/usr/local/libexec/ensemble/ensemble-broker login linear < linear-identity.json
/usr/local/libexec/ensemble/ensemble-broker linear-maintain < linear-identity.json
```

The first command may ask for the login Keychain password on the controlling terminal to restrict root access.
It may also open a macOS SecurityAgent dialog to change access permissions on the client secret's Keychain item:
enter the login Keychain password and click **Allow**. This dialog is separate from the terminal prompt.
It persists a pending binding before changing the ACL; rerun the same enrollment after a failure. Maintenance
rejects an incomplete binding and never opens an interactive root read. Host token publication narrows the read
ACL to the broker and `/usr/bin/security`; it verifies both bytes and access after writing.

The host transport invokes one-shot maintenance once per process per identity after it succeeds; every call reads
the current published entry. No worker or Docker process is needed. The broker renews once fewer than
five days remain (past half the issued lifetime for tokens under ten days),
with WDRC-67's fixed scopes. Per-identity cross-process locks cover renewal and publication; unrelated identities
use different locks. A failed publication retries the saved token without another renewal while it is still current.

On a maintenance failure, transport uses a still-valid or undated old token and reports once per process; missing
or expired material raises the existing configuration error. It never falls back to the transport writer.
Dry runs only read/report the published token: no broker call, root read, token request or write. The installer
honors the same selection before credential setup and never becomes a second renewal writer.

Host protocol 1 (`src/broker/protocol/linear-host/protocol.schema.json`) returns only version, renewal outcome and
expiry, optionally `audit_warning: "audit_write_failed"`; enrollment returns version and `enrolled`. Docker protocol
29 is unchanged. Renewal audit uses `linear-access-token` and the host identity, without secret values. Publication
or storage failure is distinct from audit failure; an audit warning preserves the successful credential result.

To rotate an enrolled app's secret, stop its host callers, rotate at Linear, replace only its root item in Keychain
Access, and delete its published token plus its `ensemble-broker-linear` account `app/<token_service>` and
`ensemble-broker-linear-grants` account `host/<token_service>`. Preserve `registry` and all other identities' items.
Store the replacement root at the configured service/account, rerun `login linear` and `linear-maintain`, then
restart callers. Removing only the published token would republish the broker's cached access token.

### Claude Code login

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | the user's own Claude Code | worker and role-run Claude Code in a container |
| Minimum permission | the user's subscription login | a setup-token: subscription, model requests only |
| Stored at | macOS Keychain (fallback `~/.claude/.credentials.json`) | `ensemble-broker-claude` / account name, one item per [account](docker-host.md#claude-accounts); one copy per running container |
| Readers | Claude Code and any process running as the user that the Keychain lets read it | the item: the broker; the copies: **three-way readers** |
| Lifetime | refreshed by Claude Code; `/login` renews | one year; the broker records it from `--fresh-token`, `--issued` or `--expires`, else unknown, and refuses starts once a known expiry passes ([Static secret expiry](docker-host.md#static-secret-expiry)) |
| Revoke | `/logout` | Unknown: no official route. `claude.ai/settings/claude-code`, cited by the community, is unverified. On a leak: run `claude setup-token` again and rerun `<B> login claude [<name>]` |

### Environment secrets

One per `<B> login env <name>`: a named value (a password, an API key, an ssh private key) that a start manifest
lists in `secrets` ([Docker host › Environment secrets](docker-host.md#environment-secrets)).

| Field | Value |
|---|---|
| Purpose | whatever the container's tools use it for; ensemble passes it only by reference, `ensemble-broker-env/<name>` |
| Minimum permission | whatever the value grants; issue one for ensemble alone |
| Stored at | `ensemble-broker-env/<name>` / `<name>`; per container `/run/ensemble/secrets/env/<file>` (0400, owner `agent`, tmpfs), written at `start` only |
| Readers | item: the broker only, once `login env <name>` has made it trust only the broker; container copy: **three-way readers**, and since `<B> start` does not check its caller, any process running as the user can start a container listing any `ensemble-broker-env/*` item and read it |
| Lifetime | until replaced or deleted (an expiry given with `--expires` refuses starts after it); a container's copy until the container goes |
| Revoke | rotate it at its source, then `<B> login env <name>` with the new value, which replaces the item; `security delete-generic-password -s ensemble-broker-env/<name>` deletes it; running containers keep the old copy until closed |

### Stored broker credentials

`ensemble-broker status` reports local usability of the default Claude token as `claude.default`:
the item must trust this broker, allow a read without a dialog, and hold a nonempty, well-formed token.
This does not contact Claude to check expiry or revocation. The installer checks this status before
requesting `claude setup-token`, even without its saved login state. Codex already skips login when
its status is `ready`.

It also lists, under `static`, each recorded Claude token, OpenCode key and environment secret: its reference,
version, expiry, provenance and any due notice (an unknown default Claude token prompts for a date). The record is
`~/.ensemble/docker/static-secrets.json`, which holds no secret. Deleting an item (`security delete-generic-password`)
retires it locally; nothing revokes a static secret remotely.

Status is read-only: it lists the stored GitHub revocation backlog, including expired entries,
without revoking tokens, changing credential records or writing audit events. Run
`ensemble-broker status --cleanup` to retry revocation and remove resolved or expired backlog
entries. This explicit option preserves the former status behavior; the installer uses it.

If a broker login reports `keychain status -25244` (`errSecInvalidOwnerEdit`), another process
owns the item's access controls. In Keychain Access, select the login keychain and delete only
the named service/account, such as `ensemble-broker-opencode` / your provider or `ensemble-broker-codex` / `auth`,
then rerun `scripts/install.sh` (or the broker login command). The installer stops retrying this
error. For a Codex `login_unavailable` failure with an existing item, it also names that item and
this recovery when another process created it. The installer leaves deletion to you.

### Codex login

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | the user's own Codex | worker and role-run Codex in a container |
| Minimum permission | the user's ChatGPT login | the broker's own ChatGPT login; the container gets an access token without a refresh token |
| Stored at | `~/.codex/auth.json` or the OS store | the full login, refresh token included: `ensemble-broker-codex` / `auth`; the access token: `/run/ensemble/secrets/codex/auth.json` per container |
| Readers | Codex and any process running as the user | refresh token: the broker only, except that during `login codex` and each renewal the full login also sits in a 0700 temp dir under `/private/tmp` that any process running as the user can read; access token: **three-way readers** |
| Lifetime | refreshed by Codex during use | access token renewed by the broker 10 minutes before it expires; refresh token: not documented |
| Revoke | `codex logout` clears it locally | Unknown upstream: no documented route. Rerun `<B> login codex` to replace the login, or delete the `ensemble-broker-codex` item |

Each Docker container's grant, `ensemble-broker-codex-grants` / `<container>`, records the login's fingerprint, expiry,
revision and delivery state, never a token. Renewals of the one login are serialized across broker processes:
concurrent renewals renew it once. A renewal whose delivery or deadline record failed delivers the same login
on retry without renewing it again. `close` deletes the grant after removing the container, leaving other containers
and the login as they are; a container an older broker started gets its grant at its next renewal.

The broker's temporary Codex login directory is removed on success, child failure and normal cancellation; SIGKILL or host failure can leave it behind. Other processes running as you can read it meanwhile. Personal `~/.codex` is neither read nor changed.

### OpenCode provider API keys

One per `[session.clients.opencode.providers.<name>]` table, such as a LAN Strata server.

The installer also registers the selected provider in local OpenCode's global config so a directly started
commander can use the key; existing definitions are preserved ([OpenCode sessions](sessions.md#opencode-sessions)).

| Field | Local (`tmux`) | Docker |
|---|---|---|
| Purpose | OpenCode's requests to the provider | the same, from a container |
| Minimum permission | whatever the provider's key grants; issue one for ensemble alone | the same |
| Stored at | OpenCode's `auth.json` (`opencode auth login`); ensemble reads the selected API entry for provider availability probes and explicit broker import | `ensemble-broker-opencode` / `<name>`; per container `/run/ensemble/secrets/opencode/<name>.key`; Docker installs also keep the local entry for commanders |
| Readers | OpenCode and any process running as the user | item: the broker only, once `login opencode <name>` has made it trust only the broker; container copy: **three-way readers**; on the network: anyone between the container and an `http://` `base_url` ([Risks](#risks)) |
| Lifetime | until the provider revokes it | the same, or until the `--expires` date, which refuses starts after it; a container's copy until the container goes |
| Revoke | revoke it at the provider; `opencode auth logout` removes it locally | revoke it at the provider, then `<B> login opencode <name>` with a new one, which replaces the item; running containers keep the old copy until closed |

`scripts/install.sh --docker` with OpenCode selected stores the key locally first, then imports that provider's
API entry into the broker ([Log in](docker-host.md#log-in)). Paste once at `opencode auth login`, or supply one
line with `--opencode-key-stdin`. The local store is `${XDG_DATA_HOME:-~/.local/share}/opencode/auth.json`.
Reruns skip an existing usable local API entry and a broker item whose recorded binary fingerprint still matches.
A missing broker item or changed broker binary imports the local entry without asking for the key again. An older
broker-only install needs one local login to fill the missing entry; the broker key is never exported. Existing
entries are not compared or synchronized when both are skipped.

The install ledger records each entry it adds: `opencode-auth` for the local provider and `keychain` for
`ensemble-broker-opencode` / `<name>`. `scripts/install-cleanup.sh` removes both recorded entries, preserving preexisting
credentials and other providers. This deletes the stored copies; it does not revoke the key at the provider.

## Keychain items

All in the login keychain. "Readers" is by design; [Measured readers](#measured-readers) records what a live read
shows. Every service starting with `ensemble-broker-` is a broker item; `<B> retrust` makes each trust a new broker
build ([Docker host › Re-trust after an upgrade](docker-host.md#re-trust-after-an-upgrade)). The `linear.*` services
are not broker items.

| Service | Account | Config key | Holds | Written by | Readers |
|---|---|---|---|---|---|
| `ensemble-broker-claude` | account name (`default` or `<name>`) | `[session.clients.claude.accounts.<name>]` | that Claude account's setup-token | `<B> login claude [<name>]` | the broker |
| `ensemble-broker-bot` | App ID | `[session.hosts.docker.github] app` | App private key | the user; access set by `<B> login github ensemble-broker-bot` | the broker, once logged in |
| `ensemble-broker-github-users` | `ensemble-broker-bot` | — | the App's slug and its bot user's ID, for git identity (not a secret) | `<B> login github ensemble-broker-bot` | the broker |
| `ensemble-broker-tokens` | container name | — | that container's installation tokens and expiries, including retained recovery material | `<B> start`, `maintain`; `close` removes only safely retired entries | the broker |
| `ensemble-broker-revocations` | `pending` | — | tokens awaiting revocation | `<B> close`, `maintain` failures | the broker |
| `ensemble-broker-codex` | `auth` | — | the full Codex login | `<B> login codex`; renewals at `start`, `maintain` | the broker |
| `ensemble-broker-codex-grants` | container name | — | that container's Codex grant: the login's fingerprint, expiry, revision and delivery state; no token | `<B> start`, `maintain`; `close` deletes it | the broker |
| `ensemble-broker-linear` | `registry`, `app/<token_service>` | — | enrolled host identities and authoritative access tokens; no client secret copy | `<B> login linear`, `linear-maintain` | the broker |
| `ensemble-broker-linear-grants` | `host/<token_service>` | — | host app's prepared/delivered access-token grant | `<B> linear-maintain` | the broker |
| `ensemble-broker-opencode` | provider name | `[session.clients.opencode.providers.<name>]` | that OpenCode provider's API key | `<B> login opencode <name>` | the broker |
| `ensemble-broker-env/<name>` | `<name>` | — | that environment secret | `<B> login env <name>` | the broker |
| the `linear.harness.secret_service` value | any | `linear.harness.secret_service` | harness app client secret | installer or the user ([Linear presets](#linear-presets)); broker owns enrolled access | broker when enrolled; otherwise any process running as the user |
| the `linear.harness.token_service` value | the existing item's, else the service | `linear.harness.token_service` | harness app token and expiry | selected writer: transport/installer or broker | any process running as the user through the restricted host reader |
| the `linear.manager.secret_service` value | any | `linear.manager.secret_service` | manager app client secret | installer or the user ([Linear presets](#linear-presets)); broker owns enrolled access | broker when enrolled; otherwise any process running as the user |
| the `linear.manager.token_service` value | the existing item's, else the service | `linear.manager.token_service` | manager app token and expiry; its comments and moves count as other whether or not `linear.manager.user_id` is set (the id only matters for a manager move that carries a `botActor`) | selected writer: transport/installer or broker | any process running as the user through the restricted host reader |
| each `linear.roles.<role>.secret_service` value | any | `linear.roles.<role>.secret_service` | role app client secret | installer or the user; broker owns enrolled access | broker when enrolled; otherwise any process running as the user |
| each `linear.roles.<role>.token_service` value | the existing item's, else the service | `linear.roles.<role>.token_service` | role app token and expiry, including configured verifier | selected writer: transport/installer or broker | any process running as the user through the restricted host reader |
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
for s in ensemble-broker-claude ensemble-broker-bot ensemble-broker-github-users ensemble-broker-tokens ensemble-broker-revocations ensemble-broker-codex ensemble-broker-codex-grants ensemble-broker-opencode; do security find-generic-password -s "$s" >/dev/null 2>&1; e=$?; r=$(bash -c 'perl -e "alarm 10; exec @ARGV" security find-generic-password -s "$0" -w 2>/dev/null | wc -c | tr -d " "; echo "exit=${PIPESTATUS[0]}"' "$s" | tr '\n' ' '); echo "$s exists=$e bytes/$r"; done
```

For each environment secret, add `ensemble-broker-env/<name>` to the loop's list. An item must exist to be measured (`exists=0`); `exists=44` means absent: record `absent, not measured` and make it
first. `ensemble-broker-claude`, `ensemble-broker-github-users`, `ensemble-broker-codex` and `ensemble-broker-opencode` exist after their `login`; `ensemble-broker-bot` once
the user stores the key; `ensemble-broker-tokens` while a Docker session runs; `ensemble-broker-codex-grants` while a Docker Codex
session runs; `ensemble-broker-revocations` after a `close` with the
network off, until the next start, close or `status --cleanup` online.

Pass: `exists=0`, `exit=` non-zero (142 when the alarm stops it, 128 when the dialog is denied) and a byte count of `0`. Click Deny on any dialog, never Always
Allow; if Always Allow was clicked, `/usr/bin/security` now reads the item: rerun the `login` that writes it
(`login github ensemble-broker-bot` for `ensemble-broker-bot` and `ensemble-broker-github-users`), or for `ensemble-broker-tokens` and
`ensemble-broker-revocations` delete the item in Keychain Access (its tokens expire within an hour), for
`ensemble-broker-codex-grants` close the session (its item holds no token), and note it here. Record only
`exists=<n>, exit=<n>, bytes=<n>`.
A read that returns bytes fails this check unless the user accepts it: mark the row `silently readable, accepted <date>`.

| Service | Manager | Shell |
|---|---|---|
| `ensemble-broker-claude` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-broker-bot` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-broker-github-users` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-broker-tokens` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-broker-revocations` | absent, not measured | absent, not measured |
| `ensemble-broker-codex` | exists=0, exit=128, bytes=0 | exists=0, exit=128, bytes=0 |
| `ensemble-broker-codex-grants` | pending | pending |
| `ensemble-broker-opencode` | pending | pending |
| `ensemble-broker-env/<name>` | pending | pending |

## What a container reaches

- Its workspace volume `ensemble-ws-<name>` at its cwd.
- Its own client state: Claude and OpenCode's read-write binds; Codex's volatile home and bound `sessions/` history.
- Its credentials on tmpfs ([Credentials](#credentials)).
- The network only through the egress proxy's policy for its caller network (`workers` unless the manifest's `gateway` names another): the public internet; the Mac (`host.docker.internal`) and LAN providers only through its exceptions ([Egress proxy](docker-host.md#egress-proxy)).
- Every repo of every App installation; none with [prefetch](docker-host.md#prefetch), whose container gets no GitHub token.

Every container runs as `agent`, with all capabilities dropped and `no-new-privileges`.

The `workers` network is the internal one the proxy advertises in its `windroc.egress.network.workers.name` label
(`egress-workers` in contract 1), with no direct route out, external DNS included. The proxy's config, checkout and Docker socket are not mounted into
workers. Only container loopback bypasses the proxy. See [egress proxy operations](docker-host.md#egress-proxy)
for the installer, policy and network-change restart commands, health checks, and public IPv4/global IPv6 limits.

Ollama's `host.docker.internal:11434` and every local/LAN provider `base_url` host and port must be listed in
`networks.workers.exceptions` in `~/.egress/config.local.yaml`. The installer preserves an existing config and
does not add provider exceptions. Every worker gets the exception, including Claude and Codex, so a permitted
provider still receives the requests and credentials its own permissions allow. Host-side provider probes can
pass before the worker's exception exists. Existing running `bridge` workers retain old access until their next
start, restart or resume ([Upgrading](upgrading.md#egress-proxy)).

## What a container cannot reach

Run each with `C` a running worker container (`docker ps --filter label=ensemble.session --format '{{.Names}}'`),
once for each client's worker: Claude, Codex, OpenCode. Each prints counts only.

| Resource | Command | Pass | Claude | Codex | OpenCode |
|---|---|---|---|---|---|
| host keychain | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/Library/Keychains*" -print 2>/dev/null \| wc -l; command -v security \| wc -c'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| Linear tokens | `docker exec "$C" sh -c 'env \| grep -ci linear; find / \( -path /proc -o -path /sys \) -prune -o -name config.local.toml -print 2>/dev/null \| wc -l'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| host `gh` login | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/gh/hosts.yml" -print 2>/dev/null \| wc -l; env \| grep -c "^GH_TOKEN="'` | `0`, `0` | `0`, `0` | `0`, `0` | pending |
| Docker socket | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -type s -name "*docker*" -print 2>/dev/null \| wc -l; command -v docker \| wc -c; env \| grep -c "^DOCKER_HOST="'` | `0`, `0`, `0` | `0`, `0`, `0` | `0`, `0`, `0` | pending |

Each search covers every mount (the workspace volume, the binds, the tmpfs), skipping only `/proc` and `/sys`. The
container's own tmux socket is not Docker's, so the socket search names `docker`.

## Linear presets

With `--board linear`, `scripts/install.sh` asks for each transport-owned harness, manager and role app's Client ID
and hidden client secret, stores its secret and first token with expiry in Keychain, and fills the app user ids
([Setup](board.md#setup)). A configured app using broker renewal instead keeps its configured identity, invokes
host maintenance and reads the published token; the installer never reads its root or requests/writes its token.
Default services are `linear-app-<slot>-secret` and `linear-app-<slot>-token`; configured services are preserved.
Transport-owned reruns reuse valid tokens with over five days left; the broker decides enrolled apps' renewal timing.
Cleanup removes only the credential items this installer created.
A key the orchestrator does not know, in `[orchestrator.linear]` or an app table, stops the installer
([Upgrading](upgrading.md#linear-apps)). The table
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
| one OAuth app per role, Client credentials on, Public and Webhooks off | `linear.roles.<role>.client_id`, `secret_service`, `token_service`, `user_id` |
| the humans subscribed on In Review | `linear.human_members` |
| the Linear project of each product repo, optional | `linear.project_repos` |
| — (local clones of target repos, optional) | `local_clones` |
| the harness's OAuth app (below): its Client ID, its client secret's and token's Keychain items, its Linear user id | `linear.harness.client_id`, `linear.harness.secret_service`, `linear.harness.token_service`, `linear.harness.user_id` |
| the manager's OAuth app (below), for the `issue` commands: its Client ID, its client secret's and token's Keychain items, its Linear user id; its comments and moves count as yours either way, the id mattering only for a move carrying a `botActor` | `linear.manager.client_id`, `linear.manager.secret_service`, `linear.manager.token_service`, `linear.manager.user_id` |

The harness app:

1. Linear › Settings › API › Applications › New: Public and Webhooks off, Client credentials on (the app page has no
   team-access setting). Its Client ID → `linear.harness.client_id`.
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

For the manager and the user, through mini-op on the test Mac mini with Docker Desktop. Never run real-machine
acceptance on a developer's Mac. The 2026-10-18 results below predate the egress proxy.

- [ ] Fresh `scripts/install.sh --docker` ends with `egress-proxy` healthy; rerunning keeps the proxy's ID
  and start time, pinned checkout, and config bytes, mode and modification time. Repeat with an existing config.
- [ ] After changing or recreating `~/.egress/config.local.yaml`, rerunning `scripts/install.sh --docker` makes the
  proxy serve the new config; with no change, the proxy keeps its start time.
- [ ] `worker --client claude`, `worker --client codex`, `worker --client opencode` pass the proxy checks in
  [Live acceptance](docker-host.md#live-acceptance). Their local/LAN negative controls use temporary reachable
  listeners; the mini's NAS already rejects it and proves no proxy isolation.

- [x] [Measured readers](#measured-readers): six services, manager and shell.
- [x] [What a container cannot reach](#what-a-container-cannot-reach): four commands, a Claude and a Codex worker
  (run by `live_docker.py worker`).
- [ ] [Linear presets](#linear-presets): in a new Linear team, only this section and the config; both dry-runs report
  no config error. Deferred until there is a new Linear team (the user, 2026-10-18).
- [x] [Live acceptance](docker-host.md#live-acceptance): `worker` for Claude and Codex, `role`, and `pr` on an unattended
  board run's PR, each exits 0.
- [ ] [Live acceptance](docker-host.md#live-acceptance): `worker --client opencode` exits 0, with a configured provider
  logged in (`login opencode <name>`).
- [ ] WDRC-67, before cutover: the harness app claims, moves, comments (the comment has `user` with `isMe` true),
  creates an issue, a relation and an attachment, archives, and subscribes the user.
- [ ] WDRC-67, before cutover: the manager app creates and edits issues, comments (`user` is the manager app user)
  and moves an issue (the move's actor is recognisable by its app user id).
- [ ] WDRC-67, before cutover: the harness and manager apps both fail to read or write another team's issue.
- [ ] WDRC-67, after cutover: each role app fails on another team's issue and runs one round: claim, write-back,
  Handoff, next role's claim.
- [ ] WDRC-67, after cutover: [Upgrading › Linear apps](upgrading.md#linear-apps) step 7 is done: the old credentials
  revoked in Linear and every Keychain item written down in its step 3 deleted.
- [ ] [Measured readers](#measured-readers): `ensemble-broker-opencode`.

A failed WDRC-67 before-cutover check stops the cutover; report it to the user.

## Risks

- **Three-way readers** of handed-out credentials: accepted by the user on 2026-10-18; the broker is unchanged.
- Transport-owned Linear client secrets and published access tokens trust `/usr/bin/security`: any process running as the user reads them without a dialog. Enrolled client secrets trust only the broker; published access tokens trust both the broker and `/usr/bin/security` ([Keychain items](#keychain-items)).
- Each broker build has its own partition ID: `scripts/install.sh --docker` runs `retrust` on each new broker, the
  login keychain password once ([Re-trust after an upgrade](docker-host.md#re-trust-after-an-upgrade)); until then a
  start fails without a dialog, naming `retrust`.
- Config picks a Docker Claude session's account ([Claude accounts](docker-host.md#claude-accounts)); it is no
  access control: any process running as the user can start a container with any logged-in account (`<B> start`
  does not check its caller).
- An environment secret reaches whoever can start a container: `<B> start` does not check its caller, so any process
  running as the user can list any `ensemble-broker-env/*` item in a manifest and read the copy (**three-way readers**).
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
