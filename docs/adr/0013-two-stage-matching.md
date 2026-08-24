# ADR-0013 — Two-stage matching: family first, configuration second

- **Status:** Proposed
- **Date:** 2026-08-22
- **Depends on:** ADR-0012 (review linkage: many-to-many with claim scope)
- **Related:** ADR-0010 (benchmark resolution: anchor tokens over threshold), ADR-0014 (non-English review coverage)

## Context

`matcher.match_laptop` resolves a YouTube video title to one laptop:

```python
MATCH_THRESHOLD = 73.0
result = process.extractOne(video_title, names, scorer=fuzz.token_set_ratio)
```

`names` is a compact match key per laptop, built by `_build_match_key` from the
brand and product name with `-inch`, RAM and storage tokens stripped. The catalog
is 274 configurations in 97 families; 87 rows share a match key with at least one
other row.

### Measured behaviour

`app/scripts/audit_match_ties.py`, run over 85 discovered videos with the
candidate limit raised until no video hit the cap (real ceiling: 23; script
default now 40):

| Tied at rank 1 | All 274 (same/cross family) | Active 238 (same/cross) |
|---|---|---|
| 1 — clear winner | 9 (9/0) | 7 (7/0) |
| 2 | 28 (28/0) | 28 (28/0) |
| 3 | 7 (7/0) | 8 (8/0) |
| 4 | 0 | 1 (0/1) |
| 5 | 3 (3/0) | 3 (3/0) |
| 6–10 | 35 (12/23) | 35 (12/23) |
| 11–14 | 1 (0/1) | 2 (0/2) |
| 15+ | 2 (0/2) | 1 (0/1) |
| **Total tied** | **76/85 (89.4%)**, 26 cross-family | 78/85 (91.8%), 27 cross-family |
| Median / max width | 6 / 23 | 5 / 18 |

- Median top-1 score is **55.0**, against a threshold of **73**.
- Autonomous match yield is **8 of 85 (9.4%)**. Of the 18 rows in `matched`
  status, 10 carry `match_confidence = 100.0`, which `router.manual_match`
  assigns — those are human decisions, not matcher output.
- 11 videos are both tied and above threshold, so they are auto-matched today to
  whichever row Postgres returned first.
- Current queue state: 52 of 85 rows (61.2%) sit in `pending`.

CJK titles, separated:

| | ASCII (n=64) | CJK (n=21) |
|---|---|---|
| Tied | 56 (87.5%) | 20 (95.2%) |
| Cross-family among ties | 15 (26.8%) | 11 (55.0%) |
| Median / max tie width | 5 / 15 | 8 / 23 |
| Median top-1 score | 55.7 | 47.6 |

The widest tie in the catalog is a CJK comparison video scoring **68.2 across 23
laptops in 4 families**. Its Latin spans — `RTX 5060`, `ROG`, `Zephyrus`, `G16`,
`TUF`, `Gaming` — are the entire signal, and every ASUS gaming SKU intersects
them equally. `token_set_ratio` splits on whitespace, which CJK does not use, so
a Chinese title collapses to one large token plus its Latin residue, and the
score measures the residue rather than the title. This is why CJK scores stay
deceptively high (75.7, 72.2 observed) while cross-family error more than
doubles.

### The problem, stated precisely

The complaint that started this work was "machine matching may not be accurate."
The measurement says something different and worse: **the matcher almost never
produces a winner, and on the rare occasions it does, the winner is arbitrary.**

Two distinct failure modes hide behind one number:

1. **No signal.** 89.4% of videos are decided by an unbroken tie, so
   `extractOne`'s "first maximum" resolves to database row order. This is the
   source of queue volume — most videos never clear threshold and land in
   `pending`.
2. **Arbitrary acceptance.** The 11 tied-and-above-threshold videos are
   auto-matched to a row chosen by nothing.

