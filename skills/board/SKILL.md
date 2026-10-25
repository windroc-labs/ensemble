---
name: board
description: "Read, find, create, edit, move, split and archive board issues with fixed ensemble-orchestrator issue commands: no hand-written tracker queries."
---

# board

Every common issue operation is one `ensemble-orchestrator issue` command: use it, never a hand-written query or another tool. Splitting an issue is creating its parts with `create --parent`.

Each command needs no run on the issue and prints one compact JSON object; diagnostics go to stderr. `<subcommand> --help` gives every option, default and output field. None is pre-approved: `read` and `list` write a file with `--full --output`. A client without this plugin's `bin/` on its PATH (Codex) runs `${CLAUDE_SKILL_DIR}/../../bin/ensemble-orchestrator`, writing `${CLAUDE_SKILL_DIR}` as this file's directory, an absolute path.

## Commands

The prefix `ensemble-orchestrator issue` is left out. `ID` is an issue identifier (`TASK-1`) or id.

| Command | Does |
|---|---|
| `read ID` | fields, newest comments and the `blocks`, `blocked_by`, `parent`, `children` columns |
| `read ID --full --output FILE` | every page and body, to FILE |
| `list [filters]` | the board's issues, newest update first; also the search |
| `create --title TEXT --body-file FILE` | one issue with its fields and blocking relations; `--parent ID` makes it a sub-issue (a split) |
| `edit ID [--title …] [--body-file …] [--role …] [--project …] [--task …] [--client …] [--priority …]` | change those fields, keeping the rest |
| `move ID STATE` | set its state |
| `comment ID --body-file FILE` | a new top-level comment |
| `relation add ID --blocks OTHER` | one blocking edge; `remove` deletes it; `--blocked-by` for the other direction |
| `archive ID [--dry-run]` | archive it once it and every sub-issue are finished |

Exit 0: success, or `"result": "unchanged"` (already so; nothing written). Exit 2: usage, configuration, or a value that does not resolve; nothing was written. Exit 1: the tracker failed, a refusal, a partial create, or a write whose result is unknown.

## Values

- **States** (`STATE`, `--state`): `backlog`, `ready`, `running`, `review`, `approved`, `done`, `canceled`. `create` defaults to `backlog`.
- **Assignment** (`--role`): a role (`researcher`, `pm`, `engineer`), `me` (the account the commands run as) or `unassigned`.
- **Task** (`--task`) and **client** (`--client`): a configured name, such as `build` or `codex`. An unknown one exits 2 listing the configured names.
- **Priority** (`--priority`): `none`, `urgent`, `high`, `medium`, `low`.

## Finding issues

```bash
ensemble-orchestrator issue list --query 'login' --state ready --task build
ensemble-orchestrator issue list --role me --include-archived
ensemble-orchestrator issue list --role unassigned --priority urgent
ensemble-orchestrator issue list --parent TASK-1 --client codex --limit 50
ensemble-orchestrator issue list --cursor <next_cursor>
```

- `list` filters all hold at once. Archived issues only with `--include-archived`.
- Pages: `--limit` 1–50 (default 20); `has_next_page` true → pass `next_cursor` as `--cursor` with the same options. `--full --output FILE` writes every page to FILE instead.

## Reading in full

```bash
ensemble-orchestrator issue read TASK-1
ensemble-orchestrator issue read TASK-1 --full --output TASK-1.json
```

`read` keeps 5 comments of 300 characters, 600 of the description and 10 issues per relation column. A non-empty `truncated` names what it cut; `continue` then holds the `--full --output` read that keeps everything. Read FILE for the rest.

## Writing

Bodies come from a UTF-8 Markdown file, or stdin with `-`, sent verbatim, minus a leading byte order mark: quotes, backticks, `$` and newlines need no escaping.

```bash
ensemble-orchestrator issue comment TASK-1 --body-file note.md
ensemble-orchestrator issue comment TASK-1 --body-file - < note.md
ensemble-orchestrator issue move TASK-1 running
ensemble-orchestrator issue create --title 'Fix login' --body-file note.md
ensemble-orchestrator issue create --title 'Fix login' --body-file - --parent TASK-1 --role engineer --task build --client codex --priority high --state ready --blocks TASK-2 TASK-3 --blocked-by TASK-4 < note.md
ensemble-orchestrator issue edit TASK-1 --title 'Fix login on Safari' --body-file note.md
ensemble-orchestrator issue edit TASK-1 --task light-build --client claude --priority low
ensemble-orchestrator issue edit TASK-1 --role engineer
```

