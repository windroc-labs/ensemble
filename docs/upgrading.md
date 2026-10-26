# Upgrading

## Client run limits

Move every entry in `[orchestrator.client_max_runs]` in `~/.ensemble/config.local.toml` to its client's session table, then delete the old table:

```toml
# Previously: [orchestrator.client_max_runs] with opencode = 1
[session.clients.opencode]
max_runs = 1
```

Zero or an omitted `max_runs` means unlimited. The old local table is now an unknown key and is ignored with a warning by the configuration overlay; it no longer limits launches. Stop and restart headless/in-process runs started by the previous version before relying on the new limit, because they have no session-layer leases. Existing TUI sessions on configured Hosts are counted immediately. The limit now covers ad-hoc role runs, crew workers and `router --issue` too; a refused board start is a normal failed attempt. See [Session run limits](sessions.md#run-limits) for counting and reconciliation.

Broker protocol 29 replaces `refresh` and `notices` with `maintain`: the broker now decides when each session's
credentials renew ([Docker host › Credential renewal](docker-host.md#credential-renewal)). Upgrade the broker with the
plugin, then restart (close and resume) every Docker session that was running: until then its old supervisor's
renewals fail, each asking for that restart, its GitHub tokens lapse within the hour and a Codex session closes at its
old deadline. The events `client_refresh_failed`, `github_refresh_failed` and `credential_notice_failed` become
`credential_refresh_failed` and `credential_maintenance_failed`. To go back to protocol 28, install its plugin and
broker, then close and resume every Docker session started under 29: a protocol 28 supervisor cannot read their
records. Pending GitHub revocations are kept either way.

Broker protocol 28 adds `notices` and `status`'s `static`, and records each Claude token's, OpenCode key's and
environment secret's expiry ([Docker host › Static secret expiry](docker-host.md#static-secret-expiry)). Upgrade the
broker with the plugin. Existing items keep working with an unknown expiry; to record one, rerun the login with
`--fresh-token` (Claude, after `claude setup-token`), `--issued` or `--expires`.

Broker protocol 27 adds the start manifest's optional `gateway` (the caller network, default `workers`) and `prefetch`
([Docker host › Prefetch](docker-host.md#prefetch)). Upgrade the broker with the plugin: sessions refuse a broker of
another protocol.

Broker protocol 26 drops `start`'s `client_update`: a start never updates Codex; the image carries Claude Code,
Codex and OpenCode at their latest releases and `ensemble-broker check-clients` rebuilds it when one moves
([Docker host › Client releases](docker-host.md#client-releases)). Upgrade the broker with the plugin. Drop
`npm install -g @anthropic-ai/claude-code` (and `@openai/codex`) from `~/.ensemble/docker/worker/Dockerfile` to shrink
the image: the ensemble layer installs its own, and a base image no longer needs `claude`.

Broker protocol 25 makes `start` require the egress proxy ([Docker host › Egress
proxy](docker-host.md#egress-proxy)): without it running and healthy, a start fails
`egress_unavailable` (exit 17) before any credential. Upgrade the broker with the plugin, then rerun
`scripts/install.sh --docker` to start the proxy ([Egress proxy](#egress-proxy)).

Broker protocol 24 adds the start manifest's optional `secrets` ([environment secrets](docker-host.md#environment-secrets)).
Upgrade the broker with the plugin: sessions refuse a broker of another protocol.

Broker protocol 23 makes `ensemble-broker status` read-only. Upgrade the broker with the
plugin, and change automation that relied on status retrying GitHub revocations to
`ensemble-broker status --cleanup`. The installer already uses this compatibility option.
Protocol 23 also carries optional `audit_warning: "audit_write_failed"` metadata independently
of success or failure; custom clients must display it while preserving the operation result.
Its `build` adds `--base-image IMAGE` and `--image-plugin NAME` ([Docker host › Base
images](docker-host.md#base-images)); a build without them is unchanged.

## Egress proxy

After updating the plugin, rerun `scripts/install.sh --docker`: it installs the pinned private
`windroc/egress-proxy` with your `gh` access, creates `~/.egress/config.local.yaml` only if absent, starts the
proxy and waits for healthy. Existing configs are preserved. Add Ollama and local/LAN provider exceptions before starting their workers
([Egress proxy operations](docker-host.md#egress-proxy)).

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

## With scripts/install.sh

On a machine already set up, by hand or by [`scripts/install.sh`](../README.md#install), run the [install command](../README.md#install) again
with the `<tag>` the `windroc` marketplace pins now, adding `--ref <ref>` for another branch or tag (OpenCode only: Claude Code and Codex take only `<tag>`): it reads your answers from its last run, else from `~/.ensemble/config.local.toml`, else
asks; skips what is installed and logged in; installs what the configuration lacks; and shows and asks before each
change to your config or shell profile. Flags change the configuration (`--clients`, `--docker`/`--no-docker`, the
`--opencode-*` flags); it removes nothing chosen before.

It does not update what is installed, but for OpenCode's ensemble plugin: when `--ref` (default `<tag>`) names a newer
commit than the one it last downloaded, it deletes OpenCode's copy and downloads it again, and the `~/.ensemble/bin`
commands run the new copy. Update the other plugins (Claude Code: `claude plugin marketplace update windroc`, then
`claude plugin update ensemble@windroc`; Codex: `codex plugin marketplace upgrade windroc`), then rerun it: when
the plugin's broker sources changed, or the installed broker speaks another protocol, it reinstalls the broker, reusing locally usable Claude and ready Codex
credentials and requesting any missing logins ([Log in](docker-host.md#log-in)).
It does not edit config keys a release renames.

For OpenCode with `--docker`, rerunning the installer also ensures the local `auth.json` entry for a commander,
then imports it into the broker without a second key entry. A broker-only install needs one local login;
existing local API entries are reused, including after a broker reinstall. Both entries follow the install ledger's
cleanup ownership ([Credentials](credentials.md#opencode-provider-api-keys)).

With OpenCode selected, an existing stable 1.x executable on `PATH` at or above the minimum version is reused.
Otherwise, the installer installs a private copy and offers a shell profile addition for `~/.ensemble/opencode/bin`;
accept it, then open a new terminal to run `opencode` directly. Existing executable paths, the selected provider
and credentials are kept on rerun.
