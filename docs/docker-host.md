# Docker host

[Architecture](architecture.md) · [Sessions](sessions.md) · [Credential logins and risks](credentials.md)

## Install and upgrade

Run `scripts/install.sh --docker` as yourself on macOS Apple silicon with Docker Desktop.
It installs the proxy and broker and requests the selected clients' logins. For a manual broker install,
with `gh` logged in and Docker Desktop running:

```sh
sh <plugin-dir>/src/broker/install.sh
```

`--build` needs Go and Xcode command line tools. The installer verifies the release asset's sha256 and protocol
and seeds your editable `~/.ensemble/docker/worker/Dockerfile` if absent. Include
`~/.ensemble/docker-state` in Docker Desktop file sharing.

After a broker upgrade, run `/usr/local/libexec/ensemble/ensemble-broker retrust` to update access to
`ensemble-broker-*` Keychain items. Expect the login keychain password once; an item written by v0.4.1 or
earlier can require one Allow dialog. Existing-installation cutovers live in [Upgrading](upgrading.md).

Commands below use `B=/usr/local/libexec/ensemble/ensemble-broker` and `PKG` for the installed plugin directory.
[`session/config.toml`](../src/ensemble/session/config.toml) owns Docker Host keys and defaults.

## Egress proxy

The installer pins private `windroc-labs/egress-proxy` under `~/.egress/current`, using
`~/.egress/config.local.yaml`. Its installed `config.example.yaml` owns the configuration format.
The `workers` network allows public destinations and denies private ranges, the Mac and LAN;
exceptions are shared by every worker. Add the Mac's public IPv4 and global IPv6 prefix to `deny_ranges`.

| Need | Command |
|---|---|
| Start / apply config | `~/.egress/current/start.sh` |
| Mac changed networks | `docker restart egress-proxy` |
| Check health | `docker inspect --format '{{json .State.Health}}' egress-proxy` |
| Diagnose | `docker logs egress-proxy` |

