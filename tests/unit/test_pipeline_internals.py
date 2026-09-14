"""
Tier 5 — the CRS pipeline's plumbing, with no model calls and no database.

The eval harness exercises these four modules end to end, through a live Gemini
agent, and grades the ANSWER. That means every failure here shows up there as
"the agent said something odd", mixed in with the model's own variance, and the
`relevance_gating` eval category actually tests the system prompt via
forbid_tools rather than app/rag/gating.py at all.

So: synthetic scores in, arithmetic out. Retrieval is stubbed, the LLM is never
constructed, and the numbers below are hand-computed from the formula in the
reranker docstring (final_score = similarity * penalty + bonus).
"""

import uuid

import pytest

from ._adapters import (
    RankedCandidate,
    RetrievalCandidate,
    UserConstraints,
    _brand_bonus,
    _budget_penalty,
    _purpose_bonus,
    _weight_penalty,
    _run_search,
    _search_tool,
    gate_threshold,
    max_results,
    min_viable_score,
    needs_relaxation,
    normalize_purpose,
    relax_and_retry,
    relaxation_steps,
    relevance_gate,
    rerank,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeLaptop:
    """Only the attributes the pipeline actually reads. A SQLModel row would
    need a database; every one of these is a plain read."""

    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4()
        self.family_id = kw.get("family_id")
        self.model_code = kw.get("model_code", "TEST-1")
        self.product_name = kw.get("product_name", "Test Laptop")
        self.price_rm = kw.get("price_rm", 4000.0)
        self.weight_kg = kw.get("weight_kg", 2.0)
        self.processor_model = kw.get("processor_model", "Intel Core 5 210H")
        self.gpu_model = kw.get("gpu_model", "Intel Arc Graphics")
        self.ram_gb = kw.get("ram_gb", 16)
        self.ssd_gb = kw.get("ssd_gb", 512)
        self.storage_type = kw.get("storage_type", "SSD")
        self.battery_wh = kw.get("battery_wh", 60.0)
        self.display_size_inch = kw.get("display_size_inch", 15.6)


def _candidate(similarity=0.70, brand="Asus", **laptop_kw) -> RetrievalCandidate:
    """similarity_score is a read-only property over cosine_distance, so the
    distance is what gets constructed."""
    return RetrievalCandidate(
        laptop=_FakeLaptop(**laptop_kw),
        brand_name=brand,
        cosine_distance=1.0 - similarity,
    )


def _ranked(final_score: float, penalty_reasons=None) -> RankedCandidate:
    return RankedCandidate(
        laptop_id=str(uuid.uuid4()),
        product_name="Asus Test Laptop",
        price_rm=4000.0,
        similarity_score=final_score,
        penalty_multiplier=1.0,
        bonus=0.0,
        final_score=final_score,
        penalty_reasons=penalty_reasons or [],
        bonus_reasons=[],
        _laptop=_FakeLaptop(),
        _brand_name="Asus",
    )


# --------------------------------------------------------------------------
# Module 4 — relevance gating
# --------------------------------------------------------------------------
# Catches: a threshold edited without re-deriving it, or a comparison flipped
# from < to <=, either of which changes which queries reach the user silently.


@pytest.mark.parametrize(
    "top_score,expected",
    [
        (0.52, "gated"),   # below the calibrated threshold
        (0.53, "pass"),    # exactly at it — the boundary is inclusive
        (0.54, "pass"),
    ],
)
def test_gate_boundary(top_score, expected):
    assert relevance_gate([_ranked(top_score)]).status == expected


def test_gate_reads_only_the_top_candidate():
    """The gate is a confidence check on the BEST match, not on the pool. A
    strong top result must not be gated because the tail is weak."""
    result = relevance_gate([_ranked(0.80), _ranked(0.10), _ranked(0.05)])
    assert result.status == "pass"
    assert result.top_score == pytest.approx(0.80)


def test_gate_threshold_is_the_calibrated_constant():
    """Pins the boundary tests above to the shipped value rather than to 0.53
    written twice."""
    assert relevance_gate([_ranked(gate_threshold())]).status == "pass"
    assert relevance_gate([_ranked(gate_threshold() - 0.001)]).status == "gated"


def test_empty_pool_is_gated_not_crashed():
    result = relevance_gate([])
    assert result.status == "gated"
    assert result.top_score is None
    assert result.bottleneck == "general"


def test_gated_result_always_carries_a_message():
    """Gating is not an error path — it redirects the conversation. A gated
    result with no message would reach the agent as a dead end."""
    for candidates in ([], [_ranked(0.10)]):
        result = relevance_gate(candidates)
        assert result.message and result.message.strip()


def test_gate_returns_no_candidates_when_gated():
    """A gated result must not leak the low-confidence pool — the agent would
    present it."""
    assert relevance_gate([_ranked(0.10)]).candidates == []


@pytest.mark.parametrize(
    "reasons,expected",
    [
        (["price RM 6000 is 50% over budget (hard penalty)"], "budget"),
        (["weight 2.4kg is far above 1.5kg limit (hard penalty)"], "weight_limit"),
        ([], "general"),
    ],
)
def test_bottleneck_is_detected_from_penalty_reasons(reasons, expected):
    """The bottleneck picks which clarifying question gets asked. Reading it off
    the penalty reasons means a reworded reason string silently degrades every
    gated reply to the generic question."""
    assert relevance_gate([_ranked(0.10, reasons)]).bottleneck == expected


# --------------------------------------------------------------------------
# Module 2 — reranker arithmetic
# --------------------------------------------------------------------------


def test_final_score_formula_is_similarity_times_penalty_plus_bonus():
    """Hand-computed: 0.80 similarity, RM4,200 against a RM4,000 budget is 5%
    over, so the soft branch gives max(0.5, 1 - 0.05*2) = 0.90, and nothing
    here earns a bonus. 0.80 * 0.90 = 0.72."""
    ranked = rerank(
        [_candidate(similarity=0.80, price_rm=4200.0)],
        UserConstraints(budget=4000.0),
    )
    assert ranked[0].penalty_multiplier == pytest.approx(0.90)
    assert ranked[0].bonus == pytest.approx(0.0)
    assert ranked[0].final_score == pytest.approx(0.72)


def test_within_budget_takes_no_penalty():
    ranked = rerank(
        [_candidate(similarity=0.80, price_rm=4000.0)],
        UserConstraints(budget=4000.0),
    )
    assert ranked[0].final_score == pytest.approx(0.80)


@pytest.mark.parametrize(
    "price,expected",
    [
        (4000.0, 1.0),   # at budget
        (4200.0, 0.90),  # 5% over  -> soft, 1 - 0.05*2
        (4800.0, 0.70),  # 20% over -> medium, 1 - 0.20*1.5
        (6000.0, 0.10),  # 50% over -> hard, flat
    ],
)
def test_budget_penalty_bands(price, expected):
    """Three bands with different formulas. An off-by-one on a band edge moves
    a whole price bracket between multipliers."""
    multiplier, _reasons = _budget_penalty(price, 4000.0)
    assert multiplier == pytest.approx(expected)


def test_weight_penalty_actually_fires():
    """THE REGRESSION TEST for this module: the weight branch was unreachable
    until weight_max was wired through from the tool, so it could have been any
    arithmetic at all and nothing would have noticed."""
    ranked = rerank(
        [_candidate(similarity=0.80, weight_kg=1.7)],
        UserConstraints(weight_limit=1.5),
    )
    assert ranked[0].penalty_multiplier == pytest.approx(0.70)
    assert ranked[0].final_score == pytest.approx(0.56)
    assert any("weight" in r for r in ranked[0].penalty_reasons)


@pytest.mark.parametrize(
    "weight,expected",
    [
        (1.5, 1.0),   # at the limit
        (1.7, 0.70),  # 13% over -> soft
        (2.2, 0.20),  # 47% over -> hard
    ],
)
def test_weight_penalty_bands(weight, expected):
    multiplier, _reasons = _weight_penalty(weight, 1.5)
    assert multiplier == pytest.approx(expected)


def test_penalties_multiply_rather_than_add():
    """Over budget AND over weight is 0.90 * 0.70 = 0.63, not 0.60. Multiplying
    is what keeps a machine that misses both constraints below one that misses
    only one, at every magnitude."""
    ranked = rerank(
        [_candidate(similarity=0.80, price_rm=4200.0, weight_kg=1.7)],
        UserConstraints(budget=4000.0, weight_limit=1.5),
    )
    assert ranked[0].penalty_multiplier == pytest.approx(0.63)
    assert ranked[0].final_score == pytest.approx(0.504)


def test_no_constraints_means_score_is_pure_similarity():
    ranked = rerank([_candidate(similarity=0.61)], UserConstraints())
    assert ranked[0].final_score == pytest.approx(0.61)


def test_purpose_bonus_is_capped():
    """Capped at +0.08 so a bonus can never outweigh a real penalty — an RTX
    machine 50% over budget must not climb back over an in-budget one."""
    bonus, _reasons = _purpose_bonus(
        _FakeLaptop(gpu_model="NVIDIA GeForce RTX 5060 Laptop GPU",
                    processor_model="Intel Core i7-14650HX"),
        ["Gaming", "Creative", "Programming", "Office"],
    )
    assert bonus == pytest.approx(0.08)


# --------------------------------------------------------------------------
# The Creative GPU-signal branch — reranker.py:22
# --------------------------------------------------------------------------
# Unreachable from the questionnaire today: _normalize_purpose coerces
# "Creative Work" to "Office" (see test_purpose_modifier_keys_match_known_purposes,
# deliberately red). These tests exercise the branch directly with the label the
# reranker itself uses, so they describe live behaviour the moment the labels are
# unified and must keep passing across that change — an eval run could not see a
# 0.04 reranker difference through its own variance.


def _bonus(gpu_model, purposes, processor_model="Intel Core 5 210H"):
    bonus, _reasons = _purpose_bonus(
        _FakeLaptop(gpu_model=gpu_model, processor_model=processor_model), purposes
    )
    return bonus


def test_creative_gpu_bonus_is_exactly_four_hundredths():
    """The branch adds +0.04 once, on the first keyword that hits — it breaks
    out of the keyword loop, so a string matching two keywords is not paid
    twice. Creative has no CPU signal set, so 0.04 is the whole bonus."""
    assert _bonus("NVIDIA GeForce RTX 5090 Laptop GPU", ["Creative"]) == pytest.approx(0.04)
    assert _bonus("AMD Radeon RX 7600S", ["Creative"]) == pytest.approx(0.04)


def test_creative_gives_no_bonus_to_a_gpu_it_does_not_recognise():
    """The pair that makes the branch measurable: same purpose, different GPU
    string, 0.04 apart."""
    assert _bonus("Intel Arc Graphics", ["Creative"]) == pytest.approx(0.0)


def test_creative_branch_keys_on_the_gpu_STRING_not_the_mark_or_the_proxy_flag():
    """
    What the branch actually reads, pinned because it is easy to assume
    otherwise: `kw in (laptop.gpu_model or "").lower()`. Not gpu_mark, not the
    resolved PassMark name, not flags.gpu_score_is_proxy — the reranker never
    touches the benchmark service at all.

    Consequence, asserted rather than described: an RTX 5090 (gpu_mark 28248,
    the catalog ceiling) and a Radeon 610M (1299, the catalog floor) earn the
    SAME +0.04. A 21x performance gap is invisible to this bonus.
    """
    flagship = _bonus("NVIDIA GeForce RTX 5090 Laptop GPU", ["Creative"])
    entry_igpu = _bonus("AMD Radeon 610M", ["Creative"])
    assert flagship == entry_igpu == pytest.approx(0.04)


def test_apple_gets_no_creative_bonus_at_all():
    """
    Apple GPU strings are core counts ('40-Core GPU'), which contain none of
    rtx/rx/radeon — so the proxy-scored rows do NOT collect this bonus. The
    amplification worry runs the other way: the machines most likely to be
    picked for creative work are the ones the branch cannot see.
    """
    assert _bonus("Apple M5 Max 40-Core GPU", ["Creative"]) == pytest.approx(0.0)


def test_creative_bonus_sits_inside_the_shared_cap_not_on_top_of_it():
    """One +0.08 cap covers every purpose's GPU and CPU bonus together, so
    Creative cannot lift a candidate beyond what Gaming alone already could."""
    assert _bonus("NVIDIA GeForce RTX 5090 Laptop GPU", ["Gaming", "Creative"]) == pytest.approx(0.08)
    assert _bonus(
        "NVIDIA GeForce RTX 5090 Laptop GPU",
        ["Gaming", "Creative", "Programming", "Office"],
        processor_model="Intel Core i7-14650HX",
    ) == pytest.approx(0.08)  # raw 0.16, capped


def test_radeon_is_the_creative_only_keyword():
    """
    'radeon' is in Creative's list and not in Gaming's; rtx/rx are in both. So
    a plain Radeon iGPU is the one string that tells the two branches apart —
    which is exactly why this is the negative case worth keeping.
    """
    assert _bonus("AMD Radeon 890M", ["Creative"]) == pytest.approx(0.04)
    assert _bonus("AMD Radeon 890M", ["Gaming"]) == pytest.approx(0.0)


@pytest.mark.parametrize("purpose", ["Gaming", "Programming", "Office"])
def test_creative_branch_does_not_fire_for_other_purposes(purpose):
    """The assertion that stays meaningful after the labels are unified: a user
    who did not say Creative Work must never collect the Creative bonus."""
    assert _bonus("AMD Radeon 890M", [purpose]) == pytest.approx(0.0)


def test_creative_bonus_reaches_final_score(monkeypatch):
    """End of the arithmetic: 0.70 similarity, no penalties, +0.04 = 0.74. The
    bonus is worth about 7.5% of the gap between the gate (0.53) and a perfect
    match, so it can move a borderline candidate across the gate."""
    ranked = rerank(
        [_candidate(similarity=0.70, gpu_model="AMD Radeon 890M")],
        UserConstraints(purpose=["Creative"]),
    )
    assert ranked[0].final_score == pytest.approx(0.74)
    assert ranked[0].bonus_reasons == ["GPU matches Creative purpose"]


def test_creative_work_does_not_currently_reach_the_branch():
    """
    Reachability, recorded once so the label fix has a witness.

    'Creative Work' is what the questionnaire stores; _normalize_purpose title-
    cases it, finds it outside _KNOWN_PURPOSES, and substitutes 'Office'. This
    test asserts the OUTCOME of the coercion (a Radeon machine earns nothing),
    so it keeps passing if the labels are unified by widening _KNOWN_PURPOSES —
    and goes red only if someone unifies them in a way that still loses the
    purpose, which is the failure worth catching.
    """
    normalized = normalize_purpose("Creative Work")
    assert _bonus("AMD Radeon 890M", normalized) == pytest.approx(0.0)


def test_ranking_is_by_final_score_not_similarity():
    """The whole point of the module: a closer topic match that breaks a hard
    constraint must lose to a slightly-less-similar machine that keeps it."""
    over_budget = _candidate(similarity=0.90, price_rm=6000.0, model_code="OVER")
    in_budget = _candidate(similarity=0.70, price_rm=3900.0, model_code="IN")
    ranked = rerank([over_budget, in_budget], UserConstraints(budget=4000.0))
    assert ranked[0]._laptop.model_code == "IN"


def test_ties_are_broken_on_laptop_id():
    """Sibling configurations of one machine tie constantly — same name, same
    embedding, same price. An unbroken tie falls through to Postgres return
    order, which reshuffles the shortlist between identical requests."""
    a = _candidate(similarity=0.70, id=uuid.UUID(int=2))
    b = _candidate(similarity=0.70, id=uuid.UUID(int=1))
    ranked = rerank([a, b], UserConstraints())
    assert [c.laptop_id for c in ranked] == sorted(c.laptop_id for c in ranked)


# --------------------------------------------------------------------------
# Brand is a soft preference (ADR-0007)
# --------------------------------------------------------------------------


def test_brand_is_a_reranker_bonus_not_a_filter():
    """A preferred brand adds +0.05; a wrong one subtracts 0.25. Both are
    scores, so a strong non-preferred machine can still surface."""
    assert _brand_bonus("Asus", ["asus"]) == (0.05, ["brand Asus matches preference"])
    penalty, _reasons = _brand_bonus("Acer", ["asus"])
    assert penalty == pytest.approx(-0.25)


def test_no_brand_preference_is_neutral():
    assert _brand_bonus("Acer", [])[0] == 0.0
    assert _brand_bonus("Acer", ["no preference"])[0] == 0.0


def test_wrong_brand_is_penalised_not_removed():
    """The non-preferred machine must still be IN the list. If brand were a
    hard filter this pool would be empty."""
    ranked = rerank(
        [_candidate(similarity=0.90, brand="Acer")],
        UserConstraints(brand_preferences=["asus"]),
    )
    assert len(ranked) == 1
    assert ranked[0].final_score == pytest.approx(0.65)  # 0.90 - 0.25


def test_brand_never_reaches_retrieve_candidates(monkeypatch):
    """
    retrieve_candidates CAN take a brand argument, and it applies it as a hard
    SQL filter. _run_search deliberately does not pass it — brand goes in as
    brand_preferences for the reranker bonus instead.

    That is a decision, not an accident, so it needs a test to stay one: wiring
    brand through to retrieval would turn "I like Asus" into "Asus only" and
    silently empty the pool when the catalog has nothing suitable from them.
    """
    seen = {}

    def _fake_retrieve(query, session, budget_max=None, brand=None, **kw):
        seen["brand"] = brand
        seen["budget_max"] = budget_max
        return [_candidate(similarity=0.80, brand="Acer")]

    constraints_seen = {}
    real_rerank = _search_tool.rerank

    def _spy_rerank(candidates, constraints):
        constraints_seen["value"] = constraints
        return real_rerank(candidates, constraints)

    _install_stubs(monkeypatch, retrieve=_fake_retrieve, rerank=_spy_rerank)

    _run_search("a light asus laptop", budget_max=4000.0, brand="Asus")

    assert seen["brand"] is None, "brand must not become a hard retrieval filter"
    assert seen["budget_max"] == 4000.0, "budget IS a hard filter, unlike brand"
    assert constraints_seen["value"].brand_preferences == ["asus"]


# --------------------------------------------------------------------------
# Module 3 — constraint relaxation
# --------------------------------------------------------------------------


def _viable_pool():
    return [_candidate(similarity=0.80)]


def _dead_pool():
    """Everything scores under _MIN_VIABLE_SCORE."""
    return [_candidate(similarity=0.10)]


def test_relaxation_order_is_weight_then_budget():
    """Weight is the least-sensitive constraint, so it is spent first. Brand is
    absent from the plan entirely — it is never auto-relaxed, because guessing
    a different brand answers a question the user did not ask."""
    assert [r["field"] for r in relaxation_steps()] == ["weight_limit", "budget"]


def test_relaxes_weight_before_budget(monkeypatch):
    calls = []

    def _fake_retrieve(query, session, budget_max=None, **kw):
        calls.append(budget_max)
        return _viable_pool()

    monkeypatch.setattr("app.rag.relaxation.retrieve_candidates", _fake_retrieve)

    result = relax_and_retry(
        "gaming laptop",
        UserConstraints(budget=4000.0, weight_limit=1.5),
        session=None,
    )
    assert result is not None
    assert result.relaxed_field == "weight_limit"
    assert result.relaxed_value == pytest.approx(1.7)  # one 0.2 step
    assert calls == [4000.0], "budget must be untouched while weight is relaxing"


def test_skips_an_inactive_constraint(monkeypatch):
    """No weight limit stated means nothing to relax there — it falls through to
    budget rather than relaxing a constraint the user never set."""
    monkeypatch.setattr(
        "app.rag.relaxation.retrieve_candidates",
        lambda query, session, budget_max=None, **kw: _viable_pool(),
    )
    result = relax_and_retry(
        "gaming laptop", UserConstraints(budget=4000.0, weight_limit=None), session=None
    )
    assert result.relaxed_field == "budget"
    assert result.relaxed_value == pytest.approx(4500.0)  # one RM500 step


def test_relaxation_stops_rather_than_looping(monkeypatch):
    """Exhaustion returns None and hands over to the gate. Bounded: 2 weight
    steps + 3 budget steps = 5 retrievals, never more."""
    calls = []

    def _fake_retrieve(query, session, budget_max=None, **kw):
        calls.append(budget_max)
        return _dead_pool()

    monkeypatch.setattr("app.rag.relaxation.retrieve_candidates", _fake_retrieve)

    result = relax_and_retry(
        "gaming laptop",
        UserConstraints(budget=4000.0, weight_limit=1.5),
        session=None,
    )
    assert result is None
    assert len(calls) == 5


def test_relaxation_never_returns_sub_viable_candidates(monkeypatch):
    """A relaxed search that still finds nothing decent must return None, not a
    bad list. Returning the pool would present exactly the misleading results
    the gate exists to block — and it would arrive with a cheerful "I've
    expanded your budget, here's what I found" attached."""
    monkeypatch.setattr(
        "app.rag.relaxation.retrieve_candidates",
        lambda query, session, budget_max=None, **kw: _dead_pool(),
    )
    assert relax_and_retry(
        "gaming laptop", UserConstraints(budget=4000.0), session=None
    ) is None


@pytest.mark.parametrize(
    "score,expected",
    [
        (min_viable_score() - 0.01, True),   # nothing viable -> relax
        (min_viable_score(), False),         # exactly at the floor is viable
        (0.80, False),
    ],
)
def test_needs_relaxation_tracks_the_viability_floor(score, expected):
    assert needs_relaxation([_ranked(score)]) is expected


def test_needs_relaxation_on_an_empty_pool():
    assert needs_relaxation([]) is True


# --------------------------------------------------------------------------
# The result cap
# --------------------------------------------------------------------------


def _install_stubs(monkeypatch, retrieve=None, rerank=None):
    """Cut the tool off from the database and the scoring engine.

    _run_search opens its own Session(engine) and calls PickScore, pgvector and
    the eval logger. None of that is what this tier is testing, and all of it
    needs a live database.
    """

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: _NullSession())
    monkeypatch.setattr(_search_tool, "_pick_scores_for", lambda top, session, pref: {})
    monkeypatch.setattr(_search_tool, "log_pipeline_result", lambda **kw: None)
    if retrieve is not None:
        monkeypatch.setattr(_search_tool, "retrieve_candidates", retrieve)
    if rerank is not None:
        monkeypatch.setattr(_search_tool, "rerank", rerank)


