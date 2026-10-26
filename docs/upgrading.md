# Upgrading

## Unified source layout

After updating the plugin to the `src/ensemble/` layout, refresh the launchers once:
`<plugin dir>/skills/manage/scripts/ctmux --install`. Use the plugin path directly because an
older installed `ctmux` launcher still looks for `manager/skills/manage/scripts/ctmux`.
Rerunning `scripts/install.sh` after the plugin update does this automatically, including when
all five launchers already exist. Python imports and user configuration stay the same.

Docker-host users must rerun `scripts/install.sh` after the release containing this layout change
to reinstall the broker. An older broker fails on the new layout with `session/src unreadable`.

## Linear apps

The Linear board's harness, manager and roles are OAuth apps now; member accounts and personal API keys are gone.
On a Linear board, switch once, in this order:

1. Create the five apps (harness, manager, one per role) in Linear › Settings › API › Applications: Client
   credentials on, Public and Webhooks off, team access limited to the team ([Linear presets](credentials.md#linear-presets)).
2. Stop the board's launchd jobs and start no router by hand; wait until no run is left.
3. Write down the value of each old setting, `linear.harness_key`, `linear.manager_key` and each
   `[orchestrator.linear.roles.<role>]` `key`: it names the Keychain item holding that personal key. Unset, it
   was `linear-<slot>` (`linear-harness`, `linear-manager`, `linear-<role>`). Then delete those settings and each
   role's `account` from `~/.ensemble/config.local.toml`: the installer and the orchestrator refuse them as unknown keys.
4. Update ensemble, then rerun `scripts/install.sh --board linear`: it asks each app's Client ID and client secret.
5. Delegate every issue not Done, Canceled or Duplicate from a role's member account to that role's app, and
   from the harness's to the manager app: Linear's bulk edit, or `ensemble-orchestrator issue edit <ID> --role <role>`
   (`--role me` for the manager app).
6. Start the jobs again and let each role run one round ([Live checks](credentials.md#live-checks)).
7. Revoke the old personal keys in Linear › Settings › Account › Security & Access, run
   `security delete-generic-password -s <service>` for each item written down in step 3, deactivate the old member
   accounts and remove their emails from `linear.human_members`. If the manager's old key was your own account's,
   revoke only that key: keep your account and email.

## With scripts/install.sh

On a machine already set up, by hand or by [`scripts/install.sh`](../README.md#install), run the same command again, with
`--ref <ref>` for a branch (OpenCode only): it reads your answers from its last run, else from `~/.ensemble/config.local.toml`, else
asks; skips what is installed and logged in; installs what the configuration lacks; and shows and asks before each
change to your config or shell profile. Flags change the configuration (`--clients`, `--docker`/`--no-docker`, the
`--opencode-*` flags); it removes nothing chosen before.

It does not update what is installed, but for OpenCode's ensemble plugin: when `--ref` (default `main`) names a newer
commit than the one it last downloaded, it deletes OpenCode's copy and downloads it again, and the `~/.ensemble/bin`
commands run the new copy. Update the other plugins (Claude Code: `claude plugin marketplace update windroc`, then
`claude plugin update ensemble@windroc`; Codex: `codex plugin marketplace upgrade windroc`), then rerun it: when
the plugin's broker sources changed it reinstalls the broker, reusing locally usable Claude and ready Codex
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