For `egress_unavailable`, apply the config, wait for healthy, then retry the worker. An invalid policy section
can make the whole proxy unhealthy. Restarting it breaks existing connections on every caller network.
The external [label contract](https://github.com/windroc-labs/egress-proxy/blob/122d7454d738db4a9b7e0c4a89d2cfa5ff564339/docs/label-contract.md)
is the integration boundary; [`launcher/egress.go`](../src/broker/internal/launcher/egress.go) consumes it.

## Create the GitHub App

The installer does not create an App. In GitHub › Settings › Developer settings › GitHub Apps,
create a private App installable only on your account, with webhooks off. Grant repository Contents,
Pull requests and Workflows read/write; Checks, Actions, Commit statuses and Metadata read-only
(at least the permissions requested in session config). Install it only on repositories agents may push to,
including document output repos.

Generate a private key, note the App ID and run:

```sh
"$B" login github ensemble-broker-bot --key-file KEY.pem --app-id APP_ID
```

Delete the PEM afterward; set the printed App slug as the session Host's `github.app`.
Grant breadth and revocation are covered in [Credentials](credentials.md#logins-and-rotation).

## Worker image

`"$B" build --help` owns build options. Builds pin the current client npm versions; existing workers keep
their image. Restart or resume creates a container from the current image; a new start clears client state.
For workspace persistence, see [volumes](#workspace-volumes).

### Plugins

Use the pinned-source syntax and defaults in session config's `hosts.docker.plugins` comments.
To upgrade a plugin, review its target commit and replace its pin, preserving other entries in the list:

```sh
"$B" build --ensemble "$PKG" --plugin '<pinned-entry>' --max-age 0
```

Check the build logs for the intended commit in both Claude Code and Codex. OpenCode reads the same skills
through `skills.paths`; on tmux it has no `/updraft:*` commands. The image excludes ensemble's own plugin.

### Base images

`--base-image IMAGE` uses a local image, never pulls. Supply `apt-get`, `dpkg`, `node`, `npm`, `tmux`, `git`
and a non-root `agent` user, without `windroc.*` or `ensemble.*` labels. Tag bases before cleaning images.
`--image-plugin NAME` uses `/opt/windroc/plugins/NAME/` from that image. Treat base images and your Dockerfile
as trusted executable input; installing a different Claude/Codex there does not override the built clients.

An amd64-only base needs Rosetta: `softwareupdate --install-rosetta`, then Docker Desktop › Settings › General ›
Use Rosetta for x86_64/amd64 emulation on Apple Silicon. Rosetta cannot be uninstalled.

## Run workers

Select Docker using the local example in session config, then use the ordinary crew/session commands:

```sh
ensemble-crew worker NAME --client claude
ensemble-session resume NAME
```

Use `resume`, not `client-resume`, for container history. Role runs cannot use host read/write dirs,
a configured role cwd, custom team-dir roles or multiple/non-GitHub targets; prepare their inputs in the workspace.
The engineer's PR is opened as the App's bot. Container flags and mount/delivery details live in
[`launcher/start.go`](../src/broker/internal/launcher/start.go) and the
[broker protocol schema](../src/broker/protocol/docker-broker/protocol.schema.json).

### Claude accounts

Register named accounts using session config's `clients.claude.accounts` examples; log the configured host
`dir` into the same subscription as its `"$B" login claude NAME` setup-token. The usage probe reads the host
login, not the injected token. A worker's `--account NAME` fixes one start; config `account` fixes the default;
otherwise selection uses remaining usage. Role/board runs have no separate account setting.

A usage-limit hold can outlast a misleading probe. Inspect `~/.ensemble/usage/claude-holds.json`;
delete it to clear a false hold. The selected account is in the session's harness log under
`~/.ensemble/harness/`. On tmux, account selection is validated but not applied to the client.

### OpenCode on Docker

Configure [OpenCode providers](customizing-roles-and-tasks.md#opencode-sessions) first. For local/LAN providers,
add their `host:port` (Ollama: `host.docker.internal:11434`) to `networks.workers.exceptions` and apply the
egress config. The installer does not add these exceptions. Host-side `probe-provider` bypasses the proxy,
so success there does not establish worker access; macOS Local Network permission may also be needed.
An `http://` provider URL sends its key in plaintext.

Keep Ollama on `127.0.0.1`. Set `launchctl setenv OLLAMA_CONTEXT_LENGTH 65536` (or the required larger window),
reopen Ollama, and pull each configured model. Match the client context settings to the server's real window.

## Environments

[`delegate/config.toml`](../src/ensemble/delegate/config.toml) owns environment selection, keys and merge rules;
they are trusted configuration, never issue input. Preview with `ensemble-delegate run --dry-run`, or open a
plain environment using `ensemble-delegate open NAME --environment ENV`.

A locked environment remains occupied after an outcome, including `needs_input`. Continue it with
`ensemble-crew resume --input`, or close its session to release it. A release is confirmed only after resource
termination; unknown liveness is still busy. `docker stop CONTAINER` is an emergency stop, not a way to restart:
use ensemble to start again. After rotating environment secrets, close waiting runs so they receive the new values.

## Workspace volumes

The workspace is `ensemble-ws-NAME`, mounted at the worker cwd. GitHub is the route for returning its work to
the host. Same-name starts keep the volume only with unchanged cwd. A new start at a different cwd removes the
old volume, refusing if Git finds unpushed work; ignored plans/logs do not prevent deletion. Changing a board
issue's client creates a new name and empty volume,
so recover unpushed work from the old one by hand. Client history under `~/.ensemble/docker-state/NAME/` is
separate from the workspace.

Close removes the container and reclaims other volumes whose cwd is gone, retaining ones with unpushed work
(warnings: `~/.ensemble/docker-state/reclaim.log`). To remove a retained volume:

```sh
"$B" workspace-rm --volume VOLUME
```

`workspace_dirty` (exit 12) names unpushed repos. `--force` bypasses the check. Git checks do not protect ignored
plans/logs; save those separately. Anyone with Docker access can read a retained volume. Board
[prune](board.md#state-cleanup) deliberately force-removes finished work, unlike this guarded removal.

## Recovery

| Symptom | Action |
|---|---|
| Gone/dead worker | `ensemble-session resume NAME` |
| Old image | Restart the worker |
| `egress_unavailable` | Repair [proxy health](#egress-proxy) |
| `client_auth_expired` | `"$B" login codex`, then resume |
| `login_required` / `keychain_failed` | Perform the login or `retrust` the error names |
| Keychain `-25244` / `errSecInvalidOwnerEdit` | In Keychain Access delete only the named service/account; rerun its login or installer |

The log is `~/Library/Logs/ensemble/broker.jsonl`. If an upgraded client cannot read old history, resume fails;
it does not silently create a new conversation. A rebuilt Claude can display onboarding. Containment and
credential caveats live in [security limits](credentials.md#security-limits); real-machine checks in [Testing](testing.md#live-acceptance).
