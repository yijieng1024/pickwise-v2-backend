import pytest

from tests.unit._adapters import invoke_with_fallback, is_overloaded, retry_wait_for

_OVERLOADED = "503 This model is currently experiencing high demand. Spikes in demand are usually temporary."
_QUOTA = "429 Resource has been exhausted (e.g. check quota)."

_MODELS = ("primary", "second", "third")


def _chains(behaviour):
    """make_chain over a {model: exception or result} map, recording calls."""
    calls: list[str] = []

    class _Chain:
        def __init__(self, model):
            self.model = model

        def invoke(self, payload):
            calls.append(self.model)
            outcome = behaviour[self.model]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return _Chain, calls


def test_overloaded_primary_falls_back_to_the_next_model():
    make_chain, calls = _chains({"primary": RuntimeError(_OVERLOADED), "second": "ok"})
    assert invoke_with_fallback(make_chain, {}, _MODELS) == "ok"
    assert calls == ["primary", "second"]


def test_quota_error_does_not_burn_the_other_models():
    # A 429 is the ACCOUNT's limit, not this model's — retrying elsewhere
    # would turn one failed extraction into three.
    make_chain, calls = _chains({"primary": RuntimeError(_QUOTA)})
    with pytest.raises(RuntimeError):
        invoke_with_fallback(make_chain, {}, _MODELS)
    assert calls == ["primary"]


def test_all_models_overloaded_raises_from_the_last():
    behaviour = {m: RuntimeError(f"{_OVERLOADED} [{m}]") for m in _MODELS}
    make_chain, calls = _chains(behaviour)
    with pytest.raises(RuntimeError, match=r"\[third\]"):
        invoke_with_fallback(make_chain, {}, _MODELS)
    assert calls == list(_MODELS)


def test_first_model_wins_when_it_answers():
    make_chain, calls = _chains({"primary": "ok"})
    assert invoke_with_fallback(make_chain, {}, _MODELS) == "ok"
    assert calls == ["primary"]


def test_overload_and_quota_are_told_apart():
    assert is_overloaded(_OVERLOADED)
    assert not is_overloaded(_QUOTA)
    # The bulk loop's sleep: a minute for quota, seconds for an overload.
    assert retry_wait_for(_QUOTA) == 65
    assert retry_wait_for(_OVERLOADED) == 20
    assert retry_wait_for("ValidationError: 3 validation errors") is None


# --------------------------------------------------------------------------
# Model selection — the DB value is admin input, so it is validated on read
# as well as on write: the row outlives any deploy that drops a model.
# --------------------------------------------------------------------------
from tests.unit._adapters import model_chain, resolve_extraction_model  # noqa: E402


class _FakeSession:
    """Stands in for `get_setting`'s session: one stored value, or none."""

    def __init__(self, stored=None):
        self.stored = stored

    def exec(self, _statement):
        return self

    def first(self):
        if self.stored is None:
            return None
        return type("Row", (), {"value": self.stored})()


def test_unset_falls_back_to_the_default_model():
    assert resolve_extraction_model(_FakeSession()) == "gemma-4-31b-it"


def test_allow_listed_choice_is_honoured():
    assert resolve_extraction_model(_FakeSession("gemma-4-26b-it")) == "gemma-4-26b-it"


def test_stale_value_is_ignored_rather_than_sent_to_the_api():
    assert resolve_extraction_model(_FakeSession("gemini-1.0-retired")) == "gemma-4-31b-it"


def test_chosen_model_never_appears_twice_in_its_own_fallback_chain():
    chain = model_chain("gemma-4-26b-it")
    assert chain[0] == "gemma-4-26b-it"
    assert len(chain) == len(set(chain))


def test_every_option_can_finish_a_full_batch():
    # A model whose daily cap is below the processor's max batch would die
    # part-way through a run the UI let the admin start. Gemini *flash* sits
    # at 20 RPD on this account, which is why the list offers flash-lite.
    from tests.unit._adapters import model_options

    assert model_options(), "the dropdown must never be empty"
    for model, limits in model_options().items():
        assert limits["rpd"] >= 500, f"{model} cannot finish a normal run"
