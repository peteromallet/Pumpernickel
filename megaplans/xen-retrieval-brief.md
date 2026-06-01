# Xen — Human-Like Retrieval & Context for Veas Agents (v2)

> Design brief, **revised after adversarial review** (human-user + three agent-task
> critics). Premise unchanged: today a Veas bot gets a one-page **briefing note**
> (hot context) + one keyword shout at the message store (`search_messages`); it
> never *browses*. But review showed v1 solved only **one** of three recall modes
> and had real safety holes. **Xen v2** is a shared retrieval substrate with three
> surfaces, a hard safety layer, and a user-facing control half.

## The reframe (what review changed)

v1 = "give the agent a chat app to browse." That serves **point-lookup** well and
**ignores** two other ways people recall:
- **Sweep** (a debrief reading a *whole* conversation) — needs enumeration with a
  completeness guarantee, not seeking. v1 had no full-span fetch.
- **Pattern** ("she's mentioned money 3× this week", "his tone got sharper") —
  needs *shapes over time*, not cursors. v1 had nothing.

So Xen v2 = **one substrate, three surfaces, behind a safety layer:**

```
                 ┌───────── SAFETY LAYER (gating, on every read) ─────────┐
 scope+visibility│ raw_message_visibility · OOB · deleted_at · partner_   │
 + embedding idx │ share · user "forget this" · no scope=all by default   │
                 └───────────────────────────────────────────────────────┘
   Surface 1: NAVIGATION   Surface 2: BULK/ENUMERATION   Surface 3: AGGREGATION
   (point lookup, scroll)  (sweep/debrief, completeness)  (trends, frequency)
```

## Problem (unchanged, grounded in code)

- Hot context is a pre-rendered snapshot (`hot_context.py:535+`), evicted under a
  token budget — synthesis (observations/distillations) dropped *before* raw
  messages, no "pin," agent blind to the loss.
- Recent messages = last 20, oldest evicted first — **no scrollback**.
- Search is substring `ILIKE` (`read_tools.py:412`): no semantic, no ranking,
  detached rows, no context, no pagination, topic-scoped.
- Strong *gist* recall (memories/observations/distillations), weak *verbatim*.

The fix is **agency over retrieval**, not a bigger dump — but "browse" alone is
only a third of it.

## Safety layer (gating — non-negotiable, applies to ALL surfaces)

Review (human-user + mediator critics) made this the gate, not a footnote:
- **No `scope=all` cross-topic/dyad search by default.** Semantic match surfaces
  things keyword never would; cross-scope reach is a *separate, explicitly
  opt-in* capability with the same semantics as `partner_share`, audited per hit.
