"""
Review processing (Review C): one Gemini call per 45-second transcript window
returns a paraphrase, sentiment, topic, scope and which laptop the window is
about; the answer decides where the claim is stored.

    laptop_review_chunks       attached to a linked family / configuration
    review_unattached_claims   about another laptop, several, none, or a
                               configuration-dependent claim whose
                               configuration is unknown (nothing public reads it)
    review_claims_staging      the next generation of a video's claims, swapped
                               in once every window has a row

A window has exactly one claim. Writing to staging instead of the live tables
is what keeps a re-run from emptying or half-updating a public page: a review
whose links changed (reprocess_requested_at) is re-processed into staging and
swapped in when complete, and the full re-run (app/scripts/reprocess_reviews)
stages everything and swaps all videos in one transaction.
"""
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlmodel import Session, select

from app.common import llm_config
from app.common.llm import build_llm
from app.common.llm_usage import LLMFeature
from app.embeddings.service import embed_text
from app.laptops.family_model import LaptopFamily
from app.logger import get_logger
from app.reviews.link_service import (
    column_label,
    config_label,
    differing_columns,
    effective_links,
    family_members,
    links_by_review,
    separability,
)
from app.reviews.models import (
    ChunkScope,
    LaptopReviewChunk,
    RawYoutubeReview,
    ReviewClaimStaging,
    ReviewStatus,
    ReviewTopic,
    ReviewUnattachedClaim,
    UnattachedReason,
    YoutubeChannel,
)

logger = get_logger(__name__)

_CHUNK_DURATION_SECONDS = 45
_INTER_REQUEST_DELAY = 4  # seconds between Gemini calls
_CONTEXT_CHARS = 300  # tail of the previous window, for resolving "it"

# --------------------------------------------------------------------------
# The rubric. The labelling page (eval/review_c) shows these same strings, so
# the model and the human labels are judged by one set of definitions.
# --------------------------------------------------------------------------
FIXED_ANSWERS = {
    "OTHER": "a laptop not in the list",
    "SEVERAL": "compares listed laptops, with no single subject",
    "NONE": "no laptop is discussed: intro, sponsor, channel talk",
}

ABOUT_RULE = (
    "Which laptop the excerpt's main point is about. Answer one id from the list. "
    "Use a configuration id (like F2.C1) only when the excerpt singles out that "
    "configuration, by its chip, GPU or memory, or because it is the unit the "
    "reviewer tested (marked in the list); otherwise use the family id (like F2). "
    "Answer OTHER for a laptop not in the list, SEVERAL when the excerpt compares "
    "listed laptops with no single subject, and NONE when no laptop is discussed "
    "(intro, sponsor, channel talk). Use the previous excerpt to resolve 'this one' "
    "or 'it'. A laptop that is only mentioned in passing (for example 'it will eat "
    "into the Air's sales') is not the subject."
)
SENTIMENT_RULE = (
    "Judge the main point, even when it is said politely: 'strength' if the reviewer "
    "is positive about it, 'weakness' if critical or warning (for example '8GB will "
    "struggle with longevity' is a weakness), 'neutral' for facts, specs, setup, "
    "prices stated without judgement, or sponsor talk."
)
TOPIC_DEFINITIONS = {
    ReviewTopic.THERMALS.value: "temperatures, fan noise, throttling, cooling",
    ReviewTopic.BUILD.value: "chassis, materials, build quality, keyboard, trackpad, hinge",
    ReviewTopic.DISPLAY.value: "screen quality: brightness, colour, resolution, refresh rate, panel type",
    ReviewTopic.BATTERY.value: "battery life, charging",
    ReviewTopic.PERFORMANCE.value: (
        "CPU/GPU speed, gaming frame rates, benchmarks, whether the RAM or storage is enough"
    ),
    ReviewTopic.PORTABILITY.value: "weight, thickness, physical size, carrying it around",
    ReviewTopic.PRICE.value: "any price, discount or value-for-money claim",
    ReviewTopic.OTHER.value: (
        "ports, speakers, webcam, software, operating system, AI features, sponsor, anything else"
    ),
}
SCOPE_RULE = (
    "'config' if the claim depends on parts that differ between the family's "
    "configurations (the list says what differs: a frame rate when the GPUs differ, "
    "'8GB is not enough' when the RAM differs); 'family' if it holds for every "
    "configuration (design, keyboard, a screen all configurations share). Answer "
    "'family' for OTHER, SEVERAL and NONE."
)

