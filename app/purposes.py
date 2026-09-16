"""
The one canonical set of purpose labels.

There used to be two. `laptop_user_preference.purpose` stores what the
questionnaire's Q2 offers (migration ffb4429867dd) and `PURPOSE_MODIFIERS` is
keyed on exactly those long labels; the agent's search tool kept its own short
set — {"Gaming", "Creative", "Programming", "Office"} — and coerced anything
outside it to "Office" after a `.title()`. Four of the five questionnaire values
therefore collapsed to Office on the way into reranking, silently, for as long
as the tool had existed.

The fix is not a translation layer between the two sets. A mapping between two
vocabularies is a second thing to keep in sync, which is the bug again with more
code. There is one vocabulary, defined here, and every consumer imports it:
`PURPOSE_MODIFIERS` (app/pickscore/engine.py), `_PURPOSE_CPU_SIGNALS`
(app/rag/reranker.py), `_KNOWN_PURPOSES` (app/agent/tools/search_laptops.py),
and the questionnaire seed.

This module deliberately imports nothing. Every one of those consumers can
import it without a cycle, which is what makes "one place" achievable at all.
"""

# Order matches the questionnaire's Q2 options.
PURPOSES: tuple[str, ...] = (
    "Office/Study",
    "Programming/Development",
    "Gaming",
    "Creative Work",
    "General Use",
)

# The slug each purpose is stored under in `laptop_pick_scores.use_case` and
# accepted as in `GET /laptops/pick-scores/ranking?use_case=`. Slugs are a
# separate identifier space on purpose — they are persisted in the database and
# published in a public URL, so they cannot carry spaces or slashes — but they
# are derived from the same list here so the two cannot drift apart.
USE_CASE_SLUGS: dict[str, str] = {
    "Office/Study": "office_study",
    "Programming/Development": "programming",
    "Gaming": "gaming",
    "Creative Work": "creative_work",
    "General Use": "general_use",
}


def normalize_purpose(purpose: str) -> str:
    """
    Return the canonical label, or raise.

    Whitespace and letter case are tolerated because they are transport noise.
    An unrecognised VALUE is not: it means a caller is working from a different
    vocabulary, which is exactly the condition that hid the original bug for
    months. Silent coercion to a default is what made it invisible — a wrong
    purpose and a missing purpose produced the same reranking, so nothing
    downstream could tell them apart.
    """
    if purpose is None:
        raise ValueError("purpose is required")
    cleaned = " ".join(purpose.split())
    for canonical in PURPOSES:
        if cleaned.casefold() == canonical.casefold():
            return canonical
    raise ValueError(
        f"unknown purpose {purpose!r}; expected one of: " + ", ".join(PURPOSES)
    )
