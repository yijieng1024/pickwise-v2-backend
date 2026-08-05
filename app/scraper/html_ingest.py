"""
html_ingest.py
--------------
Storage layer for `raw_product_htmls` — the generic side of the
upload-HTML-then-parse workflow.

It knows how to turn an uploaded page into a stored row (identify it, find the
target it belongs to, upsert it, advance the target's status) and how to read
that row back. It knows nothing about any brand's markup: the parsing lives in
the brand scraper, which only asks this module for a string.

Nothing here is laptop-specific except `_target_model_for()`, which maps a
product type to its target table. Adding monitors means adding one entry there.
"""

from typing import Optional
from urllib.parse import urlsplit
from uuid import UUID

from lxml import html as lxml_html
from sqlmodel import Session, select

from app.logger import get_logger
from app.scraper.models import ScrapeStatus, ScrapeTarget
from app.scraper.raw_html_model import RawProductHtml
from app.taxonomy.product_type_model import ProductType

logger = get_logger(__name__)

# Product type name → the table holding its scrape targets. `laptop_scrape_urls`
# is the only one today; a new product line adds a row here plus its own model.
_TARGET_MODELS = {
    "laptop": ScrapeTarget,
}

# Guards against someone uploading a video or a disk image by mistake. Saved
# product pages run ~300 KB.
MAX_HTML_BYTES = 8 * 1024 * 1024


class HtmlIngestError(Exception):
    """The document could not be stored (bad HTML, no canonical, no target)."""


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------


def decode_html(raw: bytes) -> str:
    """
    Decode an uploaded file to text.

    Browsers save pages in whatever encoding the site declared, so fall back
    through the common ones rather than failing on a stray byte.
    """
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_canonical_url(page_html: str | bytes) -> str:
    """
    The product URL a page belongs to, from its own `<link rel="canonical">`
    (falling back to `og:url`).

    This is what lets uploads be filename-agnostic — the page identifies itself.
    """
    try:
        doc = lxml_html.fromstring(page_html)
    except Exception as e:
        raise HtmlIngestError(f"not parseable as HTML — {e}") from e

    for xpath in (
        "//link[@rel='canonical']/@href",
        "//meta[@property='og:url']/@content",
    ):
        found = doc.xpath(xpath)
        if found and found[0].strip():
            return found[0].strip()

    return ""


def url_key(url: str) -> str:
    """
    Match key for a product URL — its slug, lowercased.

    Compares by the last path segment rather than the whole URL so a stored
    target still matches when it carries a trailing slash, a query string, or a
    different host/locale prefix than the page's canonical tag.
    """
    path = urlsplit(url.strip()).path.rstrip("/")
    return path.rsplit("/", 1)[-1].lower()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _target_model_for(product_type_name: str):
    model = _TARGET_MODELS.get(product_type_name.lower())
    if model is None:
        raise HtmlIngestError(
            f"No scrape-target table is registered for product type "
            f"'{product_type_name}'. Add it to _TARGET_MODELS in html_ingest.py."
        )
    return model


def get_product_type(session: Session, name: str) -> ProductType:
    product_type = session.exec(
        select(ProductType).where(ProductType.name.ilike(name))  # type: ignore[attr-defined]
    ).first()

    if product_type is None:
        raise HtmlIngestError(
            f"Product type '{name}' does not exist — create it via "
            "POST /product-types first."
        )
    return product_type


def resolve_target(
    session: Session,
    canonical_url: str,
    product_type_name: str = "laptop",
    brand_id: Optional[UUID] = None,
) -> ScrapeTarget:
    """
    Find the queued target a canonical URL belongs to.

    Exact URL match first, then a slug match so trailing slashes and query
    strings on either side do not prevent a hit. Restricted to *brand_id* when
    the caller pinned one, which turns "uploaded an ASUS page to the Acer
    importer" into a clear error instead of a silent cross-brand write.
    """
    model = _target_model_for(product_type_name)

    filters = []
    if brand_id is not None:
        filters.append(model.brand_id == brand_id)

    target = session.exec(
        select(model).where(model.url == canonical_url, *filters)
    ).first()
    if target is not None:
        return target

    key = url_key(canonical_url)
    if key:
        # `%/slug` also matches `%/slug?x=1`-free stored URLs; the trailing
        # filter below re-checks exactly, so a LIKE false positive cannot pass.
        candidates = session.exec(
            select(model).where(model.url.like(f"%/{key}%"), *filters)  # type: ignore[attr-defined]
        ).all()
        for candidate in candidates:
            if url_key(candidate.url) == key:
                return candidate

    raise HtmlIngestError(
        f"No queued scrape target matches {canonical_url}. Run the crawler for "
        "this brand first, or check that the page belongs to the brand selected."
    )


def get_raw_html(
    session: Session,
    url: str,
    product_type_name: str = "laptop",
    brand_id: Optional[UUID] = None,
) -> Optional[str]:
    """
    The stored HTML for a product URL, or None if nothing was uploaded yet.

    Same exact-then-slug matching as `resolve_target`. The slug pass selects
    only the id/url columns so a miss never drags every stored page into memory.
    """
    filters = []
    if brand_id is not None:
        filters.append(RawProductHtml.brand_id == brand_id)

    row = session.exec(
        select(RawProductHtml).where(RawProductHtml.canonical_url == url, *filters)
    ).first()
    if row is not None:
        return row.raw_html

    key = url_key(url)
    if not key:
        return None

    candidates = session.exec(
        select(RawProductHtml.id, RawProductHtml.canonical_url).where(
            RawProductHtml.canonical_url.like(f"%/{key}%"), *filters  # type: ignore[attr-defined]
        )
    ).all()

    for row_id, canonical_url in candidates:
        if url_key(canonical_url) == key:
            found = session.get(RawProductHtml, row_id)
            return found.raw_html if found else None

    return None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def store_html(
    session: Session,
    *,
    page_html: str,
    product_type: ProductType,
    target: ScrapeTarget,
    canonical_url: str,
) -> bool:
    """
    Upsert one page and advance its target to `html_uploaded`.

    Returns True when a row was inserted, False when an existing one was
    updated. Does not commit — the caller batches that.
    """
    from datetime import datetime, timezone

    existing = session.exec(
        select(RawProductHtml).where(RawProductHtml.canonical_url == canonical_url)
    ).first()

    if existing is not None:
        existing.raw_html = page_html
        existing.product_type_id = product_type.id
        existing.brand_id = target.brand_id
        existing.target_id = target.id
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        created = False
    else:
        session.add(
            RawProductHtml(
                product_type_id=product_type.id,
                brand_id=target.brand_id,
                target_id=target.id,
                canonical_url=canonical_url,
                raw_html=page_html,
            )
        )
        created = True

    # Re-parsing is what clears this; a previously parsed/failed target goes
    # back into the queue when fresh HTML arrives.
    target.scrape_status = ScrapeStatus.HTML_UPLOADED
    session.add(target)

    return created
