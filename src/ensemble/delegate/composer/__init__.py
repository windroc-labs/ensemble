"""Composer owns a config snapshot and Team, resolves run settings and access, and renders prompts.

Formats of roles/, tasks/ and templates/ files, the team root's and a team dir's:
- roles/<role>.md, a charter: a `# ` title, an intro, then `## Tasks`, the role's task index and the
  only list of its tasks: an opening sentence naming the default, "unsure → `<task>`
  (`<tasks>/<task>.md`)", then one line per task, "- `<task>` (`<tasks>/<task>.md`): …". The default
  is a listed task; each task's file is in the role's own tasks dir (a team dir's role: that team
  dir's tasks/, else the team root's).
- tasks/<task>.md: a `# ` heading, exactly one `[ensemble-progress:start] <what to report>` line (a
  progress mark, PROGRESS) and a `## Resume` section outside code fences.
- Text only: no front matter (---), no {{…}} placeholder; each `<team>/methods/<m>.md` named is a
  file of the team root's methods/.
- tools/ (a team dir's optional): files a role's commands run as `<role team>/tools/<file>`, the
  team root holding the role's charter; trusted as the team dir's text (teams.check_team_dirs).

A role's index is checked whenever it is read, a team dir file whenever it is looked up; a defect
raises ConfigError naming the file.
"""

from ensemble.delegate.composer.composer import Composer
from ensemble.delegate.composer.types import (
    ConfigError,
    DelegateConfig,
    RunConfig,
    RunParams,
    Team,
)

__all__ = [
    "ConfigError",
    "Composer",
    "DelegateConfig",
    "RunConfig",
    "RunParams",
    "Team",
]
