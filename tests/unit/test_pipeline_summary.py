"""The weekly pipeline's summary email (plan step 11, item 9): the pure builder, and the
sender with Brevo stood in. No mail server."""
from tests.unit._adapters import email_module, pipeline_summary as summary

LAPTOP = {"laptop_id": "L1", "model_code": "G614JV", "product_name": "ROG Strix G16"}


def _completed() -> dict:
    return {
        "stopped_at": None,
        "steps": {
            "crawl": {"asus": {"total_found": 40, "added_to_queue": 2,
                               "missing_pages": ["https://www.asus.com/gone/"]},
                      "hp": {"error": "TimeoutError: crawl"}},
            "scrape": {"rog": {"pages": 5, "succeeded": 4, "failed": 1, "skipped": 0,
                               "failed_pages": [{"url": "https://rog.asus.com/x/", "error": "404"}]}},
            "process": {"requests_made": 3, "total_new_variants_saved": 2, "total_variants_updated": 1,
                        "skus_attached": [],
                        "writes": [{"laptop_id": "L1", "action": "inserted"},
                                   {"laptop_id": "L2", "action": "inserted"},
                                   {"laptop_id": "L9", "action": "updated"}],
                        "sku_price_differences": [{**LAPTOP, "kept_price": 5999.0,
                                                   "other_skus": {"G614JV-N3": 6299.0}}]},
            "go_live": {"checked": 3, "activated": [LAPTOP],
                        "held": [{"laptop_id": "L2", "model_code": "FX608JM", "product_name": "TUF A16",
                                  "failed": ["price", "gpu_benchmark"]}],
                        "no_family": [{"laptop_id": "L2", "model_code": "FX608JM", "product_name": "TUF A16"},
                                      {"laptop_id": "L7", "model_code": "OLD1", "product_name": "Old one"}]},
            "pick_scores": {"skipped": "no laptop went live and no price changed"},
        },
        "went_live": [LAPTOP],
        "price_changes": [
            {"laptop_id": "L9", "model_code": "UX3405", "product_name": "Zenbook 14",
             "old_price": 5000.0, "new_price": 3500.0, "source": "page"},  # 30%: at the threshold
            {"laptop_id": "L8", "model_code": "B9406", "product_name": "ExpertBook",
             "old_price": 10000.0, "new_price": 10500.0, "source": "guide"},  # 5%
        ],
    }


def _sections(text: str) -> dict:
    out = {}
    for block in text.split("\n\n"):
        title, *lines = block.split("\n")
        out[title] = lines
    return out


def test_each_section_of_a_completed_run():
    subject, text, html = summary.build_summary(_completed(), threshold_percent=30)
    s = _sections(text)
    assert s["Outcome"] == ["- Completed."]
    assert "- Crawl asus: 40 pages found, 2 new" in s["Steps"]
    assert "- Crawl hp: failed, TimeoutError: crawl" in s["Steps"]
    assert "- Process: 3 raw rows, 2 new laptops, 1 updated, 0 SKUs attached" in s["Steps"]
    assert "- Go-live check: 3 held laptops checked, 1 went live" in s["Steps"]
    assert "- Pick Score: skipped, no laptop went live and no price changed" in s["Steps"]
    assert s["Went live"] == ["- G614JV ROG Strix G16"]
    assert s["Held back"] == ["- FX608JM TUF A16: failed price, gpu_benchmark"]
    assert s["SKUs of one laptop at different prices"] == ["- G614JV ROG Strix G16: keeps RM5,999; G614JV-N3 RM6,299"]
    assert s["Pages that failed or disappeared"] == [
        "- Failed (rog): https://rog.asus.com/x/: 404",
        "- Not found by the crawl (asus): https://www.asus.com/gone/"]
    # Only laptops this run inserted: L7 has no family too, but is not new.
    assert s["New laptops with no family"] == ["- FX608JM TUF A16"]
    assert len(s["Price changes"]) == 2
    assert "<h3>Went live</h3>" in html and "G614JV ROG Strix G16" in html


def test_price_changes_at_or_above_the_threshold_are_flagged_at_the_top():
    subject, text, _ = summary.build_summary(_completed(), threshold_percent=30)
    titles = [block.split("\n")[0] for block in text.split("\n\n")]
    assert titles[1] == "Price changes of 30% or more"
    s = _sections(text)
    assert s["Price changes of 30% or more"] == ["- UX3405 Zenbook 14: RM5,000 to RM3,500 (30%, page)"]
    assert s["Price changes"][0].startswith("- [!] UX3405")
    assert not s["Price changes"][1].startswith("- [!]")
    assert subject == "PickWise weekly run completed, 1 price jump"


def test_a_higher_threshold_flags_nothing():
    subject, text, _ = summary.build_summary(_completed(), threshold_percent=31)
    assert "Price changes of" not in text and "[!]" not in text
    assert subject == "PickWise weekly run completed"


def test_a_skipped_run_says_why():
    subject, text, _ = summary.build_summary({"skipped": "another scrape or processing job is active (job J1)"}, 30)
    assert _sections(text)["Outcome"] == ["- Skipped: another scrape or processing job is active (job J1)."]
    assert "Steps" not in _sections(text)
    assert subject == "PickWise weekly run skipped"


def test_a_stopped_run_names_the_step_its_error_and_what_did_not_run():
    result = {"steps": {"crawl": {}}, "stopped_at": "scrape", "error": "OperationalError: gone",
              "not_run": ["process", "guides"], "went_live": [], "price_changes": []}
    subject, text, _ = summary.build_summary(result, 30)
    assert _sections(text)["Outcome"] == ["- Stopped at scrape: OperationalError: gone. Not run: process, guides."]
    assert subject == "PickWise weekly run stopped"


def test_html_escapes_what_the_pages_said():
    result = {**_completed(), "went_live": [{"laptop_id": "L", "model_code": "<b>", "product_name": "x&y"}]}
    _, _, html = summary.build_summary(result, 30)
    assert "&lt;b&gt; x&amp;y" in html and "<b>" not in html


def test_no_report_email_sends_nothing_and_says_so(monkeypatch):
    email = email_module()
    monkeypatch.setattr(email, "_send", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))
    assert summary.send_summary(_completed(), "", 30) == {"sent": False, "reason": "no report email is set in admin"}


def test_the_email_goes_to_the_report_email(monkeypatch):
    email = email_module()
    sent = []
    monkeypatch.setattr(email, "_send", lambda to, subject, html, text: sent.append((to, subject)) or True)
    assert summary.send_summary(_completed(), "jack@example.com", 30) == {"sent": True, "to": "jack@example.com"}
    assert sent == [("jack@example.com", "PickWise weekly run completed, 1 price jump")]


def test_a_refused_email_is_reported(monkeypatch):
    email = email_module()
    monkeypatch.setattr(email, "_send", lambda *a: False)
    assert summary.send_summary(_completed(), "jack@example.com", 30)["sent"] is False