_SYSTEM_PROMPT = (
    "You are a laptop product analyst. You get one short excerpt from a YouTube "
    "laptop review transcript (which may be in any language), the video title, the "
    "laptops this review is linked to, and the end of the previous excerpt as "
    "context. Return:\n"
    "- summary: a 1-2 sentence English paraphrase of the excerpt's main point. Do NOT "
    "reproduce the transcript verbatim. Name its subject (for example 'The MacBook "
    "Air ...'), never just 'this laptop'.\n"
    f"- about: {ABOUT_RULE}\n"
    f"- sentiment: {SENTIMENT_RULE}\n"
    "- topic: one of "
    + "; ".join(f"'{t}' ({d})" for t, d in TOPIC_DEFINITIONS.items())
    + ".\n"
    f"- scope: {SCOPE_RULE}\n"
    "- other_laptop: when about is OTHER or SEVERAL, the laptop name(s) as the "
    "reviewer says them; otherwise null."
)

_HUMAN_PROMPT = (
    "Video title: {title}\n"
    "Laptops this review is linked to:\n{candidates}\n"
    "Other answers: "
    + ", ".join(f"{k} ({v})" for k, v in FIXED_ANSWERS.items())
    + "\nPrevious excerpt (context only, do not summarise): \"{previous}\"\n"
    "Excerpt: \"{text}\""
)


class _WindowAnalysis(BaseModel):
    summary: str
    about: str
    sentiment: Literal["strength", "weakness", "neutral"]
    topic: Literal[
        "thermals", "build", "display", "battery",
        "performance", "portability", "price", "other",
    ]
    scope: Literal["family", "config"]
    other_laptop: Optional[str] = None


class InvalidAnswer(ValueError):
    """The model named an id that was not offered. The window fails and is
    retried on the next run; it is never guessed."""


def _first_line(exc: Exception) -> str:
    """One-line exception text for a JSON payload. Several of the libraries in
    this path (google-genai, httpx) raise multi-line messages, and the raw
    string would make the /process-bulk response unreadable."""
    return str(exc).strip().split(chr(10))[0][:300]


def _chunk_transcript(
    transcript: list[dict], chunk_duration: int = _CHUNK_DURATION_SECONDS
) -> list[dict]:
    """Group raw transcript segments into ~chunk_duration-second windows."""
    chunks: list[dict] = []
    current: dict = {"texts": [], "start": None, "end": None}

    for seg in transcript:
        if current["start"] is None:
            current["start"] = int(seg["start"])
        current["texts"].append(seg["text"])
        current["end"] = int(seg["start"] + seg["duration"])

        if current["end"] - current["start"] >= chunk_duration:
            chunks.append(
                {
                    "text": " ".join(current["texts"]),
                    "start": current["start"],
                    "end": current["end"],
                }
            )
            current = {"texts": [], "start": None, "end": None}

    if current["texts"]:
        chunks.append(
            {
                "text": " ".join(current["texts"]),
                "start": current["start"],
                "end": current["end"],
            }
        )

    return chunks


def windows_with_context(segments: list[dict]) -> list[dict]:
    """Windows plus `previous`: the tail of the window before each one."""
    windows = _chunk_transcript(segments)
    for i, w in enumerate(windows):
        w["previous"] = windows[i - 1]["text"][-_CONTEXT_CHARS:] if i else ""
    return windows


# --------------------------------------------------------------------------
# Candidates: what the model may answer, and where each answer is stored.
# --------------------------------------------------------------------------

@dataclass
class Candidates:
    # "F1" -> (family_id, None), "F1.C2" -> (family_id, laptop_id)
    ids: dict[str, tuple[uuid.UUID, Optional[uuid.UUID]]] = field(default_factory=dict)
    # The one configuration a family's links name, if exactly one.
    link_config: dict[uuid.UUID, Optional[uuid.UUID]] = field(default_factory=dict)
    # The family's only active configuration, if it has exactly one.
    sole_member: dict[uuid.UUID, Optional[uuid.UUID]] = field(default_factory=dict)
    text: str = ""
    # For display (CLI, labelling page): id -> name.
    names: dict[str, str] = field(default_factory=dict)


