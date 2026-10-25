# Upgrading

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
the plugin's broker sources changed it reinstalls the broker and asks its logins again ([Log in](docker-host.md#log-in)).
It does not edit config keys a release renames.

For OpenCode with `--docker`, rerunning the installer also ensures the local `auth.json` entry for a commander,
then imports it into the broker without a second key entry. A broker-only install needs one local login;
existing local API entries are reused, including after a broker reinstall. Both entries follow the install ledger's
cleanup ownership ([Credentials](credentials.md#opencode-provider-api-keys)).

With OpenCode selected, accept the installer's shell profile addition for `~/.ensemble/opencode/bin`, then open
a new terminal to run `opencode` directly. Rerunning the install offers the same addition; it keeps the
selected provider's configuration and credentials.
