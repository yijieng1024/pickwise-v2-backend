"""
The shared model-line key: which laptop rows are configurations of one machine.

Two consumers want this grouping at opposite granularities, which is why the
function lives here rather than next to either of them:

  - YouTube review discovery (app/reviews/service.py) wants it COARSE. It
    searches once per model line, and merging two model years there costs
    nothing but saves quota.
  - Family deduplication (app/laptops/family_*.py, laptop_family table) wants
    it FINE. Merging two model years there hides a product the user could
    have bought.

This function is the coarse one, and it is the seed for the fine one -- the
laptop_family table carries a human-correctable family_id precisely because
this key is not trustworthy enough to be the last word. It is wrong in both
directions, and which direction depends on the brand: it over-merges ASUS and
Apple (year and chassis code sit inside the first parenthesis, so they are
discarded) and over-splits Acer (model code sits OUTSIDE any parenthesis, so
two SKUs of one machine keep two keys). Do not try to fix that here with a
smarter rule -- the inconsistency is in the source data, and every rule that
patches one brand is broken by the next one. Correct the family_id instead.
"""


def family_key(product_name: str) -> str:
    """
    Collapse config variants into one YouTube-search family: reviewers cover
    "ASUS TUF Gaming F15", not each RAM/SSD/CPU configuration -- and discovery
    costs 100 quota units per channel per query, so searching per variant
    would burn the 10k daily quota on duplicate queries. Everything from the
    first parenthesis on is config noise for search purposes.
    """
    base = product_name.split("(")[0]
    return " ".join(base.lower().split())
