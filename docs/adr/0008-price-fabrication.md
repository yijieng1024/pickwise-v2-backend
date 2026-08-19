# ADR-0008: Price fabrication — a prompt guard and a deterministic check are not alternatives

- **Status:** Accepted
- **Date:** 2026-08-17
- **Related:** ADR-0005 (LLM-as-judge), ADR-0006 (PickScore positioning),
  ADR-0009 (laptop status), ADR-0010 (benchmark resolution)

## Context

The agent states Malaysian market prices it never retrieved.

The first run of a new evaluation check caught `ds_zh_003` inventing a retail
range of about RM2,300 for a machine, with no tool call behind it. That looked
like one bad generation. It was not: after the check was tightened it flags 4
of 20 turns, and the flagged figures are not random noise — in `multi_zh_001`
the fabricated pair 4000 / 5500 reproduces across three separate runs while the
other flagged numbers in the same response vary. The model holds a stable
prior about Malaysian laptop prices and states it as fact.

This is the most damaging failure mode this product has, for three reasons.

**It is the one claim users act on.** Price is the highest-weighted PickScore
factor in two of the five use-case presets and the first thing a buyer checks.
A wrong specification is an annoyance; a wrong price sends someone to a shop.

**It is invisible.** A fabricated RM4,199 is indistinguishable in form from a
retrieved one. There is no hedge, no uncertainty marker, nothing in the
response that separates the two.

**The existing judge cannot see it.** ADR-0005 established an LLM-as-judge, and
its prompt forbids it from using its own product knowledge — deliberately, so
it grades against tool output rather than against its training data. That
choice makes it structurally incapable of catching a fabricated price: it has
no way to know the figure is wrong, and it is the same model as the agent
(`gemma-4-31b-it`), so it shares the prior that produced the number. A judge
asked "is this response accurate?" will read a confident price as accurate.

There was a data-side pressure feeding this. 146 of 303 catalog rows carried
`price_rm = 0`, so the agent was routinely asked about machines whose price the
system did not hold. ADR-0009 has since closed that: every active laptop
carries a price. The pressure is gone; the behaviour is not, because it was
never only a response to missing data.

## Options considered

1. **Strengthen the system prompt.** Tell the agent it may only state prices
   that appear in tool output.
2. **Strengthen the judge.** Add a rubric item asking whether prices are
   grounded.
3. **Check deterministically.** Extract price figures from the response and
   verify each against the tool output for that conversation.
4. **Remove the capability.** Strip prices from the agent's context and render
   them only in the UI from the database.

## Decision

**Options 1 and 3, as two layers with different jobs.**

The filename this ADR was created under — `prompt-vs-deterministic` — framed
these as a choice. They are not. One is a defence and the other is a detector,
and they sit at different points in the system:

| | Where it runs | What it does | What it cannot do |
|---|---|---|---|
| `PRICE CLAIMS` prompt block | Agent system prompt, production | Stops the model asserting prices it did not retrieve | Prove that it worked |
| `ungrounded_prices()` | Evaluation harness | Detects an assertion that got through | Prevent anything |

A defence with no independent detector is an assumption. This project has
already paid for that lesson twice: the `\bprocessor\b` strip in ADR-0010 was
written, committed, believed applied, and inert for two days because nothing
independently checked its output; and the Apple GPU strings were poisoning the
normalization ranges even though the engine itself branched around them. In
both cases the fix looked correct in the code and was wrong in the data, and
only a separate audit surfaced it.

The same reasoning applies here with more force, because a prompt guard has no
compile step and no test that fails when it stops working. A model update, a
prompt edit, or a context-window truncation can silently disable it.

### The detector

`ungrounded_prices()` extracts RM-prefixed figures from the response and
matches each against every bare number appearing in the cumulative tool output
for that conversation, with a 1% tolerance and an RM1 floor. Anything unmatched
is reported. It runs unconditionally in every evaluation category, not only in
the price-related ones, because fabrication is not confined to questions about
price.

Three properties are deliberate.

**It matches against bare numbers, not against a parsed price field.** A tool
returns `price_rm: 5899`; the agent may write "RM5,899", "RM 5899" or
"5,899 ringgit". Comparing numerically rather than textually removes an entire
class of formatting false positives.

**The 1% tolerance** allows rounding ("about RM5,900") without allowing
invention. The RM1 floor stops trivial figures matching by coincidence.

**Two false-positive classes were removed after the first run**, both of which
would have made the check untrustworthy:

