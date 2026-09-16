"""
The price-fabrication detector.

`ungrounded_prices()` is the DETECTOR half of the price-fabrication work; the
PRICE CLAIMS system prompt block is the DEFENCE half. The whole reason they are
separate is that a defence cannot report on itself — so the detector needs its
own tests, or nothing is checking the checker.

Tested from both sides:
  - real fabrications must be caught
  - the two false-positive classes that were already fixed must stay fixed

And one test that is really an experiment: the Chinese/English asymmetry. Four
of twenty flagged turns were all Chinese while the English mirror with the same
impossible-constraint shape was not flagged. If the detector itself is
language-sensitive, that explains it. If this test passes, the asymmetry is in
the agent, not the detector, which is a much more useful thing to know.
"""

import pytest

from ._adapters import ungrounded_prices


def _tool_output(*prices):
    """A minimal stand-in for cumulative tool output. The detector matches bare
    numbers anywhere in the text, so the surrounding shape does not matter."""
    return [
        "model_code=X price_rm=" + " price_rm=".join(str(p) for p in prices)
    ]


# --------------------------------------------------------------------------
# True positives
# --------------------------------------------------------------------------


def test_flags_a_price_that_appears_nowhere():
    """The original catch: an invented RM2,300 range in ds_zh_003."""
    flagged = ungrounded_prices(
        "This model usually sells around RM2,300 in Malaysia.",
        _tool_output(5899, 7399),
    )
    assert flagged


def test_flags_an_invented_range():
    """The stable false prior: 4000/5500 reproduced across every run."""
    flagged = ungrounded_prices(
        "Machines like this run about RM4,000 to RM5,500.",
        _tool_output(5899),
    )
    assert len(flagged) == 2


def test_flags_formatted_variants():
    """RM4,000 / RM 4000 / RM4000 are the same claim."""
    for text in ["RM4,000", "RM 4000", "RM4000"]:
        assert ungrounded_prices(f"It costs about {text}.", _tool_output(5899))


# --------------------------------------------------------------------------
# False-positive class 1: figures the user stated
# --------------------------------------------------------------------------


def test_user_stated_budget_is_not_a_fabrication():
    """
    If the user says 'my budget is RM6,000' and the agent repeats RM6,000, the
    agent invented nothing. This was one of the two classes fixed after the
    first run.
    """
    flagged = ungrounded_prices(
        "Within your RM6,000 budget, this is the strongest option.",
        _tool_output(5899) + ["user said: my budget is RM6000"],
    )
    assert not flagged


# --------------------------------------------------------------------------
# False-positive class 2: grounded in an earlier turn
# --------------------------------------------------------------------------


def test_earlier_turn_grounding_counts():
    """Tool output is CUMULATIVE across the conversation. A price retrieved in
    turn 1 is still grounded when referenced in turn 3."""
    flagged = ungrounded_prices(
        "As I mentioned, that one is RM5,899.",
        _tool_output(5899) + _tool_output(7399),
    )
    assert not flagged


# --------------------------------------------------------------------------
# Tolerance rules
# --------------------------------------------------------------------------


def test_one_percent_tolerance():
    """Rounding RM5,899 to RM5,900 is not a fabrication."""
    assert not ungrounded_prices("It is RM5,900.", _tool_output(5899))


def test_beyond_tolerance_is_flagged():
    """RM6,100 against RM5,899 is 3.4% off — that is a different claim."""
    assert ungrounded_prices("It is RM6,100.", _tool_output(5899))


def test_rm1_floor_for_small_figures():
    """1% of a small number is meaningless, so there is a RM1 absolute floor."""
    assert not ungrounded_prices("About RM10.50.", _tool_output(10))


# --------------------------------------------------------------------------
# No-claim cases
# --------------------------------------------------------------------------


def test_no_rm_figure_means_nothing_to_flag():
    assert not ungrounded_prices(
        "This one has a stronger graphics card and a bigger battery.",
        _tool_output(5899),
    )


def test_empty_tool_output_flags_every_claim():
    """If the agent retrieved nothing, every price it states is its own."""
    assert ungrounded_prices("It costs RM4,500.", [])


# --------------------------------------------------------------------------
# The language-symmetry experiment
# --------------------------------------------------------------------------


def test_detector_treats_chinese_and_english_identically():
    """
    Same claim, same grounding, two languages. The detector matches RM-prefixed
    figures and should not care what is around them.

    If this goes red, the zh/en asymmetry seen in the eval is partly the
    detector's own doing and the eval results need re-reading. If it goes green,
    the asymmetry is in the agent's behaviour, which is the real finding.
    """
    en = ungrounded_prices(
        "In Malaysia this usually costs around RM4,500.", _tool_output(5899)
    )
    zh = ungrounded_prices(
        "这台在马来西亚大概卖 RM4,500 左右。", _tool_output(5899)
    )
    assert len(en) == len(zh) == 1


def test_chinese_full_width_comma_does_not_break_matching():
    """CJK text uses full-width punctuation. If the number parser splits on
    ASCII punctuation only, a price glued to a full-width comma is missed."""
    assert ungrounded_prices("价钱是 RM4,500，比较贵。", _tool_output(5899))


@pytest.mark.parametrize(
    "text",
    [
        "大概 RM4,500 左右",
        "价格 RM 4500",
        "RM4500 上下",
    ],
)
def test_chinese_phrasings_all_detected(text):
    assert ungrounded_prices(text, _tool_output(5899))
