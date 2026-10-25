# Report: [Reference] [Title]

<type>: <task> · <web|local|mixed>

> [!IMPORTANT]
> **<one-sentence answer>** (<overall confidence>)
>
> - <reason 1>
> - <reason 2, optional>
> - <reason 3, optional>
>
> <Next step, translated>: <action>
>
> <Risk, translated>: <main risk>

## Template rules

Instruction sections: omit Template rules and Filled example from the report. Start with the H1, type line, then the alert, with no Conclusion heading between them.

- Title: no gloss; `[Title]` never repeats `[Reference]`. Combining reports: each id appears once, in its sub-report's heading.
- Type line: `<type>` is Light Research, Research or Deep Research, translated; `<task>` is the selected task name. For local or mixed, append ` · <repo>@<12-character sha>` for each repo; web has no repo segment. Task depth:

| Type | Verification |
|---|---|
| Light Research | One pass by one agent; no subagents or verification stage |
| Research | One parallel round; each agent checks its findings; no independent verification |
| Deep Research | Research workflows; independent votes on key claims |

- Alert: one bold answer with confidence; at most 3 reasons; Next step and Risk each one line. No synthesis label inside it.
- Body: text where detail is needed; a figure (Mermaid, or SVG: [Researcher › Standards › Figures](#standards)) or table wherever it makes the report easier to read. Be concise; avoid long essay-style paragraphs. One fact per list item. Keep only material relevant to the research question. Mark synthesis at most once per section, outside the alert.
- Length: display columns, lists/paragraphs 80; table last column 80, other cells 30. W/F characters in `unicodedata.east_asian_width` count 2, others 1. URLs, link targets, `[n]` and Markdown markers do not count. Code/Mermaid blocks, headings, type line and HTML tag lines are exempt; text inside `<details>` is checked. Soft-wrapped text shares one item's budget. Delivery check: [Researcher › Standards › Concise](#standards).

## Comparison table

Optional; omit when the input asks for no comparison. A table ([Principles › Writing › Diagrams and tables](#writing)), labeled as your synthesis. Score cells with ✅⚠️❌ plus short values; below it, a one-line legend. Remove rows whose compared values are the same; combine them below as `<Both have, translated>: …`. Over the list/paragraph limit → that label followed by short list items instead.

## Findings

<details>
<summary>Findings (<count>)</summary>

One subsection per part of the question, each starting with one **bold answer**; list the findings the conclusion depends on first. One list item per finding, ending with `(<confidence: high, medium or low>; [n]…; <single-source or unverified, when so>)`. Translate the label names, keeping the listed values English. Sources disagree → name both and which you favor, and why; confidence at most medium.

</details>

## Corrections to known claims

Optional; omit when the input states no known claims. One list item per claim: the claim → the correction, its sources (`[n]`).

## Gaps

Optional; omit when none. Two columns, translated; conclusion-changing gaps first:

| Impact | Gap |
|---|---|
| Could change conclusion | <uncovered, unverified, refuted or open point> |
| Does not change conclusion | <point> |

## Sources

<details>
<summary>Sources (<count>)</summary>

Numbered in first-citation order, numbers matching in-text `[n]`: `1. [<Short title>](<URL or code permalink>) (primary|secondary|code)`.

</details>

## Filled example

# Report: Cursor CLI or Grok Build for ensemble

Light Research: light-research · mixed · windroc-labs/ensemble@f597b17033e7

> [!IMPORTANT]
> **Prefer Grok Build for the next client.** (medium)
>
> - Caller-chosen session ids simplify integration.
> - Claude-style hooks reduce adapter work.
> - Account isolation has documented storage paths.
>
> Next step: verify interactive hooks before implementing.
>
> Risk: interactive session creation remains untested.

## Comparison table

- Synthesis: compare integration differences.

| Need | Cursor CLI | Grok Build |
|---|---|---|
| New session id | ⚠️ create-chat first | ✅ caller UUID via -s |
| Hooks | ⚠️ CLI coverage unclear | ✅ Claude-style payloads |
| Account isolation | ⚠️ config only documented | ✅ GROK_HOME |
| Source access | ❌ closed | ✅ Apache-2.0 |

- Legend: ✅ supported; ⚠️ gap or extra work; ❌ unavailable.
- Both have: interactive terminals, resume by id, headless output.

## Findings

<details>
<summary>Findings (4)</summary>

### Existing client contract

**The adapter needs hooks, ids and recorded replies.**

- Map hooks to HookKind; provide launch, reply, session_id and observe. (high; [1])
- Claude supplies ids and injects hooks at launch. (high; [2][3])

### Cursor CLI

**Session creation adds a step; hook coverage has gaps.**

- create-chat returns an id; --resume restores it. (high; [4])
- stop lacks reply text; afterAgentResponse carries it. (high; [5])
- CLI emits fewer hooks, says NVIDIA. (low; [6]; single-source)
- Full account isolation remains undocumented. (low; unverified)

### Grok Build

**Documented ids and hooks fit the existing adapter.**

- -s creates with a UUID; -r resumes; GROK_HOME stores sessions. (high; [7])
- Stop carries lastAssistantMessage; StopCancelled marks interruption. (high; [8])
- Disable inherited Claude/Cursor hooks using their ENABLED flags. (high; [8][9])
- GROK_HOME holds auth.json as well as sessions. (high; [7][10])
- Grok Build is Apache-2.0. (high; [11])

### Integration work

**Reuse Claude's shape with account-scoped hook files.**

```mermaid
flowchart LR
  A[Set GROK_HOME] --> B[Write hooks]
  B --> C[Launch or resume]
  C --> D[Read updates]
```

- Synthesis: map against Claude's adapter; filter hooks by sessionId. [3][7][8]
- Read updates.jsonl fields before implementing observe. (low; unverified)

</details>

## Gaps

| Impact | Gap |
|---|---|
| Could change conclusion | Neither interactive hook path was tested. |
| Does not change | Interactive -s is untested; fallback increases work. |
| Does not change | Cursor isolation and CLI hook coverage remain unverified. |
| Does not change | updates.jsonl fields still need source inspection. |
| Does not change | Current beta status could not be confirmed. |

## Sources

<details>
<summary>Sources (11)</summary>

1. [Client contract](src/ensemble/session/clients/base.py#L116-L192) (code)
2. [Claude ids](src/ensemble/session/clients/claude/__init__.py#L18-L19) (code)
3. [Claude hooks](src/ensemble/session/clients/claude/tui.py#L63-L106) (code)
4. [Cursor parameters](https://cursor.com/docs/cli/reference/parameters) (primary)
5. [Cursor hooks](https://cursor.com/docs/agent/hooks) (primary)
6. [NVIDIA CLI caveats](https://docs.nvidia.com/nemo/relay/v0.4.0/nemo-relay-cli/cursor) (secondary)
7. [Grok sessions](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md#L18-L42) (primary)
8. [Grok hooks](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md#L60-L435) (primary)
9. [Grok config](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/05-configuration.md#L21-L30) (primary)
10. [Grok authentication](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md#L44-L53) (primary)
11. [Grok repository](https://github.com/xai-org/grok-build) (primary)

</details>
