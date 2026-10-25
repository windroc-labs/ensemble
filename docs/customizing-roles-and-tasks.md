# Customizing roles and tasks

- The built-in roles, tasks and templates live in `delegate/src/ensemble/delegate/team/`; don't edit them.
- A role's run settings (model, client, destination, commands) go in `~/.ensemble/config.local.toml`.
- Your own roles, tasks and templates go in a team dir. A team dir only adds: it cannot change or replace a built-in role, task or template, so a new task needs a new role.
- Your own roles run through `/ensemble:act-as`, `/ensemble:manage` and `ensemble-delegate run`, and on the board as a workflow stage ([On the board](#on-the-board)).

## See what exists

`ensemble-delegate run --list` prints each role with its destination type and source file, then its tasks with theirs. It checks every team file and renders every role's prompt, writing nothing; exit 2 names the bad file or key.

## Change a role's run settings

Set run keys under `[delegate]` (every role) or `[delegate.roles.<role>]` (one role) in `~/.ensemble/config.local.toml`. Each key, its values and the order they resolve in: the comments of `delegate/src/ensemble/delegate/config.toml`.

```toml
[delegate.roles.engineer]
client = "codex"          # a [clients.<name>] of session/src/ensemble/session/config.toml
tier = 1

[delegate.roles.researcher.output]
type = "github"
repo = "<owner>/<name>"
branch = "main"
dir = "Research/"
```

- **Client:** `client` picks it; `ensemble-delegate run --client` overrides it for one run. The flags a role run passes its client: [Client policy](sessions.md#client-policy).
- **Claude account:** `[delegate.roles.<role>.accounts]` `claude = "work"` runs the role's Docker Claude sessions on that registered account, keyed by client: [Docker host › Claude accounts](docker-host.md#claude-accounts).
- **Destination:** `[output]` (`local`, `github`, `pull-request` or `orchestrator`); a lower level's `[output]` replaces the whole table.
- **Access:** `read`, `write` and `commands` widen what the agent run may read, write and run; `templates` inlines templates into its prompt.
- **Board runs:** the same keys under `[orchestrator.core]` (e.g. `[orchestrator.core.roles.engineer]`) apply to board runs only, after the `[delegate]` ones.

## Add roles, tasks and templates

1. Make a team dir, e.g. `~/.ensemble/team/`, holding any of `roles/`, `tasks/`, `templates/` and `tools/`. Keep it out of every dir a run can write (below).
2. List it in `~/.ensemble/config.local.toml` (only there: the packaged `config.toml` refuses `team_dirs`):
   ```toml
   [delegate]
   team_dirs = ["~/.ensemble/team"]
   ```
3. Write the role and task files (formats below), named `<name>.md` with `<name>` of `[a-z0-9-]`.
4. Check with `ensemble-delegate run --list`, then [run it](#run-and-check).

Rules a team dir follows; each break stops the run with an error naming the file:

- **Add-only:** a role, task or template name that core or an earlier team dir already defines.
- **Own tasks:** a role lists only tasks in its own team dir's `tasks/`; a built-in role only built-in tasks.
- **Templates:** any role may name any template, built-in or yours, in `templates`.
- **Text only:** no front matter (`---`) and no `{{…}}` placeholder; a file may name a method only as a built-in `<team>/methods/<m>.md`.
- **Tools:** a file in `tools/` runs as `{{role_team}}/tools/<file>` in the role's `commands` (written `<role team>/tools/<file>` in its text).
- **Out of reach:** the team dir, its files and its `tools/` tree must not overlap the run's workdir, cwd, a write dir, a dir the run adds or a dir the client can write (Codex: its `CODEX_HOME`). Codex's default `danger-full-access` ignores this: [Client policy](sessions.md#client-policy).

A team dir's role needs no `[delegate.roles.<role>]` table: unset keys take the `[delegate]` values.

### Role file: `roles/<role>.md`

A `# ` title, an intro, then `## Tasks`, the role's only task list. Its first line names the default task as `unsure → `<task>` (`<tasks>/<task>.md`)`; then one line per task. Each path is exactly `<tasks>/<task>.md`, and the default is a listed task.

```markdown
# Reviewer

You review a change against its requirement.

## Tasks

Pick the task below that fits the input; unsure → `review` (`<tasks>/review.md`).

- `review` (`<tasks>/review.md`): review a pull request against its issue.
```

### Task file: `tasks/<task>.md`

It starts with a `# ` heading, has exactly one `[ensemble-progress:start] <what to report>` line and a `## Resume` section, outside code fences. The smallest built-in example: `delegate/src/ensemble/delegate/team/tasks/echo.md`.

## Run and check

| Command | Does |
|---|---|
| `ensemble-delegate run --list` | Lists and checks every role and task. |
| `ensemble-delegate run --role <role> [--task <task>] --input <text> --dry-run` | Prints the resolved client, tier, effort, cwd, dirs, commands and prompt as JSON; starts nothing. |
| `ensemble-delegate prompt --role <role> [--task <task>] --input <text> --workdir <dir> --out <file>` | Writes the role's prompt to `<file>`; starts nothing. |
| `/ensemble:act-as <role>[:<task>] <input>` | Runs the role in this conversation. |

## On the board

The board's stages (order, input layout, title prefix, comment leads, whether a Handoff needs instructions, the state a failed first run goes to) are a workflow file. The packaged one, `orchestrator/src/ensemble/orchestrator/engine/flow/workflow.toml`, has researcher, pm and engineer; its header lists every key and its default. To change a stage, or to put your own role on the board, copy it (e.g. into your team dir), edit the copy and name it in `~/.ensemble/config.local.toml`; it replaces the packaged one whole:

```toml
[orchestrator]
workflow = "~/team/workflow.toml"

[orchestrator.linear.roles.analyst]   # each stage role's Linear account
account = "analyst@example.com"
key = "linear-analyst"
```

A stage needs only `role` and `input` (`research`, `design` or `build`: the layout its input takes):

```toml
[[stage]]
role = "analyst"
input = "research"
next = "pm"           # promote hands its Handoff issues to pm
```

- The role's charter (`roles/<role>.md`, here in a team dir) says where its output goes. Its runs end as every stage's do: done or a question → In Review, a failure → `first_failure`, a failure after a resume → In Review.
- `[orchestrator.roles.<role>] max_runs` is optional: no table → 1.
- Every `ensemble-orchestrator` command checks the file when it loads it and exits 2 naming the file and stage: an unknown or ill-typed key, a role the Delegate does not know, a `next` that names no other stage, a `next` chain with a cycle.
- At least one stage's role must still publish documents to GitHub (its `[output]`), as the packaged researcher and pm do.
- A `[orchestrator.roles.<role>]` or `[orchestrator.linear.roles.<role>]` table must name a stage's role (one for a packaged stage your workflow leaves out is ignored), and a `[orchestrator.linear.task_labels]` or `[orchestrator.task_tiers]` entry a task of one. Board runs' run keys: `[orchestrator.core]`, above.
