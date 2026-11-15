"""
The summary email of a weekly pipeline run (plan step 11, item 9; decisions 8 and 13).

`build_summary` is pure: it reads the run's result and returns the subject and the
text and HTML bodies. `send_summary` sends them through the Brevo client the auth
emails use, from the configured sender (noreply@ngyijie.com), to the admin's report
email. An empty setting sends nothing, and the run's result says so.
"""
import html as html_lib
import re
from typing import Optional

from app.scraper.bulk_scraper import page_key

# A fetch that says the page is gone, not that the site was slow (decision 8).
_GONE_ERRORS = ("HTTP 404", "No spec rows found")


def _money(value) -> str:
    return f"RM{value:,.0f}" if value else "no price"


def _laptop(entry: dict) -> str:
    return f"{entry.get('model_code') or entry.get('laptop_id')} {entry.get('product_name') or ''}".strip()


def jump_percent(change: dict) -> Optional[float]:
    old, new = change.get("old_price"), change.get("new_price")
    if not old or new is None:
        return None
    return abs(new - old) / old * 100


def _step_lines(steps: dict) -> list[str]:
    """One line per step that ran, with its counts."""
    lines = []
    for brand, crawl in (steps.get("crawl") or {}).items():
        lines.append(f"Crawl {brand}: " + (f"failed, {crawl['error']}" if "error" in crawl else
                     f"{crawl.get('total_found', 0)} pages found, {crawl.get('added_to_queue', 0)} new"))
    for brand, scrape in (steps.get("scrape") or {}).items():
        lines.append(f"Scrape {brand}: " + (f"failed, {scrape['error']}" if "error" in scrape else
                     f"{scrape.get('pages', 0)} pages, {scrape.get('succeeded', 0)} scraped, "
                     f"{scrape.get('failed', 0)} failed, {scrape.get('skipped', 0)} unchanged or skipped"))
    process = steps.get("process")
    if process is not None:
        lines.append(f"Process: {process.get('requests_made', 0)} raw rows, "
                     f"{process.get('total_new_variants_saved', 0)} new laptops, "
                     f"{process.get('total_variants_updated', 0)} updated, "
                     f"{len(process.get('skus_attached') or [])} SKUs attached")
    for kind, guide in (steps.get("guides") or {}).items():
        lines.append(f"Guide {kind}: " + (f"failed, {guide['error']}" if "error" in guide else
                     f"{guide.get('filled', 0)} prices filled, {guide.get('confirmed', 0)} confirmed, "
                     f"{guide.get('differs_from_guide', 0)} differ"))
    for source, mark in (steps.get("passmark") or {}).items():
        lines.append(f"PassMark {source.upper()}: " + (f"failed, {mark['error']}" if "error" in mark else
                     f"{mark.get('rows_written', 0)} rows"))
    go_live = steps.get("go_live")
    if go_live is not None:
        lines.append(f"Go-live check: {go_live.get('checked', 0)} held laptops checked, "
                     f"{len(go_live.get('activated') or [])} went live")
    embeddings = steps.get("embeddings")
    if embeddings is not None:
        lines.append(f"Embeddings: {embeddings.get('embedded', 0)} built, {len(embeddings.get('failed') or [])} failed")
    scores = steps.get("pick_scores")
    if scores is not None:
        lines.append(f"Pick Score: {scores.get('laptops', 0)} laptops scored")
    return lines


def _last_go_live(steps: dict) -> dict:
    """The re-check's report when it ran, else the one at the end of processing."""
    return steps.get("go_live") or (steps.get("process") or {}).get("go_live") or {}


def _pages(steps: dict) -> tuple[list[str], list[str], list[str]]:
    """(likely gone, not seen but still working, other failed fetches). A missing page is
    likely gone when its fetch failed this run with a 404 or no spec rows; both sides
    are compared as the crawler normalises a page."""
    failed = {page_key(p["url"]): (brand, p) for brand, scrape in (steps.get("scrape") or {}).items()
              for p in scrape.get("failed_pages") or []}
    gone, working = [], []
    for crawl in (steps.get("crawl") or {}).values():
        for page in crawl.get("missing_pages") or []:
            _, fetch = failed.get(page_key(page["url"]), (None, {}))
            if any(e in (fetch.get("error") or "") for e in _GONE_ERRORS):
                failed.pop(page_key(page["url"]))
                laptops = ", ".join(f"{_laptop(l)}, {l.get('status')}" for l in page.get("laptops") or [])
                gone.append(f"{page['url']} ({fetch['error']}). From it: "
                            + (laptops or "no catalog laptop traced to this page"))
            else:
                working.append(page["url"])
    other = [f"{brand}: {p['url']}: {p.get('error') or 'no reason given'}" for brand, p in failed.values()]
    return gone, working, other


