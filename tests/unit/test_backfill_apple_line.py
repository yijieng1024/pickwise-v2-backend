"""The MacBook Pro line backfill decides everything in link_new_rows() and plan()."""
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.unit._adapters import backfill_apple_line as backfill

JUNE = datetime(2026, 6, 22)
PROLED = "https://www.apple.com/my/macbook-pro/proled-xdr-display/specs/"
LIQUID = "https://www.apple.com/my/macbook-pro/liquid-retina-xdr-display/specs/"


def _row(code, created, name="old", display_type="Liquid Retina XDR", **kw):
    base = dict(id=code, model_code=code, product_name=name, status="active", created_at=created,
                display_type=display_type, display_size_inch=14.2, processor_model="Apple M5 Pro chip",
                cpu_cores=15, gpu_cores=16, ram_gb=24, ssd_gb=2048)
    base.update(kw)
    return SimpleNamespace(**base)


def _at(minute, second):
    return datetime(2026, 11, 3, 6, minute, second)


def _job(*records):
    return SimpleNamespace(
        started_at=datetime(2026, 11, 3, 6, 52, 30, tzinfo=timezone.utc),
        finished_at=datetime(2026, 11, 3, 6, 55, tzinfo=timezone.utc),
        result={"details": [{"raw_id": r, "variants_saved": n} for r, n in records]},
    )


CODE = "apple-macbook-pro-14-m5-pro-15c-16g-{}"


def test_backfill_plan():
    """The old page is Liquid Retina even where today's PROLED page overwrote the row; a
    new row takes the line of the raw page that made it, whatever display_type says; Air
    is untouched; and a second run does nothing."""
    rows = [
        _row(CODE.format("24gb-2048gb"), JUNE, display_type="PROLED XDR display"),
        # made by the PROLED page, then overwritten by the Liquid Retina one (5f0c1326)
        _row(CODE.format("48gb-1024gb"), _at(53, 31), ram_gb=48, ssd_gb=1024),
        _row(CODE.format("24gb-4096gb"), _at(54, 48), display_type="PROLED XDR display", ssd_gb=4096),
        _row("apple-macbook-air-13-m5-10c-10g-16gb-512gb", JUNE),
    ]
    links = backfill.link_new_rows([_job(("p", 1), ("l", 1))], rows, {"p": PROLED, "l": LIQUID})
    writes, skips = backfill.plan(rows, links)
    assert [(c, n) for _, c, n in writes] == [
        ("apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb",
         "14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"),
        ("apple-macbook-pro-14-proled-m5-pro-15c-16g-48gb-1024gb",
         "14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 48GB RAM, 1TB SSD)"),
        ("apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-4096gb",
         "14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 4TB SSD)"),
    ]
    assert skips == []

    done = [_row(c, l.created_at, name=n, ram_gb=l.ram_gb, ssd_gb=l.ssd_gb) for l, c, n in writes]
    again, skipped = backfill.plan(done, {d.id: links[l.id] for d, (l, _, _) in zip(done, writes) if l.id in links})
    assert again == [] and {r for _, r in skipped} == {"already done"}


def test_new_row_with_no_raw_link_is_skipped():
    """No display_type fallback: a new row no job links to a raw page is skipped and
    listed, and so is every row of a job whose counts do not add up."""
    rows = [_row(CODE.format("48gb-1024gb"), _at(53, 31), display_type="PROLED XDR display", ram_gb=48),
            _row(CODE.format("48gb-2048gb"), _at(53, 32), display_type="PROLED XDR display", ram_gb=48)]
    assert backfill.plan(rows, {}) == ([], [(rows[0], "no raw page linked"), (rows[1], "no raw page linked")])
    assert backfill.link_new_rows([_job(("p", 3))], rows, {"p": PROLED}) == {}


def test_unknown_raw_page_is_skipped():
    row = _row(CODE.format("48gb-1024gb"), _at(53, 31), ram_gb=48)
    url = "https://www.apple.com/my/macbook-pro/nano-texture/specs/"
    writes, skips = backfill.plan([row], {row.id: url})
    assert writes == [] and skips == [(row, f"unknown page: {url}")]


def test_backfill_refuses_a_code_collision():
    """The unique index: a new code another row already holds is skipped, not written."""
    taken = "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb"
    rows = [_row(CODE.format("24gb-2048gb"), JUNE), _row(taken, JUNE, name="other")]
    writes, skips = backfill.plan(rows)
    assert all(c != taken for _, c, _ in writes)
    assert any("collides" in r for _, r in skips)
