# ADR-0006: Position PickScore as a buy indicator, not a ranker

- **Status:** Accepted — amended 2026-08-20 (see Revisions)
- **Date:** 2026-08-14
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0008 (price
  fabrication), ADR-0009 (laptop status), ADR-0010 (benchmark resolution),
  ADR-0011 (normalization curve)

## Context

PickScore was redesigned for v2, moving from a flat weighted average to eight
factors under three-layer multiplicative weighting. The redesign was expected
to change recommendation ordering. It did not — v2 output closely tracked v1.

Tracing the cause surfaced five layers, each upstream of the last. The figures
below were measured against the live catalog; where a measurement has since
been superseded the current value is given and the original is kept in the
Revision sections, because the sequence of readings is itself evidence.

### 1. PickScore does not participate in ordering

`GET /conversations/{id}/laptops` sorts the persisted shortlist by
`similarity_score`, not `pick_score`. The scoring code lives outside
`app/rag/` and runs only after the relevance gate passes. Its first weighting
layer is the reranker's ordinal position, so it is structurally unable to
depart from the reranker's ordering.

Whatever PickScore computes, the user sees the reranker's sequence.

**Unchanged by any of the 2026-08-16/17 work.** Every correction below moved
raw scores; none of them moved this. It is now the only one of the five layers
still standing.

### 2. The weights are sound; the raw scores they weight are not

*Resolved 2026-08-17 by ADR-0011. This section is kept as the diagnosis; the
numbers are the corrected ones and the original readings are in Revision 1.*

The five use-case scores on the product page come from `USE_CASE_PRIORITIES`
in `app/laptops/pickscore_general.py` — absolute 1–10 weights per factor,
precomputed per laptop into `LaptopPickScore` and served from
`GET /{laptop_id}/pick-scores`.

Dumping one machine (FX608JMR — i7-14650HX, RTX 5060 Laptop, 16GB/1TB, 2.2kg,
90Wh, RM5,899) reproduces all five UI scores exactly from those weights and its
eight raw factor scores:

| Use case | 2026-08-14 (min-max) | Now (percentile) |
|---|---|---|
| Gaming | 51 | **62** |
| Creative Work | 51 | 61 |
| Programming | 54 | 59 |
| Office & Study | 62 | 57 |
| General Use | 58 | 56 |

The engine does what the table says, and the table is reasonable: Gaming
weights GPU highest (10), Office weights price (9) and battery (8).

The inversion — a gaming laptop scoring lowest on Gaming and highest on
Office & Study, with a "Best fit: Office & Study" badge derived from that
argmax — came from the raw scores underneath:

| Factor | Min-max raw | Percentile raw | What min-max was claiming |
|---|---|---|---|
| price | 87.4 | **52.9** | that an RM5,899 machine is cheap |
| ram_storage | 15.3 | **50.5** | that 16GB/1TB is a poor configuration |
| portability | 52.0 | **20.0** | that 2.2 kg is mid-weight |
| cpu | 53.0 | 70.6 | |
| gpu | 57.5 | 75.8 | |
| battery | 84.2 | 84.9 | |

The right-hand column is the finding in one line. Min-max described this
machine as cheap, poorly specified and of middling weight. It is mid-priced,
median-specified and heavy. All three misreadings came from a scale set by an
outlier: an RM36,999 workstation, a 128GB/4TB configuration, a 3.73 kg desktop
replacement.

Gaming's heavy factors (gpu, cpu, ram_storage) averaged about 42 on this
machine under min-max while Office's (price, battery, portability) averaged
about 75. **A gaming laptop could not win the preset that weights its own
hardware, because those were the factors whose normalization was most
compressed.** Re-tuning the weights could not have reached this: any weighting
is still applied to a 15.3 and a 53.0.

Three mechanisms produced the compression, all three now fixed.