def build_candidates(session: Session, links) -> Candidates:
    """The prompt's laptop list from a review's effective links.

    Each linked family is F1, F2... in link order. Its active (non-suspended)
    configurations are listed as Fk.C1... only when a review could tell them
    apart (link_service.separability); each is labelled by what differs within
    the family, and the family line says what differs, which is what the model
    needs to judge scope."""
    # Link order, so a review's ids are stable between runs.
    eff = sorted(effective_links(links), key=lambda l: (l.created_at, str(l.id)))
    family_ids = list(dict.fromkeys(l.family_id for l in eff))
    family_names = dict(session.exec(
        select(LaptopFamily.id, LaptopFamily.name).where(
            LaptopFamily.id.in_(family_ids)  # type: ignore[attr-defined]
        )
    ).all()) if family_ids else {}

    c = Candidates()
    lines = []
    for k, fid in enumerate(family_ids, start=1):
        fam = f"F{k}"
        name = family_names.get(fid, "?")
        c.ids[fam] = (fid, None)
        c.names[fam] = name
        linked = {l.laptop_id for l in eff if l.family_id == fid and l.laptop_id}
        c.link_config[fid] = next(iter(linked)) if len(linked) == 1 else None

        members = family_members(session, fid, exclude_statuses=("suspended",))
        c.sole_member[fid] = members[0].id if len(members) == 1 else None
        # Price is left out of the list too, for the reason separability ignores it.
        cols = [col for col in differing_columns(members) if col != "price_rm"]
        _, code, _ = separability(cols, len(members))
        shown = [column_label(col) for col in cols]
        if code == "single_config":
            note = "one configuration in the catalog"
        elif code == "identical_specs":
            note = "its configurations are identical in every tracked spec"
        elif code == "no_configs":
            note = "no configuration in the catalog"
        else:
            note = "configurations differ in: " + ", ".join(shown)
            if code == "ram_storage_only":
                note += " (not listed: a review cannot tell them apart)"
        lines.append(f"{fam}  {name} -- {note}")

        if code != "separable":
            continue
        seen_labels = set()
        for member in members:
            label = config_label(member, cols)
            # ponytail: catalog duplicates share a label; listing both would
            # make the model pick between identical rows, so the first wins.
            if label in seen_labels:
                continue
            seen_labels.add(label)
            cid = f"{fam}.C{len(seen_labels)}"
            c.ids[cid] = (fid, member.id)
            c.names[cid] = f"{name} {label}"
            mark = "   <- the configuration this review is linked to" if member.id in linked else ""
            lines.append(f"    {cid}  {label}{mark}")
    c.text = "\n".join(lines)
    return c


def parse_about(raw: str, cands: Candidates) -> str:
    about = raw.strip().upper().replace(" ", "")
    if about in cands.ids or about in FIXED_ANSWERS:
        return about
    raise InvalidAnswer(f"about={raw!r} is not one of {sorted(cands.ids) + list(FIXED_ANSWERS)}")


def place(about: str, scope: str, cands: Candidates) -> dict:
    """Where a window's claim is stored.

    Attached: {"family_id", "laptop_id", "scope"}.
    Unattached: {"reason", "family_id"}.

        Fk.Cj   config -> config chunk on Cj
        Fk.Cj   family -> family chunk, laptop_id Cj (tested on Cj)
        Fk      family -> family chunk, laptop_id = the link's config or NULL
        Fk      config -> config chunk on the link's config, or the family's
                          only configuration; otherwise unattached
                          config_unknown (a claim true of some configurations
                          must not be shown on all of them)
        OTHER / SEVERAL / NONE -> unattached
    """
    reasons = {
        "OTHER": UnattachedReason.OTHER_LAPTOP,
        "SEVERAL": UnattachedReason.SEVERAL,
        "NONE": UnattachedReason.NONE,
    }
    if about in reasons:
        return {"reason": reasons[about].value, "family_id": None}
    family_id, laptop_id = cands.ids[about]
    if laptop_id is not None:
        return {"family_id": family_id, "laptop_id": laptop_id, "scope": scope}
    if scope == ChunkScope.FAMILY.value:
        return {"family_id": family_id, "laptop_id": cands.link_config.get(family_id),
                "scope": scope}
    config = cands.link_config.get(family_id) or cands.sole_member.get(family_id)
    if config is not None:
        return {"family_id": family_id, "laptop_id": config, "scope": scope}
    return {"reason": UnattachedReason.CONFIG_UNKNOWN.value, "family_id": family_id}


