# Retrieval Evaluation Report

- **Adapter:** IlikeBaselineRetriever
- **Corpus:** /Users/peteromalley/Documents/Veas/.claude/worktrees/agent-aae9b9820205716dc/eval/retrieval/corpus.yaml
- **Golden Set:** /Users/peteromalley/Documents/Veas/.claude/worktrees/agent-aae9b9820205716dc/eval/retrieval/golden_set.yaml
- **Generated:** 2026-05-30T01:59:05.960598+00:00
- **Cases:** 62

## Overall Metrics

| Metric    | Value |
|-----------|-------|
| mrr | 0.6210 |
| recall@1 | 0.2691 |
| recall@10 | 0.4626 |
| recall@5 | 0.4535 |
| n         | 62 |

## Per Query-Type Metrics

### cross_thread

| Metric    | Value |
|-----------|-------|
| mrr | 0.6071 |
| recall@1 | 0.1060 |
| recall@10 | 0.2369 |
| recall@5 | 0.2083 |
| n         | 14 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC37 | bring food | topic | 6 | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC38 | deploy | topic | 6 | 3 | 0.1667 | 0.5000 | 0.5000 | 1.0000 |
| GC39 | overkill for our scale | topic | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |
| GC40 | login flow | all | 6 | 2 | 0.1667 | 0.1667 | 0.1667 | 1.0000 |
| GC41 | Lisbon | topic | 5 | 6 | 0.2000 | 0.4000 | 0.6000 | 1.0000 |
| GC42 | rate limiting | all | 5 | 6 | 0.0000 | 0.4000 | 0.6000 | 0.5000 |
| GC43 | broken | topic | 4 | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC44 | training plan | topic | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC45 | budget | topic | 4 | 2 | 0.0000 | 0.2500 | 0.2500 | 0.5000 |
| GC46 | duplicate charge | all | 4 | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC47 | 6 AM Saturday | topic | 5 | 1 | 0.2000 | 0.2000 | 0.2000 | 1.0000 |
| GC48 | audit findings | topic | 4 | 2 | 0.0000 | 0.2500 | 0.2500 | 0.5000 |
| GC49 | Atlas is down | topic | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |
| GC50 | other apartments | topic | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |

### paraphrase

| Metric    | Value |
|-----------|-------|
| mrr | 0.4773 |
| recall@1 | 0.3409 |
| recall@10 | 0.4091 |
| recall@5 | 0.4091 |
| n         | 22 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC15 | login integration | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC16 | migration scripts blocked | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC17 | don't forget sunscreen | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC18 | caching layer | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC19 | duplicating transaction | all | 2 | 2 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC20 | apartment search | all | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC21 | dishes in the sink | all | 3 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC22 | car repair | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC23 | query latency | all | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC24 | window seat | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC25 | rollback plan | all | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC26 | drafty window | all | 1 | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC27 | rooftop reservation | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC28 | token theft risk | all | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC29 | sunburn anecdote | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC30 | NPE fix | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC31 | demoralized after the launch | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC32 | UV protection | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC33 | food and drinks to pack | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC34 | hiding money stress | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC35 | brute force | all | 2 | 3 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC36 | partitioning the events table | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

### topic_recall

| Metric    | Value |
|-----------|-------|
| mrr | 0.6042 |
| recall@1 | 0.1278 |
| recall@10 | 0.2806 |
| recall@5 | 0.2806 |
| n         | 12 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC51 | authentication module | topic | 3 | 2 | 0.3333 | 0.6667 | 0.6667 | 1.0000 |
| GC52 | payment processor | topic | 4 | 3 | 0.0000 | 0.5000 | 0.5000 | 0.5000 |
| GC53 | Blue Ridge hike | thread | 5 | 1 | 0.2000 | 0.2000 | 0.2000 | 1.0000 |
| GC54 | dinner | thread | 4 | 2 | 0.0000 | 0.2500 | 0.2500 | 0.5000 |
| GC55 | latency | all | 4 | 5 | 0.0000 | 0.5000 | 0.5000 | 0.2500 |
| GC56 | feels equal | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC57 | Belem tower | thread | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |
| GC58 | gym membership | thread | 4 | 2 | 0.2500 | 0.5000 | 0.5000 | 1.0000 |
| GC59 | Atlas launch | topic | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |
| GC60 | beta rollout | thread | 4 | 1 | 0.2500 | 0.2500 | 0.2500 | 1.0000 |
| GC61 | call the landlord | thread | 4 | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC62 | half marathon training | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### verbatim_quote

| Metric    | Value |
|-----------|-------|
| mrr | 0.8750 |
| recall@1 | 0.4405 |
| recall@10 | 0.9286 |
| recall@5 | 0.9167 |
| n         | 14 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC01 | I told you so | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC02 | fine. | all | 1 | 4 | 0.0000 | 1.0000 | 1.0000 | 0.2500 |
| GC03 | osso buco | all | 4 | 4 | 0.2500 | 1.0000 | 1.0000 | 1.0000 |
| GC04 | Blue Ridge | all | 6 | 6 | 0.1667 | 0.8333 | 1.0000 | 1.0000 |
| GC05 | idempotency key | all | 2 | 3 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC06 | rate limiting | thread | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC07 | httpOnly cookies | all | 2 | 2 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC08 | worker pool | all | 4 | 4 | 0.2500 | 1.0000 | 1.0000 | 1.0000 |
| GC09 | osso buco | thread | 1 | 4 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC10 | kitchen faucet | all | 2 | 1 | 0.5000 | 0.5000 | 0.5000 | 1.0000 |
| GC11 | half marathon | all | 2 | 2 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC12 | Lisbon flights | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC13 | running shoes | all | 2 | 2 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC14 | sure | thread | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