- **The `> 0` guard existed on price only.** `pickscore_adapter.py:39` filters
  `Laptop.price_rm > 0`, so `price.min` is 1,429 and not 0. The other columns
  took a plain `func.min()` (lines 41–44), so missing data set their floor.
  A correct fix had been applied once and not to its neighbours.
- **Rows the user cannot see were setting the denominators.** `get_laptop_ranges`
  aggregated the whole table. Filtering it to `status = 'active'` — in both the
  min/max aggregate and the separate query that derives `cpu_mark`/`gpu_mark` —
  raised every artificial floor at once. See ADR-0009.
- **Min-max is sensitive at both ends, and the top end was untouched.** Raising
  the floor was not enough: against a 4–128 range, 16GB still scored 9.7. The
  128GB/4TB workstation did as much damage as the zeros did, and the RM36,999
  machine did the same in the opposite direction on price. This was ADR-0011,
  and percentile rank replaced the curve on 2026-08-17.

The residual inversion was a difference in **effective variance**, not in
weights. Under min-max the factors' p10–p90 spreads differed by 3.6× (price
34.1, ram_storage 21.3, portability 44.2, against gpu 76.5); under percentile
they differ by 1.57× (79.8 / 51.4 / 77.5, against 81.5). The presets weight
eight factors as though they shared a scale, and now they roughly do.

### 3. Every benchmark lookup ran at depressed confidence

`resolve_benchmark` in `app/pickscore/benchmark_service.py` normalized the
query — `key = _normalize(model_string)`, which lowercases — and matched it
against the raw table names, which are mixed case. RapidFuzz's `fuzz.WRatio`
does no preprocessing of its own, so the comparison was case-mismatched
throughout. The same pair scores 95–100 with consistent casing and 0.64–0.675
without.

With `CONFIDENCE_THRESHOLD = 0.6`, those degraded matches were accepted and
returned with `is_proxy: False`, indistinguishable downstream from a confident
resolution:

| Catalog string | Resolved to | Correct row |
|---|---|---|
| NVIDIA GeForce RTX 4050 Laptop GPU | mark 81 — the catalog minimum, normalizing to exactly 0.0 | 14,245 |
| GeForce RTX 4050 Laptop GPU | mark 3,617 | 14,245 |
| GeForce RTX 5060 Laptop GPU | mark 20,722 — the *desktop* card | 16,785 |

Normalizing both sides restored all three (0.95, 1.0, 1.0). A second pass was
needed for scraped trademark symbols and mojibake — `NVIDIA® GeForce RTX™`,
`RTX⁴`, `AMD Ryzen’AI`, `Intel Iris Xᵉ` — and the order mattered: U+2122 has a
compatibility decomposition to the letters "TM", so applying NFKD before
stripping the symbol turned `RTX™` into `RTXTM`.

Correcting this moved the RTX 5060 from 53.1 to 43.1, lowering Gaming from 49
to 47 while Office & Study held at 61. **The defect had been inflating exactly
the factor the Gaming preset weights most; removing it widened the inversion
rather than closing it** — which was the first hard evidence that the problem
was normalization rather than resolution.

That widening was real but not final. Two further resolution defects, both
found on 2026-08-16, moved it back: see ADR-0010 and the Revisions below.
Confidence thresholds turned out to be the wrong instrument for all of them.

### 4. Unknown and average are often the same number

