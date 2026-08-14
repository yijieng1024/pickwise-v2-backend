# ADR-0006: Position PickScore as a buy indicator, not a ranker

- **Status:** Accepted
- **Date:** 2026-08-14
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0008 (price fabrication)

## Context

PickScore was redesigned for v2, moving from a flat weighted average to eight
factors under three-layer multiplicative weighting. The redesign was expected
to change recommendation ordering. It did not — v2 output closely tracked v1.

Tracing the cause surfaced five layers, each upstream of the last. Every
number below was measured against the live catalog (303 laptops, 107 families)
and the 107 distinct queries in `pipeline_eval_logs`.

### 1. PickScore does not participate in ordering

`GET /conversations/{id}/laptops` sorts the persisted shortlist by
`similarity_score`, not `pick_score`. The scoring code lives outside
`app/rag/` and runs only after the relevance gate passes. Its first weighting
layer is the reranker's ordinal position, so it is structurally unable to
depart from the reranker's ordering.

Whatever PickScore computes, the user sees the reranker's sequence.

### 2. The weights are sound; the raw scores they weight are not

The five use-case scores on the product page come from
`USE_CASE_PRIORITIES` in `app/laptops/pickscore_general.py` — absolute 1–10
weights per factor, precomputed per laptop into `LaptopPickScore`.

Dumping one machine (FX608JMR — i7-14650HX, RTX 5060 Laptop, 16GB/1TB,
2.2kg, 90Wh, RM5,899) reproduces all five UI scores exactly from those
weights and its eight raw factor scores:

| Use case | Weighted sum / total | Score | UI |
|---|---|---|---|
| Gaming | 1750.5 / 36 | 48.6 | 49 |
| Creative Work | 1939.5 / 40 | 48.5 | 48 |
| Programming | 2207.4 / 42 | 52.6 | 53 |
| General Use | 2421.9 / 43 | 56.3 | 56 |
| Office & Study | 2412.1 / 40 | 60.3 | 60 |

So the engine is doing what the table tells it, and the table is reasonable:
Gaming weights GPU highest (10), Office weights price (9) and battery (8).

The inversion — a gaming laptop scoring lowest on Gaming and highest on
Office & Study — comes from the raw scores underneath:

| Factor | Raw | Normalized against |
|---|---|---|
| battery | 90.0 | 0–100 Wh (a real ceiling; 90Wh is genuinely near-max) |
| price | 87.4 | RM1,429–36,999 (a workstation outlier makes everything look cheap) |
| gpu | 53.1 | 81–38,963 (top-end desktop parts; a mid-range laptop GPU is genuinely mid) |
| cpu | 45.8 | 393–72,912 |
| portability | 41.0 | 0–3.73 kg inverted (the ideal anchor is a weightless laptop) |
| ram_storage | 17.5 | 0–128 GB and 0–4,096 GB (a 128GB/4TB workstation compresses everything below it) |

Gaming's heavy factors average about 41 on this machine; Office's average
about 75. **A gaming laptop cannot win the preset that weights its own
hardware, because those are the factors whose normalization is most
compressed.** Re-tuning the weights cannot fix this — the weights would still
be applied to a 17.5 and a 53.1.

Two mechanisms produce the compression:

- **The `> 0` guard exists on price only.** `pickscore_adapter.py:39` filters
  `Laptop.price_rm > 0`, so `price.min` is 1,429 and not 0. The other columns
  take a plain `func.min()` (lines 41–44), so missing data sets their floor:
  51 rows at 0 for `battery_wh`, 3 for `ram_gb`, 1 for `ssd_gb`, 2 for
  `weight_kg`. A correct fix was applied once and not to its neighbours.
- **Min-max is sensitive at both ends.** Raising the floor alone is not
  enough: against a 4–128 range, 16GB still scores 9.4. The 128GB/4TB
  workstation does as much damage as the zeros, and the RM36,999 outlier does
  the same in the opposite direction on price.

### 3. Two silent resolution failures sit underneath the raw scores

