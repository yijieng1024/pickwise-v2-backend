# ADR-0010: Resolve benchmarks by structure, not by confidence

- **Status:** Accepted
- **Date:** 2026-08-17
- **Related:** ADR-0004 (gating threshold 0.53), ADR-0006 (PickScore positioning),
  ADR-0009 (laptop status), ADR-0011 (normalization curve)

## Context

PickScore's `cpu` and `gpu` factors are the only two that come from outside the
catalog. Every other factor reads a column; these two take a manufacturer's
marketing string and try to find the matching row in a PassMark table of 2,832
GPUs and comparable CPUs. `resolve_benchmark` did that with
`process.extractOne(key, names, scorer=fuzz.WRatio)` and accepted the result if
its confidence cleared `CONFIDENCE_THRESHOLD`.

ADR-0006 raised that threshold from 0.6 to 0.85 and treated the matter as
closed. It was not. An audit across all 238 active laptops — 53 distinct GPU
strings, 98 distinct CPU strings — found four separate defects, and **not one of
them was detectable from a confidence score.**

### 1. Both sides of the comparison were not comparable

`_normalize` lowercased the query. The candidate list came straight from the
benchmark table, which is mixed case. RapidFuzz's `WRatio` does no
preprocessing of its own, so every comparison in the system was case-mismatched.
The same pair scores 95–100 with consistent casing and 0.64–0.675 without.

Every lookup ran at depressed confidence. Only parts with no close competitor
survived it intact.

### 2. A fix that was written but never ran

ADR-0006 identified the word "Processor" — the catalog writes `Intel Core 5
Processor 210H` where PassMark writes `Intel Core 5 210H` — and a strip was
added:

```python
s = re.sub(r"\s+", " ", s)
s = re.sub(r"\bprocessor\b", " ", s)
return s.strip().lower()
```

The substitution runs **before** `.lower()` and carries no `re.IGNORECASE`, so
it matched only a lowercase spelling that the catalog never produces. Fifty-one
CPU rows kept resolving to the wrong benchmark for two days after the fix was
believed applied. `Intel Core 5 Processor 210H` returned roughly 7,705 instead
of 18,294 — and at 0.855 confidence, above the threshold, flagged
`is_proxy: False`.

The whitespace collapse also ran before the substitution, so even with the case
corrected, removing the word would have left a double space in the key.

### 3. Marketing names carry no model number

Seventeen distinct GPU strings, covering about 138 of 238 laptops (58%),
contain no token that identifies a specific part: `AMD Radeon Graphics`,
`Intel Graphics`, `Qualcomm Adreno GPU`, Apple's `10-core GPU` through
`40-core GPU`. Fuzzy matching them does not fail loudly; it lands on whatever
row looks nearest.

| Catalog string | Resolved to | Confidence | Laptops |
|---|---|---|---|
| `10/16/20/32/40-core GPU` | 4 — a pre-2005 row | 0.855 | 15 |
| `AMD Radeon Graphics` | 75 | 0.855 | 21 |
| `AMD Radeon™ Graphics` | 75 | 0.855 | 4 |
| `Radeon Graphics` | 2,216 | 0.9 | 1 |
| `Intel Arc Graphics` | 3,211 | 0.95 | 13 |

The fourth and second rows are the same silicon family described two ways, and
they score **thirty times apart**. The score depended on what the scraper
happened to copy, not on the hardware.

Two further details matter. The table contains a row named literally `Intel
Graphics` at 3,211, so the 44 laptops writing that generic string matched it
exactly at confidence 1.0 — while the 13 writing `Intel Arc Graphics` were
dragged *down* onto the same row, when real Arc parts run 4,496–8,990. And the
Apple strings never reach resolution in scoring, because `_score_gpu`
short-circuits Apple to the CPU proxy — but `get_laptop_ranges` has no such
branch, so those mark-4 matches were setting the `gpu_mark` floor for every
non-Apple laptop in the catalog.

### 4. The matcher was right and the string was wrong

