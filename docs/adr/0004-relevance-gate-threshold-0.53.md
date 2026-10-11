# ADR-0004 — Relevance gate threshold of 0.53, calibrated per embedding model

- **Status:** Accepted
- **Date:** 2026-07-17 (recorded 2026-10-11, after the fact)
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0005
  (LLM-as-judge), ADR-0007 (brand as a soft preference), ADR-0010 (benchmark
  resolution — the same threshold pattern, applied elsewhere)

## Context

Vector search always returns *something*. Ask the catalog for a washing
machine and pgvector still hands back the 50 laptops nearest to that sentence.
Without a check, the agent would present the nearest laptop as an answer to a
question the catalog cannot answer.

Module 4, `relevance_gate()` in `app/rag/gating.py`, is that check. It reads
the **top candidate's `final_score`** — the score after reranking, so
similarity × penalty + bonus — and if it is below `RELEVANCE_THRESHOLD` it
returns no laptops, a `bottleneck` (`budget` / `weight_limit` / `general`,
counted from the top ten candidates' penalty reasons) and a clarifying
question. Gating is not the end of the conversation; it is a redirect.

The problem is the number. A cosine similarity is not a probability: its
scale depends entirely on the embedding model. The threshold has had three
values, one per model:

| Embedding model | Gate | `_MIN_VIABLE_SCORE` |
|---|---|---|
| `gemini-embedding-001` | 0.40 | 0.25 |
| `nemotron-embed` (one-day detour, July 2026) | 0.20 | 0.13 |
| `gemini-embedding-2` (from 2026-07-17) | **0.53** | **0.33** |

On 2026-07-17 the catalog was re-embedded with `gemini-embedding-2` and the
gate had to be set again from scratch.

## Options considered

1. **Spot-check calibration:** run known-relevant and known-irrelevant queries
   against the live catalog and set the threshold between the two groups.
2. **Labelled calibration set:** label query/laptop relevance, sweep the
   threshold, and pick it from a precision/recall curve.
3. **Relative threshold:** gate on the shape of the score list (gap between
   the top result and the rest) instead of an absolute value.
4. **No gate:** pass every result to the agent and let it judge relevance.

## Decision

**Option 1, with the threshold at 0.53.**

Measured on the re-embedded catalog:

| Query group | Top score range |
|---|---|
| Relevant laptop queries | 0.537 – 0.732 |
| Clearly irrelevant (washing machine, fridge, guitar, scooter, office chair) | 0.418 – 0.546 |

The ranges **overlap**. 0.53 sits at the boundary: every measured relevant
query passes, every irrelevant one is gated except borderline household items
at about 0.54. That leak was accepted because the agent's `SCOPE ENFORCEMENT`
prompt block refuses non-laptop requests before `search_laptops` is ever
called. The gate is the second line, not the only one.

The low end of the relevant range is **short, vague Chinese queries**. Catalog
documents are written in English, so cross-lingual similarity runs lower.
Moving the gate up to cut the household leak would start gating real Chinese
users first.

## Consequences

**Three constants are tied to this one**

All are properties of the same score distribution, and all must be
re-derived together whenever the embedding model changes:

- `_MIN_VIABLE_SCORE = 0.33` in `relaxation.py` — the floor below which a
  candidate counts as non-viable and relaxation runs — kept at 62.5% of the
  gate across all three models.
- `_FALLBACK_SIMILARITY = 0.58` in `retrieval.py` — the placeholder score given
  to every row from the relational fallback when the embedding API fails. It
  must sit above the gate or the fallback rescues nothing. Until 2026-09-14 it
  was 0.5, **below** 0.53, so every fallback search was gated and the rescue
  path had never worked. It was re-derived from the 241 rows then in
  `pipeline_eval_logs` (p10 0.5867, p50 0.6778, p90 0.7754): above the gate,
  just under p10, so a fallback row reaches the user but sorts below about nine
  in ten genuine matches.
- The gate itself.

A comment at each constant says so. That is the only enforcement.

**The gate reads the reranked score, not raw similarity**

So it gates "relevant, but nothing fits your constraints" as well as
"irrelevant". That is intended — it is how a hopeless budget turns into a
`budget` bottleneck question. But it means anything that moves `final_score`
moves what the gate lets through. The brand penalty is the largest example:
a −0.25 on a relevant top score of 0.537–0.732 lands at 0.29–0.48, below the
gate every time (ADR-0007).

**What the evidence does and does not show**

- The calibration is a spot-check, not a measured error rate. There is no
  labelled set, so the false-gate and false-pass rates on real traffic are
  unknown. `gating.py` carries a `TODO: NDCG calibration not implemented`;
  `rag/evaluation.py` has an NDCG@10 harness whose ground-truth relevance
  labels are still placeholders (all 0), so it cannot calibrate anything yet.
- The eval harness's `relevance_gating` category tests the scope-enforcement
  prompt (via `forbid_tools`), not this threshold. The threshold's *value* is
  pinned by unit tests — mutating it to 0.40 turns them red — but a pinned
  value is not a verified one.

**When this stops holding**

- **Any embedding model change.** The number means nothing on a new vector
  space. Re-embed, re-run the spot-check, and re-derive all three constants in
  the same change.
- A large change in catalog composition or document text, which moves the
  score distribution without changing the model.
- Real traffic showing Chinese queries gated that should not be. The 0.537
  floor is only 0.007 above the gate.

## Not chosen, and why

The measurements and the reason for accepting the household leak come from
the comment written at the time in `gating.py`. The reasoning against options
3 and 4 was not written down then; it is reconstructed here.

**A labelled calibration set (option 2).** The right method, and the one to
move to. Not done at the time because it needs hand-labelled relevance for
query/laptop pairs, and the catalog was changing weekly. It is still open.

**A relative threshold (option 3).** For an off-topic query every laptop is
equally irrelevant, so the scores come back flat — which is also what a broad,
legitimate query like "a laptop for university" looks like. The shape of the
list cannot tell those two apart; an absolute floor can.

**No gate (option 4).** The model would receive six irrelevant laptops and be
asked to notice they are irrelevant. The evaluation work in ADR-0008 showed
how readily it presents what it is given with confidence. Refusing in code is
cheaper and more reliable than asking a model to refuse.