def test_max_results_caps_the_payload(monkeypatch):
    """
    _MAX_RESULTS is a hard ceiling regardless of the top_k the model asks for.
    Every result is re-sent in context on each follow-up reasoning step, so this
    is the single biggest lever on Gemini's tokens-per-minute limit — a model
    that asks for top_k=50 must not be able to spend the whole budget.
    """
    pool = [_candidate(similarity=0.80, model_code=f"M{i}") for i in range(20)]
    _install_stubs(
        monkeypatch, retrieve=lambda query, session, **kw: pool
    )

    payload = _run_search("a laptop", top_k=50)

    assert len(payload["results"]) == max_results() == 6


def test_smaller_top_k_is_respected(monkeypatch):
    """The cap is a ceiling, not a quota — a model asking for 3 gets 3."""
    pool = [_candidate(similarity=0.80, model_code=f"M{i}") for i in range(20)]
    _install_stubs(monkeypatch, retrieve=lambda query, session, **kw: pool)

    assert len(_run_search("a laptop", top_k=3)["results"]) == 3


def test_gated_search_returns_no_results_and_a_bottleneck(monkeypatch):
    """The tool's gated shape: empty results plus a bottleneck/message pair for
    the agent to turn into a clarifying question. If `results` were ever
    non-empty here the agent would present low-confidence matches as answers."""
    _install_stubs(
        monkeypatch,
        retrieve=lambda query, session, **kw: [_candidate(similarity=0.10)],
    )

    payload = _run_search("a washing machine")

    assert payload["results"] == []
    assert payload["confidence"] == "low"
    assert payload["bottleneck"]
    assert payload["message"]
