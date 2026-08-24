# ADR-0014 — Non-English review coverage: ASR declined

- **Status:** Accepted
- **Date:** 2026-08-22
- **Related:** ADR-0012 (review linkage), ADR-0013 (two-stage matching)

## Context

Chinese-language review videos appeared to lack transcripts, which would have
excluded them from the evidence pipeline. Since Malaysian and Singaporean Chinese
reviewers are the corpus most relevant to this product — they quote RM prices and
local SKUs, where global English reviewers quote USD MSRP — building speech
recognition to recover them was worth considering.

`app/scripts/audit_transcript_availability.py` was written to size the gap before
designing anything. It measured the reject bucket rather than assuming its cause.

### What the audit found

The premise was largely wrong. Of 39 rejected rows:

| Bucket | n | Cause |
|---|---|---|
| Operational failure | 24 | YouTube IP-blocking; **recoverable** |
| No caption track | 15 | Genuinely absent; terminal |

The dominant cause was never a language gap. `transcript.fetch_transcript`
carried `except Exception: return None`, which collapsed "no captions", "IP
blocked", "video private" and "network timeout" into one indistinguishable
outcome — the same defect shape as `price_rm = 0` meaning both "free" and
"unknown". The reject bucket's largest component was retryable and had been
silently discarded.

A second, narrower bug: `YouTubeTranscriptApi.fetch()` defaults `languages` to
the literal code `('en',)`. A video listing `zh-Hans`, `zh-Hant` and `en-US`
still raised `NoTranscriptFound`, because `en-US` is not `en`. Language matching
must be on family prefixes, not exact codes.

All 24 operational failures have since been recovered and moved to `pending`.
The terminal figure of **15** was reached three times by independent methods —
`.list()` enumeration, live re-fetch, and a post-recovery re-audit — and the
reject bucket is now pure: `failure_reason = no_track` for all 15, zero NULLs,
zero retryables.

### Current corpus state (85 rows)

| Status | n | % |
|---|---|---|
| pending | 52 | 61.2% |
| matched | 18 | 21.2% |
| rejected | 15 | 17.6% |

ASR's addressable set is 15 videos. Nothing else will recover them, and nothing
else needs to: the other 82.4% have transcripts and are blocked on matching, not
on transcription.

## Decision

**Do not build speech recognition. Fix language selection and failure
classification instead.**

Both fixes shipped in migration `8e429682f918` and the accompanying
`transcript.py` rewrite:

1. **Language families, not codes.** Preference order `en` then `zh`, matched on
   the prefix before the region subtag, so `en-US`, `zh-Hans`, `zh-Hant` and
   `zh-TW` all resolve. Manually-created tracks are preferred over
   auto-generated ones, since ASR captions mangle product names — exactly the
   tokens the matcher depends on.
2. **Typed failure reasons.** `TranscriptFailure` splits terminal
   (`no_track`, `video_unavailable`) from retryable (`ip_blocked`, `network`,
   `unknown`), persisted to `raw_youtube_reviews.failure_reason`. The terminal
   set is the ASR decision set; conflating it with operational failure is what
   produced a 39-row problem where a 15-row one existed.
3. **Chinese transcripts are not translated at fetch time.** The source language
   is recorded in `transcript_language` and the original text is stored. The
   chunk processor paraphrases downstream anyway, and translating at ingest
   destroys the original irrecoverably.

### Revisit trigger

Reopen this decision when **the count of `no_track` rows exceeds 40**.

The trigger is an absolute count, deliberately. The ratio is unstable: as
operational failures were recovered the reject bucket drained from 39 to 15, and
`no_track` rose from 38.5% to 100% of it without a single video changing state.
A ratio-based trigger would fire on bucket drainage rather than on a growing
problem.

## Operating mode: transcript fetching is a local admin job

Webshare has no domain configured, so the rotating-residential proxy is
unavailable. YouTube blocks the transcript endpoint for datacenter IPs, which
means transcript fetching **cannot run on Render** and currently runs locally
against production Supabase.

`_build_api` raises rather than degrading silently when credentials are missing
in production. The silent fallback is what made "YouTube is rate-limiting us"
indistinguishable from "the proxy was never configured" during the audit, and
that ambiguity cost real diagnostic time.

This is the pipeline's operating mode until Webshare is set up, not a temporary
workaround. It has one incidental benefit: `no_track` and `ip_blocked` can only
be separated from an unblocked IP, so the terminal figure measured locally is
more trustworthy than one measured from Render would have been.

**Rate limiting.** A local residential IP hits YouTube's transcript rate limit at
roughly 30 fetches, and retrying inside the block window extends it. Zero YouTube
API quota cost is not zero rate limit — the transcript endpoint is unmetered by
the Data API but independently throttled. `POST /reviews/retry-transcripts`
therefore needs an inter-request delay and a circuit breaker that aborts after
consecutive `ip_blocked` results rather than pushing through and prolonging the
block.

## Not chosen

**faster-whisper locally (RTX 5070, 12GB).** Technically the strongest option —
`large-v3` in float16 fits in roughly 3GB, transcribes a 20-minute video in about
a minute, and consumes no Gemini quota. Rejected on addressable set size: 15
videos does not justify an audio download path, a model dependency, and the
YouTube ToS exposure that pulling audio carries but reading the caption API does
not.

**Gemini audio input.** Would collapse transcription, chunking and sentiment into
one call and handles code-switched Mandarin better than Whisper. Rejected for the
same reason, plus it would consume the 7 RPM quota that is already the project's
binding constraint.

**Translate Chinese tracks to English at fetch time via YouTube's translation.**
Rejected. Destroys the original wording irrecoverably at ingest for a
transformation that can be done at read time, and the agent answers Chinese
queries in Chinese — quoting a Chinese reviewer directly reads better than
quoting a translation of one.

**Frame OCR to read on-screen specification tables.** Chinese reviewers routinely
display full spec tables, which would be high-signal for ADR-0013's Stage 2
configuration binding. Deferred rather than rejected: it addresses configuration
identification, not transcript availability, and belongs to a matching revision
if Stage 2's miss rate proves too high.

## Consequences

**Positive**

- 24 videos recovered that were previously written off as a data gap.
- The reject bucket is now single-cause. Any future row with a
  `failure_reason` other than `no_track` is a genuine anomaly and visible as one
  — a baseline that did not exist before.
- Chinese-language evidence enters the corpus in its original language, which
  suits the agent's Chinese responses and the Malaysian market focus.

**Negative**

- 15 videos remain permanently unreachable without ASR. They stay in `rejected`
  with `failure_reason = no_track`.
- Transcript ingestion depends on a developer's local machine and cannot be
  scheduled on Render. Discovery and chunk processing are unaffected.
- The retryable failure branches (`ip_blocked`, `network`, `unknown`) are
  classified by exception class-name matching, since those classes have moved
  between library versions. `ip_blocked` has been exercised in practice;
  `network` and `unknown` have not. Their first real execution will be their
  first test.
- `discovery.discover_videos` still queries `f"{brand} {product} review"`. The
  literal English word excludes Chinese reviewers who title videos 開箱 / 評測 /
  實測, so the non-English corpus is still under-discovered at the search stage.
  This is a remediation item, not an ADR decision, but it bounds how much the
  language fix above actually delivers.