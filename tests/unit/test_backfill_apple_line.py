"""The MacBook Pro line backfill decides everything in plan(); this checks it."""
from datetime import datetime
from types import SimpleNamespace

from tests.unit._adapters import backfill_apple_line as backfill

JUNE = datetime(2026, 6, 22)
TODAY = datetime(2026, 11, 3, 6, 53)


def _row(code, created, display_type="Liquid Retina XDR", name="old", **kw):
    base = dict(id=code, model_code=code, product_name=name, status="active", created_at=created,
                display_type=display_type, display_size_inch=14.2, processor_model="Apple M5 Pro chip",
                cpu_cores=15, gpu_cores=16, ram_gb=24, ssd_gb=2048)
    base.update(kw)
    return SimpleNamespace(**base)


def test_backfill_plan():
    """The old page is Liquid Retina even where today's PROLED page overwrote the row,
    Air is untouched, an unstated line is skipped, and a second run does nothing."""
    rows = [
        _row("apple-macbook-pro-14-m5-pro-15c-16g-24gb-2048gb", JUNE, display_type="PROLED XDR display"),
        _row("apple-macbook-pro-14-m5-pro-15c-16g-24gb-4096gb", TODAY, display_type="PROLED XDR display", ssd_gb=4096),
        _row("apple-macbook-pro-14-m5-pro-15c-16g-48gb-4096gb", TODAY, display_type="XDR", ram_gb=48),
        _row("apple-macbook-air-13-m5-10c-10g-16gb-512gb", JUNE),
    ]
    writes, skips = backfill.plan(rows)
    assert [(l.id[-15:], c, n) for l, c, n in writes] == [
        ("16g-24gb-2048gb", "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb",
         "14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"),
        ("16g-24gb-4096gb", "apple-macbook-pro-14-proled-m5-pro-15c-16g-24gb-4096gb",
         "14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 4TB SSD)"),
    ]
    assert [r for _, r in skips] == ["line unknown: display_type 'XDR'"]

    done = [_row(c, l.created_at, l.display_type, name=n, ssd_gb=l.ssd_gb) for l, c, n in writes]
    again, skipped = backfill.plan(done)
    assert again == [] and {r for _, r in skipped} == {"already done"}


def test_backfill_refuses_a_code_collision():
    """The unique index: a new code another row already holds is skipped, not written."""
    taken = "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb"
    rows = [_row("apple-macbook-pro-14-m5-pro-15c-16g-24gb-2048gb", JUNE),
            _row(taken, TODAY, name="other")]
    writes, skips = backfill.plan(rows)
    assert all(c != taken for _, c, _ in writes)
    assert any("collides" in r for _, r in skips)
