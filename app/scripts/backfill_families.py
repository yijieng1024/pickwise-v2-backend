"""
Propose a laptop_family grouping from product names -- and show what it gets wrong.

The catalog stores laptops per configuration, so one machine occupies up to 14
rows and can fill most of a six-result shortlist on its own. Grouping them is
the easy half. The hard half is that the only signal available, the product
name, is written inconsistently per brand, so an automatic grouping fails
silently -- and a wrong grouping is invisible once written. Hence: read-only
unless --apply, and three reports whose job is to make the failures visible
before anyone writes them.

WHAT A FAMILY IS. A coarse product line, at the granularity the manufacturer
segments its own catalog by -- not a configuration, not a chassis code, not a
model year. Apple has three: MacBook Air (13 and 15 together), MacBook Pro (14
and 16 together), MacBook Neo. Size is not a product boundary there, because
the chip tiers overlap between Air and Pro. Every ROG Strix is one family --
Strix G16, SCAR 16 and SCAR 18 together.

At that granularity the seed key mostly OVER-SPLITS, which is why the three
reports are not equally interesting:

  Part 1  What grouping would the seed key produce?
  Part 2  Where would it MERGE things it should not?  INFORMATIONAL. Most of
          these need no action: a family spanning two model years or two
          chassis codes is usually right at this granularity, and a wide price
          range inside one family is fine because deduplication picks the
          member nearest the stated budget, so the family answers a RM 5,000
          question and a RM 8,000 one differently.
  Part 3  Where would it SPLIT things it should not?  THE USEFUL ONE. The seed
          key splits far finer than a product line: "asus vivobook 14" from
          "vivobook 14", "rog strix g16" from "rog strix g16 g614", every Acer
          SKU whose model code sits outside the parentheses. Each pair here is
          a merge to make through the /families CRUD.

Parts 2 and 3 report only. They never split or merge automatically: the whole
premise is that this decision needs a human, which is what laptop_family's
is_verified boolean records.

Merging is not this script's job either. --apply calls the same
family_service.regroup_unassigned the POST /families/regroup endpoint does,
which touches only laptops with a null family_id -- so it seeds the starting
point and never undoes a merge an admin already made. Do the merging in the
admin UI: reassign the members, then delete the emptied family.

Every ordering here ends on laptops.id. Ties are common (two configs of one
family at one price) and Postgres does not guarantee return order, so without
it the reports reshuffle between runs and stop diffing cleanly -- the same bug
dump_pickscore.pick_laptop's .first() had.

Usage:
    python -m app.scripts.backfill_families
    python -m app.scripts.backfill_families --similarity 85
    python -m app.scripts.backfill_families --all-statuses
    python -m app.scripts.backfill_families --apply
"""
import argparse
import re
import sys
from collections import defaultdict

from rapidfuzz import fuzz
from sqlalchemy import select as sa_select
from sqlmodel import Session

from app.database import engine

# Mapper registration for a standalone script: Laptop -> LaptopCustomization ->
# Category is a chain of string-name relationships whose targets are only
# imported under TYPE_CHECKING (same chain audit_integrated_gpu.py imports).
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.brand_model import LaptopBrand
from app.laptops.family_key import family_key
from app.laptops.family_service import regroup_unassigned
from app.laptops.laptop_models import Laptop, LaptopStatus

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEP = "=" * 78
RULE = "-" * 78

# A parenthesised model year: the ASUS/Apple over-merge signal. The seed key
# drops everything from the first "(" on, so these are exactly the tokens it
# cannot see.
_YEAR_RE = re.compile(r"\(\s*(20\d{2})\s*\)")

# Spec tokens are alphanumeric too ("16GB", "144Hz", "1TB"), and they vary
# between configurations of the SAME machine -- which is the thing we are
# trying not to flag. Filtered out so the chassis-code signal stays readable.
_SPEC_TOKEN_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)?(?:gb|tb|mb|ghz|hz|whr|wh|w|k|nits|nit|inch|in|mm|cm|bit)"
    r"|20\d{2}"
    r"|\d+(?:st|nd|rd|th)"
    r")$",
    re.I,
)


