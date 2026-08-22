# ADR-0012 — Review linkage: many-to-many with claim scope

- **Status:** Proposed
- **Date:** 2026-08-22
- **Supersedes:** none
- **Related:** ADR-0010 (benchmark resolution: anchor tokens vs threshold), ADR-0013 (two-stage matching), ADR-0014 (non-English review coverage)

## Context

The YouTube review pipeline binds each video to exactly one laptop:

```python
class RawYoutubeReview(SQLModel, table=True):
    video_id: str = Field(unique=True, index=True)
    matched_laptop_id: Optional[uuid.UUID] = Field(foreign_key="laptops.id")
    match_confidence: Optional[float]
    status: str  # pending | matched | rejected
```

`matcher.match_laptop` picks that laptop with `process.extractOne(..., scorer=fuzz.token_set_ratio)`
against a `MATCH_THRESHOLD` of 73, and `processor.process_raw_review` writes
`raw.matched_laptop_id` onto every chunk it produces. The catalog is stored at
configuration level — 274 rows, 238 active, grouped into 97 families by
`laptop_family` (ADR-0009 era work) — so a single product line such as ROG Strix
G16 is 14 rows whose product names differ only in CPU, GPU, RAM and storage.

### What was measured (Aug 2026)

Two read-only audits were run before any code changed
(`app/scripts/audit_transcript_availability.py`, `app/scripts/audit_match_ties.py`).

**Matching, over 85 discovered videos against 274 candidates:**

| Measure | All (85) | ASCII-only (64) | CJK (21) |
|---|---|---|---|
| rank1 − rank2 gap of 0 (true tie) | 89.4% | 87.5% | 95.2% |
| cumulative gap ≤ 5 | 98.8% | 98.4% | 100% |
| median top-1 score | 55.0 | 55.7 | 47.6 |

- 76 of 85 videos are decided by an unbroken tie, so `extractOne`'s "first
  maximum" resolves to database row order.
- Tie composition: **50 within one family (65.8%)**, **26 across families (34.2%)**.
  A null `family_id` was counted as its own group, so the cross-family figure is
  not inflated by ungrouped rows.
- 11 videos are both tied and above threshold — auto-matched today to an
  arbitrary row.
- Autonomous match yield is **8 of 85**. Of the 18 rows currently in `matched`
  status, 10 carry `match_confidence = 100.0`, which `router.py` assigns on
  manual match. So the matcher has produced 8 usable bindings in the pipeline's
  lifetime.
- CJK titles degrade differently than the score suggests: ties are more common
  (95.2%) and far more often cross-family (55.0% vs 26.8%), while scores stay
  high (75.7, 72.2). `token_set_ratio` splits on whitespace, which CJK lacks, so
  it scores the Latin residue and every candidate sharing that residue ties.

**Transcript availability, over the 39-row reject bucket:**

- 15 rows have no caption track at all (terminal). The same figure was reached
  twice by independent methods — `.list()` enumeration and live re-fetch.
- 24 rows were operational failures (YouTube IP blocking) that
  `except Exception: return None` had erased, and have since been recovered.

### The problem

Three symptoms were reported from the human-review side: machine matching is
inaccurate, the review UI cannot express one review covering several laptops,
and family grouping cannot be used directly because configurations within a
family differ in ways that affect what a review claims.

All three follow from a single modelling decision. `video → ONE laptop_id` is
wrong on three axes at once:

1. **Cardinality.** A versus or round-up video covers several machines; the
   schema cannot express it, and `video_id UNIQUE` forecloses it.
2. **Granularity.** The link is made at configuration level, but most of what a
   reviewer says is not configuration-specific.
3. **Unit.** The link is made per video, but a video contains claims of
   different scope. "The hinge is solid and the panel is 100% DCI-P3" is true of
   every configuration sharing that chassis. "62 FPS in Cyberpunk at 1440p" is
   true only of the configuration tested.

Consequence today: sibling configurations get no review evidence at all, which
is the mechanical cause of coverage sitting near 2%.

The tie data adds a fourth point. `MATCH_THRESHOLD = 73` is not a parameter to
tune. At a median top-1 score of 55 with 89.4% unbroken ties, no cut point on
that distribution separates correct from incorrect matches, because the score is
not measuring correctness — it is measuring string overlap between a title and a
product name that encodes specifications the title never mentions. This is the
third instance of the same pattern in this codebase: benchmark mismatches
scoring 0.855 and 1.0 (ADR-0010), `price_rm = 0` meaning both "free" and
"unknown", and now this.

## Decision

**Split the single foreign key into an explicit link table, and record the scope
of each claim on the chunk.**

### 1. `review_laptop_link` replaces `matched_laptop_id`

```
review_laptop_link
  id                uuid pk
  raw_review_id     uuid fk -> raw_youtube_reviews.id   not null, indexed
  family_id         uuid fk -> laptop_family.id         not null, indexed
  laptop_id         uuid fk -> laptops.id               NULL = tested config unknown
  match_source      enum(auto, human)                   not null
  match_confidence  float                               NULL for human matches
  tie_width         int                                 candidates tied at rank 1
  created_at        timestamptz
  unique (raw_review_id, family_id, laptop_id)
```

- `family_id` is **required**, `laptop_id` is **nullable**. This is the schema
  expression of the two-stage design in ADR-0013: a video always resolves to a
  product line, and resolves to a configuration only when evidence exists.
  "Which configuration was tested" has no answer for many videos, and NULL is
  the honest representation of that — not a guess.
- `match_source` separates a human decision from a machine one. Today
  `manual_match` writes `match_confidence = 100.0`, which is indistinguishable
  from a perfect fuzzy score. Confidence is a property of the matcher, not of
  truth; a human match carries `match_source = human` and `match_confidence = NULL`.