def build_chain(feature: Optional[str] = LLMFeature.REVIEW_CHUNK):
    """The `review_processor` use: configured primary, then each backup in
    order, on 429 / timeout / 5xx. feature=None records no usage row (the
    CLI's dry run writes nothing)."""
    llm = build_llm(llm_config.REVIEW_PROCESSOR, temperature=0, feature=feature)
    return (
        ChatPromptTemplate.from_messages([("system", _SYSTEM_PROMPT), ("human", _HUMAN_PROMPT)])
        | llm.with_structured_output(_WindowAnalysis)
    )


def analyse_window(chain, cands: Candidates, title: str, window: dict) -> tuple[_WindowAnalysis, str, dict]:
    """(analysis, about, placement) for one window. Raises InvalidAnswer."""
    analysis: _WindowAnalysis = chain.invoke({  # type: ignore[assignment]
        "title": title,
        "candidates": cands.text,
        "previous": window.get("previous", ""),
        "text": window["text"],
    })
    about = parse_about(analysis.about, cands)
    return analysis, about, place(about, analysis.scope, cands)


# --------------------------------------------------------------------------
# Storage: live tables vs staging, and the swap.
# --------------------------------------------------------------------------

def _saved_windows(session: Session, video_ids=None, staging: bool = False) -> dict[str, set[int]]:
    """video_id -> window starts that already have a claim, in the live tables
    (chunks and unattached) or in staging."""
    models = (ReviewClaimStaging,) if staging else (LaptopReviewChunk, ReviewUnattachedClaim)
    out: dict[str, set[int]] = {}
    for model in models:
        statement = select(model.video_id, model.timestamp_start_seconds)
        if video_ids is not None:
            statement = statement.where(model.video_id.in_(list(video_ids)))  # type: ignore[attr-defined]
        for video_id, start in session.exec(statement).all():
            out.setdefault(video_id, set()).add(start)
    return out


_COMMON = (
    "id, video_id, channel_name, timestamp_start_seconds, timestamp_end_seconds, "
    "chunk_text, embedding, sentiment_tag, topic, created_at"
)


def swap_claims(session: Session, video_ids: list[str], keep_previous: bool) -> None:
    """Replace these videos' live claims with their staged ones, in the
    caller's transaction (it commits). With keep_previous the live rows move
    into staging, so calling it again swaps back; otherwise they are deleted.
    Clears the videos' reprocess_requested_at."""
    v = {"v": list(video_ids)}
    if keep_previous:
        session.execute(sql_text("DROP TABLE IF EXISTS _prev_claims"))
        session.execute(sql_text(
            "CREATE TEMP TABLE _prev_claims ON COMMIT DROP AS "
            f"SELECT {_COMMON}, family_id, laptop_id, scope, NULL::varchar AS reason, "
            "NULL::varchar AS mentioned_laptop FROM laptop_review_chunks WHERE video_id = ANY(:v) "
            f"UNION ALL SELECT {_COMMON}, family_id, NULL::uuid, NULL::varchar, reason, "
            "mentioned_laptop FROM review_unattached_claims WHERE video_id = ANY(:v)"
        ), v)
    session.execute(sql_text("DELETE FROM laptop_review_chunks WHERE video_id = ANY(:v)"), v)
    session.execute(sql_text("DELETE FROM review_unattached_claims WHERE video_id = ANY(:v)"), v)
    session.execute(sql_text(
        f"INSERT INTO laptop_review_chunks ({_COMMON}, family_id, laptop_id, scope) "
        f"SELECT {_COMMON}, family_id, laptop_id, scope FROM review_claims_staging "
        "WHERE video_id = ANY(:v) AND reason IS NULL"
    ), v)
    session.execute(sql_text(
        f"INSERT INTO review_unattached_claims ({_COMMON}, family_id, reason, mentioned_laptop) "
        f"SELECT {_COMMON}, family_id, reason, mentioned_laptop FROM review_claims_staging "
        "WHERE video_id = ANY(:v) AND reason IS NOT NULL"
    ), v)
    session.execute(sql_text("DELETE FROM review_claims_staging WHERE video_id = ANY(:v)"), v)
    if keep_previous:
        session.execute(sql_text(
            f"INSERT INTO review_claims_staging ({_COMMON}, family_id, laptop_id, scope, reason, "
            f"mentioned_laptop) SELECT {_COMMON}, family_id, laptop_id, scope, reason, "
            "mentioned_laptop FROM _prev_claims"
        ))
    session.execute(sql_text(
        "UPDATE raw_youtube_reviews SET reprocess_requested_at = NULL WHERE video_id = ANY(:v)"
    ), v)


