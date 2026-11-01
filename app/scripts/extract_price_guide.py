"""
Read SKUs and prices out of an ASUS product guide PDF. Touches no database and
no network: the PDF goes in, rows.json comes out (write_price_guide stores them).

    python -m app.scripts.extract_price_guide guide.pdf --kind consumer --out rows.json

Two page layouts, found by content rather than page numbers:

- Spec sheets (consumer, ROG): one product per column, under a "Model Name" row.
  Colour variants stack in one column over one price, or sit side by side.
- Price tables (business): one product per row, under a MODEL NAME ... PRICE
  header. The SKU wraps onto two lines.

Words are paired by position, because the ROG pages have no ruled tables. Odd
pages also carry the facing page's words at negative x, so those are dropped
first; without that every SKU on the left page comes out again, on the wrong page.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

SKU = re.compile(r"^[A-Z]{1,3}\d{3,4}[A-Z]{0,4}-[A-Z0-9]{5,}$")
SKU_HEAD = re.compile(r"^[A-Z]{1,3}\d{3,4}[A-Z]{0,4}-$")
SKU_TAIL = re.compile(r"^[A-Z0-9]{4,}$")
# Desktops and all-in-ones share the guides but are not in this catalog, and
# Chromebooks are no longer sold. One that slips past this list only lands in
# the preview's unmatched list.
NOT_A_LAPTOP = re.compile(
    r"^(V50[01]|S501|VM640|V440|V470|G700|T500|GT|G22|G13|S700|M700|D[579]\d\d|PM[567]\d\d[A-Z]|PN\d|PB\d|E5\d\d|A5\d\d"
    r"|P50\d|P44\d|P47\d|E16\d\d|C[XZRM]\d)"
)
_PRICE = re.compile(r"^RM\s?(\d{1,3}(?:,\d{3})+|\d{3,})$")
_MONEY = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d{3,})$")
_MARKS = dict.fromkeys(map(ord, "®™©"), " ")  # a space: "Intel®Core™" has none of its own
_CPU_START = re.compile(r"\b(Intel|AMD|Qualcomm|Snapdragon)\b")
_CORE_COUNT = re.compile(r"\(\d+-core\)\s*", re.IGNORECASE)  # "X2 Elite (18-core) X2E88100"
_CPU_END = re.compile(
    r"\s*(\(|\d+(\.\d+)?\s*G?Hz|\d+\s*[GT]B\b|\bCache\b|\bup to\b|\bNPU\b|\bN/A\b|\bAMD XDNA|\bIntel AI"
    r"|\bQualcomm Hexagon|\bWindows\b|P ?r ?o c e).*$"
)
_RAM = re.compile(r"(?:DDR\dX?\s+(\d+)\s*GB|\b(\d+)\s*GB\s+(?:LP)?DDR)", re.IGNORECASE)
_SSD = re.compile(
    r"\b(\d+)\s*(GB|TB)(?:\s*\+\s*(\d+)\s*(GB|TB))?\s+(?:PCIe|M\.2|NVMe|SSD)", re.IGNORECASE
)
_SIZE = re.compile(r"^(\d+)\s*(GB|TB)$", re.IGNORECASE)

_COLUMN_REACH = 60.0  # a price further sideways than this belongs to another column
_ROW_TOLERANCE = 3.0


def _centre(word: dict) -> float:
    return (word["x0"] + word["x1"]) / 2


def _on_page(words: list[dict]) -> list[dict]:
    return [w for w in words if w["x0"] >= 0]


def _money(text: str) -> int:
    return int(text.replace(",", ""))


def _prices(words: list[dict]) -> list[tuple[dict, int]]:
    """Price tokens with their position. Consumer pages write "RM" "5,699" as two
    words, ROG writes "RM36,999" as one, and consumer p77 prints a bare "7499",
    which counts only on the Price row itself."""
    price_rows = [w["top"] for w in words if w["text"] in ("Price", "PRICE")]
    found: list[tuple[dict, int]] = []
    consumed = set()
    for i, word in enumerate(words):
        whole = _PRICE.fullmatch(word["text"])
        if whole:
            found.append((word, _money(whole.group(1))))
        elif word["text"] == "RM" and i + 1 < len(words):
            nxt = words[i + 1]
            if (
                _MONEY.fullmatch(nxt["text"])
                and nxt["x0"] - word["x1"] < 8
                and abs(nxt["top"] - word["top"]) < _ROW_TOLERANCE
            ):
                consumed.add(i + 1)
                found.append(({**word, "x1": nxt["x1"]}, _money(nxt["text"])))
        elif i not in consumed and _MONEY.fullmatch(word["text"]) and any(
            abs(word["top"] - top) < _ROW_TOLERANCE for top in price_rows
        ):
            found.append((word, _money(word["text"])))
    return found


def _text(words: list[dict]) -> str:
    """Reading order. Words on one printed line can differ by a point in `top`, so
    lines are grouped before sorting by x."""
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: w["top"]):
        if lines and word["top"] - lines[-1][0]["top"] < _ROW_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    ordered = [w["text"] for line in lines for w in sorted(line, key=lambda w: w["x0"])]
    text = " ".join(ordered).translate(_MARKS).replace("RyzenTM", "Ryzen")
    return re.sub(r"\s+", " ", re.sub(r"(?<=\d)(?=Processor)", " ", text))


def _cpu(text: str) -> Optional[str]:
    """The first processor in *text*, cut before its clock speed and cache."""
    start = _CPU_START.search(text)
    if not start:
        return None
    cpu = _CPU_END.sub("", _CORE_COUNT.sub("", text[start.start():])).strip()
    return cpu or None


def _ram_gb(text: str) -> Optional[int]:
    match = _RAM.search(text)
    return int(match.group(1) or match.group(2)) if match else None


def _ssd_gb(text: str) -> Optional[int]:
    # Searched after the memory row, or "DDR5 16GB" + "PCIe ..." reads as 16GB storage
    memory = _RAM.search(text)
    match = _SSD.search(text, memory.end() if memory else 0)
    if not match:
        return None
    total = int(match.group(1)) * (1024 if match.group(2).upper() == "TB" else 1)
    if match.group(3):  # "1TB + 1TB PCIe"
        total += int(match.group(3)) * (1024 if match.group(4).upper() == "TB" else 1)
    return total


def _spec_sheet_rows(words: list[dict]) -> list[dict]:
    """One table per "Model Name" label. The prices mark the columns, not the SKUs:
    colour variants stack in a column or sit side by side, off its centre."""
    starts = sorted(w["top"] - 12 for w in words if w["text"] == "Model")
    rows = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else float("inf")
        table = [w for w in words if start <= w["top"] < end]
        columns = _prices(table)

        def column_of(word):
            return min(columns, key=lambda pv: abs(_centre(pv[0]) - _centre(word)), default=None)

        for sku in (w for w in table if SKU.match(w["text"])):
            own = column_of(sku)
            if own is None or abs(_centre(own[0]) - _centre(sku)) > _COLUMN_REACH:
                rows.append({"sku": sku["text"], "price_rm": None, "cpu": None, "ram_gb": None, "ssd_gb": None})
                continue
            text = _text([w for w in table if w["top"] > own[0]["top"] and column_of(w) is own])
            rows.append({
                "sku": sku["text"],
                "price_rm": float(own[1]),
                "cpu": _cpu(text),
                "ram_gb": _ram_gb(text),
                "ssd_gb": _ssd_gb(text),
            })
    return rows


def _gigabytes(text: str) -> Optional[int]:
    match = _SIZE.match(text.strip())
    if not match:
        return None
    return int(match.group(1)) * (1024 if match.group(2).upper() == "TB" else 1)


def _price_table_rows(words: list[dict]) -> list[dict]:
    """Rows under a MODEL NAME ... PRICE header; columns come from the header."""
    headers = sorted((w for w in words if w["text"] == "MODEL"), key=lambda w: w["top"])
    rows = []
    for i, header in enumerate(headers):
        end = headers[i + 1]["top"] if i + 1 < len(headers) else float("inf")
        cells = {
            w["text"]: w["x0"] for w in words
            if abs(w["top"] - header["top"]) < _ROW_TOLERANCE
            and w["text"] in ("OS", "PROCESSOR", "GRAPHICS", "DISPLAY", "COLOR", "STORAGE", "MEMORY", "PRICE")
        }
        if "PRICE" not in cells or "PROCESSOR" not in cells:
            continue
        table = [w for w in words if header["top"] + 8 < w["top"] < end]
        model_column = [w for w in table if w["x0"] < cells.get("OS", cells["PROCESSOR"]) - 4]
        heads = sorted(
            (w for w in model_column if SKU_HEAD.match(w["text"]) or SKU.match(w["text"])),
            key=lambda w: w["top"],
        )
        for j, head in enumerate(heads):
            tail = next((
                w for w in model_column
                if SKU_TAIL.match(w["text"]) and 4 < w["top"] - head["top"] < 14
            ), None)
            sku = head["text"] + (tail["text"] if tail and SKU_HEAD.match(head["text"]) else "")
            band_top = head["top"] - 8
            band_end = heads[j + 1]["top"] - 8 if j + 1 < len(heads) else band_top + 34
            band = [w for w in table if band_top <= w["top"] < band_end]

            def cell(name: str) -> str:
                stop = min((x for x in cells.values() if x > cells[name]), default=float("inf"))
                return _text([w for w in band if cells[name] - 6 <= w["x0"] < stop - 6])

            priced = [value for word, value in _prices(band) if word["x0"] >= cells["PRICE"] - 6]
            rows.append({
                "sku": sku,
                "price_rm": float(priced[0]) if priced else None,
                "cpu": _cpu(cell("PROCESSOR")),
                "ram_gb": _gigabytes(cell("MEMORY")),
                "ssd_gb": _gigabytes(cell("STORAGE")),
            })
    return rows


def pair_skus_with_prices(words: list[dict]) -> list[dict]:
    """Every SKU on one page with the price and specs printed for it.

    *words* are pdfplumber word dicts (`text`, `x0`, `x1`, `top`). A SKU with no
    price in its own column gets `price_rm: None`, never a neighbour's.
    """
    words = _on_page(words)
    if any(w["text"] == "MODEL" for w in words) and any(w["text"] == "PRICE" for w in words):
        return _price_table_rows(words)
    return _spec_sheet_rows(words)


def is_price_page(words: list[dict]) -> bool:
    """Both RM amounts and SKU-shaped words, on the printed page. Brochure pages
    have only a "From RM" line, and accessory pages have no SKUs."""
    words = _on_page(words)
    has_sku = any(SKU.match(w["text"]) or SKU_HEAD.match(w["text"]) for w in words)
    return has_sku and bool(_prices(words))


def extract_rows(pages: list[list[dict]]) -> tuple[list[dict], list[dict]]:
    """(rows, per-page summary). *pages* are word lists, first page first. A SKU
    printed twice keeps its first priced row."""
    by_sku: dict[str, dict] = {}
    summary = []
    for number, words in enumerate(pages, 1):
        if not is_price_page(words):
            continue
        found = pair_skus_with_prices(words)
        laptops = [r for r in found if not NOT_A_LAPTOP.match(r["sku"])]
        if found:
            summary.append({
                "page": number,
                "skus": len(laptops),
                "priced": sum(r["price_rm"] is not None for r in laptops),
                "skipped_not_laptop": len(found) - len(laptops),
            })
        for row in laptops:
            kept = by_sku.get(row["sku"])
            if kept is None or (kept["price_rm"] is None and row["price_rm"] is not None):
                by_sku[row["sku"]] = {**row, "page": number}
    return list(by_sku.values()), summary


def main() -> None:
    import pdfplumber  # only this path needs it

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--kind", required=True, choices=["consumer", "rog", "business"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    digest = hashlib.sha256()
    with args.pdf.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    pages, edition = [], ""
    with pdfplumber.open(args.pdf) as pdf:
        for index, page in enumerate(pdf.pages):
            pages.append(page.extract_words())
            if index == 0:
                edition = " ".join((page.extract_text() or "").split())[:200]
            page.close()

    rows, summary = extract_rows(pages)
    args.out.write_text(json.dumps({
        "kind": args.kind,
        "filename": args.pdf.name,
        "pdf_sha256": digest.hexdigest(),
        "edition_text": edition,
        "rows": rows,
    }, indent=1, ensure_ascii=False), encoding="utf-8")

    for line in summary:
        print(f"p{line['page']:>3}: {line['skus']:>3} SKUs, {line['priced']:>3} priced, "
              f"{line['skipped_not_laptop']} not laptops")
    print(f"{len(rows)} unique SKUs, {sum(r['price_rm'] is not None for r in rows)} priced -> {args.out}")


if __name__ == "__main__":
    main()