Unresolved factors fall back to a raw score of 50. `_score_price` returns a
reason string alongside it ("Price unavailable — factor skipped, scored as
neutral (50)"), but `_score_gpu` returned only `(score, is_proxy)` and had no
channel to report why. It now carries a note; see ADR-0010.

This is the same failure as `price_rm = 0` meaning both "free" and "unknown",
one layer up: a sentinel that occupies a legal position on the real scale.

The sentinel got worse, not better, as the ranges were corrected. With
`gpu_mark` at 1,299–28,248, a raw 50 is equivalent to a benchmark mark of about
14,774 — above a real RTX 3050 (9,503), RTX 5050 (14,176) and RTX 4050
(14,245).

*As of 2026-08-17 no active laptop reaches that fallback.* The five that did
are mapped through `_INTEGRATED_GPU_BY_CPU`, and the eighteen Apple machines
through `_APPLE_GPU_EQUIVALENT`, so the branch is unreachable for the current
catalog. It remains in place for future ingests, and it remains wrong if it is
ever reached — a fallback bounded to the integrated band would be more
defensible than a global neutral.

### 5. The candidate pool has little diversity to reorder

The catalog is stored at configuration level: 303 rows across 107 families at
the time of writing, 274 rows today across 93 families, of which 238 are
active. Under RM4,000 the ten largest families alone account for roughly 64
rows. `retrieve_candidates` recalls the top 50 by cosine similarity, and
configurations of one model embed almost identically, so a recall is largely
RAM/CPU permutations of a dozen machines. Evaluation output showed two
ExpertBook P1 configurations — identical price (RM3,399), RAM, storage, weight
and display, differing only in CPU — each occupying one of the six result
slots the user sees.

Quantified over 107 distinct queries spanning RM2,000–8,000, four purposes and
two languages: the most-recommended laptop appeared in 80 of them (75%), and
the top 25 all appeared in 42% or more. Union coverage was 278/303, so this was
concentration within each query rather than a recall failure. *Those figures
predate family dedup and the status filter and have not been re-measured.*

Two data properties drove it, and neither remains. 146 of 303 rows (48%)
carried `price_rm = 0`; `price_rm <= budget_max` is trivially true for 0 and
`_budget_penalty` returns 1.0, so those rows survived every query unpenalised
— nine of the twenty most-recommended laptops were in this group. That is
closed by ADR-0009: every active row carries a price and every unpriced row is
non-active, so retrieval never sees one. The second, duplicate configurations
filling result slots, is closed by the family dedup described below.

What remains is a catalog-breadth problem rather than a scoring one. Priced
rows are heavily top-weighted — 6 below RM2,000, 13 between RM2,000–3,000, 68
between RM3,000–5,000, 141 above RM5,000 — so the two lowest budget bands the
questionnaire offers are backed by 19 machines between them while the top band
alone is over half the catalog. Constraint relaxation and relevance gating
therefore still fire largely against this catalog gap rather than against
unreasonable user requests. Separately, after dedup the Gaming ranking's top
ten is nine ASUS machines out of ten — not because the ranking is wrong, but
because the catalog is ASUS-heavy. No ordering change fixes that.

## Options considered

1. **Make PickScore an independent ranker.** Drop the base rank weight, score
   from raw attributes, and let PickScore own the final ordering with the
   reranker reduced to candidate selection.
2. **Leave it as a displayed value** with no effect on what the user sees, and
   stop investing in it.
3. **Position it as a buy indicator** — an answer to "should I buy this one,
   given what I said I care about" — with ordering left to the reranker.

## Decision

**Option 3.** PickScore is a buy indicator, not a ranker. It is derived from
the user's own stated preferences, so the question it is fit to answer is
whether a given machine suits *them*, not which of several machines should
come first.

Three changes follow from the layers above:

- **Collapse configuration variants in the result set.** Candidates are
  deduplicated by family before the six-result cap, keeping the configuration
  closest to the user's stated budget. Two configurations of the same machine
  cannot occupy two of the six slots a user sees.
  *Status: implemented 2026-08-17. A `laptop_family` table with a nullable
  `laptops.family_id`, seeded from the review pipeline's existing family key
  and then merged by hand to product-line granularity — 93 families over 274
  laptops. Dedup applies in `search_laptops` between rerank and the cap, and in
  `get_ranking_for_use_case` before the limit slice. A null `family_id` passes
  through rather than being grouped.*
- **Exclude unpriced rows when a budget is stated.** `price_rm = 0` means
  unknown, not free. The user will go and look the price up themselves and may
  find it exceeds their budget, so presenting it as a match is a promise the
  system cannot keep. With no budget stated there is nothing to verify against
  and the row stays eligible.
  *Status: superseded by ADR-0009. The `status` column now excludes unpriced
  rows from retrieval unconditionally, which is a stricter rule than this one.
  A conditional check in the tool would be unreachable code. Kept here as the
  reasoning that led to the stricter rule, not as a description of the
  implementation.*
- **When no budget is given, ask for one** before recommending. With no upper
  or lower limit stated, surface the best-configured machines.

Benchmark resolution is fixed as part of this decision — both sides
normalized, symbols and mojibake stripped before folding, and
`CONFIDENCE_THRESHOLD` raised from 0.6 to 0.85. The threshold portion of this
was later found to be insufficient on its own; see ADR-0010.

The result cap was reduced from 10 to 6 for context-window reasons, which
makes each slot more valuable and duplicate configurations more costly.

## Revision 1 — 2026-08-16

Two days of measurement after this ADR was accepted changed several of its
figures and reversed one of its rejected options. The conclusion held; the
route to it did not.

**What was found**

- The `\bprocessor\b` strip added under this ADR never fired. The `re.sub` ran
  before `.lower()` and carried no `re.IGNORECASE`, so it matched only a
  lowercase spelling while the catalog writes `Intel Core 5 Processor 210H`.
  Fifty-one CPU rows had been resolving to the wrong benchmark the whole time.
- Filtering `get_laptop_ranges` to active rows raised four floors at once
  (`ram_gb` 0→4, `ssd_gb` 0→64, `weight_kg` 0→0.79, `battery_wh` 0→36.5) and
  moved `cpu_mark` from 341 to 767.
- The `gpu_mark` floor of 4 was Apple's `N-core GPU` strings matching the
  pre-2005 tail of the PassMark table. `_score_gpu` proxied Apple via the CPU,
  so their own scores were unaffected — but `get_laptop_ranges` has no Apple
  branch, so those rows were setting the denominator for every non-Apple
  laptop.
- The `gpu_mark` ceiling of 38,963 was the **desktop** RTX 5090. Seven catalog
  strings named desktop parts, covering fifteen laptops, all of them Acer:
  Acer's source writes `GeForce RTX 5060` where ASUS writes `GeForce RTX 5060
  Laptop GPU`. Identical silicon was scoring 23% apart by brand. Fixing this
  moved the ceiling to 28,248 and decompressed every discrete GPU score.
- The same source gap explained 43 laptops with `battery_wh = 0`: Acer's
  official pages do not publish battery capacity, so there was nothing to
  scrape. Filled by hand.

**What moved**

| | 2026-08-14 | 2026-08-16 |
|---|---|---|
| Gaming | 49 | 51 |
| Creative Work | 48 | 51 |
| Programming | 53 | 54 |
| General Use | 56 | 58 |
| Office & Study | 60 | 62 |
| Inversion gap | 11 | 11 |

The gap widened to 14 in the middle of this work and returned to 11. Arriving
back at the starting number is not a null result: on 2026-08-14 the gap was 11
on top of five separate defects, and it is now 11 on measured data. Every
correction changed the gap by a few points in one direction or the other, and
none of them approached closing it — which is the strongest evidence available
that the cause is the normalization curve and nothing else.

**What this ADR got wrong**

- *"The presets are the weak point of the algorithm."* Contradicted. The
  presets are the one component that reproduces exactly and behaves as
  documented. The compression is in the raw scores, and specifically in the
  difference in effective variance between factors.
- *"Generic part names are left unresolved."* Reversed. See below.
- The `> RM5000` open-ended band, `_KNOWN_PURPOSES`, the dual weighting
  schemes and the `/10` denominator were all listed as known gaps and all
  remain open. None of them turned out to be involved in the inversion.

## Revision 2 — 2026-08-17

The prediction Revision 1 ended on was tested and held. Three changes shipped
together: percentile normalization (ADR-0011), family dedup, and a fix to the
personalized price branch.

**The inversion is closed.** On the reference machine Gaming is now the highest
of the five presets at 62 and Office & Study the second-lowest at 57, a gap of
−5 where it had been +11. Nothing about the weights changed.

**The curve change was verified against an independent implementation.** All
238 active laptops × 5 use cases were compared between the engine and
`simulate_normalization.py`'s patched curve: 1,190 comparisons, 0 differences.
The five reference scores landed exactly on the values the simulation had
predicted before the engine was touched.

**Where it showed.** The Office & Study ranking shares 1 of 10 entries with its
previous top ten — four 16" MacBook Pros at RM12,499–20,999 replaced by
Zenbook A16, Zenbook S14, Zenbook 14 OLED and Swift Go 16 at RM4,599–7,699.
Gaming shares 7 of 10; that list was already mostly right, because it rides
`gpu`, whose spread barely changed. The change landed exactly where the
effective-variance diagnosis said it would.

**Personalized mode was inconsistent with general mode and now is not.**
`_score_price` returned a flat 100.0 for anything within a stated budget, so
price contributed an identical constant to every affordable candidate — the
highest weight in two presets, spent on nothing. Once general mode moved to
percentile the two modes disagreed by 47 points on the same machine (52.9
against 100.0). The personalized branch now uses the same percentile base and
applies the existing `DECAY_K` decay as a multiplier on it rather than a
subtraction from a constant. On the reference machine with a RM6,000 budget,
price moves 100.0 → 52.9 and the personalized total drops 8 points.

**A persistence path this ADR did not account for.** Personalized scores are
computed live and are not stored in `laptop_pick_scores` — but
`search_laptops` scores personalized when the requesting user has a preference
row, and `agent/router.py:135` writes that number into
`conversation_laptops.pick_score` as a per-thread snapshot. Reopened
conversations therefore show pre-change scores until that thread's next search.
Accepted rather than backfilled, on the same reasoning as the stale non-active
scores in ADR-0009: a snapshot is refreshed by the next action, and a thread
nobody reopens costs nothing.

**What this leaves.** Layer 1 is now the only one of the five still standing,
which changes the status of Option 1 from "the argument against it is
overwhelming" to "the argument against it is one layer deep". See the amended
note under *When this decision stops holding*.

## Consequences

**What this buys**

- PickScore's output becomes honest about its role. A narrow spread is
  acceptable for a per-user suitability indicator and misleading as a ranking.
- Family dedup removed the visible duplication from the six cards a user sees
  and from the public use-case rankings — the single most user-facing symptom
  of the whole chain.
- Hardware factors are now scored against the parts the catalog actually
  contains rather than against whatever a degraded fuzzy match returned, and
  against the parts a *laptop* contains rather than their desktop namesakes.
- Each factor's weight now buys roughly the same amount of discrimination as
  any other factor's, which is what the preset tables always assumed.

**What it gives up**

- Personalization no longer influences ordering at all. A user's ranked
  priorities change the indicator and the badge, not the sequence.
- Family dedup reduces the number of distinct catalog rows reachable through
  chat, since only one configuration per family is surfaced.
- Keeping the configuration closest to budget means, in practice, the
  highest-specified configuration the user can afford — everything retrieved
  is already within budget. This systematically presents the top of the user's
  range: a defensible default, but not a neutral one.
- Suggesting an upgrade when a better configuration costs slightly more
  requires deliberately over-fetching above the budget ceiling, since those
  rows are removed at the SQL level. Not implemented.
- A percentile score is relative to the current catalog: adding or removing
  laptops moves everyone else's number slightly. At 238 active rows one
  addition moves any percentile by at most 0.4 points, but regeneration cadence
  now matters in a way it did not under min-max, where only a change to an
  extreme moved other laptops' scores.
- Every correction to the ranges or the curve invalidates the stored scores.
  `LaptopPickScore` has been regenerated four times in three days; any baseline
  or screenshot from before the last one corresponds to nothing.

**When this decision stops holding**

If the catalog is re-modelled from configuration level to model level, layer 5
largely dissolves: the candidate pool gains real diversity, family dedup
becomes redundant, and a scoring function would have meaningfully different
machines to separate. At that point Option 1 becomes worth reconsidering, and
this ADR should be superseded rather than amended.

*Amended 2026-08-17.* Layers 2, 3, 4 and most of 5 are now closed, so the case
against Option 1 rests on layer 1 alone — the shortlist is sorted by
`similarity_score` because that is what the code does, not because PickScore is
unfit to sort it. The remaining argument is about meaning rather than
mechanics: a percentile score answers "how does this rank against the catalog",
which is closer to a ranking claim than the buy-indicator framing this ADR
chose. If PickScore is ever to own the ordering, that reframing should be the
reason, and it should be a new ADR rather than a revision of this one.

**Known gaps left open**

- The `/10` denominator in the priority display does not correspond to the
  rank weights it renders; the questionnaire collects a drag-to-rank, and
  `W = N − i` turns position into weight.
- Screen size, and the lower bound of the questionnaire's budget band, remain
  collected and unused. The band is stored as JSONB `{"min": …, "max": …}` and
  read in two places, but only as agent context and as a display string;
  `search_laptops` has no `budget_min` parameter, so the agent is told the
  floor in prose and has no way to act on it.
- For the open-ended `> RM5000` band, `max` is null, so the personalized price
  branch does not engage and the score falls through to the catalog-wide
  inverse percentile — a user who stated a high budget still receives a price
  score that rewards cheapness. The most defensible reading of "> RM5000" is
  probably that price is *not* a primary concern for that user, which argues
  for flattening the price factor's weight rather than giving it a score in
  either direction. Not decided.
- `_KNOWN_PURPOSES` holds four of the five options the questionnaire offers,
  so "General Use" silently normalizes to "Office" in the reranker. Separately,
  `PURPOSE_MODIFIERS` is keyed on different labels again ('Office/Study',
  'Programming/Development', 'Creative Work'), so three of the four would fail
  to key into it.
- Two weighting schemes coexist for the same job: `USE_CASE_PRIORITIES`
  (absolute, precomputed) and `DEFAULT_PRIORITY` + `PURPOSE_MODIFIERS`
  (rank-derived, multiplicative). The five UI scores reproduce from the first
  alone, so they are alternatives rather than composed — two independent
  definitions of what matters for gaming, free to drift apart.
- `screen_size` and `brand` return a constant 50 in general mode. They
  contribute no discrimination and only dilute the other factors — 6 of 43
  weight in `general_use`, about 14%, spent on two constants. Now that the
  other six factors spread properly, these two are the largest remaining
  distortion in general-mode scores.
- The "Best fit" badge is an argmax over five scores that span about 6 points.
  An argmax over near-equal numbers is unstable by construction: small catalog
  changes will flip the badge even when nothing about the machine changed.
  Either it should require a margin over the runner-up, or the five bars should
  be shown without a winner.
- Personalized mode has no automated test coverage at all: the eval harness
  binds `search_laptops` with `user_id=None`, so `user_pref` is always None.
  The personalized price change shipped on manual verification only.
- `get_laptop_ranges` caches under one module-level key with a 300-second TTL
  and no invalidation on write, so a regeneration run within five minutes of a
  catalog edit scores against the previous ranges.
- The use-case rankings have no budget notion. Gaming's top entries are
  RM19,999–32,999 machines, correct as "best in class" and not necessarily what
  a Malaysian buyer browsing that card wants. Whether the card means
  best-in-class or best-value is undecided.
- *Closed 2026-08-16:* the word "Processor" at 0.855 confidence. The strip was
  already written but inert; fixing its case resolved all 51 rows.
- *Closed 2026-08-17:* the five laptops reaching the 50.0 GPU fallback; family
  duplication in the result set; the flat-100 personalized price score.

## Not chosen, and why

**Blanket exclusion of `price_rm = 0` from retrieval.** Rejected on
measurement at the time: it removed 48% of the catalog, including most of the
high-end gaming line (ROG Strix G18, TUF F15/A14, Gaming V16) — exactly the
machines a high-budget gaming query needs. *This reasoning no longer applies.
Manual price backfill reduced the unpriced set to 35 rows, and ADR-0009 moved
them out of retrieval by status rather than by price. The measurement was
correct when taken; it stopped being a constraint once the data changed.*

**Re-tuning `USE_CASE_PRIORITIES` to fix the inversion.** The original
hypothesis, contradicted by measurement. All five UI scores reproduce exactly
from the current weights, and under min-max Gaming's heavy factors scored 42
where Office's scored 75. Any weighting is applied to the same raw scores.
*Confirmed 2026-08-17: the inversion closed with the weights untouched.*

**Raising `CONFIDENCE_THRESHOLD` before fixing normalization.** Rejected on
the distribution: at 0.90 it would have pushed 21–24% of the catalog into the
50.0 fallback, because the 0.85–0.88 band was real parts carrying trademark
symbols, not genuine misses. *Later found to be the right call for the wrong
reason. Confidence is the wrong axis entirely: the two worst mismatches in the
catalog —* `Intel Core 5 Processor 210H` *and* `AMD Radeon Graphics` *— both
scored 0.855, above the threshold, and a desktop-versus-laptop mismatch scores
1.0. No cut point on a confidence distribution separates these. ADR-0010
replaces the threshold question with a structural one.*

**Re-modelling the catalog to model level with configuration as an
attribute.** The correct long-term fix; it would improve retrieval diversity,
review coverage and score normalization at once. Out of scope for v2: it
touches roughly 40 endpoints, the embedding corpus, and every migration
downstream of the laptop table. Deferred to v2.1. *The family table added on
2026-08-17 is a partial substitute — it gives model-level grouping for
deduplication without re-modelling the rows themselves.*

**Maximal Marginal Relevance for retrieval diversity.** The principled fix for
layer 5 — score candidates on similarity minus their similarity to
already-selected results — rather than the family heuristic chosen. Deferred
without evaluation: family dedup captures most of the user-visible benefit at a
fraction of the risk, and MMR introduces a tuning parameter that would need its
own evaluation baseline before it could be trusted.

**Percentile or bucketed normalization curves.** Identified as the correct fix
for layer 2 and deliberately not bundled into this decision. It changes every
score in the catalog at once, so it needs a before/after baseline and a
regenerated `LaptopPickScore` table — a change of its own size, and a design
call rather than a bug fix. *Became ADR-0011 and shipped 2026-08-17. Clamped
min-max and a log transform were simulated alongside it and rejected: clamp
moved the gap only from +11 to +9 and saturated battery outright, and log
lifted every score without separating them.*

**Forcing a match for generic part names.** `AMD Radeon Graphics` and the
Apple `N-core GPU` strings carry no model number; resolving them to a specific
benchmark row would invent precision the source does not have. They were left
unresolved. *Reversed 2026-08-16.* Leaving them unresolved was not neutral:
`AMD Radeon Graphics` was resolving to mark 75 across 25 laptops and `Intel
Graphics` to a generic 3,211 row across 44, so "unresolved" in practice meant
"resolved to something arbitrary". The precision does exist, just not in the
GPU string — an integrated GPU is fused to a CPU, and the CPU model always
carries a model number. ADR-0010 records the CPU-keyed map that replaces this.