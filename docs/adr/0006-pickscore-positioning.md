# ADR-0006: Position PickScore as a buy indicator, not a ranker

- **Status:** Accepted — amended 2026-08-17 (see Revision)
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
Revision section, because the sequence of readings is itself evidence.

### 1. PickScore does not participate in ordering

`GET /conversations/{id}/laptops` sorts the persisted shortlist by
`similarity_score`, not `pick_score`. The scoring code lives outside
`app/rag/` and runs only after the relevance gate passes. Its first weighting
layer is the reranker's ordinal position, so it is structurally unable to
depart from the reranker's ordering.

Whatever PickScore computes, the user sees the reranker's sequence.

**Unchanged by the 2026-08-16 work.** Every correction below moved raw scores;
none of them moved this.

### 2. The weights are sound; the raw scores they weight are not

The five use-case scores on the product page come from `USE_CASE_PRIORITIES`
in `app/laptops/pickscore_general.py` — absolute 1–10 weights per factor,
precomputed per laptop into `LaptopPickScore` and served from
`GET /{laptop_id}/pick-scores`.

Dumping one machine (FX608JMR — i7-14650HX, RTX 5060 Laptop, 16GB/1TB, 2.2kg,
90Wh, RM5,899) reproduces all five UI scores exactly from those weights and its
eight raw factor scores:

| Use case | Weighted sum / total | Computed | Shown in UI |
|---|---|---|---|
| Gaming | 1841.9 / 36 | 51.2 | 51 |
| Creative Work | 2034.7 / 40 | 50.9 | 51 |
| Programming | 2274.8 / 42 | 54.2 | 54 |
| General Use | 2496.9 / 43 | 58.1 | 58 |
| Office & Study | 2483.7 / 40 | 62.1 | 62 |

The engine does what the table says, and the table is reasonable: Gaming
weights GPU highest (10), Office weights price (9) and battery (8).

The inversion — a gaming laptop scoring lowest on Gaming and highest on
Office & Study, with a "Best fit: Office & Study" badge derived from that
argmax — comes from the raw scores underneath:

| Factor | Raw | Normalized against |
|---|---|---|
| price | 87.4 | RM1,429–36,999 — a workstation outlier makes everything look cheap |
| battery | 84.2 | 36.5–100 Wh — a real ceiling, so 90Wh is genuinely near-max |
| gpu | 57.5 | 1,299–28,248 — laptop parts only, so this is an honest mid-high |
| cpu | 53.0 | 767–62,748 |
| portability | 52.0 | 0.79–3.73 kg inverted |
| ram_storage | 15.3 | 4–128 GB and 64–4,096 GB — a 128GB/4TB workstation compresses everything below it |

Gaming's heavy factors (gpu, cpu, ram_storage) average about 42 on this
machine; Office's (price, battery, portability) average about 75. **A gaming
laptop cannot win the preset that weights its own hardware, because those are
the factors whose normalization is most compressed.** Re-tuning the weights
cannot reach this: any weighting is still applied to a 15.3 and a 53.0.

Three mechanisms produce the compression. The first two were identified on
2026-08-14 and have since been fixed; the third is what remains, and it is the
whole of the residual inversion.

- **The `> 0` guard existed on price only.** `pickscore_adapter.py:39` filters
  `Laptop.price_rm > 0`, so `price.min` is 1,429 and not 0. The other columns
  took a plain `func.min()` (lines 41–44), so missing data set their floor.
  A correct fix had been applied once and not to its neighbours.
- **Rows the user cannot see were setting the denominators.** `get_laptop_ranges`
  aggregated the whole table. Filtering it to `status = 'active'` — in both the
  min/max aggregate and the separate query that derives `cpu_mark`/`gpu_mark` —
  raised every artificial floor at once. See ADR-0009.
- **Min-max is sensitive at both ends, and the top end is untouched.** Raising
  the floor was not enough: against a 4–128 range, 16GB still scores 9.7. The
  128GB/4TB workstation does as much damage as the zeros did, and the RM36,999
  machine does the same in the opposite direction on price. This is ADR-0011.

