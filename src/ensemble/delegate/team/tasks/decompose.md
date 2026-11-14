# Decompose

Turn one handed-off PRD into child issues with the to-tickets method, updraft's `skills/to-tickets/SKILL.md`. This task's method overrides take precedence. Publish with this task's commands and fields.

## Inputs

The source PRD (`<PRD>`, its identifier), the target repos it names, and the documents the input links. [Engineer › Input processing](#input-processing) says which Engineer sections apply.

## Capabilities

| Step | updraft capability | Use |
|---|---|---|
| 2 | to-tickets method, updraft's `skills/to-tickets/SKILL.md` | draft and review the tickets, as Method overrides amend it |

## Role limits

- Target repos: read-only checkouts (step 1).
- Board writes: only step 3's command; no approval asked for the breakdown, no implementation (Method overrides, Steps 4–5).
- [Engineer › Rules](#rules): **Scenarios are acceptance**.

## Steps

1. **Read** the source PRD (`ensemble-orchestrator issue read <PRD> --full --output <Workdir>/tmp/prd.json`) and check out each target repo read-only ([Principles › Work › Worktree](#work)), branch `<id>-decompose`; fetch in full each document the input links but names no file for. The PRD's file holds its original text: the file the input names for it, else the one you fetched. Then report:
   [ensemble-progress:start] decompose, the source PRD
2. **Draft** with the method. Each description opens with `Repo:`, `Covers:` (PRD identifier + requirement identifiers) and `Task:`; engineer work then acceptance quoting, verbatim and in full, the PRD scenarios that are this ticket's. Every requirement in scope is in exactly one engineer ticket's `Covers:`, whole: never split one identifier over several tickets, leave it to a verifier ticket, or drop it because it may already hold. Every scenario is quoted in exactly one engineer ticket, the one that covers the first requirement in the scenario's tags; a requirement that rule leaves without a scenario goes on the ticket that quotes the last of its scenarios in PRD order. A documentation requirement goes on the ticket that implements the behaviour it documents, never on a documentation-only ticket. Verifier tickets: exactly one for each engineer ticket that quotes `@ops` scenarios, blocked by it and blocking nothing; it lists the ids of those scenarios and links the PRD instead of quoting them. An order the input sets is a blocking edge: a ticket it calls last is blocked by every other engineer ticket. Write each ticket's full description to its own file in `<Workdir>/tmp/tickets/`, each scenario's text taken from `python3 -P <team>/tools/tickets_check.py scenario <the PRD's file> <S-id> …`, and the blocking edges to `<Workdir>/tmp/edges.txt`, one line per blocked ticket: `<its file>: <blocker file> …`. Then run `python3 -P <team>/tools/tickets_check.py check <the PRD's file> <Workdir>/tmp/tickets --edges <Workdir>/tmp/edges.txt [--drop <requirement the input takes out of scope> …]` and fix what it prints until it exits 0; it checks the opening fields, that each requirement is in one engineer ticket's `Covers:`, that each scenario is quoted verbatim in one, and that the edges have no cycle, so write no check of your own for those.
3. **Publish**: `ensemble-orchestrator issue create --title <title> --body-file <its description file> --parent <PRD> --state ready --role engineer|verifier --client claude --priority <the PRD's> --project <the repo's mapped project> --blocked-by <issue>`, adding `--task light-build` for a small engineer ticket.

## Method overrides

- **Step 1, Gather context.** Skip `/setup-matt-pocock-skills`. Task step 1 supplies the source PRD and repos; task step 3 supplies the board command and fields.
- **Step 2, Explore the codebase.** Exploration is mandatory. Grep the target repo for every symbol, option, command and file the PRD changes or removes, plus their callers and tests. Use the hits to split the tickets and set their blocking edges: every hit belongs to one ticket's work or is ruled out in a line. For each feasibility claim the PRD marks as a guess or unverified (e.g. that an existing tool does a job), run a check in `<Workdir>/tmp/`, leaving the target repos unmodified and implementing no ticket, and put its command and result in the ticket the claim affects; a claim you cannot check: say `unverified` there and why.
- **Step 3, Draft vertical slices.** Add task step 2's fields and scenario acceptance to the method's issue template.
- **Step 4, Quiz the user.** Once every description file is written and before publishing, a fresh subagent reviews the tickets. Give it only the path of the PRD's file and of each description file and the check command above, never a summary or the command's result. It runs that command itself and reports what fails, and reads the tickets only for what the command cannot judge: do two tickets with no blocking path between them edit the same region of a file (a function, parser, command table, list, or a section or table of a document), naming the file and region; can fewer stages (a shorter dependency chain) deliver the PRD; does each ticket run end to end once done, or is one a placeholder or a single layer; does each dependency genuinely block its ticket; is any ticket too big for one run to finish. Revise the description files per its answers, then publish. Do not ask the user or wait for approval.
- **Step 5, Publish.** Publish only with task step 3, in dependency order (blockers first), using its native blocking and sub-issue relationships. `--state ready` replaces the `ready-for-agent` label. Write none of the method's `.scratch/` ticket files. Stop after publishing; this task does not implement the tickets.

## Result

No deliverable ([Engineer › Input processing](#input-processing)).

- `done`: the tickets published; `title`: the PRD's product name; `summary`: `Created <identifiers>; reused none`.
- `needs_input`: a scenario judged wrong ([Engineer › Rules](#rules)).

## Failure

[Output](#output): `failed` when nothing usable, `summary` what failed.

## Resume

Continue this session's source PRD; never repeat a create.
