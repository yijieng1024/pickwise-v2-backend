# ADR-0011: Normalize by percentile rank, not by min-max

- **Status:** Accepted
- **Date:** 2026-08-17
- **Related:** ADR-0006 (PickScore positioning — this closes its outstanding
  diagnosis), ADR-0009 (laptop status), ADR-0010 (benchmark resolution)

## Context

A gaming laptop scores lowest on the Gaming preset and highest on Office &
Study, and the product page renders a "Best fit: Office & Study" badge from
that argmax.

ADR-0006 traced this through five layers and ADR-0010 removed four benchmark
resolution defects underneath it. The gap between Office & Study and Gaming on
the reference machine moved 11 → 13 → 14 → 11 across those fixes and did not
close. Every remaining explanation had been eliminated except one: the curve
that turns a specification into a 0–100 score.

`_normalize` is min-max over catalog-wide extremes. That is sensitive to
exactly two data points per factor, and for three of the six factors those two
points are not representative of anything.

Measured across the 238 active laptops, as the p10–p90 range of each factor's
resulting raw score — the "effective spread" ADR-0006 named but never
quantified:

| Factor | min-max spread | Why |
|---|---|---|
| price | **34.1** | inverted against an RM36,999 workstation, so the whole catalog looks cheap |
| ram_storage | **21.3** | 16GB measured against a 128GB machine; 1TB against 4TB |
| portability | **44.2** | 3.73 kg ceiling, but most laptops are 1.2–2.0 kg, so 2.2 kg reads as "mid" at 52 |
| cpu | 69.9 | bounds are real parts |
| gpu | 76.5 | bounds are real parts, after ADR-0010 |
| battery | 75.6 | ceiling is a physical and regulatory limit that machines actually reach |

The widest factor spreads 3.6× more than the narrowest. The presets weight all
eight as though they were comparable, so the preset leaning on the
near-saturated factors — Office & Study weights price 9, battery 8, portability
7 — wins by construction, while Gaming's heavy factors sit mid-scale or crushed.

The distinguishing property is not the spread number itself but **whether the
extremes are representative**. Price, RAM, storage and weight all have a long
tail: one workstation, one 4TB configuration, one 3.73 kg desktop replacement.
Battery and the two benchmark ranges do not, because their bounds are physical
or are real parts in the catalog.

## Method

Four curves were run over the live catalog by patching
`app.pickscore.engine._normalize`, which every `_score_*` funnels through,
rather than reimplementing scoring in a script. Reimplementation would have
risked diverging from the engine, which is precisely the class of bug the
preceding two days were spent removing. The patched function identifies which
factor it is scoring by the `(min, max)` pair it was handed; the script asserts
that pair is unique across factors.

Three things were read off each run: the effective spread per factor, the five
use-case scores for the reference machine, and the top-10 per use case compared
against today's list — because a curve that changes no rankings changes nothing
a user sees, whatever it does to the numbers.

### Results

Effective spread (p10..p90):

| Factor | min-max | percentile | clamp p5/p95 | log |
|---|---|---|---|---|
| price | 34.1 | 79.8 | 61.7 | 47.4 |
| cpu | 69.9 | 80.7 | 87.4 | 34.0 |
| gpu | 76.5 | 79.5 | 82.4 | 84.3 |
| ram_storage | 21.3 | 51.4 | 51.1 | 25.6 |
| portability | 44.2 | 77.5 | 70.0 | 47.8 |
| battery | 75.6 | 78.4 | **100.0** | 75.7 |

Reference machine (ASUS TUF Gaming F16, i7-14650HX / RTX 5060 Laptop /
16GB / 1TB / 2.2 kg / 90Wh / RM5,899):

| Use case | min-max | percentile | clamp | log |
|---|---|---|---|---|
| office_study | 62 | 57 | 62 | 65 |
| programming | 54 | 59 | 56 | 65 |
| gaming | 51 | **62** | 53 | 69 |
| creative_work | 51 | 61 | 52 | 67 |
| general_use | 58 | 56 | 58 | 63 |
| **office − gaming** | **+11** | **−5** | +9 | −4 |

Top-10 overlap with min-max: percentile 2/6/8/6/4 across the five use cases,
clamp 5/6/7/6/5, log 2/7/6/7/4.

### The deciding evidence

Neither the spread table nor the reference machine settles it on its own. What
does is the top of the Office & Study list.

| Rank | min-max | percentile |
|---|---|---|
| 1 | 16" MacBook Pro M5 Pro — RM12,499 | Zenbook A16 — RM6,599 |
| 2 | ProArt GoPro PX13 — RM13,599 | Zenbook S14 — RM7,699 |
| 3 | 16" MacBook Pro M5 Pro — RM15,199 | Zenbook 14 OLED — RM4,599 |
| 4 | 16" MacBook Pro M5 Max — RM18,499 | Swift Go 16 — RM6,899 |
| 5 | 16" MacBook Pro M5 Max — RM20,999 | Zenbook 14 OLED — RM4,599 |

"Best for office and study" currently returns four 16-inch MacBook Pros between
RM12,499 and RM20,999. Under percentile it returns thin-and-light ultrabooks
between RM4,599 and RM7,699. The second list is what the category means.
`general_use` improves the same way. `programming` returns MacBook Pro M5 Max
under both, which is defensible either way.

## Options considered

1. **Keep min-max.**
2. **Percentile rank** — score by position within the catalog's distribution.
3. **Clamped min-max** — min-max between p5 and p95 instead of the extremes.
4. **Log transform then min-max.**
5. **Percentile for the skewed factors only**, min-max elsewhere.

## Decision

**Option 2: percentile rank, for all six normalized factors.**