The residual inversion is a difference in **effective variance**, not in
weights. Price is inverted against an RM36,999 ceiling, so the band where the
catalog actually sits — RM3,000–8,000 — all scores between 81 and 96. Price
barely discriminates between two real candidates, yet it carries the highest
weight (9) in both `office_study` and `general_use`. Battery behaves the same
way. Gaming's factors, by contrast, use most of their range. The presets
weight eight factors as though they shared a scale when their spread across
the catalog differs by roughly fivefold, so the preset that leans on the
near-saturated factors wins by construction.

Correcting `ram_storage` alone would close the gap to about 8 points, not
flip it. Price is the larger offender.

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
found on 2026-08-16, moved it back: see ADR-0010 and the Revision below.
Confidence thresholds turned out to be the wrong instrument for all of them.

### 4. Unknown and average are often the same number

Unresolved factors fall back to a raw score of 50. `_score_price` returns a
reason string alongside it ("Price unavailable — factor skipped, scored as
neutral (50)"), but `_score_gpu` returns only `(score, is_proxy)` and has no
channel to report why.

This is the same failure as `price_rm = 0` meaning both "free" and "unknown",
one layer up: a sentinel that occupies a legal position on the real scale.

The sentinel got worse, not better, as the ranges were corrected. With
`gpu_mark` now 1,299–28,248, a raw 50 is equivalent to a benchmark mark of
about 14,774 — above a real RTX 3050 (9,503), RTX 5050 (14,176) and RTX 4050
(14,245). Only the gaming ranking demotes proxy rows, so in the other four use
cases an unknown GPU outranks three whole tiers of measured ones. Five laptops
are still in this state.

### 5. The candidate pool has little diversity to reorder

The catalog is stored at configuration level: 303 rows across 107 families at
the time of writing, 276 rows today of which 238 are active.
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

Two data properties drove it, and one of them no longer exists. 146 of 303
rows (48%) carried `price_rm = 0`; `price_rm <= budget_max` is trivially true
for 0 and `_budget_penalty` returns 1.0, so those rows survived every query
unpenalised — nine of the twenty most-recommended laptops were in this group.
That is now closed by ADR-0009: every active row carries a price and every
unpriced row is non-active, so retrieval never sees one.

The second property remains. Priced rows are heavily top-weighted — 6 below
RM2,000, 13 between RM2,000–3,000, 68 between RM3,000–5,000, 141 above
RM5,000 — so the two lowest budget bands the questionnaire offers are backed
by 19 machines between them while the top band alone is over half the catalog.
Constraint relaxation and relevance gating therefore still fire largely
against this catalog gap rather than against unreasonable user requests.

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
  deduplicated by family key before the six-result cap, keeping the
  configuration closest to the user's stated budget. Two configurations of the
  same machine cannot occupy two of the six slots a user sees.
  *Status: not yet implemented.*
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

## Revision — 2026-08-17

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
  pre-2005 tail of the PassMark table. `_score_gpu` proxies Apple via the CPU,
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

## Consequences

**What this buys**

- PickScore's output becomes honest about its role. A narrow spread is
  acceptable for a per-user suitability indicator and misleading as a ranking.
- Family collapse would remove visible duplication from the six cards a user
  sees — the single most user-facing symptom of the whole chain. Still to do.
- Hardware factors are now scored against the parts the catalog actually
  contains rather than against whatever a degraded fuzzy match returned, and
  against the parts a *laptop* contains rather than their desktop namesakes.

**What it gives up**

- Personalization no longer influences ordering at all. A user's ranked
  priorities change the indicator and the badge, not the sequence.
- Family collapse reduces the number of distinct catalog rows reachable
  through chat, since only one configuration per family is surfaced.
- Keeping the configuration closest to budget means, in practice, the
  highest-specified configuration the user can afford — everything retrieved
  is already within budget. This systematically presents the top of the user's
  range: a defensible default, but not a neutral one.
- Suggesting an upgrade when a better configuration costs slightly more
  requires deliberately over-fetching above the budget ceiling, since those
  rows are removed at the SQL level. Not implemented.
- Fixing benchmark resolution lowers some scores and raises others, and
  changes `ranges["gpu_mark"]` for every laptop, so the whole
  `LaptopPickScore` table has to be regenerated and prior score screenshots no
  longer correspond to anything. This has now happened three times in two
  days; each regeneration invalidates any baseline captured before it.

**When this decision stops holding**

If the catalog is re-modelled from configuration level to model level, layer 5
largely dissolves: the candidate pool gains real diversity, family collapse
becomes redundant, and a scoring function would have meaningfully different
machines to separate. At that point Option 1 becomes worth reconsidering, and
this ADR should be superseded rather than amended.

Separately, if normalization moves to percentile ranks (ADR-0011), layer 2's
compression eases and the use-case scores become comparable across factors.
That removes the strongest argument against PickScore ranking, but not layer 1
— the shortlist would still be sorted by `similarity_score` until that is
changed deliberately.

**Known gaps left open**

- The `/10` denominator in the priority display does not correspond to the
  rank weights it renders; the questionnaire collects a drag-to-rank, and
  `W = N − i` turns position into weight.
- Screen size, and the lower bound of the questionnaire's budget band, remain
  collected and unused. The band is stored as JSONB `{"min": …, "max": …}` and
  read in two places, but only as agent context and as a display string;
  `search_laptops` has no `budget_min` parameter, so the agent is told the
  floor in prose and has no way to act on it.
- For the open-ended `> RM5000` band, `max` is null, so `engine.py:91` falls
  through to catalog-wide inverse normalization — a user who stated a high
  budget receives a price score that rewards cheapness, computed by a
  different method from every other band, with no signal in the output.
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
- Five laptops still resolve to the 50.0 GPU fallback, which now outranks
  three tiers of real discrete parts. Two CPU models are involved; both are
  ambiguous in the PassMark table rather than absent from it.
- `get_laptop_ranges` caches under one module-level key with a 300-second TTL
  and no invalidation on write, so a regeneration run within five minutes of a
  catalog edit scores against the previous ranges.
- *Closed 2026-08-16:* the word "Processor" at 0.855 confidence. The strip was
  already written but inert; fixing its case resolved all 51 rows.

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
from the current weights, and Gaming's heavy factors score 42 where Office's
score 75. Any weighting is applied to the same compressed raw scores.

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
downstream of the laptop table. Deferred to v2.1.

**Maximal Marginal Relevance for retrieval diversity.** The principled fix for
layer 5 — score candidates on similarity minus their similarity to
already-selected results — rather than the family-key heuristic chosen.
Deferred without evaluation: family collapse captures most of the user-visible
benefit at a fraction of the risk, and MMR introduces a tuning parameter that
would need its own evaluation baseline before it could be trusted.

**Percentile or bucketed normalization curves.** Identified as the correct fix
for layer 2 and deliberately not bundled into this decision. It changes every
score in the catalog at once, so it needs a before/after baseline and a
regenerated `LaptopPickScore` table — a change of its own size, and a design
call rather than a bug fix. *Now ADR-0011, and after two days of measurement
it is the only remaining explanation for the inversion.*

**Forcing a match for generic part names.** `AMD Radeon Graphics` and the
Apple `N-core GPU` strings carry no model number; resolving them to a specific
benchmark row would invent precision the source does not have. They were left
unresolved. *Reversed 2026-08-16.* Leaving them unresolved was not neutral:
`AMD Radeon Graphics` was resolving to mark 75 across 25 laptops and `Intel
Graphics` to Intel Arc's 3,211 across 44, so "unresolved" in practice meant
"resolved to something arbitrary". The precision does exist, just not in the
GPU string — an integrated GPU is fused to a CPU, and the CPU model always
carries a model number. ADR-0010 records the CPU-keyed map that replaces this.