- *Figures the user stated.* "I have RM4,000 to spend" appearing in the
  agent's reply is a restatement, not a claim.
- *Figures grounded in an earlier turn.* The check accumulates tool output
  across the conversation rather than examining a turn in isolation, because
  an agent answering from the existing shortlist pool legitimately cites
  prices it retrieved two turns ago.

Both were found by reading every flag from the first run rather than trusting
the count. A checker that cries wolf is worse than no checker, because its
output stops being read.

### The prompt guard

A `PRICE CLAIMS` block in the agent system prompt, stating that Malaysian
market prices may only be given when they appear in tool output, and that the
absence of a price is to be stated plainly rather than filled in.

This is the layer that actually protects users, because it runs in production
and the detector does not.

## Consequences

**What this buys**

- A regression on price grounding now fails an evaluation run instead of
  reaching a user. Before this check, nothing in the system could tell a
  retrieved price from an invented one.
- The check is deterministic, so unlike a judge rubric it does not consume
  quota, does not vary run to run, and does not share the agent's priors.
- It produced a finding no rubric would have: the flags are not uniformly
  distributed.

**What it revealed**

All four flagged turns are Chinese — `ds_zh_003`, `multi_zh_001` (twice) and
`multi_zh_003` — while `multi_en_003`, the English mirror of the same
impossible-constraint scenario, was not flagged. That is suggestive, not
conclusive, on a sample of 20, and it is the highest-value open question this
ADR leaves. If the behaviour really is language-dependent, a prompt guard
written in English may simply be weaker in the other language, which would
change how the guard is written rather than how the detector works.

**What it costs**

- Old evaluation baselines are not comparable. The check runs in every
  category, so any run predating it under-reports failures by construction.
- The measured variance floor makes single runs unreliable: across three runs
  of the `multi_turn` category, four of six items pass stably, `multi_zh_001`
  fails persistently, and `multi_zh_003` alternates. A single run can be off
  by roughly one item in six — about 17 percentage points — so a pass→fail
  change of one item must be treated as noise until it reproduces.
- The detector runs offline. In production there is only the prompt.

**Known gaps**

- **Grounded is not the same as correct.** `search_malaysian_market_price`
  returns live listings without verifying they describe the same machine: a
  query for an "Asus Vivobook 14" — a Core Ultra 5 225H configuration listed at
  RM3,399 — returned an "ASUS Vivobook Go 14" with a Core i3-N305 at RM1,848.
  That price is in the tool output, so the check passes it. The catalog match
  path attaches a mismatch note; the live-listing path has no equivalent. The
  detector can only verify provenance, never accuracy.
- The check sees RM-prefixed figures. A price written as "around four
  thousand", or in another currency, passes through unexamined.
- The same mechanism has not been applied to any other class of factual claim.
  Specifications, benchmark figures and battery life are asserted with the same
  confidence and are checked by nothing.
- The zh/en asymmetry is untested. It needs a category built for it — matched
  pairs of the same scenario in both languages — rather than the incidental
  mirrors that exist now.

## Not chosen, and why

**A judge rubric item for price grounding (option 2).** The judge is the same
model as the agent and is instructed not to use its own product knowledge, so
it cannot distinguish a retrieved price from a plausible one. Adding a rubric
item would have produced a number that looked like a measurement and was not.
Determinism matters more than sophistication here: the question "does this
figure appear in the tool output?" has a correct answer that does not require a
model.

**Removing prices from the agent's context (option 4).** It would make
fabrication impossible rather than merely detectable, and it was tempting for
that reason. Rejected because the agent needs prices to do its job — comparing
two machines, explaining why one is recommended over another, and answering
"can I get something cheaper" all require the figure in context. Removing it
solves the failure by removing the capability.

**Blocking the response when the check fires.** The check lives in the
evaluation harness, where the cost of a false positive is a wasted
investigation. Moving it inline would make a false positive a broken answer,
and the check has already needed two rounds of false-positive removal. It is a
reasonable future step — the cumulative tool output is available in the graph
state, so the check would run unchanged — but not before the flag rate is
understood on a larger sample and the zh/en question is settled.

**Waiting for the catalog price backfill to fix it.** ADR-0009 gave every
active laptop a price, which removes the situation that most obviously invites
invention. It did not remove the behaviour: the stable 4000 / 5500 pair
reproduces on machines whose prices the system holds. The model is not filling
a gap; it is stating what it believes.