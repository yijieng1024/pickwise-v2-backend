"""
The summary email of a weekly pipeline run (plan step 11, item 9; decisions 8 and 13).

`build_summary` is pure: it reads the run's result and returns the subject and the
text and HTML bodies. `send_summary` sends them through the Brevo client the auth
emails use, from the configured sender (noreply@ngyijie.com), to the admin's report
email. An empty setting sends nothing, and the run's result says so.
"""
import html as html_lib
from typing import Optional


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
        lines.append("Pick Score: " + (f"skipped, {scores['skipped']}" if "skipped" in scores else
                     f"{scores.get('laptops', 0)} laptops recalculated"))
    return lines


def _last_go_live(steps: dict) -> dict:
    """The re-check's report when it ran, else the one at the end of processing."""
    return steps.get("go_live") or (steps.get("process") or {}).get("go_live") or {}


def build_summary(result: dict, threshold_percent: int) -> tuple[str, str, str]:
    """(subject, text, html) for one run. *result* is the run's job result: `steps`,
    `stopped_at`/`error`/`not_run` for a stopped run, `skipped` for a skipped one,
    `went_live` and `price_changes`."""
    steps = result.get("steps") or {}
    sections: list[tuple[str, list[str]]] = []

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

    sections.append(("Price changes", [
        f"{'[!] ' if c in jumps else ''}{_laptop(c)}: {_money(c['old_price'])} to {_money(c['new_price'])}"
        f" ({c.get('source') or 'no source'})" for c in changes] or ["None."]))

    differences = (steps.get("process") or {}).get("sku_price_differences") or []
    sections.append(("SKUs of one laptop at different prices", [
        f"{_laptop(d)}: keeps {_money(d['kept_price'])}; "
        + ", ".join(f"{sku} {_money(p)}" for sku, p in (d.get("other_skus") or {}).items())
        for d in differences] or ["None."]))

    pages = []
    for brand, scrape in (steps.get("scrape") or {}).items():
        pages += [f"Failed ({brand}): {p['url']}: {p.get('error') or 'no reason given'}"
                  for p in scrape.get("failed_pages") or []]
    for brand, crawl in (steps.get("crawl") or {}).items():
        pages += [f"Not found by the crawl ({brand}): {url}" for url in crawl.get("missing_pages") or []]
    sections.append(("Pages that failed or disappeared", pages or ["None."]))

    inserted = {w["laptop_id"] for w in (steps.get("process") or {}).get("writes") or [] if w.get("action") == "inserted"}
    no_family = [l for l in go_live.get("no_family") or [] if l["laptop_id"] in inserted]
    sections.append(("New laptops with no family", [_laptop(l) for l in no_family] or ["None."]))

    status = "skipped" if result.get("skipped") else "stopped" if result.get("stopped_at") else "completed"
    flag = f", {len(jumps)} price jump{'s' if len(jumps) != 1 else ''}" if jumps else ""
    subject = f"PickWise weekly run {status}{flag}"

    text = "\n\n".join(f"{title}\n" + "\n".join(f"- {line}" for line in lines) for title, lines in sections)
    body = "".join(
        f"<h3>{html_lib.escape(title)}</h3><ul>" + "".join(f"<li>{html_lib.escape(line)}</li>" for line in lines) + "</ul>"
        for title, lines in sections)
    return subject, text, f"<html><body>{body}</body></html>"


def send_summary(result: dict, report_email: str, threshold_percent: int) -> dict:
    """Send the run's email; what happened goes into the run's result."""
    if not report_email:
        return {"sent": False, "reason": "no report email is set in admin"}
    from app.users.email import _send
    subject, text, html = build_summary(result, threshold_percent)
    sent = _send(report_email, subject, html, text)
    return {"sent": sent, "to": report_email} if sent else {"sent": False, "to": report_email,
                                                            "reason": "Brevo refused it or could not be reached; see the log"}
