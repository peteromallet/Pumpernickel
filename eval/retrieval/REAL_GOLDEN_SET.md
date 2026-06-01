# Real-Data Golden Set — Labeling Guide

## Why this exists (the launch gate)

We are switching the retriever to an **OpenAI hosted hybrid** implementation.
Before launch we must prove it works on **real production messages**, not just
the synthetic corpus.

Synthetic eval **#1 already passed**: hosted/hybrid held **recall@10 ≈ 0.86**
overall. But it **trailed the local semantic retriever on `topic_recall`
(0.70 vs 0.78)** — that is the watch-item. This real-data golden set is the gate
that confirms the hosted retriever holds up on actual data and doesn't regress
topic recall before we ship.

⚠️ **Privacy:** the extracted corpus and your filled-in golden set contain real,
intimate user messages. Both files are **gitignored** (`real_corpus.yaml`,
`real_golden_set.yaml`). Keep them local and **delete them after labeling** if
you no longer need them.

---

## The 3-step workflow

### Step 1 — Extract a real corpus (writes real data to disk; gitignored)

```bash
python -m eval.retrieval.extract_real_corpus \
    --limit 300 \
    --since 2026-01-01 \
    --out eval/retrieval/real_corpus.yaml
```

- Bounded by default (`--limit 300`); never unbounded. Narrow further with
  `--since YYYY-MM-DD`, `--topic <uuid>`, or `--thread-root <uuid>`.
- Excludes deleted and search-suppressed messages.
- `thread_id` is **synthesized** by walking `in_reply_to` to the reply-chain
  root (a root message is its own thread). `topic_id` falls back to the literal
  `no_topic` when null. Participants resolve to `users.name` (or a stable
  `direction:uuid8` label if a name is missing).
- Prints only a summary (counts + date range) — never message content.

### Step 2 — Browse the corpus to find expected_message_ids

```bash
# Find messages containing a phrase:
python -m eval.retrieval.browse_corpus --corpus eval/retrieval/real_corpus.yaml \
    --grep "daycare"

# List a whole thread (use the thread_id printed above):
python -m eval.retrieval.browse_corpus --corpus eval/retrieval/real_corpus.yaml \
    --thread <thread_id>

# Look up one message by id:
python -m eval.retrieval.browse_corpus --corpus eval/retrieval/real_corpus.yaml \
    --id <message_id>
```

Each line prints `[id] (thread=…, topic=…, sender→recipient, sent_at) content`.
Copy the `id`s of the messages that genuinely answer your query.

### Step 3 — Copy the template and fill in real ids

```bash
cp eval/retrieval/real_golden_set.template.yaml \
   eval/retrieval/real_golden_set.yaml
# then edit real_golden_set.yaml: replace every REPLACE_WITH_REAL_* placeholder
# with real ids/thread_ids/topic_ids found in Step 2.
```

The loader validates that every `expected_message_id` exists in the corpus, that
`expected_message_ids` is non-empty, and that `scope`/`thread_id`/`topic_id` are
consistent (thread scope needs `thread_id`; topic scope needs `topic_id`).

---

## query_type taxonomy (aim for 20–40 cases, balanced across all 4)

Build **20–40 cases total, roughly balanced** across the four types. Deliberately
include **several `topic_recall` cases** — that is the watch-item where the hosted
retriever trailed local.

| query_type      | what it probes | scope it usually uses | example query |
|-----------------|----------------|------------------------|---------------|
| `topic_recall`  | "what did we say about X" over a topic; paraphrased; semantic-favored | `topic` (needs `topic_id`) | "what did we decide about the daycare schedule" |
| `verbatim_quote`| a near-exact phrase the user typed; keyword-favored | `all` | "running fifteen minutes late, traffic on the bridge" |
| `paraphrase`    | query uses different words than the message; semantic-favored | `thread` (needs `thread_id`) or `all` | "did they ever apologize for missing the appointment" |
| `cross_thread`  | same theme across separate threads; expected ids span ≥2 threads | `all` | "every time money came up as a source of tension" |

The committed template (`real_golden_set.template.yaml`) has one fully-worked
example of each type, including a thread-scoped and a topic-scoped case so the
scope/id rules are demonstrated.

---

## Step 4 — Run the eval (the comparison)

Run all three adapters against the SAME real corpus + golden set and compare:

```bash
# Hosted hybrid (the candidate under test):
python -m eval.retrieval.runner --adapter hybrid-openai \
    --corpus eval/retrieval/real_corpus.yaml \
    --golden  eval/retrieval/real_golden_set.yaml

# Local semantic (the incumbent to beat / not regress against):
python -m eval.retrieval.runner --adapter semantic \
    --corpus eval/retrieval/real_corpus.yaml \
    --golden  eval/retrieval/real_golden_set.yaml

# Lexical baseline (sanity floor):
python -m eval.retrieval.runner --adapter baseline \
    --corpus eval/retrieval/real_corpus.yaml \
    --golden  eval/retrieval/real_golden_set.yaml
```

---

## The GATE (launch criteria)

Before launch, the hosted/hybrid retriever (`hybrid-openai`) must:

1. **Hold recall@10 ≥ ~0.80 overall** on the real golden set, AND
2. **Not regress `topic_recall`** versus the local `semantic` adapter on the
   same real set (the #1 watch-item: hosted 0.70 vs local 0.78).

If either fails, do not launch — investigate the topic_recall gap first.

---

## Privacy note (read again)

- `real_corpus.yaml` and `real_golden_set.yaml` are **gitignored** — never commit
  them. Only `real_golden_set.template.yaml` (no real data) is committed.
- These files contain real intimate user data in plaintext. Keep them local and
  **delete them when you're done labeling** if you don't need to re-run the gate.