`MATCH_THRESHOLD = 73` cannot fix either. At a median of 55 with 89.4% ties, no
cut point on that distribution separates correct from incorrect, because the
score is not measuring correctness — it measures string overlap between a title
and a product name encoding specifications the title never mentions. This is the
third occurrence of the pattern documented in ADR-0010 (six benchmark mismatches
at confidence 0.855–1.0) and in the `price_rm = 0` ambiguity: **a confidence
number derived from surface similarity is not evidence of a correct match.**

## Decision

**Replace single-stage fuzzy matching with two stages, remove the score
threshold, and escalate on tie width instead.**

### Stage 1 — family, by anchor tokens

Match the title against the 97 families, not the 274 configurations. Candidates
are built from `laptop_family`, not by recomputing keys in `matcher.py`.

Matching is on **anchor tokens** — brand, product line, chassis code, generation
— extracted from the title rather than scored as whole strings. This is the same
mechanism adopted in ADR-0010 for benchmark resolution: keep the tokens that
carry discriminating information, drop the ones that do not.

Configuration tokens (CPU, GPU, RAM, storage) are **excluded** from Stage 1. They
are noise at family level — every ROG Strix G16 shares them — and including them
is what drags family confidence down today.

**CJK titles:** extract ASCII and digit spans first and match on those. Product
names remain Latin even in Chinese titles (`ROG Strix G16`, `RTX 4060`,
`i7-14650HX`). The CJK remainder is weak supporting signal only — 開箱 or 評測
confirms the video is a review, 華碩 confirms the brand — and never a primary
match key.

### Stage 2 — configuration, within the matched family only

Search title, description and transcript for CPU, GPU and RAM strings belonging
to that family's members. If found, bind `laptop_id`. If not found, leave
`laptop_id` NULL.

**A Stage 2 miss is not a failure and does not queue a human.** Per ADR-0012,
NULL `laptop_id` means "tested configuration unknown", which is the honest answer
for most videos, and the review still attaches at family scope.

### Escalation by tie width, not score

`MATCH_THRESHOLD` is **removed**, not tuned. Stage 1 produces a tie width, and
routing is by that width:

| Stage 1 tie width | Route | Justification |
|---|---|---|
| 1–5 | Auto-accept at family scope | 47 of 85 videos; **all same-family** in the measurement |
| 6–10 | Human queue | 35 videos, 23 of them cross-family — genuine ambiguity |
| ≥11 | Flag `no_signal`, do not queue | 3 videos, **all cross-family**; nothing for a human to disambiguate from a title alone |

The boundaries are structural, not tuned. Wide ties are necessarily cross-family:
a tie can only stay inside one family while it is narrower than that family, and
the largest family is 14 rows. Every observed tie at width ≥11 spanned families,
and that follows from catalog shape rather than from this sample.

The `no_signal` bucket exists because a human staring at a 23-wide tie has no
more information than the matcher did. Those videos need a different input —
description text, or the video itself — not a longer list to click through.

### `match_source` and `tie_width` persist

Both are written to `review_laptop_link` (ADR-0012). `tie_width` is the queue's
sort key: a 2-wide tie is a fast decision, a 10-wide one is not, and the queue
should surface the cheap decisions first.

## Not chosen

**Tune `MATCH_THRESHOLD`.** Rejected on measurement. Lowering it converts
arbitrary rejections into arbitrary acceptances — with 89.4% ties, whatever
clears the bar is still chosen by row order. Raising it drives yield below the
current 8/85. There is no third option on a distribution where the score does not
separate the classes.

**Swap `token_set_ratio` for a CJK-aware scorer (`partial_ratio`, character
n-grams).** Would improve CJK scores but not CJK *correctness*: the underlying
problem is that a title contains no configuration information at all, so no
scorer can recover which of 14 SKUs was tested. Better scoring of an
under-determined question yields more confident wrong answers.

