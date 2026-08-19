# ADR-0009: A `status` column with two kinds of hidden

- **Status:** Accepted
- **Date:** 2026-08-17
- **Related:** ADR-0006 (PickScore positioning — supersedes one of its
  decisions), ADR-0010 (benchmark resolution), ADR-0011 (normalization curve)

## Context

Two separate problems pointed at the same missing concept.

**Unpriced rows dominated every recommendation.** 146 of 303 catalog rows
carried `price_rm = 0`, which means "price unknown" and not "free". Retrieval's
`price_rm <= budget_max` filter is trivially true for 0 and the reranker's
budget penalty returns 1.0, so those rows survived every query unpenalised.
Nine of the twenty most-recommended laptops were in that group. ADR-0006
measured the concentration: over 107 distinct queries, the most-recommended
laptop appeared in 75% of them.

ADR-0006's answer was to exclude unpriced rows only when the user states a
budget, on the grounds that a blanket exclusion would remove 48% of the
catalog including most of the high-end gaming line. That reasoning was correct
for the data as it stood.

**Discontinued models had nowhere to go.** The catalog is scraped from
manufacturer sites. Machines leave the market; their rows stay. Deleting them
is destructive — they appear in past conversations, in wishlists, and in the
`pipeline_eval_logs` history that every concentration measurement is computed
from. Keeping them means recommending machines nobody can buy.

The two problems share a shape: **a row that should exist but should not be
recommended.** There was no way to express that, so the only available tools
were a hard delete or a special case in every query.

Manual price backfill then changed the arithmetic underneath ADR-0006's
decision. The unpriced set fell from 146 to about 35, so a blanket exclusion
was no longer half the catalog — it was 13%.

## Decision

Add `status` to `laptops` with three values.

| Value | Meaning | Reversible |
|---|---|---|
| `active` | On sale, priced, recommendable | — |
| `inactive` | Awaiting a price; goes live once one is found | Yes, by design |
| `suspended` | Soft delete: retired, no longer sold | In principle, not in practice |

The distinction between the two hidden states is the point of the column, and
it is not visible from the data: as of this writing every non-active row is
also unpriced, so a query cannot tell them apart. `inactive` is a work queue —
the set of machines to go and find prices for. `suspended` is an archive.
Conflating them would mean either hunting prices for discontinued machines or
losing track of which current machines are still missing one.

### Where the filter is applied

Retrieval and every display path, but **not** scoring.

- `retrieve_candidates` (`app/rag/retrieval.py`) — on both the pgvector query
  and `_relational_fallback`. Placed here rather than in `search_laptops`
  because constraint relaxation retries and `rag/evaluation.py` re-enter
  through this same function; filtering in the tool would have left both paths
  unfiltered.
- `conversation_laptops` joins at `service.py:162` and `rag/router.py:85`.
- `graph.py::_pool_block`, alongside the existing deleted-row skip. This is the
  one that mattered most: it is the agent's answer-from-memory path, and the
  last route by which the agent could have described a machine
  `search_laptops` would never return. Pool rows are left in place rather than
  removed, so re-activating a laptop restores it to the thread.
- `get_ranking_for_use_case` — the public use-case cards. This was missed in
  the first pass and shipped soft-deleted machines to a public endpoint until
  it was found.
- `GET /laptops/?status=` takes the value as a `LaptopStatus` enum, so an
  invalid value is a 422 rather than a silently empty list. Omitting the
  parameter returns every status, leaving the admin catalog view unchanged.

`GET /{laptop_id}/pick-scores` deliberately does *not* filter: an admin looking
at a specific retired machine should see its scores.

### Hard delete is guarded, not removed

`DELETE /laptops/{id}` counts references across the five child tables that have
no ORM mapping and raises 409 with a readable tally rather than cascading.
The message points the caller at `status` instead. An unreferenced row still
deletes.

### Regeneration covers active rows only

`generate_all_pick_scores` scores active laptops only, so non-active rows keep
whatever PickScore they last had. This is deliberate. Promoting an inactive row
to active adds it to the active-only range calculation, where a single extreme
value can shift a denominator for the entire catalog — so promotion requires a
full regeneration anyway, and pre-computing scores for rows that will be
recomputed on promotion buys nothing.