Seven catalog strings name desktop parts. All fifteen affected laptops are
Acer, whose source writes `GeForce RTX 5060` where ASUS writes `GeForce RTX
5060 Laptop GPU`.

| String | Resolved | Correct laptop row |
|---|---|---|
| GeForce RTX 3050 | 10,745 | 9,503 (4GB) |
| GeForce RTX 4050 | 7,711 | 14,245 |
| GeForce RTX 5050 | 17,001 | 14,176 |
| GeForce RTX 5060 | 20,722 | 16,785 |
| GeForce RTX 5070 | 28,703 | 19,146 |
| GeForce RTX 5080 | 35,672 | 26,326 |
| GeForce RTX 5090 | 38,963 | 28,248 |

An Acer Predator and an ASUS TUF carrying the same RTX 5060 scored 23% apart on
brand string conventions alone. There is no desktop RTX 4050 at all, so 7,711
was a match onto some unrelated row.

The RTX 5090 case had a second effect: at 38,963 it was the `gpu_mark` ceiling,
and therefore the denominator for all 100 discrete laptops. A real RTX 3050 was
scoring 25.1 out of 100 because it was being measured against a desktop
flagship.

### Why the threshold could never have worked

The confidence scores of the four worst mismatches in the catalog:

| Mismatch | Confidence |
|---|---|
| `Intel Core 5 Processor 210H` → wrong CPU | 0.855 |
| `AMD Radeon Graphics` → mark 75 | 0.855 |
| `Intel Graphics` → generic 3,211 row | 1.0 |
| `GeForce RTX 5060` → desktop card | 1.0 |

They sit at and above the threshold, two of them at the maximum. Meanwhile the
0.85 threshold was already rejecting only two genuine misses catalog-wide, and
raising it to 0.90 would have pushed 21–24% of real parts into the fallback.

There is no cut point on this distribution that separates right from wrong,
because **confidence measures string similarity and the failures are not string
failures.** ADR-0006 rejected the move to 0.90 on distribution grounds and was
right for the wrong reason.

## Options considered

1. **Keep tuning the threshold**, possibly per-category (CPU vs GPU).
2. **Curate the benchmark table** — delete the pre-2005 tail so nothing can
   land there.
3. **Patch `laptops.gpu_model` in the database** so the strings say what the
   hardware is.
4. **Resolve structurally**: ask what a string can identify before asking what
   it resembles, and supply the missing identity from a source that has it.

## Decision

**Option 4**, in four mechanisms.

**Normalization order.** Strip trademark, superscript and curly-quote
characters *first*, then apply NFKD, then `.lower()`, then the word strip, then
collapse whitespace. The order is load-bearing at both ends: U+2122 has a
compatibility decomposition to the letters "TM", so folding before stripping
turns `RTX™` into `RTXTM`; and collapsing whitespace before removing a word
leaves a double space behind. An early exit returns unresolved for the literal
placeholder strings (`unknown`, `n/a`, `none`, `-`). `CONFIDENCE_THRESHOLD`
stays at 0.85 — still useful for the residual case, no longer load-bearing.

**Anchor-token gate.** A model string with no token containing a digit and at
least three characters cannot identify a specific part. Such strings are not
fuzzy-matched at all. This is the structural question that replaces the
confidence question: *can this string, in principle, name one row?*

**`_INTEGRATED_GPU_BY_CPU`.** The anchor gate on its own would be worse than
the bug — 138 laptops into a 50.0 fallback that outranks real discrete GPUs. An
integrated GPU is fused to its CPU, and CPU strings always carry a model
number, so the CPU is the reliable key. The map holds 47 entries and covers
every anchorless laptop in the catalog; keys are matched longest-first so
`ryzen ai 7 350` wins over `ryzen 7`. Values must name rows that exist in the
GPU table, verified by `audit_integrated_gpu.py --validate-only`, which resolves
each value and reports its confidence — all 47 are at 1.0.