# CPU part numbers have the same letter+digit shape as chassis codes and are
# the single biggest source of noise here ("255HX", "9955HX3D", "20-core").
# They are separated by one reliable property: a chassis code starts with a
# LETTER (FX608JMR, UX3405CA, G835, TMP614-54-52N9), a CPU part number starts
# with a digit. The one exception is Intel's "i5-13420H" family, which starts
# with a letter and needs naming explicitly.
_CPU_TOKEN_RE = re.compile(r"^(?:i[3-9]-|core|ryzen|ultra)", re.I)


def _tokens(name):
    """Alphanumeric tokens carrying BOTH a letter and a digit -- the shape of a
    chassis code (FX608JMR, UX3405CA, G835). Spec and CPU tokens are excluded;
    model years are handled separately by _YEAR_RE."""
    out = set()
    for raw in re.split(r"[^A-Za-z0-9-]+", name):
        tok = raw.strip("-")
        if len(tok) < 3:
            continue
        if not (re.match(r"[A-Za-z]", tok) and re.search(r"\d", tok)):
            continue
        if _SPEC_TOKEN_RE.match(tok) or _CPU_TOKEN_RE.match(tok):
            continue
        out.add(tok.upper())
    return out


def load_rows(session, active_only=True):
    stmt = (
        sa_select(Laptop.id, Laptop.product_name, Laptop.price_rm,
                  Laptop.brand_id, Laptop.family_id, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .order_by(Laptop.product_name, Laptop.id)  # total order: names tie often
    )
    if active_only:
        stmt = stmt.where(Laptop.status == LaptopStatus.ACTIVE.value)
    return session.exec(stmt).all()


def group(rows):
    families = defaultdict(list)
    for r in rows:
        families[family_key(r.product_name)].append(r)
    for members in families.values():
        members.sort(key=lambda r: (r.product_name, str(r.id)))
    return families


def report_grouping(families):
    print(SEP)
    print("PART 1  Proposed grouping ({} seed families from {} laptops)".format(
        len(families), sum(len(m) for m in families.values())))
    print(SEP)
    print("A seed family is a STARTING POINT, not the product line. Expect to")
    print("merge several of these into one family in the admin UI.\n")
    # Biggest families first -- they are where dedup pays off most, and where a
    # wrong merge hides the most products.
    ordered = sorted(families.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for key, members in ordered:
        brands = {m.name for m in members}
        flag = ""
        if len(brands) > 1:
            flag = "  [SPANS BRANDS: {}]".format(", ".join(sorted(brands)))
        assigned = sum(1 for m in members if m.family_id is not None)
        if assigned:
            flag += "  [{} of {} already assigned]".format(assigned, len(members))
        print("\n[{:>2}] {}{}".format(len(members), key, flag))
        for m in members:
            print("       - {}  (RM{})".format(m.product_name, m.price_rm))
    return ordered


def report_over_merges(ordered):
    print("\n" + SEP)
    print("PART 2  Possible OVER-MERGES -- INFORMATIONAL, nothing is split")
    print(SEP)
    print("Families whose members disagree on model year or chassis code.")
    print("At product-line granularity most of these are FINE: one family is")
    print("meant to span model years and chassis codes, and a wide price range")
    print("inside it is resolved per query by picking the member nearest the")
    print("stated budget. Read this as context for Part 3, not as a worklist.\n")

    hits = 0
    for key, members in ordered:
        if len(members) < 2:
            continue
        years = defaultdict(list)
        chassis = defaultdict(list)
        for m in members:
            for y in _YEAR_RE.findall(m.product_name):
                years[y].append(m.product_name)
            for t in _tokens(m.product_name):
                chassis[t].append(m.product_name)

        differing_years = len(years) > 1
        # A chassis token shared by every member is the family's own code, not
        # a disagreement; one that covers only some members splits the family.
        partial = {t: n for t, n in chassis.items() if len(n) < len(members)}
        differing_chassis = len(partial) > 1

        if not (differing_years or differing_chassis):
            continue
        hits += 1
        print(RULE)
        print("{}  ({} members)".format(key, len(members)))
        if differing_years:
            print("   years   : {}".format(", ".join(sorted(years))))
        if differing_chassis:
            print("   chassis : {}".format(", ".join(sorted(partial))))
        for m in members:
            print("       - {}".format(m.product_name))
    if not hits:
        print("(none)")
    print("\n{} family/families span a year or chassis boundary.".format(hits))
    return hits


def report_over_splits(ordered, threshold):
    print("\n" + SEP)
    print("PART 3  Possible OVER-SPLITS -- report only, nothing is merged")
    print(SEP)
    print("The actionable report. Each pair below is one merge: create or pick")
    print("the family that should hold both, POST its members across, then")
    print("delete the emptied one.\n")

    singles = [(k, m) for k, m in ordered if len(m) == 1]
    print("3a. Single-member families ({}):".format(len(singles)))
    print("    A real one-config machine is fine here; an Acer SKU whose model")
    print("    code leaked into the key is not. The pairs below disambiguate.\n")
    for key, members in sorted(singles, key=lambda kv: kv[0]):
        print("    - {}   [{}]".format(key, members[0].name))

    keys = sorted(k for k, _ in ordered)
    print("\n3b. Key pairs scoring >= {} on token_set_ratio:".format(threshold))
    print("    Score is printed so the threshold can be tuned from real output.")
    print("    Note token_set_ratio will NOT surface the merges the taxonomy")
    print("    wants most -- 'macbook air' vs 'macbook pro' score low, and")
    print("    'rog strix scar 18' vs 'rog strix g16' lower still. Those come")
    print("    from knowing the product line, not from string distance.\n")
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            score = fuzz.token_set_ratio(a, b)
            if score >= threshold:
                pairs.append((score, a, b))
    # Highest score first, then lexical -- a total order, so reruns diff clean.
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    for score, a, b in pairs:
        print("    {:5.1f}  {}\n           {}".format(score, a, b))
    if not pairs:
        print("    (none)")
    print("\n{} single-member families, {} similar pairs.".format(
        len(singles), len(pairs)))
    return singles, pairs


def apply_grouping(session):
    """Seed families over the unassigned laptops only.

    Delegates to family_service.regroup_unassigned -- the same function
    POST /families/regroup calls -- so the script and the endpoint can never
    group differently. It skips any laptop that already has a family, which is
    what makes re-running safe: a merge made in the admin UI is invisible to
    it, so nothing it does can undo one.
    """
    result = regroup_unassigned(session)
    print("\n" + SEP)
    print("APPLIED: {} families created, {} laptops assigned, {} left null "
          "(seed key spans two existing families -- resolve in the admin UI)"
          .format(result["families_created"], result["laptops_assigned"],
                  result["left_null"]))
    print(SEP)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the grouping (default is read-only)")
    ap.add_argument("--similarity", type=float, default=90.0,
                    help="token_set_ratio threshold for Part 3b (default 90)")
    ap.add_argument("--all-statuses", action="store_true",
                    help="include inactive/suspended laptops")
    args = ap.parse_args()

    with Session(engine) as session:
        rows = load_rows(session, active_only=not args.all_statuses)
        if not rows:
            print("No laptops found.")
            return
        families = group(rows)
        ordered = report_grouping(families)
        report_over_merges(ordered)
        report_over_splits(ordered, args.similarity)

        if args.apply:
            # Note: --apply runs over the whole catalog's unassigned rows, not
            # just the --all-statuses slice the reports above were built from.
            # Grouping an inactive laptop is harmless (retrieval filters it out
            # anyway) and leaving it ungrouped would only make it reappear as
            # backlog every time it is reactivated.
            apply_grouping(session)
        else:
            print("\n" + SEP)
            print("DRY RUN -- nothing written. Re-run with --apply to seed the")
            print("families, then merge them up to product lines in the admin")
            print("UI. Part 3 is the worklist; Part 2 is context.")
            print(SEP)


if __name__ == "__main__":
    main()
