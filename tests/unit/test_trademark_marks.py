"""The extraction turned "™ + digit" into a superscript ("Ryzen™ 5 PRO 230" became
"Ryzen⁵ PRO 230", "Ryzen™ 9 8940HX" became "Ryzen⁴ 9 8940HX"), so the marks are taken out
of the prompt text, and a superscript digit that still comes back is reported, never
converted: the ⁴ in the second one is not part of the model at all."""
from tests.unit._adapters import pipeline_summary, processor_engine as engine


def test_a_mark_before_a_digit_leaves_a_space():
    assert engine.strip_marks("AMD Ryzen™ 5 PRO 230") == "AMD Ryzen 5 PRO 230"
    assert engine.strip_marks("AMD Ryzen™5 PRO 230") == "AMD Ryzen 5 PRO 230"


def test_ryzen_9_8940hx_keeps_its_model():
    assert engine.strip_marks("AMD Ryzen™ 9 8940HX Processor") == "AMD Ryzen 9 8940HX Processor"
    assert engine.strip_marks("NVIDIA® GeForce RTX™ 5080 Laptop GPU") == "NVIDIA GeForce RTX 5080 Laptop GPU"


def test_a_leftover_superscript_is_reported_not_converted():
    found = engine.superscript_fields({"processor_model": "AMD Ryzen⁴ 9 8940HX", "gpu_model": "RTX 5080"})
    assert found == {"processor_model": "AMD Ryzen⁴ 9 8940HX"}


def test_the_email_lists_spec_text_to_check():
    result = {"steps": {"process": {"spec_text_to_check": [
        {"product_name": "ROG Strix G16", "field": "processor_model", "value": "AMD Ryzen⁴ 9 8940HX"}]}}}
    _, text, _ = pipeline_summary.build_summary(result, 30)
    assert "Spec text to check\n- ROG Strix G16: processor_model 'AMD Ryzen⁴ 9 8940HX'" in text
