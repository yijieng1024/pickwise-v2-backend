# ADR-0007 — Brand is a soft preference on the agent path, not a hard filter

- **Status:** Accepted — with an open question on the penalty size (see
  Consequences)
- **Date:** Recorded 2026-10-11. The behaviour was in place by 2026-08-15,
  when it was first checked against the code; when it was introduced, and the
  reasoning at the time, were not written down.
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0004 (gating
  threshold), ADR-0006 (PickScore positioning), ADR-0015 (one definition of
  GPU strength — first flagged the penalty size)

## Context

When a user says "an Asus for programming under RM5,000", the brand can be
treated in two ways:

- **Hard:** only Asus laptops are retrieved. Nothing else can appear.
- **Soft:** every laptop is retrieved; Asus ones are pushed up, others pushed
  down.

The code base does both, in different places, and for a while its own
comments disagreed about which one the agent uses:

| Where | What it says or does |
|---|---|
| `search_laptops` docstring | brand is "a soft preference, not a hard filter" |
| `_run_search` | never passes `brand` to `retrieve_candidates`; turns it into `brand_preferences=[brand.lower()]` for the reranker |
| `retrieve_candidates` (`retrieval.py`) | has a `brand` parameter applied as a hard SQL filter, and calls it an "absolute constraint" — but no caller on the agent path ever passes it |
| `relaxation.py` | "Brand → NEVER auto-relaxed; must explicitly ask the user" — true only because there is nothing to relax |
| `POST /recommendations/laptops`, `POST /laptops/hybrid-search` | brand **is** a hard SQL filter |

When the code was traced on 2026-08-15 — while investigating what looked like
a bug where relaxation dropped the brand — it turned out brand had never been
a hard filter on the agent path at all. The docstring matched the code; the
comments elsewhere did not. This ADR records what the agent path does, so the
next reader does not repeat that investigation.

## Options considered

1. **Hard filter** in SQL, using the existing `retrieve_candidates(brand=…)`.
2. **Hard filter with relaxation**: filter by brand, and drop the filter as a
   relaxation step when nothing viable is found.
3. **Soft preference** in the reranker: a bonus for the named brand, a penalty
   for the others.
4. **Ignore brand** in retrieval and leave it to PickScore's brand factor and
   the agent's reply.

## Decision

**Option 3.** In `app/rag/reranker.py`:

```python
def _brand_bonus(brand_name, brand_preferences):
    if not brand_preferences or "no preference" in brand_preferences:
        return 0.0      # no brand named
    if brand_name.lower() in brand_preferences:
        return +0.05    # named brand
    return -0.25        # any other brand
```

The brand term is added to `final_score` alongside the purpose bonus, and sits
**outside** the purpose bonus's +0.08 cap.

The case for soft over hard, as it stands today (reconstructed — see Date):
the catalog is concentrated (under RM4,000 the ten largest families
alone account for about 64 rows, per ADR-0006), so a hard brand filter combined with
a budget can empty the candidate pool. And relaxation never relaxes brand by
design — a user's brand is treated as something only they can give up — so a
hard filter that empties the pool would have no way out except the generic
"general" gate message. A soft preference keeps the named brand on top when it
has a good match and lets the shortlist fill with other brands when it does
not.

## Consequences

**−0.25 is soft in name, close to hard in effect**

Relevant queries score 0.537–0.732 before any penalty (ADR-0004). Subtract
0.25 and every one of them lands at 0.29–0.48 — **below the 0.53 gate**. The
gate only reads the top candidate, so:

- If the named brand has at least one good match, it ranks first, the search
  passes, and other-brand laptops still fill the rest of the six slots. Soft,
  as intended.
- If the named brand has **no** good match — or the catalog does not carry it
  at all (a user asking for Dell or Lenovo, say) — every candidate takes the
  −0.25, the top score falls under the gate, and the whole search is gated.
  The user sees no alternatives at all.

So in the case where a soft preference matters most — "the brand you want has
nothing for you, here is what else there is" — the current numbers behave like
a hard filter.

**The gate message does not name the cause**

`_detect_bottleneck()` counts `penalty_reasons` only. The brand term lands in
`bonus_reasons` (it is a negative bonus, not a multiplier penalty), so a search
gated purely by brand is reported as `bottleneck: "general"` and the user is
asked which requirement they are flexible on — not told that the brand is the
problem.

**The brand term is now the biggest lever in the reranker**

Since ADR-0015 removed the GPU keyword bonus, purpose contributes at most
+0.04 for two purposes and 0.00 for Gaming and Creative Work. Brand ranges
from +0.05 to −0.25 — a 0.30 swing, against a 0.04 one. The reranker's ranking
is now shaped more by the brand name than by what the user needs the laptop
for.

**Two brand mechanisms, two sources**

The reranker's brand comes from the tool argument the model fills in for this
turn. PickScore's brand factor (100 if preferred, else 50) comes from the
user's saved questionnaire profile. A signed-in user who saved "Asus" and then
asks for "a MacBook" gets the reranker favouring Apple and PickScore favouring
Asus, on the same results.

**Tests**

The Tier 5 suite pins the decision: a recording stub asserts `_run_search`
never passes `brand` to `retrieve_candidates`, that it reaches the reranker as
`brand_preferences=["asus"]`, and that a wrong-brand laptop stays in the
ranked list.

**When this stops holding**

- If users treat brand as non-negotiable ("only Apple"), a soft preference
  will keep showing them machines they will never buy. That is a product
  question the eval set does not currently ask.
- If the catalog widens to many brands with deep ranges each, the pool-emptying
  risk that argues against a hard filter shrinks.

**Open — not decided here**

Whether the penalty should be smaller (so a brand with no good match yields
alternatives instead of a gate), whether brand should move into
`penalty_reasons` so the gate can say "nothing from that brand fits", and
whether the agent path should agree with `/recommendations/laptops` at all.
Each needs its own measurement; none is changed by this record.

## Not chosen, and why

As noted above, the reasoning at the time was not recorded; the following is
reconstructed from the code and the catalog's shape.

**Hard filter (option 1).** It exists in `retrieve_candidates` and is unused
on the agent path. Combined with the catalog's concentration and a budget, it
would empty the pool, and relaxation is designed never to relax brand.

**Hard filter with brand as a relaxation step (option 2).** It would silently
replace the user's brand with another one, which is exactly what
`relaxation.py` says must only happen after asking. Doing it as a soft
preference makes the trade visible in the ranking instead.

**Ignoring brand in retrieval (option 4).** The user named it. A shortlist that
ignores a stated brand reads as not having listened. PickScore's brand factor
cannot stand in for it: it only scores laptops that were already retrieved and
kept by the gate, and the shortlist is ordered by the reranker's score, not
PickScore (ADR-0006), so it never changes which laptops are shown.