The cost is that `laptop_pick_scores` holds rows computed against ranges that
no longer exist. As of 2026-08-17 all 238 active rows date from that day's run
while all 36 non-active rows are frozen three days earlier, with `gpu_mark` and
four other ranges since changed underneath them.

## Consequences

**What this buys**

- One mechanism replaces a conditional rule. ADR-0006's "exclude unpriced rows
  when a budget is stated" is superseded: unpriced rows are non-active, so
  retrieval never sees one, whether or not a budget was given. The conditional
  version would now be unreachable code.
- Every active laptop has a price. This is the precondition for the price
  fabrication work in ADR-0008 — the agent can no longer be asked about a
  machine whose price the system does not hold.
- Discontinued machines stop being recommended without destroying the history
  that concentration and coverage measurements are computed from.
- The retire path is reversible, which a delete is not.

**What it costs**

- The invariant "active implies priced" holds by data, not by construction.
  `status` defaults to `active` and `price_rm` has no linked constraint, so the
  next ingest of an unpriced machine lands it active and recommendable. A
  `CHECK (status <> 'active' OR price_rm > 0)` would enforce it, but only after
  the ingest path is changed to write `inactive` for unpriced rows — added in
  the other order it would break ingestion.
- Nothing promotes a row when its price arrives. Filling `price_rm` without
  changing `status` leaves the machine hidden indefinitely, and it looks
  identical to a machine still waiting. The monitoring query is
  `status = 'inactive' AND price_rm > 0`, which should always return nothing.
- Non-active pick scores go stale silently. `GET /pick-scores/status` already
  reports coverage; a count of rows whose `updated_at` predates the newest
  would surface this without relying on anyone remembering, and would also
  catch staleness from weight changes, benchmark refreshes and map edits.
- Validation is a `field_validator` on `LaptopBase` and `LaptopUpdate`, and
  SQLModel skips validation on `table=True` models. The guarantee therefore
  covers writes through the API schemas and nothing else — data migrations,
  scripts and raw SQL can write any string. The column is a plain VARCHAR with
  no database-level constraint.
- `server_default='active'` means applying the migration backfills every
  existing row to active. Status assignments made locally do not travel with
  it and must be replayed after deployment, by script rather than by hand, for
  the reason immediately above.

**Known gaps**

- The enum was written with the value `suspend` while the data holds
  `suspended`, which is also what `users.status` uses. One of the two has to
  move; `suspended` is the better target, since two columns of the same name
  and meaning holding different strings will silently break any query, helper
  or admin component that spans both tables.
- The 409 message from the delete guard reads "Set status to 'inactive' to
  retire it instead." By the semantics above, retiring is `suspended`;
  `inactive` is the price queue. As written, the guard teaches callers to file
  discontinued machines into a queue for a price that will never come.
- `laptop_pick_scores.breakdown` and `flags` are Postgres `json` rather than
  `jsonb`, so the staleness and fallback checks above need an explicit cast and
  cannot use an index. Worth a migration only if those checks become routine.

## Not chosen, and why

**A boolean `is_active`.** Loses the distinction the column exists for. A
machine awaiting a price and a machine that no longer exists need different
handling: one is a task, the other is history.

**A native Postgres enum type.** Adding a fourth state would need an
`ALTER TYPE` migration. `users.status` is already a plain VARCHAR, so this also
keeps the two columns the same shape. The cost is no database-level constraint,
which is the reason typos are a live concern rather than a theoretical one.

**Hard delete for discontinued models.** Destroys wishlist entries and
conversation shortlists, and orphans the `pipeline_eval_logs` history that
ADR-0006's concentration figures are derived from. Some rows were removed this
way before the guard existed, which is why the catalog went from 303 rows to
276.

**Filtering by `price_rm > 0` at the query level instead.** This is what
ADR-0006 proposed, and it conflates two different facts. A machine can be
current and unpriced (the scraper failed, or the manufacturer never listed a
price) or discontinued and priced. Price is a data state; being on sale is a
product state. Encoding both in one column means a future price backfill
silently un-retires discontinued machines.

**Regenerating pick scores for every status.** Considered and rejected above:
promotion forces a full regeneration regardless, so the extra rows would be
recomputed the moment they became visible.