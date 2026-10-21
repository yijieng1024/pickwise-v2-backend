"""
The catalog processor's transient-failure handling.

Model FALLBACK no longer lives here: the processor's overload-only
`_invoke_with_fallback` was replaced by the shared primary -> ordered-backups path
(app/common/llm.py), tested in test_llm_fallback.py. What remains is the bulk
loop's wait-and-retry, which runs after every model in the chain has failed.
"""
from tests.unit._adapters import is_overloaded, retry_wait_for

_OVERLOADED = "503 This model is currently experiencing high demand. Spikes in demand are usually temporary."
_QUOTA = "429 Resource has been exhausted (e.g. check quota)."


def test_overload_and_quota_are_told_apart():
    assert is_overloaded(_OVERLOADED)
    assert not is_overloaded(_QUOTA)
    # The bulk loop's sleep: a minute for quota, seconds for an overload.
    assert retry_wait_for(_QUOTA) == 65
    assert retry_wait_for(_OVERLOADED) == 20
    assert retry_wait_for("ValidationError: 3 validation errors") is None


def test_a_classified_error_still_reads_as_quota():
    # build_llm re-raises a 429 as LLMUnavailable; its message must keep the
    # original text, or the loop would stop waiting out quota errors.
    assert retry_wait_for("LLMUnavailable: ChatGoogleGenerativeAIError: Error calling model "
                          "'gemma-4-31b-it' (RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED") == 65


def test_every_option_can_finish_a_full_batch():
    # A model whose daily cap is below the processor's max batch would die
    # part-way through a run the UI let the admin start. Gemini *flash* sits
    # at 20 RPD on this account, which is why the list offers flash-lite.
    from tests.unit._adapters import model_options

    assert model_options(), "the dropdown must never be empty"
    for model, limits in model_options().items():
        assert limits["rpd"] >= 500, f"{model} cannot finish a normal run"
