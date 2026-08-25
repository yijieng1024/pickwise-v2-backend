"""What the source material actually says about which configuration was tested.

The configuration step used to hand the human a list of four rows and ask which
one the reviewer had. Nothing on the screen could answer that: the title is
"Asus ExpertBook Ultra: The First Panther Lake Laptop I Strongly Recommend" —
no CPU, no GPU, no RAM — so answering meant watching the video, minutes of work
on a screen budgeted for ten seconds a review.

This module turns that investigation into a confirmation. It scans the video
description and the stored transcript for the spec strings that actually tell
this family's members apart, and returns each hit with its surrounding words.
Chinese-language channels routinely paste a full spec table into the
description, so on those the question is often simply answered outright.

Finding nothing is a real answer, not a failure — it means the video never says,
and the human should stop looking and leave the configuration unset. That is why
this always returns a result object with an explicit `searched` list rather than
an empty list that could equally mean "not scanned".

Two design rules worth keeping:

*Probes are drawn from the catalog rows, never from a general spec vocabulary.*
We only look for strings that would discriminate between THESE members. A
generic "find any CPU mentioned" scan would report the competitor's chip in a
comparison video as evidence about this family.

*A probe must be specific enough to be worth trusting.* "430" out of "AMD Ryzen
AI 5 430" is three digits that also appear in prices, view counts and
timestamps, so a bare numeric token is never used alone — see
`_probes_for_model`. A false hit is worse than no hit here: it is a
confident-looking wrong answer on a screen built to be trusted at a glance.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from app.laptops.laptop_models import Laptop

# Words that carry no discriminating information in a CPU/GPU string. Stripped
# before probe extraction so "Intel Core Ultra 7 Processor 358H" reduces to the
# part a reviewer would actually say out loud.
_NOISE_TOKENS = {
    "intel", "core", "processor", "cpu", "gpu", "amd", "ryzen", "nvidia",
    "geforce", "graphics", "vpro", "radeon", "arc", "apple", "laptop", "with",
    "series", "edition", "mobile",
}

# How much text to show either side of a hit. Enough to see the phrase it sits
# in — a bare "32GB" proves nothing, "記憶體 32GB / 1TB SSD" proves everything.
_CONTEXT_CHARS = 70

# Per probe, per source. A spec table repeated in three languages should not
# produce thirty rows on the screen.
_MAX_HITS_PER_PROBE = 2


@dataclass
class EvidenceHit:
    column: str
    label: str
    value: str          # the catalog value this is evidence for
    matched_text: str   # what was actually found in the source
    source: str         # "description" | "transcript"
    context: str
    # Transcript hits only: where in the video, so the human can jump straight
    # to it and confirm in seconds instead of scrubbing. None for description.
    timestamp_seconds: Optional[int] = None
    # Which members of the family carry this value. One id means this hit alone
    # narrows the family to a single row; three means it narrows nothing.
    laptop_ids: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "label": self.label,
            "value": self.value,
            "matched_text": self.matched_text,
            "source": self.source,
            "context": self.context,
            "timestamp_seconds": self.timestamp_seconds,
            "laptop_ids": self.laptop_ids,
        }


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _probes_for_model(value: str) -> list[str]:
    """Search strings for a CPU or GPU name, most specific first.

    Prefers the alphanumeric part number — "358H", "B390", "i7-1370P" — because
    that is what both a written spec table and a spoken review use, and it
    survives the vendor's marketing words changing around it.

    A token of digits alone is deliberately NOT used on its own. "430" from
    "AMD Ryzen AI 5 430" matches a price, a timestamp, a view count and half the
    numbers in any transcript; it is only used inside a longer phrase, where the
    preceding words carry the specificity.

    Returns [] for a name with no part number at all ("Intel Graphics"). That is
    correct rather than a gap: an integrated-graphics string identifies no
    specific part — the same reason `_score_gpu` treats it as a proxy — so it
    cannot be evidence for one member over another, and inventing a probe for it
    would match every row in the family at once.
    """
    tokens = [t for t in re.split(r"[\s,]+", value) if t]
    kept = [t for t in tokens if t.lower().strip("()") not in _NOISE_TOKENS]

    probes: list[str] = []
    for token in kept:
        bare = token.strip("()")
        has_digit = any(c.isdigit() for c in bare)
        has_alpha = any(c.isalpha() for c in bare)
        if has_digit and has_alpha and len(bare) >= 3:
            probes.append(bare)

    if not probes and kept:
        # No self-sufficient part number. Fall back to the tail of the cleaned
        # name as one phrase — "AI 5 430" — which is specific because of its
        # length, not because any single token in it is.
        tail = " ".join(kept[-3:])
        if any(c.isdigit() for c in tail) and len(tail) >= 5:
            probes.append(tail)

    return probes


def _probes_for_ram(value: int) -> list[str]:
    return [f"{value}GB", f"{value} GB"]


def _probes_for_storage(value: int) -> list[str]:
    """Storage is stored in GB but written in TB once past 1024, and both
    spellings occur in the wild — a spec table says "1TB", a spec list says
    "1024GB"."""
    probes = [f"{value}GB", f"{value} GB"]
    if value >= 1024 and value % 1024 == 0:
        tb = value // 1024
        probes += [f"{tb}TB", f"{tb} TB"]
    return probes


# Only these four columns are scanned. The rest of the differing-column set —
# panel type, weight, colour, price — is either never stated in a review or
# stated in words too generic to match on, and every extra probe is another
# chance at a false positive.
_PROBE_BUILDERS: dict[str, Callable[[Any], list[str]]] = {
    "processor_model": _probes_for_model,
    "gpu_model": _probes_for_model,
    "ram_gb": _probes_for_ram,
    "ssd_gb": _probes_for_storage,
}


def _transcript_text(segments: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Flatten transcript segments into one searchable string.

    Joined rather than searched segment by segment because a probe routinely
    straddles two segments — YouTube splits on timing, not on phrases — and a
    per-segment scan would miss exactly those. The returned index maps a
    character offset back to the second it was spoken at, so a hit still carries
    a timestamp.
    """
    parts: list[str] = []
    index: list[tuple[int, int]] = []
    cursor = 0
    for seg in segments:
        text = str(seg.get("text", "") or "")
        if not text:
            continue
        index.append((cursor, int(seg.get("start", 0) or 0)))
        parts.append(text)
        cursor += len(text) + 1
    return " ".join(parts), index