The GPU string alone is *not* the key, and this was the correction that shaped
the whole mechanism: `AMD Radeon Graphics` spans Ryzen 5 150, Ryzen AI 7 350
and Ryzen AI 5 330, whose integrated GPUs are three different parts. Any map
built on the GPU string would have been a single wrong answer for three
machines.

**Read-time laptop-variant rewrite.** For a string that names a part with no
"Laptop GPU" suffix, if a laptop row exists whose name is exactly that string
plus the suffix, resolve the laptop row instead. Exact match after suffix
removal, not fuzzy: `rtx 5070` must not win `rtx 5070 ti laptop gpu`, and a
fuzzy comparison here would reintroduce the ambiguity the rule exists to
remove. A hand-verified `_GPU_VARIANT_OVERRIDES` handles models whose laptop
variants differ by VRAM.

Two audit scripts are part of this decision, not tooling around it.
`audit_benchmark_matches.py` classifies every catalog string into resolved-below-floor,
no-anchor-token, and accepted-at-0.85–0.90, and with `--rewrites` shows what the
variant rule changes before it is trusted. `audit_integrated_gpu.py` emits the
CPU worklist ordered by laptop count and validates existing map values.

### Why read-time rather than a DB patch

Option 3 was the first instinct and it fails on one property: the scraper
overwrites `gpu_model` on the next ingest, silently, and the scores return to
where they were with nothing to indicate it. Acer's source will keep writing
bare desktop names because that is what Acer's pages say.

Resolving at read time is also what keeps `get_laptop_ranges` and the engine
agreeing. Both call `resolve_gpu_benchmark`, so the denominator and the
numerator are computed from the same rules by construction. A DB patch would
have to be re-applied before every range recalculation to hold that property.

Cost was measured rather than assumed: `_laptop_variant` scans the benchmark
list uncached at 1.2 ms per call, 91 ms across a full `generate-all`. Not worth
a cache.

### The two judgment calls

Both were settled on evidence, after "when uncertain, choose the lower value"
was proposed twice and superseded twice.

**Meteor Lake (Core Ultra 7 165H, Ultra 5 125H).** PassMark files the iGPU
under two unlabelled aggregates, `Intel Arc` (5,483) and `Intel Arc GPU`
(9,445), 2× apart. The two CPUs differ by one Xe-core — 8 versus 7, about 14%
— so those two rows cannot be 165H versus 125H; they are some other pair.
The map settles it internally: Arrow Lake H's Arc 130T is 6,122, and Meteor
Lake is a generation older, so its Arc must sit below that. 5,483 for both.
Giving two chips 14% apart the same value is a smaller error than choosing
wrongly between rows 72% apart.

**Ryzen 5 150.** AMD's product page does not name the iGPU on the CPU listing,
but the specification does: Zen 3+ (Rembrandt-R), 6C/12T, Radeon 660M, which
resolves to 3,142.

**RTX 3050.** The exact-match rule's default would have been the generic
`GeForce RTX 3050 Laptop GPU` at 12,003. The Acer Aspire 7 A715-59G-54Q6 is the
4GB SKU per two Malaysian retailers naming the exact model code, so 9,503 — the
rule's default would have been 26% high. This is the one place where a
principled rule produced a confidently wrong answer, and it is why the override
table exists.

## Consequences

**What this buys**

- Resolution failures now fail as "unknown" rather than as a plausible number.
  The four defects above all produced legal-looking scores; that is why they
  survived a threshold change, a code review and two days of use.
- The `gpu_mark` range is now bounded by parts that exist in laptops: 1,299
  (Radeon 610M) to 28,248 (RTX 5090 Laptop). Previously 4 to 38,963, both ends
  set by mismatches.
- Identical silicon now scores identically regardless of which manufacturer's
  page it was scraped from.
- Every anchorless laptop resolves. The 50.0 GPU fallback is unreachable for
  the current catalog.

**What it costs**

- A 47-entry hand-curated map that must be extended for every new CPU
  generation. It is verifiable — `--validate-only` catches a value that does
  not exist in the table — but not self-maintaining. A new CPU with an
  anchorless GPU string and no map entry silently takes the fallback.
