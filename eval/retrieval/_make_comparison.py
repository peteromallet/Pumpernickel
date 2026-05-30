"""Build the FAIR three-way comparison report from the per-adapter JSON reports.

Run AFTER:
    python -m eval.retrieval.runner --adapter baseline
    python -m eval.retrieval.runner --adapter semantic
    python -m eval.retrieval.runner --adapter hybrid

Then:
    python -m eval.retrieval._make_comparison

Writes eval/retrieval/reports/comparison_report.md.
"""

from __future__ import annotations

import json
from pathlib import Path

REPORTS = Path(__file__).resolve().parent / "reports"
QTS = ["verbatim_quote", "paraphrase", "cross_thread", "topic_recall"]


def load(name):
    return json.load(open(REPORTS / f"{name}_report.json"))


def cell(x):
    return f"{x:.3f}"


def main():
    b, s, h = load("baseline"), load("semantic"), load("hybrid")
    n = b["overall"]["n"]
    lines = []
    A = lines.append

    A("# FAIR Retrieval Comparison — Baseline vs Semantic vs Hybrid")
    A("")
    A(f"- Corpus: `{Path(b['corpus_path']).name}` ({_corpus_size()} messages, "
      f"{_topic_count()} topics, {_thread_count()} threads)")
    A(f"- Golden set: `{Path(b['golden_set_path']).name}` ({n} cases)")
    A("- Embedding backend: local sentence-transformers `all-MiniLM-L6-v2` "
      "(384-dim, offline, cached). Hybrid = Reciprocal Rank Fusion (k=60) of "
      "baseline + semantic.")
    A("")
    A("> **These numbers supersede the first experiment's (recall@10 0.26 -> "
      "0.87).** The first run was rigged: every paraphrase and cross-thread "
      "query was constructed to share ZERO substrings with its target, pinning "
      "the keyword baseline at exactly 0% on those types and inflating the "
      "semantic lift to ~3x+. This fair rebuild gives paraphrase/cross-thread "
      "queries realistic lexical overlap (short, keyword-style search phrases "
      "that the production `%text_contains%` ILIKE can actually match), adds "
      "hard distractors (near-duplicate incidents, same-word-different-meaning "
      "traps), and scales the corpus and golden set up. As a result the baseline "
      "is no longer artificially 0% on paraphrase/cross-thread.")
    A("")

    # Overall
    A("## Overall")
    A("")
    A("| Metric    | Baseline (ILIKE) | Semantic | Hybrid (RRF) |")
    A("|-----------|-----------------:|---------:|-------------:|")
    for key, label in [("recall@1", "recall@1"), ("recall@5", "recall@5"),
                       ("recall@10", "recall@10"), ("mrr", "MRR")]:
        A(f"| {label} | {cell(b['overall'][key])} | "
          f"{cell(s['overall'][key])} | {cell(h['overall'][key])} |")
    A("")

    # Per-type recall@10
    A("## Per query-type — recall@10")
    A("")
    A("| Query type | n | Baseline | Semantic | Hybrid |")
    A("|------------|--:|---------:|---------:|-------:|")
    for qt in QTS:
        bn = b["by_query_type"][qt]
        A(f"| {qt} | {bn['n']} | {cell(bn['recall@10'])} | "
          f"{cell(s['by_query_type'][qt]['recall@10'])} | "
          f"{cell(h['by_query_type'][qt]['recall@10'])} |")
    A("")

    # Per-type MRR
    A("## Per query-type — MRR")
    A("")
    A("| Query type | n | Baseline | Semantic | Hybrid |")
    A("|------------|--:|---------:|---------:|-------:|")
    for qt in QTS:
        bn = b["by_query_type"][qt]
        A(f"| {qt} | {bn['n']} | {cell(bn['mrr'])} | "
          f"{cell(s['by_query_type'][qt]['mrr'])} | "
          f"{cell(h['by_query_type'][qt]['mrr'])} |")
    A("")

    # Verdict — computed deltas
    r10_b, r10_s = b["overall"]["recall@10"], s["overall"]["recall@10"]
    mrr_b, mrr_s = b["overall"]["mrr"], s["overall"]["mrr"]
    pp = b["by_query_type"]["paraphrase"]
    sp = s["by_query_type"]["paraphrase"]
    bc = b["by_query_type"]["cross_thread"]
    sc = s["by_query_type"]["cross_thread"]
    A("## Verdict")
    A("")
    A(f"With a **fair** keyword baseline, semantic search still wins clearly but "
      f"by a realistic margin, not the inflated ~3x of the first run. Overall "
      f"recall@10 goes {cell(r10_b)} -> {cell(r10_s)} "
      f"(~{r10_s / r10_b:.1f}x) and MRR {cell(mrr_b)} -> {cell(mrr_s)} "
      f"(~{mrr_s / mrr_b:.1f}x). The baseline is no longer 0% on the hard types: "
      f"paraphrase recall@10 is now {cell(pp['recall@10'])} for keyword "
      f"(vs {cell(sp['recall@10'])} semantic) and cross_thread is "
      f"{cell(bc['recall@10'])} for keyword (vs {cell(sc['recall@10'])} "
      f"semantic) — keyword genuinely finds *some* of these because the queries "
      f"share real words with their targets. Semantic's edge is concentrated "
      f"exactly where it should be: restated intent (paraphrase) and answers "
      f"that span both threads of a topic (cross_thread), plus precision against "
      f"same-word-different-meaning distractors. On verbatim quotes the keyword "
      f"baseline is competitive (recall@10 "
      f"{cell(b['by_query_type']['verbatim_quote']['recall@10'])} vs "
      f"{cell(s['by_query_type']['verbatim_quote']['recall@10'])}). Hybrid (RRF) "
      f"tracks pure semantic on recall and is the safer production default "
      f"(it never loses the keyword hits) but adds little measurable recall on "
      f"this corpus. **Conclusion: semantic search is still worth building — the "
      f"~1.9x recall@10 / ~1.4x MRR lift over a fair baseline is meaningful and "
      f"shows up precisely on the query shapes keyword search structurally "
      f"cannot serve — but the honest expected gain is roughly half of what the "
      f"first rigged experiment advertised.** Caveat unchanged: synthetic corpus + "
      f"simplified scope model; confirm against real queries with the full "
      f"production scope before shipping.")
    A("")

    out = REPORTS / "comparison_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    # also echo the tables for convenience
    print("\n".join(lines[: lines.index("## Verdict")]))


def _corpus_size():
    from eval.retrieval.loader import load_corpus
    return len(load_corpus(Path(__file__).resolve().parent / "corpus.yaml").messages)


def _topic_count():
    from eval.retrieval.loader import load_corpus
    c = load_corpus(Path(__file__).resolve().parent / "corpus.yaml")
    return len({m.topic_id for m in c.messages})


def _thread_count():
    from eval.retrieval.loader import load_corpus
    c = load_corpus(Path(__file__).resolve().parent / "corpus.yaml")
    return len({m.thread_id for m in c.messages})


if __name__ == "__main__":
    main()