`get_laptop_ranges` gains a sorted value list per factor alongside the existing
min and max, and `_normalize` scores by position within it — the midpoint of
any tied block, so identical configurations score identically rather than
depending on sort order.

Two changes ship with it, because percentile alone would make one existing
defect worse.

**`_score_gpu`'s Apple branch must change.** It returns `cpu_score` directly as
the GPU score. Under min-max an Apple CPU landed mid-scale and the effect was
muted; under percentile a top-end CPU sits near the 95th percentile, so the
same number is counted twice — cpu at weight 8 and gpu at weight 10, half of
the Gaming preset. The simulation shows 16" MacBook Pro M5 Max taking Gaming
ranks 1 and 2 at score 77, above every ROG Strix SCAR 18. The public gaming
card would not show that, because `get_ranking_for_use_case` demotes proxy rows
for gaming — but `creative_work` has no such demotion and weights gpu 9, and
its top two are the same machines for the same wrong reason. Either the proxy
must stop being a GPU score, or the demotion must follow the gpu weight rather
than a hardcoded use-case name.

**The score is presented as a percentile.** "Better than 62% of the catalog for
gaming", not "62 out of 100". Under this curve that is what the number means,
and stating it removes the main objection to the curve rather than hiding it.

### On the relativity objection

Percentile makes a score relative to the current catalog: adding or suspending
a laptop moves every other laptop's number. ADR-0006 positions PickScore as a
buy indicator, and the answer to "should I buy this one" should not change
because a competitor was listed.

This was overstated when it was first raised, and the arithmetic is why. Across
238 laptops, one addition moves any percentile by at most 0.4 points. Moving a
score by a visible amount takes a change of tens of machines — and a catalog
that has genuinely shifted by tens of machines is one where "is this a lot of
storage" has a different answer than it did before.

That is the deeper point. Whether 512GB is generous, whether RM5,899 is
expensive, whether 2.2 kg is heavy — these are comparative questions, and they
were always being answered comparatively. Min-max answered them against two
outliers instead of against the market. It was not more absolute; it was
absolute with respect to a worse reference.

Battery and the two benchmark factors keep their physical meaning under
min-max, which is why option 5 was attractive. It was rejected on measurement:
see below.

## Consequences

**What this buys**

- The inversion closes. On the reference machine Gaming becomes the highest of
  the five (62) and Office & Study the lowest but one (57), and the spread
  across presets is 6 points rather than 11 in the wrong direction.
- Every factor spreads 51–81 instead of 21–77, so the preset weights finally
  apply to comparable quantities. That is the whole of ADR-0006's diagnosis.
- Use-case lists start returning the kind of machine the use case names.

**What it costs**

- `laptop_pick_scores` must be regenerated, and prior scores and screenshots
  correspond to nothing. This is the fourth such regeneration in three days.
- Scores now drift with catalog composition, so regeneration cadence matters in
  a way it did not before: previously only a change to an extreme moved other
  laptops' scores, now any addition or removal moves them slightly. The
  staleness counter proposed in ADR-0009 becomes more useful, not less.
- `get_laptop_ranges` must cache seven sorted lists of 238 floats rather than
  seven pairs. Trivial in size, but the cache key and its 300-second TTL now
  govern more state.
- The absolute number becomes harder to explain without the percentile
  framing. "72" meant something loosely intuitive before; it now means a rank,
  and the UI has to say so.

**Known gaps**

- The "Best fit" badge is an argmax over five scores that span about 6 points
  under this curve and 11 under the old one. An argmax over near-equal numbers
  is unstable by construction: small catalog changes will flip the badge even
  when nothing about the machine changed. Either it should require a margin
  over the runner-up, or the five bars should be shown without a winner. This
  is not a normalization problem and is not fixed here.
- `_score_price` in personalized mode never reaches `_normalize` at all: within
  a stated budget it returns a flat 100.0, which is zero discrimination, and
  above it decays by `DECAY_K`. So personalized and general mode now use two
  entirely different price curves. This needs its own decision.
- `screen_size` and `brand` return a constant 50 in general mode. They
  contribute no discrimination and only dilute the other factors — 6 of 43
  weight in `general_use`, about 14%, spent on two constants.
- The simulation ranks by raw score and does not apply
  `get_ranking_for_use_case`'s proxy demotion, so its gaming list is not what
  the endpoint would render. The creative_work list is, since that use case has
  no demotion.

## Not chosen, and why

**Min-max (option 1).** Sensitive to two data points per factor, three of which
are outliers. This is the diagnosed cause.

**Clamped min-max (option 3).** Moved the gap from +11 to +9 — it does not fix
the problem. It also broke battery outright: p10 and p90 of the resulting
scores are 0.0 and 100.0, because many laptops tie at the physical maximum, so
a p5/p95 window pins both tails. Clamping is unsafe on any factor whose values
cluster at a bound, and it needs a window parameter that would require its own
justification.

**Log transform (option 4).** Closed the gap (−4) but by lifting everything:
the reference machine scores 63–69 across all five presets. It crushed cpu to a
34.0 spread, worse than min-max, and barely helped ram_storage (25.6), whose
values are a handful of discrete powers of two rather than a continuum.

**Percentile on the skewed factors only (option 5).** This was the preferred
option before it was measured, because it confines relativity to the factors
that need it. Two runs:

| | office − gaming | top-10 overlap |
|---|---|---|
| price + ram + storage | +5 | 7/7/9/7/6 |
| ...plus weight | **0** | 6/6/9/6/6 |

The first leaves the badge wrong. The second closes the gap to exactly zero,
but by flattening: the reference machine scores 52–54 across all five presets,
so the presets stop distinguishing anything rather than ranking correctly. Gap
size was the wrong success criterion. The right one is whether a machine scores
highest on its own use case, and only full percentile achieves that (Gaming 62,
six points clear of General Use at 56).