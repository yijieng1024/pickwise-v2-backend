"""The configuration step on the Link Reviews screen.

The step asks which configuration a reviewer tested. Two things have to hold for
that question to be fair, and both are asserted here:

  1. It must not be asked when it cannot be answered. A family whose members
     differ only in RAM and storage is unanswerable in principle — no review
     states whether the unit had 32GB or 64GB, and no conclusion in the video
     would change if it did. Asking anyway invites a guess, and a guessed
     laptop_id is worse than a null one.

  2. Where it can be answered, the evidence must come from the source material
     rather than from the human's memory of a video they have not watched.

No database and no network: everything under test is a pure function over
plain objects, which is why the separability rule and the probe builders were
put in functions rather than inline in the route.
"""

import pytest

from app.laptops.laptop_models import Laptop
from app.reviews.config_evidence import (
    _probes_for_model,
    _probes_for_storage,
    scan_config_evidence,
)
from app.reviews.link_service import (
    column_label,
    config_label,
    config_row,
    differing_columns,
    mark_indistinguishable,
    separability,
)


def _laptop(**kwargs) -> Laptop:
    """A catalog row with only the fields this screen reads."""
    base = dict(
        product_name="ASUS ExpertBook Ultra B9406CAA",
        model_code="asus-expertbook-ultra-b9406caa",
        price_rm=11999.0,
        processor_model="Intel Core Ultra 7 Processor 358H",
        gpu_model="Intel Arc B390",
        ram_gb=32,
        ssd_gb=1024,
        status="active",
    )
    base.update(kwargs)
    return Laptop(**base)


# --- 1. Do not ask a question that has no answer -----------------------------

def test_ram_and_storage_only_family_is_not_separable():
    """The case that prompted this: same CPU, same GPU, different RAM/storage."""
    members = [
        _laptop(ram_gb=16, ssd_gb=512),
        _laptop(ram_gb=32, ssd_gb=1024),
        _laptop(ram_gb=64, ssd_gb=2048),
    ]
    columns = differing_columns(members)
    assert set(columns) == {"ram_gb", "ssd_gb"}

    separable, code, reason = separability(columns, len(members))
    assert separable is False
    assert code == "ram_storage_only"
    assert "RAM and storage" in reason


def test_a_cpu_difference_keeps_the_family_separable():
    """RAM differing as well does not make a family unanswerable — the human
    answers the CPU question and the RAM rides along with it."""
    members = [
        _laptop(processor_model="Intel Core Ultra 5 Processor 325", ram_gb=16),
        _laptop(processor_model="Intel Core Ultra 7 Processor 358H", ram_gb=32),
    ]
    separable, code, _ = separability(differing_columns(members), len(members))
    assert separable is True
    assert code == "separable"


def test_single_config_family_is_reported_distinctly():
    """Not the same case as ram_storage_only, and the code has to say so: there
    is nothing to choose between, but the one row may still be the right link."""
    separable, code, _ = separability([], 1)
    assert separable is False
    assert code == "single_config"


def test_an_all_suspended_family_is_not_reported_as_a_single_config():
    """Reachable, and not a data error: ExpertBook P3 G2 is two rows and both
    are suspended, so the config filter empties it. Saying "one configuration"
    there would be a plain lie about the catalog."""
    separable, code, reason = separability([], 0)
    assert separable is False
    assert code == "no_configs"
    assert "one configuration" not in reason


def test_identical_members_are_reported_distinctly():
    separable, code, _ = separability([], 3)
    assert separable is False
    assert code == "identical_specs"


# --- 2. Recompute the table after dropping suspended rows --------------------

def test_dropping_a_suspended_row_can_remove_a_column():
    """Price looked like it distinguished the ExpertBook Ultra configs. It only
    did so because a suspended row was priced RM 0 — the catalog's "unknown"
    showing through as "free". With that row gone, price is constant and must
    stop being offered as a distinguishing column.

    This is why differing_columns is recomputed over the filtered members and
    not over the full family.
    """
    suspended = _laptop(price_rm=0.0, status="suspended", ram_gb=16, ssd_gb=512)
    active = [
        _laptop(price_rm=11999.0, ram_gb=32, ssd_gb=1024),
        _laptop(price_rm=11999.0, ram_gb=64, ssd_gb=2048),
    ]

    assert "price_rm" in differing_columns([suspended] + active)
    assert "price_rm" not in differing_columns(active)


