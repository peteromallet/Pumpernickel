# Retrieval Evaluation Report

- **Adapter:** SemanticRetriever
- **Corpus:** /Users/peteromalley/Documents/Veas/.claude/worktrees/agent-a4a00ba1c970b3f45/eval/retrieval/corpus.yaml
- **Golden Set:** /Users/peteromalley/Documents/Veas/.claude/worktrees/agent-a4a00ba1c970b3f45/eval/retrieval/golden_set.yaml
- **Generated:** 2026-05-30T01:26:09.419358+00:00
- **Cases:** 28

## Overall Metrics

| Metric    | Value |
|-----------|-------|
| mrr | 0.7241 |
| recall@1 | 0.3077 |
| recall@10 | 0.8732 |
| recall@5 | 0.6527 |
| n         | 28 |

## Per Query-Type Metrics

### cross_thread

| Metric    | Value |
|-----------|-------|
| mrr | 0.8750 |
| recall@1 | 0.1007 |
| recall@10 | 0.6667 |
| recall@5 | 0.4271 |
| n         | 4 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC25 | What are the weekend plans for food? | topic | 8 | 10 | 0.0000 | 0.3750 | 0.5000 | 0.5000 |
| GC26 | What deployment issues have come up? | topic | 9 | 10 | 0.1111 | 0.3333 | 0.6667 | 1.0000 |
| GC27 | performance and scaling discussions | topic | 6 | 10 | 0.1667 | 0.5000 | 1.0000 | 1.0000 |
| GC28 | Saturday morning plans | topic | 8 | 10 | 0.1250 | 0.5000 | 0.5000 | 1.0000 |

### paraphrase

| Metric    | Value |
|-----------|-------|
| mrr | 0.3610 |
| recall@1 | 0.2000 |
| recall@10 | 0.9000 |
| recall@5 | 0.6000 |
| n         | 10 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC15 | login system status | all | 1 | 10 | 0.0000 | 1.0000 | 1.0000 | 0.2000 |
| GC16 | migration scripts delayed | all | 1 | 10 | 0.0000 | 0.0000 | 1.0000 | 0.1000 |
| GC17 | UV protection reminder | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC18 | in-memory cache architecture | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC19 | meal beverage planning | all | 1 | 10 | 0.0000 | 1.0000 | 1.0000 | 0.2500 |
| GC20 | schedule coordination | all | 1 | 10 | 0.0000 | 0.0000 | 1.0000 | 0.1667 |
| GC21 | NPE resolution | all | 1 | 10 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC22 | test environment throttle | all | 1 | 10 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC23 | sunburn story | all | 1 | 10 | 0.0000 | 1.0000 | 1.0000 | 0.2500 |
| GC24 | weekend logistics | all | 1 | 10 | 0.0000 | 0.0000 | 1.0000 | 0.1429 |

### topic_recall

| Metric    | Value |
|-----------|-------|
| mrr | 0.8611 |
| recall@1 | 0.1465 |
| recall@10 | 0.7970 |
| recall@5 | 0.4557 |
| n         | 6 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC01 | What's the status of the Nexus project authentication module? | topic | 3 | 10 | 0.3333 | 1.0000 | 1.0000 | 1.0000 |
| GC02 | What bugs have been reported in the Nexus project? | topic | 7 | 10 | 0.0000 | 0.0000 | 0.2857 | 0.1667 |
| GC03 | What's the plan for the Saturday hike? | thread | 9 | 10 | 0.1111 | 0.5556 | 0.8889 | 1.0000 |
| GC04 | Where are we eating dinner Saturday night? | thread | 6 | 10 | 0.1667 | 0.5000 | 1.0000 | 1.0000 |
| GC05 | What are our weekend plans? | topic | 8 | 10 | 0.1250 | 0.2500 | 0.7500 | 1.0000 |
| GC06 | Has anyone mentioned performance problems or scaling concerns? | all | 7 | 10 | 0.1429 | 0.4286 | 0.8571 | 1.0000 |

### verbatim_quote

| Metric    | Value |
|-----------|-------|
| mrr | 1.0000 |
| recall@1 | 0.6667 |
| recall@10 | 1.0000 |
| recall@5 | 0.9792 |
| n         | 8 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC07 | I told you so | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC08 | fine. | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC09 | sure | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC10 | osso buco | all | 3 | 10 | 0.3333 | 1.0000 | 1.0000 | 1.0000 |
| GC11 | Blue Ridge | all | 6 | 10 | 0.1667 | 0.8333 | 1.0000 | 1.0000 |
| GC12 | CI pipeline is green | all | 1 | 10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC13 | idempotency key | all | 2 | 10 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC14 | rate limiting | thread | 3 | 10 | 0.3333 | 1.0000 | 1.0000 | 1.0000 |