The GPU raw score of 53.1 back-solves to a benchmark mark of ~20,727. That is
the **desktop** "GeForce RTX 5060" (20,722) — not "GeForce RTX 5060 Laptop
GPU" (16,785), which exists in the table under exactly the catalog's own
string. The likely cause is that RapidFuzz `token_set_ratio` scores a strict
subset as a perfect match, so shorter and less specific benchmark names beat
exact ones. The CPU resolved correctly on the same machine because only one
candidate row existed.

Correcting this makes Gaming *worse*, not better: the real laptop part
normalizes to ~43 against the current ~53.

The same scorer and the same `extractOne` pattern are used in
`matcher.py` for review-video titles.

### 4. Unknown and average are the same number

Unresolved factors fall back to a raw score of 50, which is indistinguishable
in the output from a genuinely mid-range score. On a second variant (FX607VU,
`price_rm = 0`), price scored 50 — and was the single largest contribution to
its total of 29. `screen_size` and `brand` also sat at exactly 50.

This is the same failure as `price_rm = 0` meaning both "free" and "unknown",
one layer up: a sentinel value that occupies a legal position on the real
scale.

### 5. The candidate pool has little diversity to reorder

The catalog is stored at configuration level: 303 rows across 107 families.
Under RM4,000 the ten largest families alone account for roughly 64 rows.
`retrieve_candidates` recalls the top 50 by cosine similarity, and
configurations of one model embed almost identically, so a recall is largely
RAM/CPU permutations of a dozen machines. Evaluation output shows two
ExpertBook P1 configurations — identical price (RM3,399), RAM, storage, weight
and display, differing only in CPU — each occupying one of the six result
slots the user sees.

Quantified over 107 distinct queries spanning RM2,000–8,000, four purposes and
two languages: the most-recommended laptop appears in 80 of them (75%), and
the top 25 all appear in 42% or more. Union coverage is 278/303, so this is
concentration within each query rather than a recall failure.

Two data properties drive it. 146 of 303 rows (48%) carry `price_rm = 0`;
`price_rm <= budget_max` is trivially true for 0 and `_budget_penalty` returns
1.0, so those rows survive every query unpenalised — nine of the twenty
most-recommended laptops are in this group. And priced rows are heavily
top-weighted: 3 below RM2,000, 6 between RM2,000–3,000, 49 between
RM3,000–5,000, 99 above RM5,000, so two of the four budget bands the
questionnaire offers are backed by 3 and 6 machines. Constraint relaxation and
relevance gating therefore fire largely against this catalog gap rather than
against unreasonable user requests.

## Options considered

1. **Make PickScore an independent ranker.** Drop the base rank weight, score
   from raw attributes, and let PickScore own the final ordering with the
   reranker reduced to candidate selection.
2. **Leave it as a displayed value** with no effect on what the user sees, and
   stop investing in it.
3. **Position it as a buy indicator** — an answer to "should I buy this one,
   given what I said I care about", with ordering left to the reranker.

## Decision

**Option 3.** PickScore is a buy indicator, not a ranker. It is derived from
the user's own stated preferences, so the question it is fit to answer is
whether a given machine suits *them* — not which of several machines should
come first.

Three changes follow from the layers above:

- **Collapse configuration variants in the result set.** Candidates are
  deduplicated by family key before the six-result cap, keeping the
  configuration closest to the user's stated budget. Two configurations of the
  same machine cannot occupy two of the six slots a user sees.
- **Exclude unpriced rows when a budget is stated.** `price_rm = 0` means
  unknown, not free. The user will go and look the price up themselves and may
  find it exceeds their budget, so presenting it as a match is a promise the
  system cannot keep. With no budget stated there is nothing to verify against
  and the row stays eligible.
- **When no budget is given, ask for one** before recommending. With no upper
  or lower limit stated, surface the best-configured machines.

The result cap was reduced from 10 to 6 for context-window reasons, which
makes each slot more valuable and duplicate configurations more costly.

## Consequences

**What this buys**

- PickScore's output becomes honest about its role. A four-to-fifteen point
  spread is acceptable for a per-user suitability indicator and misleading as
  a ranking.
- Family collapse removes visible duplication from the six cards a user sees —
  the single most user-facing symptom of the whole chain.
- The conditional exclusion of unpriced rows reduces per-query concentration
  without removing high-end machines from the unbudgeted queries that need
  them.

**What it gives up**