def _seconds_at(offset: int, index: list[tuple[int, int]]) -> Optional[int]:
    """The start time of the segment a character offset falls in.

    Linear rather than a bisect: the index is a few thousand entries at most and
    is walked a handful of times per scan, and a hand-rolled bisect over
    (offset, seconds) tuples is the kind of thing that goes subtly wrong.
    """
    found = None
    for start_offset, seconds in index:
        if start_offset > offset:
            break
        found = seconds
    return found


def _find(
    haystack: str,
    probe: str,
    offset_index: Optional[list[tuple[int, int]]],
) -> list[tuple[str, str, Optional[int]]]:
    """Case-insensitive search for one probe. Returns (matched, context, seconds).

    Word-bounded on whichever edges are alphanumeric, so "512GB" does not match
    inside "2512GB" while a probe with a space or punctuation at an edge still
    works.
    """
    if not haystack or not probe:
        return []
    left = r"\b" if probe[0].isalnum() else ""
    right = r"\b" if probe[-1].isalnum() else ""
    pattern = re.compile(left + re.escape(probe) + right, re.IGNORECASE)

    out: list[tuple[str, str, Optional[int]]] = []
    for match in pattern.finditer(haystack):
        start, end = match.span()
        context = _clean(
            haystack[max(0, start - _CONTEXT_CHARS) : end + _CONTEXT_CHARS]
        )
        seconds = _seconds_at(start, offset_index) if offset_index is not None else None
        out.append((match.group(0), context, seconds))
        if len(out) >= _MAX_HITS_PER_PROBE:
            break
    return out


def scan_config_evidence(
    description: Optional[str],
    transcript_segments: Optional[list[dict]],
    members: Iterable[Laptop],
    columns: list[str],
    label_for: Callable[[str], str],
) -> dict:
    """Scan the source material for anything that names one of these configs.

    `columns` is the family's differing-column set, so the scan asks only about
    fields that could change the answer — a family whose members all share a CPU
    is never searched for that CPU, because finding it would prove nothing.

    The result carries `searched` (which fields were looked for, and how many
    probes each produced) even when `hits` is empty, because "we looked and the
    video does not say" is the answer that lets the human stop, and it is only
    credible if the screen can show what was looked for.
    """
    members = list(members)
    scannable = [c for c in columns if c in _PROBE_BUILDERS]

    sources: list[tuple[str, str, Optional[list[tuple[int, int]]]]] = []
    if description:
        sources.append(("description", description, None))
    if transcript_segments:
        text, index = _transcript_text(transcript_segments)
        if text:
            sources.append(("transcript", text, index))

    hits: list[EvidenceHit] = []
    searched: list[dict] = []

    for column in scannable:
        builder = _PROBE_BUILDERS[column]
        # Distinct values, each carrying the rows that hold it — the same 32GB
        # is usually shared by several members, and the human needs to see that
        # a hit narrows the list to two rows rather than to one.
        by_value: dict[Any, list[Any]] = {}
        for laptop in members:
            value = getattr(laptop, column, None)
            if value is None:
                continue
            by_value.setdefault(value, []).append(laptop.id)

        probe_count = 0
        for value, laptop_ids in by_value.items():
            probes = builder(value)
            probe_count += len(probes)
            for probe in probes:
                for source_name, haystack, index in sources:
                    for matched, context, seconds in _find(haystack, probe, index):
                        hits.append(
                            EvidenceHit(
                                column=column,
                                label=label_for(column),
                                value=str(value),
                                matched_text=matched,
                                source=source_name,
                                context=context,
                                timestamp_seconds=seconds,
                                laptop_ids=laptop_ids,
                            )
                        )
        searched.append(
            {
                "column": column,
                "label": label_for(column),
                "distinct_values": len(by_value),
                "probes": probe_count,
            }
        )

    # Description before transcript: a written spec table is stronger evidence
    # than a passing mention, and ASR mis-hears part numbers — which is the one
    # thing these probes are made of.
    order = {"description": 0, "transcript": 1}
    hits.sort(
        key=lambda h: (order.get(h.source, 9), h.column, h.timestamp_seconds or 0)
    )

    return {
        "hits": [h.as_dict() for h in hits],
        "searched": searched,
        "sources_available": {
            "description": bool(description),
            "transcript": bool(transcript_segments),
        },
        # The plain answer the screen shows when it is true, and deliberately
        # not the same thing as "nothing was scanned": with no source material
        # at all this stays False, and sources_available tells the screen to say
        # the video has no description or transcript stored rather than to claim
        # the video says nothing.
        "found_nothing": not hits and bool(sources) and bool(scannable),
    }
