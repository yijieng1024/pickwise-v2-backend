# ADR-0016 — What an unresolved benchmark means

- **Status:** Accepted
- **Date:** 2026-09-15
- **Related:** ADR-0010 (benchmark resolution — *how* matching works; this ADR
  is about what its failure *means*), ADR-0006 (PickScore positioning),
  ADR-0009 (a status column with two kinds of hidden), ADR-0011 (normalization)

## Context

`_score_cpu` and `_score_gpu` fall back to `50.0` when a benchmark cannot be
resolved. On the shipped percentile curve, 50.0 is also a perfectly ordinary
score — the middle of the catalog. So a laptop whose CPU could not be
identified and a laptop that is genuinely average produced **the same number,
with nothing anywhere able to tell them apart**.

This is the same defect as `price_rm = 0` meaning both "free" and "unknown",
one layer up. That one was already fixed: `_score_price` returns 50.0 **with a
reason string**, so a reader of the breakdown can see the factor was skipped.
CPU and GPU never got the same treatment — `_score_cpu` returned a bare float
with nowhere to put a reason, and `_score_gpu`'s tuple carried `is_proxy` but
nothing meaning "did not resolve at all".

It stayed open for three rounds as a strict xfail
(`test_unresolved_benchmark_is_flagged`) because it read as a cosmetic gap.
What forced it was ADR-0010 Amendment II's CPU audit, which found the real
cost of leaving it open.

### The finding that made this urgent

Across 98 distinct `processor_model` strings over 238 active laptops, every
fuzzy match at **exactly 0.85** — the confidence floor — resolved to a wrong
part:

| catalog string | laptops | resolved to | mark |
|---|---|---|---|
| `Snapdragon X X1 26 100 Processor` (+ ® variant) | 7 | `Cobalt 100` | 8134 |
| `Snapdragon X2 Elite (18-core) X2E88100` (+ ® variant) | 3 | **`AMD Athlon 64 X2 4200+`** | **767** |
| `Snapdragon® X Elite X1E 78 100 Processor` | 1 | `Cobalt 100` | 8134 |
| `Snapdragon X Plus X1P 42 100 Processor` | 1 | `Cobalt 100` | 8134 |

A 2025 flagship ARM laptop chip scoring 767 — the catalog floor — because
`X2` matched `X2` in a desktop CPU from 2005.

The obvious remedy, raising the confidence threshold, **could not be applied
first**. Raising it to 0.90 gates those 12 wrong rows and also gates 9 correct
ones (`Apple M5 (10-core)` → `Apple M5 10 Core`, confidence 0.882). Without a
way to express "unresolved", that trade is: 12 visibly-wrong marks become 12
**invisibly**-wrong 50.0s, and 9 correct marks become 9 more. Strictly worse.

**The channel has to exist before the threshold can move.** That ordering is
the decision this ADR records as much as the states themselves.

## Options

**A — fabricated neutral (the status quo).** Return 50.0, say nothing. Cheapest,
and wrong in the specific way this project keeps rediscovering: it does not
fail, it lies quietly, and every downstream consumer treats the number as a
measurement because nothing marks it otherwise.

**B — flagged neutral.** Return 50.0 and say so, in the `flags` dict that
already carries `gpu_score_is_proxy` and `price_unavailable`. The score is
unchanged; what changes is that a reader can tell a fabricated neutral from a
real mid score. Exactly the shape `_score_price` already has.

**C — withhold.** Publish no score at all.

## Decision

**B as the baseline. C in the extreme case, where the extreme case is both CPU
and GPU unresolved.**

Two new flags, deliberately separate from `gpu_score_is_proxy`:

| flag | meaning |
|---|---|
| `cpu_benchmark_unresolved` | no CPU mark; the 50.0 is fabricated |
| `gpu_benchmark_unresolved` | no GPU mark; the 50.0 is fabricated |
| `gpu_score_is_proxy` | (existing) the mark is real but belongs to a stand-in part |
| `score_withheld` | both defining factors unresolved; `score` is `null` |

