"""The weekly pipeline's settings (plan step 11, item 8): defaults and validation."""
import pytest
from pydantic import ValidationError

from tests.unit._adapters import pipeline_settings as ps


def test_defaults_are_off_sunday_no_email_30_percent_every_site_on():
    row = ps.PipelineSettings()
    assert (row.enabled, row.weekday, row.report_email, row.price_jump_percent) == (False, "sunday", "", 30)
    assert row.sites() == {"asus": ["www.asus.com", "rog.asus.com"], "hp": ["www.hp.com"]}


def test_a_brand_with_every_site_off_is_not_crawled():
    assert ps.PipelineSettings(asus_enabled=False, hp_enabled=False).sites() == {"asus": ["rog.asus.com"]}
    assert ps.PipelineSettings(asus_enabled=False, rog_enabled=False).sites() == {"hp": ["www.hp.com"]}


@pytest.mark.parametrize("percent", [1, 30, 100])
def test_a_threshold_from_1_to_100_is_accepted(percent):
    assert ps.PipelineSettingsUpdate(price_jump_percent=percent).price_jump_percent == percent


@pytest.mark.parametrize("percent", [0, 101, -5])
def test_a_threshold_outside_1_to_100_is_refused(percent):
    with pytest.raises(ValidationError):
        ps.PipelineSettingsUpdate(price_jump_percent=percent)


def test_an_email_must_be_an_address_and_empty_turns_it_off():
    assert ps.PipelineSettingsUpdate(report_email=" jack@example.com ").report_email == "jack@example.com"
    assert ps.PipelineSettingsUpdate(report_email="").report_email == ""
    for bad in ("jack", "jack@", "@example.com", "jack example.com"):
        with pytest.raises(ValidationError):
            ps.PipelineSettingsUpdate(report_email=bad)


def test_a_weekday_must_be_a_day_name():
    assert ps.PipelineSettingsUpdate(weekday="Saturday").weekday == "saturday"
    for bad in ("sun", "6", "someday"):
        with pytest.raises(ValidationError):
            ps.PipelineSettingsUpdate(weekday=bad)
