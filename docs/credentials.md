# Credentials

[Architecture](architecture.md) · [Docker setup](docker-host.md) · [Upgrade cutovers](upgrading.md)

## Access boundary

Credential declarations carry references, never secret values. The broker interprets them; the
[protocol schema](../src/broker/protocol/docker-broker/protocol.schema.json) owns the kinds, paths and validation.
Board declarations and task inheritance are defined in
[`orchestrator/config.toml`](../src/ensemble/orchestrator/config.toml). To supply an explicit declaration:

| Entry | Form |
|---|---|
| Session / Delegate | `ensemble-session start NAME --credentials JSON` / `ensemble-delegate run --credentials JSON …` |
| Crew worker | `ensemble-crew worker NAME --credentials JSON …` |
| Crew role | `ensemble-crew role [--task TASK] --credentials JSON ROLE --input TEXT` |

An explicit crew declaration replaces its role/task default. Resume/restart/reopen replay saved declarations;
use a new start to change them. `github` and `env` request kinds currently validate but do not deliver credentials;
legacy GitHub/environment grants still apply, even with an empty declaration. Do not treat `{}` as revoking them.
tmux ignores declarations; in-process runs refuse them. The broker currently delivers requested `linear`
credentials; container CLI consumption is separate.

**Three-way readers:** anything delivered to a container can be read by the broker, every process in that
container, and any process running as you. Broker start does not authenticate callers; your Docker socket also
allows `exec` or starting another container. App private keys, Codex refresh tokens and enrolled Linear client
secrets stay broker-side, subject to the temporary Codex-login caveat below. Container copies live on tmpfs and
disappear with the container; closing a conversation alone is not upstream revocation.

## Logins and rotation

`B` below is `/usr/local/libexec/ensemble/ensemble-broker`. Run broker logins and `retrust` as yourself;
only these should show Keychain dialogs. Use `"$B" status` for readiness and static-expiry notices.
A Claude token being usable there means it is readable and well formed, not verified with Claude.