- **The embedding index is in-scope of every visibility rule** — `raw_message_
  visibility()`, `out_of_bounds`, `partner_share`, and `deleted_at`. Prove it
  before launch (don't leave as "open question").
- **User-driven forgetting**, distinct from the bot's `out_of_bounds`: a
  user-facing "don't retrieve this / let this go" that redacts a message/span from
  scrollback **and the vector index**. Honor `deleted_at` in every new verb (old
  code does; new tools must not regress).
- **Gist-first, verbatim-rare-and-visible.** Default retrieval returns gist;
  opening the actual old transcript is a privileged action (recency / user
  referenced it first) and should be *narratable to the user* ("looking back at
  February…"). Recall may stay vague on purpose.
- **Present before precise.** Answer from carried context first; navigate only
  when the user clearly asks to remember something. Never make a user wait on
  retrieval for an emotional reply (do it after a first acknowledging response).

## The inbound moment — relationship card (revised)

Auto-surface a compact card, but **frame the current message first, card second**:
- Who they are to the user, name, local time; **last talked** + a *fast-decaying*
  tone hint (not an always-on "you were angry last time" anchor — review flagged
  this re-anchors users to their lowest moment).
- **Open loops** that link to `supporting_message_ids` so gist→verbatim is one hop
  and gist is never mistaken for a quote.
- A live handle to "scroll our history" — not the history itself.

## Surface 1 — Navigation (point lookup & scrollback)

Over `messages` (`content`, `media_analysis`, `direction`, `sender_id/recipient_id`,
`sent_at`, `charge`, `edited_at`, `edit_history`, `deleted_at`):

```
relationship_card(contact_user_id)         → identity + last-talked + open loops
open_thread(around = id|date|"latest", n)  → { messages[], cursor }   (land in context)
scroll(cursor, up|down, n)                 → { messages[], cursor }   (the "jumping")
search(query, mode = exact|semantic, scope = thread|topic, limit)
    → ranked hits, each { message, snippet, cursor, match_type, why_matched }
open_hit / next_hit / jump_to(date)        → reuse open_thread
```

**Verbatim integrity (mediator critic — "retrieval is testimony"):**
- **`mode=exact` vs `mode=semantic` are distinct.** Semantic finds the *topic*;
  exact finds the *words*. **Verbatim quotes may only come from exact matches** —
  never present a semantic hit as a quote. Add a `verify_quote(message_id, text)`.
- **Every row carries a resolved `{speaker_label, speaker_user_id, direction}`** —
  not a raw UUID to re-map (attribution is the highest-stakes field in a dyad).
- **Surface edits & retractions:** results expose `edited_at` + `edit_history`
  original text, and **retracted (`deleted_at`) messages are flagged, not dropped**
  ("she took that back" is often the key fact). Schema already stores these.
- **Stable cursors over message identity** + a "content changed since you opened
  it" signal, so a mid-turn edit can't silently alter a quote.
- Per-hit `match_type` (exact/semantic) + `why_matched` + enough metadata
  (date/thread/participants) to disambiguate near-duplicate incidents.

## Surface 2 — Bulk / enumeration (sweep & synthesis)

Sweep is **enumeration with a completeness contract**, not navigation. The
codebase's debrief already does the right thing (`debrief.py:61` — one unbounded
ordered `fetch`); Xen must not regress to paging that through `scroll`.

```
span_manifest(scope, start, end)
    → { count, first_ts, last_ts, id_checksum }        (verify coverage)
fetch_span(scope, start, end, order=asc, as_of=snapshot)
    → streamed/paged FULL enumeration of a CLOSED set (conversation_id, or
      dyad+date-range), with { returned, total, redacted_count, gap_detected,
      next_cursor }; redacted rows COUNTED as placeholders, never silently dropped
      (mirrors debrief's inline [REDACTED]).
summarize_span(scope, start, end, map_prompt, reduce_prompt)
    → server-side map-reduce so a huge span never lands raw in context
      (the deterministic synthesize_review in synthesis.py is a no-LLM ancestor).
```

- **Snapshot pinning** (`as_of`): pin the span to a timestamp/snapshot at sweep
  start so concurrent inserts can't create silent gaps/dupes mid-drain — this is
  the *correct* answer to the cursor-stability open question for the bulk case.
- **Completeness is checkable:** `Σ returned + redacted == total`, verified against
  `span_manifest`. Turns "I scrolled and hopefully got it all" into a proof.

## Surface 3 — Aggregation (trend / frequency / change)

Humans recall by *pattern*, not just by exchange. These return **shapes, not
cursors**; they are mostly SQL aggregation (`date_trunc`+`GROUP BY`+`FILTER`, as
`conversation_load` (`hot_context.py:574`) already does) + the deterministic
summarizer pattern (`adherence.py`). Share Surface-1's embedding index.

```
count_mentions(query, by = day|week|month, window, scope, semantic=true)
    → [{ bucket_start, count }]            "money came up 3× this week vs 1× last"
charge_trend(by, window, scope)
    → per-bucket distribution over routine/notable/charged/crisis   (charge field
      already on every message; never aggregated today — highest value/cost)
cadence_stats(by, window)
    → volume in/out + response-latency distribution per bucket   ("replies got
      shorter and slower")
what_changed(since, baseline_window)
    → topic/charge/cadence deltas, new vs dormant themes/watch-items
```

Generalize the `hector` adherence aggregator beyond pre-declared commitments so
"did they follow through" works for organic intentions too. **This layer belongs
neither in the nav verbs (wrong return type) nor the gist memory layer (it's
conclusions, not the time-series) — it's a thin analytics tier over raw
`messages`.**

## What shrinks out of hot context

Hot context becomes **relationship-card + the open thread (small window) + gist
memories**; older messages, full search, spans, and trends move to on-demand. Per-
turn token cost drops, lossy eviction goes away. **Caveat (sweep critic):** the
shrink is right for *reply* turns and wrong for *sweep* turns — bulk work uses
Surface 2 / `summarize_span`, not the shrunk dump.

## User-facing control half (ships WITH the agent half)

Review's strongest cross-cutting demand: retrieval power and user control ship
together or not at all. The user can:
- view (and correct) their own relationship card; see/clear open loops;
- browse and **redact** their own scrollback ("forget this") — reaching the vector
  index;
- toggle whether semantic / cross-scope recall is on at all.

## Feasibility / build sketch

- **Schema:** `message_embedding vector` + ANN index on `messages`; backfill once;
  embed-on-write. `redacted`/forget flag honored by index + all verbs. No change
  to message semantics.
- **Tools:** three verb-sets in `read_tools.py`/`registry.py`; cursor = opaque
  `{anchor_sent_at, anchor_id, scope, as_of}`. Reuse scope + `raw_message_
  visibility()`; resolve speaker labels server-side.
- **Hot context:** trim `recent_messages`, always-present card, drop synthesis
  eviction.
- **Cost:** embeddings backfill one-time; per-reply-turn token cost drops.

## Open questions (post-review)

1. Embedding granularity: per-message vs per-window (cost vs recall).
2. Can we *prove* the embedding index honors OOB/partner_share/deleted before
   launch? (Gate, not a nice-to-have.)
3. How much hot context can we remove before first-reply quality drops (the
   "free context → now a tool call" latency/turn tradeoff)?
4. Will the agent reliably *choose* the right surface, or default to gist and skip
   retrieval? (Prompting / affordance design — and a quality risk for sweep, where
   under-traversal is *silent* incompleteness.)
5. Forgetting vs. completeness: a user-redacted message must vanish from scrollback
   AND be counted-but-withheld in a debrief span — reconcile the two contracts.

## v3 verdict — round-2 critique (descope + prerequisites + honest forget)

A second adversarial round (forget-adversary, architecture, feasibility) converged:

**Descope to a minimal correct core (build this, defer the rest):**
- **v1 = Surface 1 only:** `open_thread`/`scroll` (scrollback), `search(mode=exact|
  semantic)` with ranking + snippets + resolved speaker labels + edit/retraction
  surfacing; per-message embedding index **provably inside `raw_message_visibility`
  /`deleted_at`/`partner_share`**; a single **suppress-tier** forget.
- **Defer:** Surface 3 (aggregation — speculative, "analytics nobody queried"),
  `summarize_span`, erase-tier + derived-data cascade, and co-shipping the control
  UI (enforcement ships now; the UI can lag one release). "Three surfaces / one
  substrate" is half-true — Surface 2 (bulk) is deterministic enumeration with a
  snapshot-consistency model incompatible with the others; it's `debrief.py` in a
  coat, not the same machine.

**Two prerequisites BEFORE any sprint (the doc previously had neither):**
1. **Eval harness / golden set.** Semantic search is unfalsifiable without it, and
   the corpus is hostile to embeddings (terse, context-dependent dyadic messages —
   meaning lives in the thread, not the message). The per-message-vs-per-window
   granularity choice is a *recall-quality* decision (not just the privacy one in
   v2.1) — **do not lock it until the eval set can test it.** Need: ~labeled
   query→expected-message set + a recall@k go/no-go threshold.
2. **Pooler/pgvector infra reality.** The app runs on Supabase's **transaction
   pooler (6543)**, `statement_cache_size=0`, transaction-per-call (`app/db.py`) —
   already bitten by dropped session state (`migrations/validation/s2a_preflight.md`).
   Runtime ANN tuning (`SET LOCAL hnsw.ef_search` only), the held-snapshot
   completeness contract (can't span tool calls on 6543 — needs a session-mode
   held connection), and `CREATE INDEX CONCURRENTLY` (must run on 5432 session
   mode, out of band) all require state the pooler can't hold. Also reverses the
   README "must not create pgvector" invariant — a conscious sign-off. Backfill +
   embed-on-write must be async, not inline on the write path.

**Honest-forget reframe (the v2.1 model below is partially UNSOLVABLE — narrow the
promise):**
- True forgetting is impossible once gist is derived: a distillation's *prose*
  encodes the substance, and over-determined conclusions re-derive from surviving
  sources. The cascade must **synchronously hard-delete/re-derive before the
  artifact can reload** (no lazy queue), and the UI must tell the user conclusions
  already formed may persist.
- **The honest scope:** "forget" = forget within Veas's *live + derived* layers. It
  does **not** mean the conclusion was never formed, the partner never saw it, or
  the vendor never logged it.
- **Forget is per-viewer, not per-message** — in a dyad, A's forget touches A's
  view + A's derivations only; B retains B's copy. The single-corpus / single-
  `total` model is false for dyads.
- **Tombstones must be de-positioned + coarsened** (a positional withheld-count
  leaks timing/valence; "don't speculate" is a prompt, not enforcement). Keep the
  completeness checksum **server-side only**; never put withheld positions in a
  prompt.
- **Surface invisibility must be a schema fact, not discipline:** reply + aggregation
  surfaces read a view that physically excludes suppressed rows + carries no
  withheld metadata; only the bulk surface sees content-free tombstones.
- **Legal/safety hold:** human-placed only (never bot-auto), enumerated, time-boxed,
  disclosed to the user, stored in a segregated safety store the reply agent can't
  read — else it's a backdoor that defeats forgetting exactly where the user wants
  it most.
- **Blast radius is wider than the index:** forgetting must also reach cached
  hot-context snapshots and `edit_history`; it *cannot* reach vendor logs or aged
  backups — disclose that gap.

## Resolving forgetting vs. completeness (v2.1)

The tension: a user-redacted message must vanish from scrollback/search (navigation)
yet a debrief's completeness contract (`Σ returned + withheld == total`) must not
silently drop messages. These look contradictory — "be gone" vs "be accounted for".
Resolution: **forgetting is two distinct operations, and the contract is defined
over the live corpus that already excludes the hard kind.**

**Kind 1 — Suppress ("don't bring this up").** The user wants the agent to stop
surfacing/acting on it; not a claim it never happened.
- Removed from search, scrollback, and the embedding index (zero hits, no nav).
- A **counted tombstone** remains for the bulk surface only: `withheld_count += 1`.
- **The tombstone is invisible to the reply-turn (navigation) agent** — it is
  *accounting metadata for the debrief surface, not a conversational signal.* The
  agent talking to the user never sees "something withheld here"; it simply isn't
  there. Only the debrief's internal coverage check sees the count, and is told
  "N withheld by user request — do not speculate about content."

**Kind 2 — Erase ("it never happened").** GDPR-style. The message and its embedding
are deleted, and **`total` is decremented** — an erased message is not "withheld",
it is *outside the corpus*. The `span_manifest` checksum is computed over the
post-erasure corpus, so there is no tombstone, no gap, and the contract still holds
(`Σ returned + withheld == total`, where `total` never counted the erased row).

So both kinds disappear from navigation/search/aggregation; the only difference is
the bulk surface — suppress leaves a content-free counted placeholder, erase leaves
nothing because it was never in the count.

**Three consequences this forces (design wins, not afterthoughts):**
1. **Per-message embeddings, not per-window** — else forgetting one message
   contaminates a shared window vector (open-Q on granularity is hereby decided by
   the forget requirement: per-message, or re-embed the window on redaction).
2. **Forgetting must cascade to derived data.** A suppressed/erased message may
   already be cited by a distillation/observation (`supporting_message_ids`) or
   baked into a shown aggregate. Redaction triggers a cascade: artifacts whose
   support set is now empty/compromised are flagged for re-derivation or retired.
   *Forgetting the raw message without this leaves the gist remembering it* — the
   single most important correctness point.
3. **Aggregation (Surface 3) queries the live corpus** so trends never count
   forgotten messages; already-emitted aggregates are stale-by-design (acceptable).

**The one carve-out (unresolved, needs a human/legal call):** hard-erase destroys
the audit trail. In a mediation/safety context some records (crisis disclosures,
active OOB) may warrant a **legal/safety hold** that blocks erasure. That trades the
user's right-to-forget against duty-of-care and is a policy decision, not a
technical one — flagged for the second critique round and product/legal.

## Comparison

| | Veas today | Xen v2 |
|---|---|---|
| New message | one-page briefing note | current-msg-first + relationship card |
| Reading back | last-20, no scrollback | navigable scrollback (Surface 1) |
| Quote-finding | substring rows | exact-mode + speaker labels + edit/retraction (Surface 1) |
| Whole-conversation sweep | (debrief bypasses tooling) | `fetch_span` + completeness contract (Surface 2) |
| Trends / frequency | nothing | `count_mentions`/`charge_trend`/… (Surface 3) |
| Recall | strong gist, weak verbatim | gist + verbatim + pattern |
| Safety | scope-gated snapshot | gated across embeddings; user can forget |
| User control | internal-only | user-facing card/redact/toggle |
| Posture | passive snapshot | active, multi-modal, governed retrieval |

The unifying principle: **three modes of human recall (point, sweep, pattern) over
one governed substrate — with the user able to see and forget.** Veas already
collects the material and already proves each aggregator pattern in miniature
(`conversation_load`, `adherence.py`, `debrief.py`); Xen v2 generalizes them behind
one safety layer.
