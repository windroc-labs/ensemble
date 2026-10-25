# Ultracode Method

A fixed method for the ultracode round. Input: the round's scope and cap, from the calling task, which also sets the subagents' kind and scope. The agent following this method (the **main agent**) decomposes, selects what to verify, and synthesizes itself; subagents do the rest, and only they count toward the cap.

## Steps

1. **Decompose**: split the scope into subquestions ranked by importance, so that dispatch leaves room in the cap for the key claims' votes.
2. **Dispatch**: in importance order, one subagent per subquestion; each returns claims with sources, each claim rated by importance (central / supporting / tangential).
3. **Adversarial verify**: rank all claims by importance and verify from the top while ≥ 3 cap slots remain, by **Voting**, with voters of the dispatched subagents' kind.
4. **Synthesize**: merge into findings, marking each claim's verification result and sources.

## Rules

- **Cap**: all subagents ≤ the cap. When it runs out, undispatched subquestions and unverified claims go under Gaps.
- **Restrictions**: put the calling task's restrictions for the subagents' kind into every subagent prompt, voters included.
- **Voting**: 3 independent subagents vote on each claim verified. Each tries to refute the claim, checking that its source says it, contradicting sources, the source's quality against the claim's strength, and currency, and votes refuted or not, with evidence; unsure → votes refuted. **Valid votes** = votes that came back. ≥ 2 refutes → refuted; else ≥ 2 valid votes → confirmed; else (agent errors, missing votes) → unverified.
- **Pipeline**: ≤ 10 subagents running at once. Each result of a stage goes to the next stage as soon as it arrives, never in batches; only verification waits for all claims, to rank them.