- Personalization no longer influences ordering at all. A user's ranked
  priorities change the indicator and the badge, not the sequence.
- Family collapse reduces the number of distinct catalog rows reachable
  through chat, since only one configuration per family is surfaced.
- Keeping the configuration closest to budget means, in practice, the
  highest-specified configuration the user can afford — everything retrieved
  is already within budget. This systematically presents the top of the user's
  range, which is a defensible default but not a neutral one.
- Suggesting an upgrade when a better configuration costs slightly more
  requires deliberately over-fetching above the budget ceiling, since those
  rows are removed at the SQL level. Not implemented yet.

**When this decision stops holding**

If the catalog is re-modelled from configuration level to model level, layer 5
largely dissolves: the candidate pool gains real diversity, family collapse
becomes redundant, and a scoring function would have meaningfully different
machines to separate. At that point Option 1 becomes worth reconsidering and
this ADR should be superseded rather than amended.

Separately, if normalization moves to percentile ranks, layer 2's compression
eases and the use-case scores become comparable across factors. That would
remove the strongest argument against PickScore ranking, but not layer 1 — the
shortlist would still be sorted by `similarity_score` until that is changed
deliberately.

**Known gaps left open**

- The `/10` denominator in the priority display does not correspond to the
  rank weights it renders.
- Screen size, and the lower bound of the questionnaire's budget band, remain
  collected and unused. The missing lower bound is the same absence that lets
  the cheapest catalog entry appear in most queries.
- `_KNOWN_PURPOSES` holds four of the five options the questionnaire offers,
  so "General Use" silently normalizes to "Office" in the reranker.
- Two weighting schemes coexist for the same job: `USE_CASE_PRIORITIES`
  (absolute, precomputed) and `DEFAULT_PRIORITY` + `PURPOSE_MODIFIERS`
  (rank-derived, multiplicative). The five UI scores reproduce from the first
  alone, so they are alternatives rather than composed — two independent
  definitions of what matters for gaming, free to drift apart.

## Not chosen, and why

**Blanket exclusion of `price_rm = 0` from retrieval.** The first fix
proposed, rejected on measurement: it removes 48% of the catalog, including
most of the high-end gaming line (ROG Strix G18, TUF F15/A14, Gaming V16) —
exactly the machines a high-budget gaming query needs. The conditional form
keeps them reachable where no budget claim has to be verified.

**Re-tuning `USE_CASE_PRIORITIES` to fix the inversion.** This was the
original hypothesis and the measurement contradicted it. All five UI scores
reproduce exactly from the current weights, and Gaming's heavy factors score
41 where Office's score 75. Any weighting is applied to the same compressed
raw scores.

**Re-modelling the catalog to model level with configuration as an
attribute.** The correct long-term fix; it would improve retrieval diversity,
review coverage and score normalization at once. Out of scope for v2: it
touches roughly 40 endpoints, the embedding corpus, and every migration
downstream of the laptop table. Deferred to v2.1.

**Maximal Marginal Relevance for retrieval diversity.** The principled fix for
layer 5 — score candidates on similarity minus their similarity to
already-selected results — rather than the family-key heuristic chosen.
Deferred without evaluation: family collapse captures most of the
user-visible benefit at a fraction of the risk, and MMR introduces a tuning
parameter that would need its own evaluation baseline before it could be
trusted.

**Percentile or bucketed normalization curves.** Identified as the correct fix
for layer 2 and deliberately not bundled into this decision. It changes every
score in the catalog at once, so it needs a before/after baseline and a
regenerated `LaptopPickScore` table — a change of its own size, and a design
call rather than a bug fix.

## Open questions

- [ ] On the FX607VU variant the GPU scored 0.0 while the benchmark table
      contains exactly one matching row ("GeForce RTX 4050 Laptop GPU" →
      14,245, which would normalize to ~36). This is a different failure from
      the RTX 5060 mis-resolution — a miss rather than a wrong match — and the
      0.0 does not match the documented 50 fallback either. Needs the GPU
      resolution branch of `calculate_pick_score` read directly.
- [ ] What `budget_max` the personalized path passes for the open-ended
      `> RM5000` band. `None` would mean no budget filter at all, admitting
      the full unpriced set; a literal `5000` would invert the user's intent.