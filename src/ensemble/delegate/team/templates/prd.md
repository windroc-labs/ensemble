# PRD: [Reference] [Product name]

Title: no gloss; `[Product name]` never repeats `[Reference]`. One fact per list item or requirement row; its reason goes under Assumptions or Approach and trade-offs, not in that item or row. Markers, leading the item or table cell: ✅ the chosen approach, ❌ a rejected one, ❓ an Open question, ⚠️ a Risk; no other emoji.

## Problem and goals
Opens with one sentence: the problem, for whom, and the goal. Then list items: its evidence (a report finding, a source or the user's words), the goals, and what it explicitly won't do.

## Users and scenarios
Target users and where they use it.

## User flow
The main flow from start to finish: what the user sees and does at each step; a diagram where [Principles › Writing › Diagrams and tables](#writing) calls for one.

## Assumptions, open questions and risks
### Assumptions
Inferences the PRD rests on; one resting on research cites the finding and its confidence.
### Open questions
What the user must decide, each with a suggested answer; until the user answers, the suggested answer holds. Its name, in both headings and every mention: Open questions; in Chinese 开放问题, no other rendering.
### Risks
Unknowns, and the low-confidence or single-source findings the PRD relies on.

## Approach and trade-offs
Optional; omit when nothing is compared. 2–3 viable product or scope options, or architecture options only when the requirement is itself an architecture or layering decision ([PM › Boundaries](#boundaries)), and their trade-offs as a table ([Principles › Writing › Diagrams and tables](#writing)), which one is chosen and why.

## System diagram
Optional; omit when the change has no structure worth drawing. The existing components the change touches and how they connect or flow, plus the chosen architecture under the exception in [PM › Boundaries](#boundaries), as a diagram ([Principles › Writing › Diagrams and tables](#writing)).

## Requirements
Ids (`FR-<n>`, `NFR-<n>`) are never renumbered on revision. Each subsection is one table: the header row shown, then one row per requirement, split finer when one spans modules. Rows of one functional module are adjacent; its name fills the Module cell of its first row only, left empty on the rest (no HTML).
### Functional requirements
Header `| ID | <Module, translated> | <Requirement, translated> | <Check, translated> |`; a row `| FR-<n> | <module> | <the requirement> | <an observable pass condition (an input → an output or state)> |`.
### Non-functional requirements
Optional; omit when none. Header `| ID | <Module, translated> | <Constraint, translated> | <Threshold or check, translated> |`; a row `| NFR-<n> | <module> | <a constraint this product actually has> | <its threshold or check> |`.

## Done
Optional; omit when the product has nothing built yet. Under `###` copies of the original headings, by id, naming where each is implemented (file or commit).