- `create` resolves every value before writing; unset fields stay unset. `--blocks` and `--blocked-by` repeat.
- `edit` changes only what it is given; `--task` and `--client` replace the issue's current task or client, `--role` its assignment (an issue created without `--role` can be assigned later).
- A write prints a receipt: `issue`, `operation`, `result` (`changed` or `unchanged`), `actor`, and `comment_id` for `comment`. It never echoes the body.

## Blocking relations

`ensemble-orchestrator issue relation add A --blocks B` is `ensemble-orchestrator issue relation add B --blocked-by A`: one edge, A blocks B. `read`'s `blocks` lists the issues it blocks, `blocked_by` the issues blocking it.

```bash
ensemble-orchestrator issue relation add TASK-1 --blocks TASK-2
ensemble-orchestrator issue relation remove TASK-2 --blocked-by TASK-1
```

Each run adds or removes that one edge in that one direction; other relations stay.

## When a write fails

- **Result unknown** (a timeout or outage mid-write, exit 1): `read` the issue before running the write again; nothing retries by itself. For `create`, first run the `list --query=…` command its error prints.
- **Partial create** (exit 1, `"result": "partial"`): the issue exists; `done` and `failed` list its relations, and `recover` holds the `relation add` command for each failed one. A report too long for stdout cuts `done` and `failed` before `recover`, naming each cut in `truncated`. Run only the `recover` commands; never run `create` again.

## Archiving

```bash
ensemble-orchestrator issue archive TASK-1 --dry-run
ensemble-orchestrator issue archive TASK-1
```

Archiving takes every sub-issue with it. `archive` reads all of them, at every depth, and archives only when the issue and each unarchived sub-issue are finished (`done` or `canceled`); prune follows the same rule. Otherwise it exits 1 naming the unfinished ones; there is no override. `--dry-run` lists what would go, writing nothing, and exits 0 whatever it finds: read its `result` (`refused` when anything blocks) and `unfinished`. It closes no run and deletes no file.

## Local board

On a local board (`$ENSEMBLE_BOARD_DIR`), the commands write as `other`, a person, so your comments and moves count as the user's. `me` is the board's `me/` directory and a role its `<role>/` directory; ids are the board's (`LOC-1`); a task or client is the issue file's `task` or `client` (`--client` takes any name without whitespace). `lookup`, `label swap`, `--label`, `--project` and `--milestone` exit 2.

## Linear

On Linear (`[orchestrator.linear]`), the commands act as the `manager_key` account (Keychain), separate from the harness and role apps, so your comments and moves count as the user's; `me` is that account, a role is its app's `[orchestrator.linear.roles.<role>] user_id`, a task or client is its label in `task_labels` or `agent_labels`, and a state is its `[orchestrator.linear.states]` id. `read` and `list` show Linear's state names; finished means a state of type completed, canceled or duplicate. These exist only on Linear:

```bash
ensemble-orchestrator issue lookup states
ensemble-orchestrator issue lookup labels --query bug
ensemble-orchestrator issue lookup milestones --project 'Ensemble'
ensemble-orchestrator issue lookup users --query ada
ensemble-orchestrator issue lookup teams --full --output teams.json
ensemble-orchestrator issue list --label bug --project 'Ensemble'
ensemble-orchestrator issue create --title 'Fix login' --body-file note.md --label bug --project 'Ensemble' --milestone M1
ensemble-orchestrator issue edit TASK-1 --project 'Ensemble'
ensemble-orchestrator issue label swap TASK-1 --from todo --to doing
```

- `lookup RESOURCE` lists `teams`, `states`, `labels`, `projects`, `milestones` (`--project`) or `users` with their ids.
- `--label`, `--project` and `--milestone` take an id or an exact name. No match, several, or one outside the team exits 2 naming the candidates and the `lookup` command that lists them: pick an id from it, never the first match.
- `edit --project` changes only the issue's project. The router starts only issues with a project: give one to an issue created without `--project`.
- `label swap` replaces one label, keeping the rest; it refuses when the issue carries neither label, or when NEW's group already has another of its labels.
- An operation no command covers (other Linear entities) falls back to your own Linear tool, such as a personal `/linear`.