def build_summary(result: dict, threshold_percent: int, admin_url: str = "") -> tuple[str, str, str]:
    """(subject, text, html) for one run. *result* is the run's job result: `steps`,
    `stopped_at`/`error`/`not_run` for a stopped run, `skipped` for a skipped one,
    `went_live` and `price_changes`. *admin_url* is the frontend's base URL, for links."""
    steps = result.get("steps") or {}
    sections: list[tuple[str, list[str]]] = []
    folded: set[str] = set()  # titles shown folded in HTML

    if result.get("skipped"):
        outcome = f"Skipped: {result['skipped']}."
    elif result.get("stopped_at"):
        not_run = ", ".join(result.get("not_run") or []) or "none"
        outcome = f"Stopped at {result['stopped_at']}: {result.get('error')}. Not run: {not_run}."
    else:
        outcome = "Completed."
    sections.append(("Outcome", [outcome]))

    changes = result.get("price_changes") or []
    jumps = [c for c in changes if (jump_percent(c) or 0) >= threshold_percent]
    if jumps:
        sections.append((f"Price changes of {threshold_percent}% or more", [
            f"{_laptop(c)}: {_money(c['old_price'])} to {_money(c['new_price'])} "
            f"({jump_percent(c):.0f}%, {c.get('source') or 'no source'})" for c in jumps]))

    if steps:
        sections.append(("Steps", _step_lines(steps)))

    sections.append(("Went live", [_laptop(l) for l in result.get("went_live") or []] or ["None."]))

    go_live = _last_go_live(steps)
    held = go_live.get("held") or []
    sections.append(("Held back", [f"{_laptop(l)}: failed {', '.join(l['failed'])}" for l in held] or ["None."]))

    guides = [f"{kind}: {g.get('awaiting_confirmation', 0)} prices awaiting your confirmation, "
              f"{g.get('differs_from_guide', 0)} differ from the guide. {admin_url}/admin/price-guides"
              for kind, g in (steps.get("guides") or {}).items() if "error" not in g]
    sections.append(("Product guides: these need a person", guides or ["None."]))

    sections.append(("Price changes", [
        f"{'[!] ' if c in jumps else ''}{_laptop(c)}: {_money(c['old_price'])} to {_money(c['new_price'])}"
        f" ({c.get('source') or 'no source'})" for c in changes] or ["None."]))

    differences = (steps.get("process") or {}).get("sku_price_differences") or []
    sections.append(("SKUs of one laptop at different prices", [
        f"{_laptop(d)}: keeps {_money(d['kept_price'])}; "
        + ", ".join(f"{sku} {_money(p)}" for sku, p in (d.get("other_skus") or {}).items())
        for d in differences] or ["None."]))

    gone, working, other = _pages(steps)
    sections.append(("Pages likely gone: not found by the crawl, and the fetch failed", gone or ["None."]))
    if working:
        title = "Not seen by the crawl, but the page still works"
        sections.append((title, [f"{len(working)} page{'s' if len(working) != 1 else ''}"] + working))
        folded.add(title)

    no_price = list(dict.fromkeys((steps.get("process") or {}).get("prices_not_extracted") or []))
    sections.append(("Pages that gave no price (the old price was kept)", no_price or ["None."]))
    sections.append(("Other pages that failed", other or ["None."]))

    inserted = {w["laptop_id"] for w in (steps.get("process") or {}).get("writes") or [] if w.get("action") == "inserted"}
    no_family = [l for l in go_live.get("no_family") or [] if l["laptop_id"] in inserted]
    sections.append(("New laptops with no family", [_laptop(l) for l in no_family] or ["None."]))

    status = "skipped" if result.get("skipped") else "stopped" if result.get("stopped_at") else "completed"
    flag = f", {len(jumps)} price jump{'s' if len(jumps) != 1 else ''}" if jumps else ""
    subject = f"PickWise weekly run {status}{flag}"

    text = "\n\n".join(f"{title}\n" + "\n".join(f"- {line}" for line in lines) for title, lines in sections)
    def li(lines: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{_linked(line)}</li>" for line in lines) + "</ul>"

    body = "".join(
        f"<h3>{html_lib.escape(title)}</h3>"
        + (f"<details><summary>{html_lib.escape(lines[0])}</summary>{li(lines[1:])}</details>" if title in folded else li(lines))
        for title, lines in sections)
    return subject, text, f"<html><body>{body}</body></html>"


def _linked(line: str) -> str:
    return re.sub(r"https?://[^\s<)]+", lambda m: f'<a href="{m[0]}">{m[0]}</a>', html_lib.escape(line))


def send_summary(result: dict, report_email: str, threshold_percent: int, admin_url: str = "") -> dict:
    """Send the run's email; what happened goes into the run's result."""
    if not report_email:
        return {"sent": False, "reason": "no report email is set in admin"}
    from app.users.email import _send
    subject, text, html = build_summary(result, threshold_percent, admin_url)
    sent = _send(report_email, subject, html, text)
    return {"sent": sent, "to": report_email} if sent else {"sent": False, "to": report_email,
                                                            "reason": "Brevo refused it or could not be reached; see the log"}