# --- 3. The table itself -----------------------------------------------------

def test_row_label_is_readable_and_replaces_the_slug():
    laptop = _laptop()
    columns = ["processor_model", "gpu_model", "ram_gb", "ssd_gb"]
    assert config_label(laptop, columns) == "Ultra 7 358H / Arc B390 / 32GB / 1TB"


def test_config_row_does_not_expose_model_code():
    """The slug is a database key. It is unreadable and it duplicates the
    CPU/GPU/RAM/Storage columns sitting beside it, so it must not be sent."""
    row = config_row(_laptop(), ["processor_model", "ram_gb"])
    assert "model_code" not in row
    assert row["label"] == "Ultra 7 358H / 32GB"


def test_label_is_capped_and_skips_price():
    """A wide family makes the difference stark: the ROG Zephyrus G16 differs in
    eight tracked columns, and naming a row by all of them gives "11299.0 /
    Ultra 9 285H / RTX 5070 Laptop GPU / 32GB / 1TB / ROG Nebula Display OLED /
    1.85 / 2025" — eight wrapped lines, beside the same eight values in their
    own cells. A label is a name, not a spec sheet."""
    laptop = _laptop(
        processor_model="Intel Core Ultra 9 Processor 285H",
        gpu_model="NVIDIA GeForce RTX 5070 Laptop GPU",
        ram_gb=32,
        ssd_gb=1024,
    )
    columns = [
        "price_rm", "processor_model", "gpu_model", "ram_gb", "ssd_gb",
        "display_type", "weight_kg", "release_year",
    ]
    label = config_label(laptop, columns)
    assert label == "Ultra 9 285H / RTX 5070 Laptop GPU / 32GB / 1TB"
    assert "11999" not in label


def test_rows_identical_in_every_shown_column_are_flagged():
    """Two identical radio options are a coin flip — the invited guess arriving
    through the data instead of through the question. The Zephyrus G16 family
    really does hold two such pairs."""
    columns = ["processor_model", "ram_gb"]
    rows = [
        config_row(_laptop(ram_gb=32), columns),
        config_row(_laptop(ram_gb=32), columns),
        config_row(_laptop(ram_gb=64), columns),
    ]
    flagged = mark_indistinguishable(rows, columns)
    assert flagged == 2
    assert [r["indistinguishable"] for r in rows] == [True, True, False]


def test_label_falls_back_to_product_name_when_nothing_differs():
    assert config_label(_laptop(), []) == "ASUS ExpertBook Ultra B9406CAA"


# --- 4. Evidence, and the specificity of its probes --------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("Intel Core Ultra 7 Processor 358H", ["358H"]),
        ("Intel Arc B390", ["B390"]),
        ("Intel Core i7-1370P vPro", ["i7-1370P"]),
        ("NVIDIA GeForce RTX 5060 Laptop GPU", ["RTX 5060"]),
    ],
)
def test_probes_reduce_a_spec_string_to_its_part_number(value, expected):
    assert _probes_for_model(value) == expected


def test_integrated_graphics_yields_no_probe():
    """"Intel Graphics" identifies no specific part, so it cannot be evidence
    for one member over another. A probe for it would match every row at once."""
    assert _probes_for_model("Intel Graphics") == []


def test_a_bare_number_is_never_a_probe_on_its_own():
    """"430" appears in prices, view counts and timestamps. It is only used
    inside a phrase, where the preceding words carry the specificity."""
    probes = _probes_for_model("AMD Ryzen AI 5 430")
    assert "430" not in probes
    assert probes == ["AI 5 430"]


def test_storage_is_probed_in_both_gb_and_tb():
    assert set(_probes_for_storage(1024)) == {"1024GB", "1024 GB", "1TB", "1 TB"}


