"""ensemble-orchestrator issue: the fixed issue subcommands, parsed and checked here, then run over
the configured tracker's backend; output.py holds the stdout, stderr and exit rules they share.
"""

import argparse
import functools
import sys

from ensemble.delegate import ConfigError as DelegateConfigError
from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow.types import (
    PRIORITY_NUMBERS,
    ArchiveRefused,
    IssueState,
    Priority,
)
from ensemble.orchestrator.engine.types import (
    ConfigError,
    GraphQLErrors,
    IssueEdit,
    IssueError,
    LabelRefused,
    LinearError,
    ListQuery,
    LookupQuery,
    NewIssue,
    RelationsFailed,
    Resource,
    TrackerFileError,
    TrackerKind,
    Unavailable,
)
from ensemble.orchestrator.issue.backend import Backend
from ensemble.orchestrator.issue.output import (
    FAILED,
    FULL,
    MAX_PAGE,
    OK,
    PAGE,
    PROG,
    TEXTS,
    USAGE,
    compact,
    diagnose,
    dry_run,
    encode,
    fit,
    fit_page,
    partial_message,
    partial_report,
    receipt,
    receipt_payload,
    refusal,
    write_full,
)
from ensemble.orchestrator.trackers.linear.client import linear_gql, messages
from ensemble.orchestrator.trackers.linear.issues import LinearIssues
from ensemble.orchestrator.trackers.local.issues import LocalIssues