| Credential | Storage and grant | Replace / revoke |
|---|---|---|
| Host `gh` | System store or plaintext file (`gh auth status`); host repo/PR/Actions operations, never mounted in a container. Classic login needs `repo`, `read:org`, `gist`; fine-grained also needs Actions read | Revoke GitHub CLI access in GitHub Settings › Applications; `gh auth logout` only removes the local login |
| Host Claude | Claude's Keychain login; fallback `~/.claude/.credentials.json` (0600) | Claude `/login`, `/logout` |
| Host Codex | `~/.codex/auth.json` or its configured OS store | `codex login`; `codex logout` removes local access |
| Docker Claude | `ensemble-broker-claude` / account name; setup-token, model requests only; container `/run/ensemble/secrets/claude`, supplied as `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token`, then `"$B" login claude [NAME]`; one-year token, known expiry enforced; no documented upstream revocation route |
| Docker Codex | `ensemble-broker-codex` / `auth` holds full login; container gets access token only at `/run/ensemble/secrets/codex/auth.json` | First `sudo "$B" install-codex VERSION` (at least 0.163.0), then `"$B" login codex`; `update-codex` updates. Access renews before expiry; refresh lifetime/revocation undocumented |
| OpenCode provider | Host OpenCode `auth.json`; Docker `ensemble-broker-opencode` / provider name, copied under `/run/ensemble/secrets/opencode/` | [Provider setup](customizing-roles-and-tasks.md#opencode-sessions); revoke at provider, then import a new key with `"$B" login opencode NAME`. Existing containers retain old static copies |
| GitHub App key | `ensemble-broker-bot` / App ID; broker-only signing key. Bot identity cache `ensemble-broker-github-users` is not secret | [Create/login](docker-host.md#create-the-github-app); rotate/delete key in App settings, delete old Keychain item, log in with new PEM and remove that PEM |
| GitHub installation token | Container `/run/ensemble/secrets/github/OWNER`; all installed repos of the App, with configured permissions. Prefetch environments restrict the worker token to its output repo | Close tries revocation; a replaced/lost token or failed revocation can remain valid for one hour. `unrevoked` is logged, not retried |
| GitHub prefetch token | Helper tmpfs HOME; read-only on the input repo, only during clone | Revoked on helper exit; failed revocation expires within one hour |
| Environment secret | `ensemble-broker-env/NAME` / `NAME`; container `/run/ensemble/secrets/env/FILE`, shared long-lived value | Rotate at source, `"$B" login env NAME [--expires DATE]`; delete the service to remove it. Close old containers to replace their copy |
| Linear harness/manager/role app | Client secret and token services named by orchestrator config; harness reads/routes, manager operates issues, role writes back. Host token lasts 30 days | For transport renewal: rotate app client secret in Linear API Applications (invalidates all app tokens), update only its secret-service item, delete its token item. Enrolled apps use the [broker cutover/rotation procedure](upgrading.md#linear-broker-renewal) |

For a static secret with known issue/expiry dates, use `login claude --fresh-token`, `--issued DATE` or
`--expires DATE`; provider/environment logins support `--expires DATE`. The broker warns before expiry and
refuses expired values. Environment secrets accept 1–16384 bytes; pipe a long value rather than placing it in argv.

Codex browser login and renewal temporarily write the full login into a 0700 directory under `/private/tmp`.
A SIGKILL or host failure can leave it behind; any process running as you can read it. Clean up abandoned login
directories after confirming no login/renewal uses them. Broker Keychain items require re-trust after a binary
upgrade ([install/recovery](docker-host.md#install-and-upgrade)).

## Linear container credentials

Enable client credentials on the role app and complete [broker enrollment](upgrading.md#linear-container-credentials)
before declaring it. Keep host/container scopes identical: changing scopes invalidates the app's other tokens.
Each container gets an independent token plus nonsecret board settings under `/run/ensemble/secrets/linear/`,
not the client secret or host token. Nondeclaring containers get neither file. Environment variables and
config files are not the credential transport.

Close removes the container and revokes its recorded grants. Retry a failed close: failed revocations remain
recorded for retry until expiry. An unclosed container token may live for up to 30 days. Records under
`ensemble-broker-linear` are broker-owned; do not delete them to clear a failure. Host token renewal remains
independent; its configuration and troubleshooting live in [Upgrading](upgrading.md#linear-broker-renewal).

## Security limits

- Treat Docker as containment from the host, not separation between tools and client inside one container.
  Clients can forge output/transcript records, including rate-limit signals; a false Claude limit may require
  clearing its [account hold](docker-host.md#claude-accounts).
- Three-way readers apply to every handed-out credential. Any process running as you can request any logged-in
  Claude account, any `ensemble-broker-env/*` secret, or use `probe-provider` to send a stored provider key to an
  endpoint it chooses. Transport-owned Linear secrets and published access tokens trust `/usr/bin/security`;
  enrolled client secrets trust only the broker.
- A worker's GitHub token can push any unprotected branch in its installation scope. Review a bot PR on GitHub
  before checkout; inspect executable hooks/config, `Makefile`, package scripts, `.claude/`, `.vscode/`, `.envrc`,
  `.githooks/`, `.husky/`, `.pre-commit-config.yaml`, `core.hooksPath` and `core.fsmonitor`. For output repos pushed
  directly, inspect `git diff --stat HEAD @{upstream}` before pulling. Require approval for target-repo workflows.
- Checkouts run their own Git hooks with container tokens available; images trust base images and client npm
  packages. Claude's writable client-state bind can contain worker-planted files: host readers must not follow
  its symlinks.
- The guard heartbeat detects harness failure, not a hostile client. The shared uid can forge it through
  `/proc`, keeping a container/lock alive until close or a later sweep. Docker attach EOF does not close detached
  container stdin; the heartbeat timeout also kills a worker when its harness stalls. A launch/staging longer
  than the guard's handoff deadline fails. Use ensemble starts, not manual `docker start`.
- Anyone with your Docker socket can attach/exec, and your audit log can be edited by you. It records attempted
  actions, not proof of complete revocation or absence of other actions.
- For provider network setup and transport limits, see [OpenCode on Docker](docker-host.md#opencode-on-docker).
  Replace setup-tokens and Codex logins after suspected exposure; their upstream revocation limits are in the table.

Isolation checks belong to [`live_docker.py`](../tests/session/live_docker.py), run only through the
[test-machine procedure](testing.md#live-acceptance); their shell probes are not a second credential specification.