def test_description_spec_table_answers_the_question():
    """The Chinese-channel case: a full spec table pasted into the description."""
    members = [
        _laptop(ram_gb=16, ssd_gb=512),
        _laptop(ram_gb=32, ssd_gb=1024),
    ]
    result = scan_config_evidence(
        description="規格：Intel Core Ultra 7 358H / 32GB DDR5 / 1TB PCIe 4.0 SSD",
        transcript_segments=None,
        members=members,
        columns=differing_columns(members),
        label_for=column_label,
    )
    found = {(h["column"], h["value"]) for h in result["hits"]}
    assert ("ram_gb", "32") in found
    assert ("ssd_gb", "1024") in found
    assert result["found_nothing"] is False


def test_transcript_hits_carry_a_timestamp():
    """So the human can jump to the moment and confirm in seconds."""
    members = [
        _laptop(processor_model="Intel Core Ultra 5 Processor 325"),
        _laptop(processor_model="Intel Core Ultra 7 Processor 358H"),
    ]
    segments = [
        {"text": "welcome back to the channel", "start": 0, "duration": 5},
        {"text": "our unit has the Ultra 7 358H inside", "start": 42, "duration": 5},
    ]
    result = scan_config_evidence(
        None, segments, members, ["processor_model"], column_label
    )
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["timestamp_seconds"] == 42
    assert hit["source"] == "transcript"
    assert "358H" in hit["context"]


def test_a_probe_spanning_two_segments_is_still_found():
    """YouTube splits segments on timing, not on phrases, so a joined scan is
    the only one that sees these."""
    members = [_laptop(ram_gb=16), _laptop(ram_gb=32)]
    segments = [
        {"text": "it ships with 32", "start": 10, "duration": 2},
        {"text": "GB of memory", "start": 12, "duration": 2},
    ]
    result = scan_config_evidence(None, segments, members, ["ram_gb"], column_label)
    assert [h["value"] for h in result["hits"]] == ["32"]


def test_nothing_found_is_reported_as_a_real_answer():
    """"The video does not say" is the answer that lets the human stop looking,
    and it is only credible next to a record of what was searched for."""
    members = [
        _laptop(processor_model="Intel Core Ultra 5 Processor 325"),
        _laptop(processor_model="Intel Core Ultra 7 Processor 358H"),
    ]
    result = scan_config_evidence(
        description="The First Panther Lake Laptop I Strongly Recommend",
        transcript_segments=[{"text": "great keyboard", "start": 3, "duration": 2}],
        members=members,
        columns=["processor_model"],
        label_for=column_label,
    )
    assert result["hits"] == []
    assert result["found_nothing"] is True
    assert result["searched"][0]["column"] == "processor_model"


def test_no_source_material_is_not_the_same_as_nothing_found():
    """With no description and no transcript, the screen must say the video has
    nothing stored — not that the video says nothing about the configuration."""
    members = [_laptop(ram_gb=16), _laptop(ram_gb=32)]
    result = scan_config_evidence(None, None, members, ["ram_gb"], column_label)
    assert result["found_nothing"] is False
    assert result["sources_available"] == {"description": False, "transcript": False}


def test_only_differing_columns_are_scanned():
    """A CPU shared by every member proves nothing when found, so it is never
    searched for."""
    members = [_laptop(ram_gb=16), _laptop(ram_gb=32)]
    columns = differing_columns(members)
    assert columns == ["ram_gb"]
    result = scan_config_evidence(
        description="Intel Core Ultra 7 358H tested here",
        transcript_segments=None,
        members=members,
        columns=columns,
        label_for=column_label,
    )
    assert result["hits"] == []


def test_a_hit_reports_every_member_carrying_that_value():
    """One id means the hit narrows the family to a single row; two means it
    narrows it to two. The screen needs the difference."""
    shared_a = _laptop(ram_gb=32, ssd_gb=512)
    shared_b = _laptop(ram_gb=32, ssd_gb=1024)
    other = _laptop(ram_gb=64, ssd_gb=2048)
    result = scan_config_evidence(
        "32GB of RAM", None, [shared_a, shared_b, other], ["ram_gb"], column_label
    )
    assert len(result["hits"]) == 1
    assert set(result["hits"][0]["laptop_ids"]) == {shared_a.id, shared_b.id}
