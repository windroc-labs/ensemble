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

Instruction section: omit Template rules from the report. Start with the H1, type line, then the alert, with no Conclusion heading between them.

- Title: no gloss; `[Title]` never repeats `[Reference]`. Combining reports: each id appears once, in its sub-report's heading.
- Type line: `<type>` is Light Research, Research or Deep Research, translated; `<task>` is the selected task name. For local or mixed, append ` · <repo>@<12-character sha>` for each repo; web has no repo segment. Task depth:

| Type | Verification |
|---|---|
| Light Research | One pass by one agent; no subagents or verification stage |
| Research | One parallel round; each agent checks its findings; no independent verification |
| Deep Research | Research workflows; independent votes on key claims |

- Alert: one bold answer with confidence; at most 3 reasons; Next step and Risk each one line. No synthesis label inside it.
- Body: text where detail is needed; a figure (Mermaid, or SVG: [Researcher › Standards › Figures](#standards)) or table wherever it makes the report easier to read. No essay paragraphs. Outside Findings, one fact per list item. Keep only material relevant to the research question. Mark synthesis at most once per section, outside the alert.
- Citations: `[n]` is source number n in Sources, written as a link to that source, `[[n]](<URL or code permalink>)`, the same target as its Sources item, so a click opens it. Put it right after the sentence or cell it supports, never in a trailing pile.
- Length, in display columns: W/F characters in `unicodedata.east_asian_width` count 2, others 1. URLs, link targets, `[n]` and Markdown markers do not count. Code/Mermaid blocks, headings, type line and HTML tag lines are exempt. Soft-wrapped text shares one item's budget.
  - Outside Findings: lists/paragraphs 80; table last column 80, other cells 30.
  - Findings: limits in [Findings](#findings) below. Delivery check: [Researcher › Standards › Concise](#standards).

## Comparison table

Optional; omit when the input asks for no comparison. A table ([Principles › Writing › Diagrams and tables](#writing)), labeled as your synthesis. A compared project with its own address is a link to it, wherever the table names it: `[<name>](<URL>)`, the URL its official code repository, else its official homepage; no address → plain text. Score cells with ✅⚠️❌ plus short values; below it, a one-line legend. Remove rows whose compared values are the same; combine them below as `<Both have, translated>: …`. Over the list/paragraph limit → that label followed by short list items instead.

## Findings

<details id="findings" open>
<summary>Findings: <A> aspects, <F> findings</summary>

The body of the report: the evidence behind the alert, organized so a reader can open one aspect and read it alone.

**Aspects.** One aspect per part of the research question, named after that part (e.g. "What it is for", "How to use it", "Practice in real projects", "How <person> uses it"), in the question's order. Every finding belongs to exactly one aspect; if it bears on another, that aspect says `see <n.m>` instead of repeating it. Number aspects 1, 2, 3…; findings inside aspect n are n.1, n.2…; the summary line counts both (`<A>` aspects, `<F>` findings), translated.

**Aspect layout.** Each aspect, in this order:

1. A blank line, then `---` on its own line, then a blank line (the blank line before `---` is required: without it the line above becomes a heading).
2. `### <n>. <Aspect name>`.
3. `> **<Answer, translated>:** <this aspect's answer, 1–2 sentences> (<confidence>)`. It answers this part of the question only; it does not repeat the alert.
4. Optional orientation, at most 2 sentences: the scope of the aspect or what the reader needs to know first (a term, a timeline).
5. Findings, the ones this aspect's answer depends on first. Each: `#### <n.m> <claim>`, the claim one plain sentence; then a body of 2–5 sentences: the evidence (what, measured on what, how big, quoted where wording matters), why it holds, what it means for this aspect; then the trailer line `(<confidence: high, medium or low>; <single-source, unverified or sources disagree, when so>)`. Translate labels; keep the listed values English.
6. Optional, at most one per aspect: a table, figure or example that the prose would otherwise describe badly, placed after the finding it belongs to and referenced by it.

**Shape by aspect kind.** Pick the supporting element that fits; prose alone is fine when nothing fits.

| Aspect kind | Best supporting element |
|---|---|
| What it is, why it exists | Quote of the defining source; timeline |
| How to use it | Minimal example (code ≤ 15 lines); pipeline figure |
| Practice, pitfalls | Do / avoid table with the reason per row |
| How a person or project does it | Steps figure; table mapping their parts to ours |
| Option vs option | Small table on this aspect's criteria only |

**Rules.**

- Sources disagree → name both, which you favor and why; confidence at most medium.
- Every sentence carries a fact, a number, a name or a consequence. No warm-up ("It is worth noting"), no restating the claim heading, no summary sentence at the end of an aspect.
- A finding that does not bear on the research question goes; a weak one stays only if it is flagged low.
- More than 7 findings in an aspect → split the aspect or cut the weakest.

**Limits** (display columns, counted as in Template rules; this whole block at most 15000):

| Item | Limit |
|---|---|
| Aspect answer | 160 |
| Orientation | 160 |
| Claim heading | 80 |
| Finding body | 5 sentences, 480 |
| One aspect, everything in it | 3500 |
| Code example | 15 lines (exempt from columns) |
| Table cells | 30; last column 80 |

</details>

## What it means for us

Optional; omit when the input asks no decision. 1–3 list items tying the findings to that decision: what they favor, rule out or still require. Point to findings by number (`see 2.3`) rather than restating them.

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
