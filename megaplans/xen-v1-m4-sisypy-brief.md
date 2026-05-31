# Xen v1 — M4: Sisypy agent-behavior validation

> Added after the user flagged that the retrieval eval harness (recall@k/MRR on a
> golden set) validates the **retriever**, not the **agent**. The goal is about
> agents navigating *intuitively* — so we must test that the agent actually
> *chooses and uses* the search/nav tools correctly in realistic situations, with
> evidence, not narrative. Sisypy is the tool for that; it is **not currently
> embedded** in this repo, so M4 embeds it + authors search/nav scenarios.

## Depends on
M2 (the agent tools must exist) and M3 (hot-context integration). M4 runs last.

## Outcome
A Sisypy suite embedded in the Veas repo that validates, on evidence, that agents
serve content-relevant queries and navigate intuitively — both simple and complex —
using the M2/M3 tools, with matched negative/recovery cases.

## Scope (IN)
- **Embed Sisypy** (adapter.py, runner.py) per the `sisypy-embed` flow — first a
  fake/no-GPU structural run proving harness + evidence capture work.
- **Author scenarios** (sisypy-design → sisypy-author) covering the goal's verbs:
  - SIMPLE nav: "show me the messages right before this one" → agent calls
    `messages_before(anchor="current")`, not a semantic search; "what came before
    message X" → `messages_before(anchor=id)`; scrollback; topic-recent.
  - COMPLEX: a paraphrase/topic query → agent runs `search(mode=semantic)` and
    surfaces the right exchange (evidence: the retrieved message ids / the quote).
  - **Tool-choice discrimination**: the agent must pick the *cheap simple* verb for
    positional queries and *semantic* only when meaning-match is needed — a wrong
    choice is a graded failure.
  - **Proactive context-gathering (the "push to search" behavior)**: in a state
    where the hot-context "Previous on this topic" gist is *insufficient* to answer
    correctly, the agent must *proactively* call a nav/search tool (open_thread /
    scroll / search, paging via cursor if needed) to pull the fuller context
    **before** answering — answering from the partial gist alone is a graded
    failure. Distinct from tool-choice discrimination (which verb): this grades
    *whether the agent reaches deeper at all* when the surfaced slice isn't enough.
  - HOT-CONTEXT: given a conversational state, the right "previous on this topic"
    messages are present in the agent's context (ties to M0's hot-context-inclusion
    metric, but here observed through real agent behavior).
  - Negative/recovery: forgotten (suppressed) messages must NOT surface; mid-call
    /unsupported asks handled gracefully.
- Evidence-backed rubric items (enforced / graded / observed) per `sisypy-author`.

## Scope (OUT)
- Re-testing retriever ranking (M0's harness owns that).
- New product features; M4 is validation only.

## Done criteria
- Sisypy suite runs (structural/fake first, then real) and passes for the
  search/nav scenarios; failures are evidence-backed, not narrative-graded.
- At least one matched negative (forget/suppress) and one recovery scenario.
- A short report mapping each goal clause → a passing scenario.

## Dials
profile `partnered`, robustness `full`, depth `medium` — scenario design is the
judgment-heavy part; execution is bounded.

## Touchpoints
- New `sisypy/` (adapter.py, runner.py, scenarios/*.yaml, briefs/*.md)
- References M2 tools (`registry.py`) + M3 hot-context section.