def on_links_changed(session: Session, review_id: uuid.UUID) -> None:
    """A review's links changed: drop any staged claims (they were written for
    the old links) and, if it has live claims, mark it for re-processing. The
    live claims stay public until the new set is complete. Does not commit."""
    review = session.get(RawYoutubeReview, review_id)
    if review is None:
        return
    session.execute(
        sql_text("DELETE FROM review_claims_staging WHERE video_id = :v"), {"v": review.video_id}
    )
    if _saved_windows(session, [review.video_id]):
        review.reprocess_requested_at = datetime.now(timezone.utc)
        session.add(review)


class AlreadyProcessed(ValueError):
    """Every window of the transcript already has a claim.

    The duplicate guard. 11 of the 15 processed videos were processed twice
    before it existed (205 duplicate rows), because nothing stopped
    /process/{id} from running again on a finished review."""


def processing_candidates(session: Session) -> list[dict]:
    """Reviews /process-bulk should run, and /pipeline-status counts -- one
    function so the two can never disagree.

    A candidate is MATCHED, has at least one effective link and a transcript,
    and either has a window with no claim yet or is marked for re-processing
    (its staged set is incomplete or not yet swapped in). "Any chunk exists"
    is NOT "processed": a run that saved 3 of 40 windows is retried.
    """
    reviews = session.exec(
        select(RawYoutubeReview)
        .where(RawYoutubeReview.status == ReviewStatus.MATCHED.value)
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
    ).all()
    links = links_by_review(session, [r.id for r in reviews])
    live = _saved_windows(session)
    staged = _saved_windows(session, staging=True)

    out = []
    for r in reviews:
        segments = (r.raw_transcript or {}).get("segments", [])
        if not effective_links(links[r.id]) or not segments:
            continue
        expected = {c["start"] for c in _chunk_transcript(segments)}
        marked = r.reprocess_requested_at is not None
        done = (staged if marked else live).get(r.video_id, set()) & expected
        if marked or done != expected:
            out.append({
                "review_id": r.id,
                "video_title": r.video_title,
                "windows_expected": len(expected),
                "windows_saved": len(done),
                "reprocess": marked,
            })
    return out


def _claim_row(staging: bool, placement: dict, common: dict, other_laptop: Optional[str]):
    if staging:
        return ReviewClaimStaging(
            **common, **placement,
            mentioned_laptop=other_laptop if "reason" in placement else None,
        )
    if "reason" in placement:
        return ReviewUnattachedClaim(**common, **placement, mentioned_laptop=other_laptop)
    return LaptopReviewChunk(**common, **placement)