**Filter candidates to `status = active` as a tie fix.** Measured and rejected in
that role: it makes ties slightly *worse* (76 → 78 tied, 26 → 27 cross-family).
Suspended rows share match keys with active siblings, so removing them shortens
ties without dissolving them, while removing a row that was a lone rank-1 winner
creates a new tie among the runners-up. **The status filter should still ship** —
spending YouTube quota on and matching reviews to retired products is a
correctness bug — but its justification is correctness and quota, not match
quality. It must not be budgeted as a tie reduction.

**Use an embedding similarity instead of fuzzy matching.** Deferred, not
rejected. Would cost a Gemini call per title against a 7 RPM quota that is
already the project's binding constraint, to solve a problem where the
discriminating information is absent from the input rather than hard to compare.
Revisit only if anchor-token Stage 1 leaves cross-family ties above 10%.

**Ask a human to confirm every match.** The status quo by default, since 61.2% of
rows sit in `pending`. Rejected because it spends human judgement on verifying
machine guesses — high volume, low value per decision — rather than on the cases
where human judgement is the scarce input.

## Consequences

**Positive**

- The 47 videos at width ≤5, all same-family, stop reaching a human at all. They
  auto-accept at family scope, which is the single largest reduction in queue
  volume available.
- The 11 tied-and-above-threshold arbitrary auto-matches disappear, because there
  is no threshold to be above.
- Stage 1 searches 97 candidates rather than 274, and the full catalog no longer
  loads per call (today `match_laptop` joins 274 rows to brands on every
  invocation, called in a loop from `rematch_pending`).
- CJK videos become matchable at family level, which is the more valuable corpus
  for a Malaysian product: local reviewers quote RM prices and local SKUs.

**Negative / costs**

- `matcher.py` is effectively rewritten, and `_build_match_key` retires. Anchor
  token extraction is more code than one `extractOne` call, and the token
  vocabulary (chassis codes, line names) needs maintenance as the catalog grows.
- Stage 2 needs description and transcript text at match time. Today
  `ingest_for_laptop` matches on title alone, so the ordering of fetch and match
  changes again.
- The width boundaries (5, 11) are calibrated on 85 videos from one catalog
  snapshot. They should be re-measured after the catalog grows materially — the
  observed ceiling of 23 is a property of today's 274 rows, not an invariant.
- `audit_match_ties.py` must default its candidate limit to the full candidate
  count rather than a fixed 40. A fixed cap silently returns to truncation as the
  catalog grows, and the first run of this audit reported a floor as if it were a
  measurement.

**Neutral**

- Yield is not the success metric. A correct family match with NULL
  configuration is a better outcome than a confident wrong configuration, so
  "matched rows" will not be comparable before and after.

## Verification

The escalation rule is a machine judgement and must have an independent check,
consistent with the pattern in this codebase where every fix that looked applied
was only proven by a separate audit.

- Re-run `audit_match_ties.py` against Stage 1 candidates and confirm the
  same-family / cross-family composition per width bucket holds at family
  granularity.
- Assert the structural claim directly: no auto-accepted match (width ≤5) spans
  families. This is an invariant, not a statistic — a single violation means the
  boundary is wrong, in the same way `matched_name == target` became a validator
  invariant in ADR-0010.
- Track the auto-accept rate and the `no_signal` count as ongoing metrics. A
  rising `no_signal` count means the anchor token vocabulary has fallen behind
  the catalog.

## Open questions deferred

- Whether comparison and round-up videos should be detected explicitly. The
  23-wide CJK example is a genuine multi-laptop review, so its wide tie is
  correct behaviour rather than matcher failure, and ADR-0012's link table can
  represent it. Distinguishing "wide tie because it is a comparison video" from
  "wide tie because there is no signal" is left for a later revision.
- Whether description text should feed Stage 1 as well as Stage 2. Chinese
  reviewers routinely paste full specification tables into the description, which
  may make Stage 2 succeed more often than on English videos — untested.