- `tie_width` persists the signal the audit had to reconstruct. With 89.4% of
  matches tied, tie width is the escalation signal that score is not: a 2-wide
  tie is a fast human decision, a 14-wide tie is not, and the queue should be
  able to sort on it.

`raw_youtube_reviews.video_id` stays `UNIQUE` — one row per video is still
correct. Cardinality lives in the link table.

### 2. Chunks carry a scope

```
laptop_review_chunks
  laptop_id   uuid fk -> laptops.id          becomes NULLABLE
  family_id   uuid fk -> laptop_family.id    new, not null
  scope       enum(family, config)           new, not null
```

`scope` is assigned per chunk by the existing Gemini call in
`processor.process_raw_review`, which already produces a paraphrase and a
sentiment tag. Adding one output field costs no additional quota — which matters,
because Gemini quota at 7 RPM is the binding constraint across the whole project.

The assignment rule is a 2×2 over (claim class × whether the tested config is known):

| | Chassis-level claim | Performance-level claim |
|---|---|---|
| **Tested config known** | `family` | `config` |
| **Tested config unknown** | `family` | `config`, held unpublished |

The governing principle: **breadth requires evidence, narrowness is safe.**
Attributing an RTX 4060's frame rates to its RTX 5070 sibling is a false claim.
Attributing a keyboard opinion to the whole product line is simply true. So
chunks fan out only where the claim class licenses it, and a performance claim
from an unidentified configuration is withheld rather than widened.

### 3. Read path

`get_review_evidence(laptop_id)` becomes the union:

```
scope = 'config' AND laptop_id = :id
OR
scope = 'family' AND family_id = (SELECT family_id FROM laptops WHERE id = :id)
```

No chunk rows are duplicated across family members. Fanning out by writing one
row per member would duplicate a 768-dimension vector up to 14 times per chunk
for the largest families, and would break `aggregate_for_laptop`'s deduplication
and `review_count`.

The scope must be surfaced in the citation UI — "reviewer tested the RTX 5060
configuration" versus "applies to the TUF F16 line" — so the user can discount
appropriately. This is the same posture as the PickScore XAI badges: the system
states what it knows and how it knows it.

## Not chosen

**Keep one FK, point it at the family instead of the laptop.** Simpler, and it
would fix coverage immediately. Rejected because it makes every performance
claim family-wide, which is precisely the inaccuracy raised as symptom three.
Coverage would rise while correctness fell, and the failure would be invisible —
a fabricated frame rate reads exactly like a real one.

**Keep one FK and raise recall by tuning `MATCH_THRESHOLD`.** Rejected on the
measurement: 89.4% ties and a median score of 55 mean no threshold separates the
classes. Lowering it converts arbitrary rejections into arbitrary acceptances.

**Fan out by duplicating chunk rows per family member.** Rejected for embedding
storage and for corrupting aggregation, as above.

**Scope at the video level rather than the chunk level.** Simpler to assign, but
wrong: a single review video contains both claim classes, usually within a few
minutes of each other. Video-level scope forces a whole review to be either
over-broad or under-informative.

**A three-state review flag on each link (approved / rejected / needs-review).**
Considered and dropped as over-engineering. `match_source` plus NULL confidence
already distinguishes what needs distinguishing.

## Consequences

**Positive**

- Sibling configurations gain review evidence for chassis-level claims, which is
  the direct fix for ~2% coverage.
- The 65.8% of ties that are within-family stop being failures: they resolve
  structurally to `laptop_id = NULL` at family scope, with no human involvement.
  Only the 34.2% cross-family ties are genuine escalations.
- Human review moves to the question a human is actually needed for. Confirming
  a product line is one click; identifying a configuration becomes optional
  rather than a dead end.
- Comparison and round-up videos become representable for the first time.

**Negative / costs**

- Migration touches two tables and a backfill. `laptop_review_chunks.laptop_id`
  must drop NOT NULL, existing chunks backfill to `scope = 'config'` with
  `family_id` derived from their laptop, and existing `matched_laptop_id` values
  backfill into `review_laptop_link` as `match_source = auto` — except the 10
  rows at `match_confidence = 100.0`, which are human matches and backfill as
  `match_source = human, match_confidence = NULL`.
- `matched_laptop_id` cannot be dropped in the same migration without breaking
  running code. Two phases: add the link table and backfill; drop the column in a
  later migration once all readers are cut over.
- `aggregate_for_laptop` must be rewritten against the union query, and its
  `review_count` semantics change — a laptop's count now includes family-scoped
  videos it did not individually appear in.
- `scope` is an LLM judgement and therefore unverified by construction. The
  recurring lesson in this codebase is that the defence and the detector must be
  separate: every fix that looked applied was only proven by an independent
  audit. A deterministic `audit_review_scope.py` must ship with this change,
  sampling family-scoped chunks and asserting none contain frame rates,
  temperatures or battery hours — the exact failure mode that would be
  embarrassing in production.

**Neutral**

- The `LaptopReviewChunk` table already carries a per-chunk `laptop_id`, so it
  was already at the right grain; `processor.py` flattened it back by writing
  `raw.matched_laptop_id` to every chunk. This ADR restores the grain the schema
  already had.

## Open questions deferred

- Whether `laptop_review_summary` remains keyed on `laptop_id` or gains a
  family-level sibling. Deferred until the union read path is measured.
- `YoutubeChannel.trust_tier` is written by the admin API and read nowhere. It is
  the natural ordering key for `aggregate_for_laptop`, which currently has no
  ORDER BY and returns "top 5" in database order. Whether trust tier should first
  be split into separate evidence-quality, market-relevance and language fields
  is a smaller decision handled in the remediation pass.
- Round-up videos linking to many families will make `tie_width` less meaningful.
  Revisit if they become common.