`gpu_score_is_proxy` and `gpu_benchmark_unresolved` answer different questions
and are kept apart on purpose. An Apple or integrated GPU **did** resolve, to a
real mark belonging to a stand-in part. An unresolved one resolved to nothing.
Every unresolved GPU is also flagged a proxy, so folding the two together would
have hidden a fabricated neutral behind a flag `get_ranking_for_use_case`
already demotes for unrelated reasons — a behaviour that looks deliberate.

### Why both-unresolved is the line for withholding

One unknown factor is defensible to neutralise: seven others still carry the
score. Both defining factors unknown is different arithmetic. In the `gaming`
preset, `cpu` 8 + `gpu` 10 = **18 of 36 total weight** — half the score —
resting on fabricated neutrals. A number built that way is not a score, and
publishing it as one is the problem, not the imprecision.

### Withheld must be explicit, not absent

The score is `None`, **never 0**. `0` means "scored, and badly"; `None` means
"not scored". A consumer treating falsy as absent conflates them, which is the
`price_rm = 0` failure exactly.

And a withheld score is a **row that exists with a null score**, not a missing
row:

| surface | behaviour |
|---|---|
| `generate_all_pick_scores` | writes the row, `score = NULL`, `score_withheld = true` |
| `GET /{id}/pick-scores` | returns it: null score, flag set, **full breakdown** |
| `get_ranking_for_use_case` | omits it — and only here |
| `search_laptops` (agent) | `pick_score: null` + `pick_score_unavailable` explaining why |

Skipping the row was rejected: a missing row and a withheld score look
identical to a caller, which is the ambiguity being fixed. It also leaves stale
rows behind — the failure already seen with non-active laptops keeping scores
computed against ranges that no longer exist.

Withholding the **total** does not withhold the **evidence**: the full
eight-factor breakdown is still returned, because a caller explaining "why is
there no score" needs the six factors that did resolve.

The ranking is the one place absence is correct. A ranking answers "what is
best"; a laptop nobody could score has no answer to that question. Its detail
view still does.

Migration `2d909406ade7` widens `laptop_pick_scores.score` to nullable. Its
downgrade deletes withheld rows first — a withheld score cannot be represented
in the old schema, and inventing a number for it is what this ADR forbids.

## Consequences

**Measured blast radius: zero.** No active laptop is both-CPU-and-GPU
unresolved today, and none is after the threshold raise — every gated
Snapdragon still resolves its Adreno through `_INTEGRATED_GPU_BY_CPU`, which is
keyed on the CPU *string*, not on the resolved mark. The withholding path is
built ahead of its need, which is the whole point: it is what makes the
threshold raise an improvement rather than a re-hiding.

**The threshold is now two constants** (see ADR-0010 Amendment II):
`CPU_CONFIDENCE_THRESHOLD = 0.90`, `GPU_CONFIDENCE_THRESHOLD = 0.85`.

**Nine Apple laptops lost a correct mark.** `Apple M5 (10-core)` resolves at
0.882 and is now gated, falling to a flagged 50.0 on the `cpu` factor. This is
accepted, not ignored: the loss is visible in the flags rather than hidden in
the number, and the obvious recovery — normalising Apple's `(n-core)` parens
the way the GPU side already does — is separate work with its own evidence.

**The frontend needs one field it does not read today.** To render "this factor
could not be evaluated", it consumes `flags` on the PickScore response:
`score_withheld` (boolean) decides whether to show a score at all, and
`cpu_benchmark_unresolved` / `gpu_benchmark_unresolved` decide whether to mark
an individual factor row as unevaluated rather than showing its 50. `score` is
now nullable and must be handled as such. The per-factor `note` already carries
human-readable text for `price`, `gpu` and now `cpu`. Not built here — separate
repo.

**Still open, deliberately.** No Qualcomm equivalence map. That is
hand-verified anchor work of the same kind as `_INTEGRATED_GPU_BY_CPU` and
`_APPLE_GPU_EQUIVALENT` — one source per entry — and it is a better decision to
make now that those 12 rows are honestly marked unresolved instead of silently
carrying an Athlon's mark.