- The override table is keyed on the GPU string, not the SKU. A second machine
  writing a bare `GeForce RTX 3050` with a 6GB part would be scored as 4GB. The
  collision audit should warn when one bare string appears under more than one
  model code.
- Every correction changed `ranges` and therefore every laptop's score, so
  `LaptopPickScore` had to be regenerated three times in two days. Any baseline
  or screenshot from before this ADR corresponds to nothing.

**Effect on the numbers**

| | Before | After |
|---|---|---|
| `cpu_mark` range | 393–72,912 | 767–62,748 |
| `gpu_mark` range | 81–38,963 | 1,299–28,248 |
| F16 gpu raw | 53.1 | 57.5 |
| Real RTX 3050 gpu raw | 25.1 | 30.4 |
| Unresolved GPU strings | 5 | 0 |

The use-case inversion ADR-0006 was chasing moved 11 → 13 → 14 → 11 across
these fixes and did not close. That is the finding this ADR hands to ADR-0011:
none of the resolution defects were causing it.

**Known gaps**

- `resolve_benchmark`'s module-level `_cache` is keyed on the normalized model
  string only, not on which benchmark table was passed, while the same function
  serves both CPU and GPU lookups. No collision has been observed; nothing
  prevents one.
- `flags` reports `gpu_score_is_proxy` and `price_unavailable` but has no flag
  for an unresolved benchmark, so a rejected match still cannot be
  distinguished downstream from a genuine mid-range score.
- The 50.0 fallback remains in `_score_gpu` for future ingests. On the current
  range it corresponds to a mark of about 14,774 — above a real RTX 3050
  (9,503), RTX 5050 (14,176) and RTX 4050 (14,245), and near the top of the
  integrated band, which spans 1,299–18,082. It is unreachable today and wrong
  if it is ever reached. A fallback bounded to the integrated band would be
  more defensible.
- The GPU table contains a generic row `GeForce GPU` (mark 1,153) that ties at
  100.0 under `token_set_ratio` against every GeForce query. Nothing in
  PickScore uses that scorer, but `matcher.py` does, for review titles.

## Not chosen, and why

**Per-category or further threshold tuning.** The four worst mismatches score
0.855, 0.855, 1.0 and 1.0. No cut point separates them from correct matches,
and the two at 1.0 are exactly correct as string comparisons. Tuning refines
the wrong measurement.

**Curating the benchmark table.** Deleting the pre-2005 tail would stop
`AMD Radeon Graphics` landing on mark 75 — and send it to the next-nearest row
instead, plausibly a mid-range part in the thousands. The error would stop
looking absurd, which is the only reason it was noticed: a floor of 4 stood out
in the range dump. Removing the evidence is not the same as removing the fault.

**Patching `gpu_model` in the database.** Overwritten by the next ingest, with
no signal that it happened, and it would have to be re-applied before every
range recalculation to keep the engine and the ranges consistent.

**Proxying integrated GPUs through the CPU score the way Apple rows are.** The
Apple branch returns `cpu_score` directly as the GPU score, which is defensible
for an ARM SoC where PassMark has no separate GPU entry. Applied to x86 it
would be badly wrong: a Ryzen AI 7 350 has a strong CPU and an integrated GPU
nowhere near a discrete part. The map resolves CPU → iGPU *name* and then looks
that name up normally, so the middle step is not skipped.

**Fuzzy-matching inside the laptop-variant rule.** The rule compares exactly
after removing the suffix. A fuzzy comparison would let `rtx 5070` match
`rtx 5070 ti laptop gpu` — a different part — which is the ambiguity the rule
exists to eliminate.

**Leaving generic strings unresolved, as ADR-0006 decided.** Reversed here.
Unresolved was never what actually happened: `AMD Radeon Graphics` resolved to
75 across 25 laptops and `Intel Arc Graphics` to a generic 3,211 across 13, so
"left unresolved" meant "resolved to something arbitrary". The precision does
exist — just not in the GPU string.