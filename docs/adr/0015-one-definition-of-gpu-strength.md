# ADR-0015 — One definition of GPU strength: the reranker defers to PickScore

- **Status:** Accepted
- **Date:** 2026-09-14
- **Related:** ADR-0006 (PickScore positioning), ADR-0007 (brand as a soft
  preference), ADR-0010 (benchmark resolution), ADR-0011 (percentile
  normalization)

## Context

`app/rag/reranker.py::_purpose_bonus` awarded +0.04 when a laptop's `gpu_model`
string contained one of a per-purpose keyword list:

```python
_PURPOSE_GPU_SIGNALS = {
    "Gaming":   ["rtx", "rx", "rog"],
    "Creative": ["rtx", "rx", "radeon"],
}
```

The Creative half had never executed in production — `_normalize_purpose`
coerced the questionnaire's `Creative Work` to `Office` — so unifying the
purpose labels was about to make it live to real users. Pinning it with tests
first is what surfaced what it actually does.

### What the branch measures

Not GPU strength. A substring of the marketing name.

| GPU string | `gpu_mark` | bonus |
|---|---|---|
| NVIDIA GeForce RTX 5090 Laptop GPU | 28248 | +0.04 |
| AMD Radeon 610M | 1299 | +0.04 |
| Apple M5 Max 40-Core GPU | 22465 (via equivalence) | **0.00** |
| Intel Arc Graphics | resolved via the CPU's iGPU | 0.00 |

A 21× performance gap earns the same bonus, and Apple earns nothing at all,
because Apple's strings are core counts (`40-Core GPU`) containing none of the
three keywords.

### Why that matters beyond being crude

The catalog already has a component whose job is judging GPU strength, and it
disagrees. PickScore resolves the real PassMark mark, routes anchorless strings
through `_INTEGRATED_GPU_BY_CPU`, maps Apple through `_APPLE_GPU_EQUIVALENT`,
and normalizes by percentile against the catalog (ADR-0010, ADR-0011). It ranks
MacBook Pro at the top of `creative_work` — precisely the machine the reranker
branch pays nothing.

Two independent definitions of the same property, one of them string-based and
wrong, applied at two different stages of one pipeline.

## Decision

Delete `_PURPOSE_GPU_SIGNALS` and the branch that reads it, for **all** purposes
— not only Creative. GPU strength is PickScore's judgement; reranking defers to
it rather than voting alongside it with worse evidence.

`_PURPOSE_CPU_SIGNALS` stays. It has the same structural weakness — `i7` is a
substring, not a measurement — but there is no downstream component reranking
could defer to in the same way, so removing it is a separate decision on its own
evidence, not a corollary of this one.

## Consequences

**Gaming and Creative now have no reranking effect at all.** Neither has a CPU
signal set, so with the GPU half gone their purpose bonus is always 0.00 and the
reranker treats those users exactly like a user who stated no purpose.
Per-purpose `_purpose_bonus` ceilings after the change:

| Purpose | before | after |
|---|---|---|
| Gaming | 0.04 | **0.00** |
| Creative | 0.04 | **0.00** |
| Programming | 0.04 | 0.04 |
| Office | 0.04 | 0.04 |

This is acceptable because purpose still reaches PickScore's `PURPOSE_MODIFIERS`
(Gaming → GPU ×1.3, Creative → GPU ×1.3 / RAM ×1.2), which is where a GPU-heavy
use case is now expressed, on resolved marks. It is worth stating plainly rather
than discovering later: for two of five purposes, the reranker is now
purpose-blind.

**The brand bonus is untouched and is now the largest lever in the reranker.**
`bonus = p_bonus + br_bonus` puts brand outside the +0.08 purpose cap, so the
real ceiling is 0.13 and a non-preferred brand still costs −0.25. That sits
uneasily against ADR-0007's framing of brand as a soft preference, and now that
purpose contributes less it is the dominant additive term. Flagged, not changed
— it needs its own decision.

**Tests.** Nine tests describing the deleted branch were deleted with it. Three
replaced them: a recording stub asserting `_purpose_bonus` never reads
`gpu_model` (a value assertion cannot distinguish "does not use it" from "uses
it and happens to agree here"), one pinning the surviving CPU values, and one
recording the 0.00 ceiling for Gaming and Creative.