STATES = [str(state) for state in IssueState]
PRIORITIES = [str(priority) for priority in PRIORITY_NUMBERS]
_LINEAR_ONLY = '{}: Linear only, not on a local board ([orchestrator] tracker = "local")'
_RULES = (
    "Prerequisites and effects:\n"
    "  Reads the configured board: Linear as the linear.manager_key account\n"
    "  (Keychain); a local board ($ENSEMBLE_BOARD_DIR) directly, writing as other.\n"
    "  An issue needs no recorded run. Help needs no credentials or services.\n"
    "  stdout: one JSON object of at most 8192 bytes; diagnostics go to stderr.\n"
    "  Exit 0: success; 1: the board failed (Linear: GraphQL errors, HTTP,\n"
    "  timeout); 2: usage, configuration, Keychain, a board that cannot be\n"
    "  scanned, or an issue or option value that does not resolve.\n\n"
)
_HELP = (
    _RULES.removesuffix("\n")
    + "  create, edit, move, comment, archive, relation and label swap write as that\n"
    "  account (a local board: other) and print a receipt instead; for them exit\n"
    "  1 also means the write failed or its result is unknown, for create also a\n"
    "  relation that failed, for archive and label swap also a refusal (their\n"
    "  --help).\n\n"
    "Values: abstract names; on Linear each the configured team's id.\n"
    "  States: backlog, ready, running, review, approved, done, canceled.\n"
    "  --role SPEC: a role of [orchestrator.linear.roles], me (the\n"
    "  linear.manager_key account) or unassigned; a local board: that directory.\n"
    "  --task NAME / --client NAME: on Linear a key of\n"
    "  [orchestrator.linear.task_labels] / agent_labels; a local board: a\n"
    "  configured role's task / any client name.\n"
    "  --priority NAME: none, urgent, high, medium, low.\n"
    "  Splitting an issue: create --parent ID.\n"
    "  Linear only (a local board exits 2): lookup, label swap, --label, --project,\n"
    "  --milestone.\n\n"
    "Example (a configured board holding TASK-1; lookup and label swap: Linear):\n"
    "  ensemble-orchestrator issue read TASK-1\n"
    "  ensemble-orchestrator issue list --state running --role me\n"
    "  ensemble-orchestrator issue lookup labels --query bug\n"
    "  ensemble-orchestrator issue create --title 'Fix login' --body-file note.md --task build \\\n"
    "    --role engineer --priority high --blocks TASK-1\n"
    "  ensemble-orchestrator issue create --title 'Split login' --body-file note.md \\\n"
    "    --parent TASK-1\n"
    "  ensemble-orchestrator issue edit TASK-1 --title 'Fix login' --task build\n"
    "  ensemble-orchestrator issue move TASK-1 done\n"
    "  ensemble-orchestrator issue comment TASK-1 --body-file note.md\n"
    "  ensemble-orchestrator issue archive TASK-1 --dry-run\n"
    "  ensemble-orchestrator issue relation add TASK-1 --blocks TASK-2\n"
    "  ensemble-orchestrator issue label swap TASK-1 --from todo --to doing\n\n"
    "Recovery and next help:\n"
    "  Command details: ensemble-orchestrator issue read --help,\n"
    "  ensemble-orchestrator issue list --help, ensemble-orchestrator issue lookup --help,\n"
    "  ensemble-orchestrator issue create --help, ensemble-orchestrator issue edit --help,\n"
    "  ensemble-orchestrator issue move --help, ensemble-orchestrator issue comment --help,\n"
    "  ensemble-orchestrator issue archive --help, ensemble-orchestrator issue relation add --help,\n"
    "  ensemble-orchestrator issue label swap --help\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)
_PAGES = (
    f"Pages: --limit N items, 1-{MAX_PAGE} (default {PAGE}); --cursor TOKEN, a page's\n"
    "  next_cursor, gives the next page under the same options. stdout:\n"
    "  {items, has_next_page, next_cursor (null on the last page), truncated}.\n"
    "  Over 8192 bytes, items drop from the end (next_cursor then continues\n"
    "  after the last one kept); a lone item's longest text is shortened, never\n"
    "  an id, each named in truncated as items.<n>.<field>.\n"
    f"  {FULL}: every page, written as indented JSON {{items, truncated}}\n"
    "  to FILE; prints only {output: FILE's absolute path}; not with\n"
    "  --cursor or --limit.\n"
)
_LIST_HELP = (
    _RULES + "Output items: id, identifier, title, url, state {id, name}, labels\n"
    "  [{id, name}], assignee {id, name}, project {id, name}, parent_id; empty:\n"
    "  null or []. The configured team's issues, every given filter holding, most\n"
    "  recently updated first; archived ones only with --include-archived.\n"
    "  --query TEXT matches title or description in any case; --state STATE:\n"
    "  backlog, ready, running, review, approved, done or canceled; --role SPEC:\n"
    "  a configured role, me (the linear.manager_key account) or\n"
    "  unassigned (no assignee); --task NAME, --client NAME: a configured task or\n"
    "  client name; --priority NAME: none, urgent, high, medium or low; --parent\n"
    "  ID: the parent issue's identifier or id. Linear only (a local board exits\n"
    "  2): --label (not a group or retired) and --project, each an id or exact\n"
    "  name in the team.\n"
    + _PAGES
    + "References: no match, several, or one outside the team exits 2 naming\n"
    "  candidates and the lookup command listing them; a name the configuration\n"
    "  lacks exits 2 naming the configured ones.\n\n"
    "Examples (a configured board holding TASK-1; --label and --project: Linear):\n"
    "  ensemble-orchestrator issue list --query 'linear issue'\n"
    "  ensemble-orchestrator issue list --state ready --task build --priority high\n"
    "  ensemble-orchestrator issue list --role me --project 'Ensemble'\n"
    "  ensemble-orchestrator issue list --role unassigned --client <client> --label bug\n"
    "  ensemble-orchestrator issue list --parent TASK-1 --include-archived --limit 50\n"
    "  ensemble-orchestrator issue list --cursor <next_cursor>\n"
    "  ensemble-orchestrator issue list --label bug --full --output bugs.json\n\n"
    "Recovery and next help:\n"
    "  Label and project values: ensemble-orchestrator issue lookup --help\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)
_LOOKUP_HELP = (
    _RULES + "Output items, every same-named entity its own:\n"
    "  teams       id, name, key (--query matches name or key)\n"
    "  states      id, name, type: the team's workflow states\n"
    "  labels      id, name, team_id (null: workspace), group_id: the team's and\n"
    "              the workspace's labels an issue can carry (no groups, none retired)\n"
    "  projects    id, name, team_ids: the team's projects\n"
    "  milestones  id, name, project_id: needs --project, within that project\n"
    "  users       id, name, email, active: --team's members, else the workspace's\n"
    "              users (--query matches name or email)\n"
    "  --query TEXT keeps names holding TEXT in any case.\n"
    + _PAGES
    + "References: an id or an exact name; a team also takes its key. Without\n"
    "  --team, the configured team. No match, several, or one outside the team\n"
    "  exits 2 naming candidates and the lookup command listing them.\n"
    "  Linear only: a local board exits 2.\n\n"
    "Examples (configured Linear access):\n"
    "  ensemble-orchestrator issue lookup teams\n"
    "  ensemble-orchestrator issue lookup labels --team TASK --query bug\n"
    "  ensemble-orchestrator issue lookup milestones --project 'Ensemble' --limit 50\n"
    "  ensemble-orchestrator issue lookup users --cursor <next_cursor>\n"
    "  ensemble-orchestrator issue lookup states --full --output states.json\n\n"
    "Recovery and next help:\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)
_READ_HELP = (
    _RULES + "Output: id, identifier, title, url, state, labels, project, milestone,\n"
    "  assignee, description, comments (newest first: id, author, created_at, body,\n"
    "  parent_id, quoted_text, on_behalf_of), blocks (issues it blocks), blocked_by\n"
    "  (issues blocking it), parent, children; empty: null or [].\n"
    "  Defaults: 5 comments of at most 300 characters, a 600-character\n"
    "  description, 10 issues per relation column. truncated lists every field\n"
    f"  cut or with more pages; continue then gives the read with {FULL}.\n"
    "  Over 8192 bytes, list items drop from the end, then the longest text\n"
    "  (title, description, names) is shortened, each named in truncated.\n"
    f"  {FULL}: read every page, keep every body, write the view as indented\n"
    "  JSON to FILE and print only {identifier, output: FILE's absolute path};\n"
    "  either option alone is a usage error. A failed read writes no file.\n\n"
    "Example (a configured board holding TASK-1):\n"
    "  ensemble-orchestrator issue read TASK-1\n"
    "  ensemble-orchestrator issue read TASK-1 --full --output TASK-1.json\n\n"
    "Recovery and next help:\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)


_WRITE_RULES = (
    "Prerequisites and effects:\n"
    "  Writes the configured board: Linear as the linear.manager_key account\n"
    "  (Keychain); a local board ($ENSEMBLE_BOARD_DIR) directly, as other. An\n"
    "  issue needs no recorded run. Help needs no credentials or services.\n"
    "  stdout: one receipt of at most 1024 bytes: issue {id, identifier, url},\n"
    "  operation, result (changed or unchanged), actor {id, name}%s.\n"
    "  It never echoes what was written or the board's response. Diagnostics go\n"
    "  to stderr.\n"
    "  Exit 0: changed or unchanged; 1: the board failed (Linear: GraphQL errors,\n"
    "  success false, HTTP; a local board: a file write) or the result is\n"
    "  unknown; 2: usage, configuration, Keychain, a board that cannot be\n"
    "  scanned, or an issue or value that does not resolve: nothing was written.\n"
    "  A timeout or outage during the write leaves the result unknown: check with\n"
    "  ensemble-orchestrator issue read ID before running it again; nothing retries.\n\n"
)
_CREATE_HELP = (
    _WRITE_RULES % "" + "Creates one issue in the configured team (linear.team; a local board:\n"
    "  <assignee>/<state>/<ID> <title>.md), then its blocking relations. Every\n"
    "  value resolves first, as ensemble-orchestrator issue list --help describes;\n"
    "  one that does not exits 2 with nothing created.\n"
    "--title TEXT: not blank. --body-file FILE: the Markdown description, UTF-8,\n"
    "  sent verbatim, minus a leading byte order mark; - reads stdin; blank or\n"
    "  unreadable exits 2.\n"
    "--state STATE: backlog, ready, running, review, approved, done or canceled;\n"
    "  default backlog.\n"
    "--role SPEC: the assignee: a configured role's account, me (the\n"
    "  linear.manager_key account) or unassigned (as without --role).\n"
    "--task NAME / --client NAME: the configured task / client label.\n"
    "--priority NAME: none, urgent, high, medium or low.\n"
    "--parent ID: an issue, the new one its sub-issue: this splits an issue.\n"
    "Linear only (a local board exits 2): --label VALUE, repeatable: not a group\n"
    "  or retired. --project VALUE: the team's. --milestone VALUE: --project's,\n"
    "  which it needs.\n"
    "Any other option not given stays unset.\n"
    "--blocks ID… / --blocked-by ID…: issues the new one blocks / that block it;\n"
    "  repeatable, repeats count once; one issue given to both exits 2.\n"
    "A timeout while creating leaves the result unknown: look for the issue with\n"
    "  ensemble-orchestrator issue list --query=TITLE before creating it again.\n"
    "A relation that fails exits 1 once the others are tried, stdout holding, in\n"
    "  at most 8192 bytes, the receipt with result partial, done, failed (each\n"
    "  with its error), recover and truncated; done and failed are cut before\n"
    "  recover. Run only the recover commands\n"
    "  (ensemble-orchestrator issue relation add …), never create again.\n\n"
    "Example (TASK-1, TASK-2 and TASK-3 on the configured board; the last: Linear):\n"
    "  ensemble-orchestrator issue create --title 'Fix login' --body-file note.md\n"
    "  ensemble-orchestrator issue create --title 'Split login' --body-file note.md \\\n"
    "    --parent TASK-1 --task build --role engineer --priority high\n"
    "  ensemble-orchestrator issue create --title 'Fix login' --body-file - --label bug \\\n"
    "    --project 'Ensemble' --milestone M1 --role me --state ready \\\n"
    "    --blocks TASK-2 --blocked-by TASK-3 < note.md\n\n"
    "Recovery and next help:\n"
    "  The new issue: ensemble-orchestrator issue read <identifier>\n"
    "  Label, project and milestone values: ensemble-orchestrator issue lookup --help\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)
_MOVE_HELP = (
    _WRITE_RULES % "" + "STATE: backlog, ready, running, review, approved, done or canceled: on\n"
    "  Linear the state [orchestrator.linear.states] backlog, todo, in_progress,\n"
    "  in_review, handoff, done or canceled names, in that order, and only an\n"
    "  issue of the configured team (linear.team) moves: another team's exits 2\n"
    "  naming its team; on a local board the file moves to <assignee>/<STATE>/,\n"
    "  an archived issue exits 2. Any other STATE is a usage error. Already in\n"
    "  that state: result unchanged, nothing written.\n\n"
    "Example (a configured board holding TASK-1):\n"
    "  ensemble-orchestrator issue move TASK-1 approved\n"
    "  ensemble-orchestrator issue move TASK-1 running\n\n"
    "Recovery and next help:\n"
    "  The issue's current state: ensemble-orchestrator issue read TASK-1\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)

_EDIT_HELP = (
    _WRITE_RULES % "" + "Changes the given fields of one issue in one update naming only what\n"
    "  changes; give at least one of --title, --body-file, --role, --project,\n"
    "  --task, --client, --priority. Every given value already so: result\n"
    "  unchanged, nothing written. A name the configuration lacks exits 2 before\n"
    "  anything is read.\n"
    "--title TEXT: not blank. --body-file FILE: the new Markdown description,\n"
    "  UTF-8, sent verbatim, minus a leading byte order mark; - reads stdin;\n"
    "  blank or unreadable exits 2.\n"
    "--role SPEC: the assignee: a configured role's account, me (the\n"
    "  linear.manager_key account) or unassigned (no assignee); a local board:\n"
    "  the file moves to <SPEC>/<state>/, an archived issue exits 2.\n"
    "--project VALUE: Linear only (a local board exits 2): a project of the\n"
    "  configured team, an id or exact name, resolved before the issue is read;\n"
    "  no match, several or another team's exits 2 naming the candidates. Only\n"
    "  on an issue of the configured team (linear.team): another team's exits 2.\n"
    "--task NAME / --client NAME: a configured task / client name (a local\n"
    "  board: a configured role's task / any client name). On Linear its label\n"
    "  replaces the issue's labels of that group: every other label stays, and\n"
    "  only on an issue of the configured team (linear.team): another team's\n"
    "  exits 2.\n"
    "--priority NAME: none, urgent, high, medium or low.\n\n"
    "Example (a configured board holding TASK-1; the last: Linear):\n"
    "  ensemble-orchestrator issue edit TASK-1 --title 'Fix login' --task build\n"
    "  ensemble-orchestrator issue edit TASK-1 --body-file note.md --priority high\n"
    "  ensemble-orchestrator issue edit TASK-1 --body-file - --client <client> < note.md\n"
    "  ensemble-orchestrator issue edit TASK-1 --role engineer\n"
    "  ensemble-orchestrator issue edit TASK-1 --project 'Ensemble'\n\n"
    "Recovery and next help:\n"
    "  The issue's fields and labels: ensemble-orchestrator issue read TASK-1\n"
    "  Project values: ensemble-orchestrator issue lookup projects\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)

_COMMENT_HELP = (
    _WRITE_RULES
    % ", comment_id"
    + "--body-file FILE: the comment's Markdown body, UTF-8, sent verbatim as a\n"
    "  GraphQL variable: quotes, backticks, $ and newlines need no escaping; a\n"
    "  leading byte order mark is dropped.\n"
    "  - reads stdin. Blank, unreadable or non-UTF-8, or --body-file given twice:\n"
    "  exit 2 before anything is written. Each run posts a new top-level comment.\n\n"
    "Example (a configured board holding TASK-1):\n"
    "  ensemble-orchestrator issue comment TASK-1 --body-file note.md\n"
    "  ensemble-orchestrator issue comment TASK-1 --body-file - < note.md\n\n"
    "Recovery and next help:\n"
    "  Whether a comment landed: ensemble-orchestrator issue read TASK-1\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)

_ARCHIVE_HELP = (
    _WRITE_RULES
    % ""
    + "Archives the issue only when it and every unarchived sub-issue, at any depth,\n"
    "  are finished: state type completed, canceled or duplicate (Linear archives\n"
    "  sub-issues with their parent). It first reads every page of sub-issues at\n"
    "  every level; a failed read writes nothing. There is no override: an\n"
    "  unfinished one exits 1 naming it. Already archived: result unchanged,\n"
    "  nothing written. It closes no run and deletes no file.\n"
    "--dry-run: write nothing and print, in at most 8192 bytes, {issue, operation,\n"
    "  dry_run, result (what archive would do: changed, unchanged or refused),\n"
    "  descendant_count, descendants (the sub-issues it would archive too: id,\n"
    "  identifier, state), unfinished_count, unfinished, truncated}; over 8192\n"
    "  bytes, list items drop from the end, each such list named in truncated.\n"
    "  Exit 0 whatever result says.\n\n"
    "Example (a configured board holding TASK-1):\n"
    "  ensemble-orchestrator issue archive TASK-1 --dry-run\n"
    "  ensemble-orchestrator issue archive TASK-1\n\n"
    "Recovery and next help:\n"
    "  An unfinished issue: ensemble-orchestrator issue move TASK-1 done\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)

_RELATION_HELP = (
    _WRITE_RULES
    % ""
    + "--blocks OTHER: the edge ID blocks OTHER; --blocked-by OTHER: OTHER blocks ID.\n"
    "  Exactly one, given once; OTHER an issue identifier (TASK-2) or id, never ID\n"
    "  itself (exit 2). ID --blocks OTHER and OTHER --blocked-by ID name the\n"
    "  same edge. add creates it, already there: result unchanged; remove\n"
    "  deletes it, not there: result unchanged; nothing is written then. The\n"
    "  reverse edge and relations of other types (related, duplicate) between\n"
    "  the two stay as they are. remove deletes every record of the edge; one\n"
    "  failing after others went exits 1 naming the deleted ids: check with\n"
    "  ensemble-orchestrator issue read ID before running it again.\n\n"
    "Example (a configured board holding TASK-1 and TASK-2):\n"
    "  ensemble-orchestrator issue relation add TASK-1 --blocks TASK-2\n"
    "  ensemble-orchestrator issue relation remove TASK-1 --blocks TASK-2\n"
    "  ensemble-orchestrator issue relation add TASK-2 --blocked-by TASK-1\n\n"
    "Recovery and next help:\n"
    "  The issue's relations (blocks, blocked_by): ensemble-orchestrator issue read TASK-1\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)

_SWAP_HELP = (
    _WRITE_RULES
    % ""
    + "Replaces the label --from OLD names on ID by the one --to NEW names, in one\n"
    "  update naming only those two: every other label stays. Each is an id or an\n"
    "  exact name (case-sensitive) of a label of the issue's team or the\n"
    "  workspace; OLD is first looked for among the issue's labels and may be\n"
    "  retired. NEW is no label group and not retired. NEW already there is not\n"
    "  added again; OLD missing and NEW there: result unchanged, nothing written.\n"
    "  Refused (exit 1, nothing written): the issue carries neither, or NEW is in\n"
    "  a label group the issue already holds another label of.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  ensemble-orchestrator issue label swap TASK-1 --from todo --to doing\n"
    "  ensemble-orchestrator issue label swap TASK-1 --from bug --to feature\n\n"
    "Recovery and next help:\n"
    "  The issue's labels: ensemble-orchestrator issue read TASK-1\n"
    "  Label names and ids: ensemble-orchestrator issue lookup labels --team TASK\n"
    "  Configuration and command discovery: ensemble-orchestrator --help"
)


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given twice")
        setattr(namespace, self.dest, values)


def _parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read and write board issues and look up what their options name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser(
        "read",
        help="print one issue's fields, newest comments and relations",
        description="Print one issue's fields, newest comments and relations as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_READ_HELP,
    )
    read.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    read.add_argument("--full", action="store_true", help="every page and body; needs --output")
    read.add_argument("--output", metavar="FILE", help="with --full: where the JSON goes")
    found = commands.add_parser(
        "list",
        help="search the configured team's issues by text, state, role, task, client, priority, "
        "label, project or parent",
        description="List the configured team's issues that every given filter holds as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_LIST_HELP,
    )
    found.add_argument("--query", metavar="TEXT", help="title or description holds TEXT, any case")
    found.add_argument("--state", choices=STATES, metavar="STATE", help=", ".join(STATES))
    found.add_argument("--role", metavar="SPEC", help="a configured role, me or unassigned")
    found.add_argument("--task", metavar="NAME", help="a configured task name")
    found.add_argument("--client", metavar="NAME", help="a configured client name")
    found.add_argument("--priority", choices=PRIORITIES, metavar="NAME", help=", ".join(PRIORITIES))
    found.add_argument("--label", metavar="VALUE", help="Linear only: label id or name")
    found.add_argument("--project", metavar="VALUE", help="Linear only: project id or name")
    found.add_argument("--parent", metavar="ID", help="parent issue identifier (TASK-1) or id")
    found.add_argument("--include-archived", action="store_true", help="archived issues too")
    _paged(found)
    lookup = commands.add_parser(
        "lookup",
        help="Linear only: list teams, states, labels, projects, milestones or users with ids",
        description="List one kind of Linear entity, with its id and fixed fields, as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_LOOKUP_HELP,
    )
    lookup.add_argument(
        "resource",
        type=Resource,
        choices=list(Resource),
        metavar="RESOURCE",
        help=", ".join(Resource),
    )
    lookup.add_argument("--query", metavar="TEXT", help="keep names holding TEXT, any case")
    lookup.add_argument(
        "--team", metavar="VALUE", help="team id, name or key (default: configured)"
    )
    lookup.add_argument("--project", metavar="VALUE", help="milestones: the project's id or name")
    _paged(lookup)
    create = commands.add_parser(
        "create",
        help="create one issue with state, role, task, client, priority, parent, labels, project, "
        "milestone and blocking relations",
        description="Create one issue in the configured team with its fields and blocking "
        "relations in one run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_CREATE_HELP,
    )
    create.add_argument("--title", required=True, action=_Once, metavar="TEXT", help="not blank")
    create.add_argument(
        "--body-file", required=True, action=_Once, metavar="FILE", help="UTF-8 Markdown; -: stdin"
    )
    create.add_argument(
        "--state",
        action=_Once,
        choices=STATES,
        metavar="STATE",
        help=", ".join(STATES) + " (default: backlog)",
    )
    for option, metavar, what in (
        ("--role", "SPEC", "assignee: a configured role, me or unassigned"),
        ("--task", "NAME", "a configured task name"),
        ("--client", "NAME", "a configured client name"),
        ("--parent", "ID", "parent issue identifier (TASK-1) or id: a split"),
        ("--project", "VALUE", "Linear only: project id or name"),
        ("--milestone", "VALUE", "Linear only: milestone id or name, of --project"),
    ):
        create.add_argument(option, action=_Once, metavar=metavar, help=what)
    create.add_argument(
        "--priority", action=_Once, choices=PRIORITIES, metavar="NAME", help=", ".join(PRIORITIES)
    )
    create.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="VALUE",
        help="Linear only: label id or name; repeatable",
    )
    create.add_argument(
        "--blocks", action="extend", nargs="+", default=[], metavar="ID", help="issues it blocks"
    )
    create.add_argument(
        "--blocked-by",
        action="extend",
        nargs="+",
        default=[],
        metavar="ID",
        help="issues blocking it",
    )
    change = commands.add_parser(
        "edit",
        help="change one issue's title, description, assignee, project, task, client or priority",
        description="Change the title, description, assignee, project, task, client or priority "
        "of one issue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EDIT_HELP,
    )
    change.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    change.add_argument("--title", action=_Once, metavar="TEXT", help="not blank")
    change.add_argument(
        "--body-file", action=_Once, metavar="FILE", help="the description, UTF-8; -: stdin"
    )
    change.add_argument(
        "--role", action=_Once, metavar="SPEC", help="assignee: a configured role, me or unassigned"
    )
    change.add_argument(
        "--project", action=_Once, metavar="VALUE", help="Linear only: project id or name"
    )
    change.add_argument("--task", action=_Once, metavar="NAME", help="a configured task name")
    change.add_argument("--client", action=_Once, metavar="NAME", help="a configured client name")
    change.add_argument(
        "--priority", action=_Once, choices=PRIORITIES, metavar="NAME", help=", ".join(PRIORITIES)
    )
    move = commands.add_parser(
        "move",
        help="set one issue's state, an abstract name",
        description="Set the state of one issue of the configured team by its abstract name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MOVE_HELP,
    )
    move.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    move.add_argument("state", choices=STATES, metavar="STATE", help=", ".join(STATES))
    comment = commands.add_parser(
        "comment",
        help="post a comment on one issue from a UTF-8 file or stdin",
        description="Post a new top-level comment on one issue, its body read verbatim.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_COMMENT_HELP,
    )
    comment.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    comment.add_argument(
        "--body-file", required=True, action=_Once, metavar="FILE", help="UTF-8 body; -: stdin"
    )
    archive = commands.add_parser(
        "archive",
        help="archive one issue once it and every sub-issue are finished",
        description="Archive one issue once it and every unarchived sub-issue are finished.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_ARCHIVE_HELP,
    )
    archive.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    archive.add_argument(
        "--dry-run", action="store_true", help="list what it would archive; write nothing"
    )
    relation = commands.add_parser(
        "relation",
        help="add or remove one blocking relation between two issues",
        description="Add or remove one blocking relation between two issues.",
    )
    edits = relation.add_subparsers(dest="op", required=True)
    for op, verb in (("add", "Add"), ("remove", "Remove")):
        edit = edits.add_parser(
            op,
            help=f"{op} the edge ID blocks OTHER, or OTHER blocks ID",
            description=f"{verb} one blocking edge between ID and OTHER, in the given direction.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=_RELATION_HELP,
        )
        edit.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
        way = edit.add_mutually_exclusive_group(required=True)
        for direction, what in (("blocks", "ID blocks OTHER"), ("blocked_by", "OTHER blocks ID")):
            way.add_argument(
                "--" + direction.replace("_", "-"),
                dest=direction,
                action=_Once,
                metavar="OTHER",
                help=f"{what}: an issue identifier or id",
            )
    labels = commands.add_parser(
        "label",
        help="Linear only: replace one label of an issue, keeping the rest",
        description="Change one issue's labels one at a time.",
    )
    swap = labels.add_subparsers(dest="op", required=True).add_parser(
        "swap",
        help="replace label OLD by NEW on one issue, keeping every other label",
        description="Replace one label of an issue by another, keeping every other label.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_SWAP_HELP,
    )
    swap.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    swap.add_argument(
        "--from", dest="old", required=True, action=_Once, metavar="OLD", help="label id or name"
    )
    swap.add_argument(
        "--to", dest="new", required=True, action=_Once, metavar="NEW", help="label id or name"
    )
    return parser


def _paged(parser):
    parser.add_argument(
        "--limit", type=_limit, metavar="N", help=f"items per page, 1-{MAX_PAGE} (default {PAGE})"
    )
    parser.add_argument("--cursor", metavar="TOKEN", help="a page's next_cursor: the next page")
    parser.add_argument("--full", action="store_true", help="every page; needs --output")
    parser.add_argument("--output", metavar="FILE", help="with --full: where the JSON goes")


def _limit(text):
    try:
        found = int(text)
    except ValueError:
        found = 0
    if not 1 <= found <= MAX_PAGE:
        raise argparse.ArgumentTypeError(f"want 1-{MAX_PAGE}, got {text!r}")
    return found


def _usage(args):
    """The usage error in args' options, or None."""
    if args.command == "edit" and (
        (args.title, args.body_file, args.role, args.project, args.task, args.client)
        + (args.priority,)
        == (None,) * 7
    ):
        return (
            "edit: give at least one of --title, --body-file, --role, --project, --task, "
            "--client, --priority"
        )
    if args.command in ("create", "edit") and args.title is not None and not args.title.strip():
        return "--title: empty title"
    if "full" not in args:
        return None
    if args.full != (args.output is not None):
        if args.command == "read":
            return f"--full and --output go together: read {args.id} {FULL}"
        return f"--full and --output go together: {args.command} … {FULL}"
    if args.command != "read" and args.full and (args.cursor, args.limit) != (None, None):
        return "--full reads every page: drop --cursor and --limit"
    return None


def read_body(path, stdin=None):
    """The UTF-8 text of path, or for - of stdin (bytes; None: sys.stdin's), verbatim but for a
    leading byte order mark; IssueError when it cannot be read or holds only whitespace and byte
    order marks.
    """
    try:
        if path != "-":
            with open(path, "rb") as f:
                raw = f.read()
        elif (stream := getattr(sys.stdin, "buffer", None) if stdin is None else stdin) is None:
            raise IssueError("--body-file -: no stdin to read")
        else:
            raw = stream.read()
        body = raw.decode("utf-8-sig")
    except OSError as error:
        raise IssueError(f"--body-file {path}: {error.strerror or error}") from None
    except UnicodeDecodeError:
        raise IssueError(f"--body-file {path}: not UTF-8") from None
    if not body.replace("﻿", "").strip():
        raise IssueError(f"--body-file {path}: empty body")
    return body


def _request(args, body):
    """The typed request of list, lookup, create or edit from args (body: the read --body-file);
    None for the other commands.
    """
    if args.command == "list":
        return ListQuery(
            query=args.query,
            state=_state(args.state),
            role=args.role,
            task=args.task,
            client=args.client,
            priority=_priority(args.priority),
            parent=args.parent,
            include_archived=args.include_archived,
            label=args.label,
            project=args.project,
        )
    if args.command == "lookup":
        return LookupQuery(
            resource=args.resource, query=args.query, team=args.team, project=args.project
        )
    if args.command == "create":
        return NewIssue(
            title=args.title,
            body=body,
            state=_state(args.state),
            role=args.role,
            task=args.task,
            client=args.client,
            priority=_priority(args.priority),
            parent=args.parent,
            labels=tuple(args.label),
            project=args.project,
            milestone=args.milestone,
            blocks=tuple(args.blocks),
            blocked_by=tuple(args.blocked_by),
        )
    if args.command == "edit":
        return IssueEdit(
            id=args.id,
            title=args.title,
            body=body,
            role=args.role,
            project=args.project,
            task=args.task,
            client=args.client,
            priority=_priority(args.priority),
        )
    return None


def _state(name):
    return None if name is None else IssueState(name)


def _priority(name):
    return None if name is None else Priority(name)


def _run(
    board: Backend, args, request: ListQuery | LookupQuery | NewIssue | IssueEdit | None, body
):
    if args.command == "move":
        return receipt(receipt_payload(board.move(args.id, args.state)))
    if args.command == "comment":
        return receipt(receipt_payload(board.comment(args.id, body)))
    if isinstance(request, IssueEdit):
        return receipt(receipt_payload(board.edit(request)))
    if isinstance(request, NewIssue):
        return receipt(receipt_payload(board.create(request)))
    if args.command == "relation":
        direction = "blocks" if args.blocks is not None else "blocked_by"
        other = getattr(args, direction)
        return receipt(receipt_payload(board.relation(args.op, args.id, direction, other)))
    if args.command == "archive" and args.dry_run:
        plan = dry_run(board.archive_plan(args.id))
        return fit(plan, (("descendants", "unfinished"),), (), None)
    if args.command == "archive":
        return receipt(receipt_payload(board.archive(args.id)))
    if isinstance(request, ListQuery):
        return _listed(board.page, board.walk, request, args)
    view = board.view(args.id, full=args.full)
    if args.full:
        return _written(args.output, view, {"identifier": view["identifier"]})
    return fit(
        compact(view),
        (("comments", "labels", "blocks", "blocked_by", "children"),),
        TEXTS,
        f"ensemble-orchestrator issue read {view['identifier']} {FULL}",
    )


def _listed(page, walk, query, args):
    """list or lookup: page(query, …)'s page under args' --limit and --cursor, or with --full
    walk(query)'s every row written to --output.
    """
    if not args.full:
        rows, more, end = page(query, args.limit or PAGE, args.cursor)
        return fit_page(rows, end if more else None)
    rows = walk(query)
    cut = [f"items.{n}.{field}" for n, (_, fields, _) in enumerate(rows) for field in fields]
    payload = {"items": [item for item, _, _ in rows], "truncated": cut}
    return _written(args.output, payload, {})


def _written(output, payload, summary):
    """summary and output's absolute path, once payload is written there."""
    try:
        path = write_full(output, payload)
    except OSError as error:
        raise IssueError(f"--output {output}: {error.strerror or error}") from None
    return encode({**summary, "output": path})


def _linear_only(command, request):
    """What command and its request ask of a local board that only Linear has, or None."""
    if command == "lookup":
        return "lookup"
    if command == "label":
        return "label swap"
    if isinstance(request, ListQuery):
        given = {"--label": request.label is not None, "--project": request.project is not None}
    elif isinstance(request, NewIssue):
        given = {
            "--label": bool(request.labels),
            "--project": request.project is not None,
            "--milestone": request.milestone is not None,
        }
    elif isinstance(request, IssueEdit):
        given = {"--project": request.project is not None}
    else:
        return None
    return next((option for option, is_set in given.items() if is_set), None)


def _linear(cfg, gql):
    """cfg's LinearIssues, as linear.manager_key."""
    linear = config.linear_settings(cfg, "issue")
    if not linear.manager_key:
        raise ConfigError("orchestrator/config.toml: linear.manager_key is unset")
    return LinearIssues(functools.partial(gql, service=linear.manager_key), linear)


def main(argv, *, gql=linear_gql, config_path=None, out=None, err=None, stdin=None):
    args = _parser().parse_args(argv)
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr if err is None else err
    if usage := _usage(args):
        diagnose(err, usage)
        return USAGE
    body = None
    if getattr(args, "body_file", None) is not None:
        try:
            body = read_body(args.body_file, stdin)
        except IssueError as error:
            diagnose(err, error)
            return USAGE
    try:
        cfg = config.load(path=config_path)
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        diagnose(err, error)
        return USAGE
    request = _request(args, body)
    if cfg.tracker is TrackerKind.LOCAL and (what := _linear_only(args.command, request)):
        diagnose(err, _LINEAR_ONLY.format(what))
        return USAGE
    try:
        board = (
            LocalIssues(cfg.board, cfg.roles, prefix=cfg.local_prefix)
            if cfg.tracker is TrackerKind.LOCAL
            else _linear(cfg, gql)
        )
    except (ConfigError, TrackerFileError) as error:
        diagnose(err, error)
        return USAGE
    try:
        if isinstance(board, LinearIssues) and args.command == "label":
            data = receipt(receipt_payload(board.swap(args.id, args.old, args.new)))
        elif isinstance(board, LinearIssues) and isinstance(request, LookupQuery):
            data = _listed(board.lookup_page, board.lookup_walk, request, args)
        else:
            data = _run(board, args, request, body)
    except IssueError as error:
        diagnose(err, error)
        return USAGE
    except GraphQLErrors as error:
        diagnose(err, "linear api error: " + "; ".join(messages(error.errors)))
        return FAILED
    except RelationsFailed as error:
        out.write(fit(partial_report(error), (("done", "failed"), ("recover",)), (), None))
        diagnose(err, partial_message(error))
        return FAILED
    except ArchiveRefused as refused:
        diagnose(err, refusal(args.id, refused.plan))
        return FAILED
    except (
        LinearError,
        Unavailable,
        OverflowError,
        LabelRefused,
        TrackerFileError,
        OSError,
    ) as error:
        diagnose(err, error)
        return FAILED
    out.write(data)
    return OK