def process_raw_review(raw_review_id: uuid.UUID, session: Session, stage_only: bool = False) -> dict:
    """
    Analyse, embed and store every window of a matched review that has no
    claim yet.

    Where the windows go:
      - a review marked for re-processing (its links changed) goes to staging,
        and is swapped into the live tables once every window is staged;
      - stage_only (the full re-run) goes to staging and is never swapped
        here -- app/scripts/reprocess_reviews swaps all videos at once;
      - otherwise straight into the live tables.

    Returns a per-window outcome report:

        {"windows_expected", "windows_already_saved", "chunks_total",
         "chunks_saved", "chunks_failed", "failures": [...], "complete",
         "destination", "swapped"}

    `complete` is whether every window now has a claim in the destination; a
    partial run is retried by /process-bulk for its missing windows only.

    Raises AlreadyProcessed (a ValueError) when there is nothing left to do,
    and ValueError when the review is not processable.
    """
    raw: RawYoutubeReview | None = session.exec(
        select(RawYoutubeReview).where(RawYoutubeReview.id == raw_review_id)
    ).first()

    if not raw:
        raise ValueError(f"RawYoutubeReview {raw_review_id} not found.")
    links = links_by_review(session, [raw.id])[raw.id]
    if raw.status != ReviewStatus.MATCHED.value or not effective_links(links):
        raise ValueError(
            f"Review {raw_review_id} is not processable (status={raw.status}): it "
            "must be matched and linked to at least one family."
        )

    transcript_segments: list[dict] = (raw.raw_transcript or {}).get("segments", [])
    if not transcript_segments:
        raise ValueError(f"Review {raw_review_id} has no transcript segments stored.")

    # Read the columns we need BEFORE the loop: each window commits, and a
    # commit expires every loaded instance.
    video_id, title = raw.video_id, raw.video_title
    auto_swap = not stage_only and raw.reprocess_requested_at is not None
    staging = stage_only or auto_swap

    all_windows = windows_with_context(transcript_segments)
    already = _saved_windows(session, [video_id], staging=staging).get(video_id, set())
    windows = [w for w in all_windows if w["start"] not in already]
    already_saved = len(all_windows) - len(windows)
    report = {
        "windows_expected": len(all_windows),
        "windows_already_saved": already_saved,
        "destination": "staging" if staging else "live",
        "swapped": False,
    }
    if not windows:
        if auto_swap:
            swap_claims(session, [video_id], keep_previous=False)
            session.commit()
            return {**report, "complete": True, "chunks_total": 0, "chunks_saved": 0,
                    "chunks_failed": 0, "failures": [], "swapped": True}
        raise AlreadyProcessed(
            f"Review {raw_review_id} is already fully processed "
            f"({already_saved}/{len(all_windows)} windows saved)."
        )

    cands = build_candidates(session, links)
    channel = session.exec(
        select(YoutubeChannel).where(YoutubeChannel.channel_id == raw.channel_id)
    ).first()
    channel_name = channel.channel_name if channel else raw.channel_id
    chain = build_chain()
    # End the read transaction before the first Gemini + embedding call.
    session.commit()

    saved = 0
    failures: list[dict] = []

    for index, window in enumerate(windows):
        try:
            analysis, _, placement = analyse_window(chain, cands, title, window)
            common = dict(
                video_id=video_id,
                channel_name=channel_name,
                timestamp_start_seconds=window["start"],
                timestamp_end_seconds=window["end"],
                chunk_text=analysis.summary,
                embedding=embed_text(analysis.summary),
                sentiment_tag=analysis.sentiment,
                topic=analysis.topic,
            )
            session.add(_claim_row(staging, placement, common, analysis.other_laptop))
            # Commit per window: a 64-window review spends minutes in network
            # I/O, and holding one transaction across it pins a pooler
            # connection and throws away every finished window if the last
            # one fails. Saved windows are the "already processed" marker.
            session.commit()
            saved += 1
        except Exception as e:
            # A failed flush aborts the transaction; roll back or every later
            # commit fails with InFailedSqlTransaction.
            session.rollback()
            failures.append(
                {
                    "chunk_index": index,
                    "start_seconds": window["start"],
                    "end_seconds": window["end"],
                    "error_type": type(e).__name__,
                    "error": _first_line(e),
                }
            )
            logger.warning(
                "Window processing failed for video %s at %ds: %s: %s",
                video_id, window["start"], type(e).__name__, e,
            )
        finally:
            # In `finally`: the most likely reason a window fails IS a 429, and
            # skipping the delay on failure turns one 429 into a cascade.
            time.sleep(_INTER_REQUEST_DELAY)

    complete = already_saved + saved == len(all_windows)
    if complete and auto_swap:
        swap_claims(session, [video_id], keep_previous=False)
        session.commit()
        report["swapped"] = True

    logger.info(
        "Processed %d/%d windows for video %s into %s (%d failed, %d already saved)",
        saved, len(windows), video_id, report["destination"], len(failures), already_saved,
    )
    return {
        **report,
        "complete": complete,
        "chunks_total": len(windows),
        "chunks_saved": saved,
        "chunks_failed": len(failures),
        "failures": failures,
    }
