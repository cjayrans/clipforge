"""Nightly orchestration: ingest -> transcribe -> rank -> render -> QA -> review (P2.2), and the bot's New hook /
Shorter re-render (P2.3).

Entry points:
- `run_once(settings, *, deps=None) -> RunReport`: `cf run --once` and the worker's 01:00 `nightly_pipeline` job.
- `make_review_hooks(settings, factory, deps=None) -> review.service.ReviewHooks`: wired into `cf bot`.

How a nightly run works (docs/PRD.md §4.2, ARCHITECTURE §4):
1. `costs.guard()` first. At the cap (or paused) the run pauses (`costs.pause`, which the bot delivers as an alert)
   and does nothing else. An existing pause is not re-recorded, so the operator gets one alert, not one per night.
2. `ingest.inbox.scan_inbox` registers files dropped in `inbox/<slug>/`.
3. Sweeps: expire unpublished clips of drained/ended campaigns; recover clips a crashed re-render left in RENDERED.
4. Render candidates left over from earlier runs (costs no API money), best first.
5. Phase A (moments-v2): sources of active campaigns (priority, then oldest), at most
   `pipeline.max_sources_per_night`: download (after a disk-space check), transcribe, rank (each paid stage after
   `costs.guard()`, inside `costs.for_source` so the per-source cap applies and cost rows name the source). Moment
   candidates are stored without copy.
6. Candidates left unrendered for `CANDIDATE_TTL` since ranking expire (their footage can then be deleted).
7. Phase B: every CANDIDATE across sources competes (campaign priority, then score); the best are rendered until
   `pipeline.nightly_candidate_target` clips passed QA *in this run*. Copy (Sonnet) is written right before a
   candidate renders, so only the clips that go to review are paid for. The rest stay CANDIDATE for the next night.
8. Footage of a source with nothing pending (no clip awaiting review/approval/publishing, no unrendered candidate) is
   deleted (`ingest.purge.purge_if_done`).

Database discipline:
- Every step runs in its own short `session_scope`. Nothing holds a write transaction while a paid call, a download
  or a render runs: the bot writes to the same SQLite file, and the Claude/Groq cost ledgers commit on their own
  (docs/contract-requests/rank.md item 7).
- Every state change goes through `db.transition()` / `set_source_status()` and writes an `events` row.

Error isolation: an expected failure of one stage (refused host, download error, transcription error, rank error,
render/QA error) is already recorded by that module (status + alert); the run commits it and moves on to the next
source or candidate. An unexpected exception is rolled back, written as a `pipeline_stage_failed` alert, and the run
moves on. `SpendCapReached` from any paid call stops the whole run and pauses it (COMPLIANCE #12).

Layouts (feat/reframe): before a game clip renders, the source's facecam is resolved once (`resolve_facecam`: the
campaign's manual `facecam_box`, else the result stored on `sources.facecam_json`, else a detection run that is then
stored, "none" included, with an events row). The clip row records the layout used and the render time.
`cf clip relayout` re-renders clips that are still pending review with the current layout code (`relayout_clips`):
same window, hook and captions, a fresh `rendered_at`, so the bot sends a new preview and any approval must be new.

Render output: `media/clips/<campaign slug>/<account set>/r<UTC stamp>/clip_<candidate id>.mp4` (+ .ass, .manifest.json,
and `<clip>.preview.mp4` when the clip is over Telegram's 50 MB limit). A re-render writes a new `r<stamp>` folder,
so a file the database still points at is never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx2
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge import compliance, costs
from clipforge.compliance import ComplianceError
from clipforge.config import AccountSet, Settings
from clipforge.db import (
    ApprovalRow,
    CampaignRow,
    CandidateRow,
    ClipRow,
    FlagRow,
    PostRow,
    SourceRow,
    TranscriptRow,
    make_engine,
    make_session_factory,
    record_event,
    session_scope,
    transition,
    utcnow,
)
from clipforge.db.migrate import upgrade
from clipforge.ingest import media, watchdog
from clipforge.ingest.download import DownloadError, GDriveClient, download_source
from clipforge.ingest.inbox import scan_inbox
from clipforge.ingest.models import clip_windows, facecam_from_row, set_source_status, store_facecam
from clipforge.ingest.purge import purge_if_done
from clipforge.publish.dispatcher import expire_campaign_clips, release_clip
from clipforge.publish.queries import campaign_model, lineage
from clipforge.qa.checks import FaceCheckFn, default_face_check, qa_clip
from clipforge.qa.preview import TELEGRAM_VIDEO_LIMIT_BYTES, preview_for_telegram
from clipforge.qa.probe import ProbeError, ProbeResult, probe
from clipforge.rank.claude_client import ClaudeClient, RankError
from clipforge.rank.copy import RankResult, rank_source, write_copy
from clipforge.rank.media_signals import MediaSignals
from clipforge.rank.media_signals import extract as extract_media_signals
from clipforge.rank.moment_score import moment_note, overlaps_any, rank_moments
from clipforge.rank.profile import MomentProfile, ProfileError, SignalRules, load_profile
from clipforge.rank.prompts import Fewshot, PromptError, load_fewshots
from clipforge.rank.select import shorter_range, text_between
from clipforge.rank.vision import FramesFn, source_frames
from clipforge.render.assets import AssetNotFoundError
from clipforge.render.clip import ClipRequest, render_candidate
from clipforge.render.encode import RenderError
from clipforge.render.facecam import FacecamDetection, FacecamSpec, default_detect
from clipforge.review import alerts
from clipforge.review.service import ReviewHooks, get_flag, set_flag
from clipforge.schemas import (
    Campaign,
    CampaignStatus,
    Candidate,
    ClipState,
    GateResult,
    MomentScore,
    Platform,
    QACheck,
    QAReport,
    RenderManifest,
    SourceStatus,
    Transcript,
    Word,
)
from clipforge.transcribe.groq_client import TranscriberError
from clipforge.transcribe.service import Transcriber, load_transcript, transcribe_source

log = logging.getLogger(__name__)

# Render/QA failures a candidate can recover from on a later night (a second failure expires it).
RENDER_ERRORS: tuple[type[Exception], ...] = (
    RenderError,
    ProbeError,
    ComplianceError,
    AssetNotFoundError,
    media.MediaToolError,
    OSError,
)
RENDER_RETRIES = 1  # one more try next night, then the candidate expires
RERENDER_FLAG = "rerender:"  # + clip id; set while a New hook / Shorter re-render runs
STRANDED_AFTER = timedelta(hours=1)  # a clip left RENDERED longer than this by a re-render is recovered
CANDIDATE_TTL = timedelta(days=3)  # an unrendered candidate expires this long after ranking (moments-v2)
# Phase A starts another source only while the night has used less than this much active time; with the 90 min
# per-source ceiling (ingest/watchdog.CEILINGS) a night's ranking can never run for hours.
NIGHT_RANK_BUDGET_S = 60 * 60.0
RUBRIC_COLUMNS = ("hook", "arc", "arousal", "sendability", "clarity", "density")  # CandidateRow.score_<id>
V1_COLUMNS = ("reaction", "event", "setup", "absurdity")  # weights_version gta6-streamer-v1


# --------------------------------------------------------------------------------------------------
# Dependencies and report
# --------------------------------------------------------------------------------------------------


RenderFn = Callable[[ClipRequest], RenderManifest]
MomentSignalsFn = Callable[[Path, SignalRules], MediaSignals]


@dataclass
class PipelineDeps:
    """Everything external the pipeline touches, so tests can replace it. Defaults are the real implementations."""

    transcriber: Transcriber | None = None  # None: Groq (with the cost ledger)
    local_transcriber: Transcriber | None | Literal["auto"] = "auto"  # faster-whisper fallback
    media_runner: media.Runner = media.run  # ffmpeg/ffprobe for ingest and transcribe
    gdrive: GDriveClient | None = None
    ytdl_factory: Callable[[dict[str, object]], object] | None = None
    claude: ClaudeClient | None = None  # None: built from settings with the session factory as ledger
    claude_http_client: httpx2.Client | None = None
    render_kwargs: dict[str, Any] = field(default_factory=dict)  # detector_factory / shots_fn / probe_fn / run
    qa_probe: Callable[[Path], ProbeResult] = probe
    preview_fn: Callable[[Path], Path] = preview_for_telegram
    clock: Callable[[], datetime] = utcnow
    moment_signals: MomentSignalsFn | None = None  # None: ffmpeg loudness + motion passes (rank/media_signals.py)
    # once per source, sampled inside its clip windows (render/facecam.py)
    facecam_fn: Callable[[Path, list[tuple[float, float]]], FacecamDetection | None] = default_detect
    qa_face_check: FaceCheckFn = default_face_check  # stacked clips: a face in the rendered facecam panel
    # moments-v2 keyframe check: (source path, shortlist windows) -> FramesFn; None reads JPEGs with ffmpeg
    vision_frames: Callable[[Path, Sequence[tuple[float, float]]], FramesFn | None] | None = None


@dataclass
class RunReport:
    started_at: datetime
    paused: str | None = None
    inbox_registered: int = 0
    expired: int = 0
    recovered: int = 0
    sources_seen: int = 0
    downloaded: int = 0
    transcribed: int = 0
    ranked: int = 0
    candidates_created: int = 0
    rendered: int = 0
    ready: int = 0  # clips that passed QA in this run (PENDING_REVIEW)
    qa_failed: int = 0
    copy_written: int = 0
    expired_stale: int = 0
    purged: int = 0
    stopped_early: bool = False
    problems: list[str] = field(default_factory=list)
    source_stats: list[str] = field(default_factory=list)  # per source: download size, disk, stage times

    def summary(self) -> str:
        lines = [
            f"nightly run: {self.ready} clip(s) ready for review"
            + (" (target reached, stopped early)" if self.stopped_early else ""),
            f"sources {self.sources_seen}: downloaded {self.downloaded}, transcribed {self.transcribed}, "
            f"ranked {self.ranked}; candidates {self.candidates_created}; rendered {self.rendered}, "
            f"QA failed {self.qa_failed}",
            f"inbox {self.inbox_registered} new; expired {self.expired} (+{self.expired_stale} stale candidates); "
            f"recovered {self.recovered}; copy written {self.copy_written}; footage deleted {self.purged}",
        ]
        lines.extend(self.source_stats[:10])
        if self.paused:
            lines.append(f"PAUSED: {self.paused}")
        lines.extend(f"problem: {p}" for p in self.problems[:20])
        return "\n".join(lines)


class _StageFailed(Exception):
    """An expected failure already recorded by the stage (status/alert); the run moves on."""


# --------------------------------------------------------------------------------------------------
# Helpers shared by the nightly run and re-renders
# --------------------------------------------------------------------------------------------------


def account_for(settings: Settings, campaign: Campaign) -> tuple[str, AccountSet] | None:
    """The account set a campaign's clips are made for: the first in config with a platform the campaign allows.

    v1 makes one clip per candidate (one account set), so the same content never goes to two accounts."""
    for name, account in settings.app.account_sets.items():
        if compliance.allowed_platforms(campaign, account.platforms):
            return name, account
    return None


def render_dir(settings: Settings, campaign_slug: str, account_set: str, stamp: datetime) -> Path:
    return settings.media_dir / "clips" / campaign_slug / account_set / f"r{stamp:%Y%m%dT%H%M%S%fZ}"


def _captions(candidate: CandidateRow) -> dict[Platform, str]:
    raw = candidate.captions_json or {}
    out: dict[Platform, str] = {}
    for key, value in raw.items():
        try:
            platform = Platform(key)
        except ValueError:
            continue
        if isinstance(value, str):
            out[platform] = value
    return out


def _latest_transcript(session: Session, source_id: int) -> Transcript:
    row = session.scalar(
        select(TranscriptRow).where(TranscriptRow.source_id == source_id).order_by(TranscriptRow.id.desc()).limit(1)
    )
    if row is None:
        raise _StageFailed(f"source {source_id} has no transcript")
    return load_transcript(row.words_json_path)


USED_CLIP_STATES = (ClipState.APPROVED, ClipState.SCHEDULED, ClipState.PUBLISHED)


def used_windows(session: Session, source_id: int) -> list[tuple[float, float]]:
    """Footage ranges of this source already used by an approved, scheduled or published clip. A re-rank never
    proposes a window overlapping one of them."""
    rows = session.execute(
        select(CandidateRow.start_s, CandidateRow.end_s)
        .join(ClipRow, ClipRow.candidate_id == CandidateRow.id)
        .where(CandidateRow.source_id == source_id, ClipRow.state.in_(USED_CLIP_STATES))
        .distinct()
    ).all()
    return [(a, b) for a, b in rows]


def moment_from_row(row: CandidateRow) -> MomentScore | None:
    """The stored moment-ranker judgement of a candidate, or None for transcript-window candidates."""
    if row.archetype is None or row.moment_profile is None or row.weights_version is None:
        return None
    reason = row.reasons
    if reason.startswith(f"[{row.archetype}] "):
        reason = reason[len(row.archetype) + 3 :]
    scores: dict[str, int] = {}
    if isinstance(row.scores_json, dict):
        scores = {str(k): int(v) for k, v in row.scores_json.items() if isinstance(v, int | float)}
    else:  # weights_version v1 rows: the four original scores
        for key in V1_COLUMNS:
            value = getattr(row, f"score_{key}")
            if value is not None:
                scores[key] = int(value)
    found_by = row.found_by if row.found_by in ("content", "signals", "both") else None
    return MomentScore(
        profile=row.moment_profile,
        weights_version=row.weights_version,
        archetype=row.archetype,
        scores={k: min(max(v, 0), 10) for k, v in scores.items()},
        weighted=float(min(max(row.score, 0), 100)),
        reason=reason[:300],
        skip_flags=[str(x) for x in row.skip_flags or []],
        review_flags=[str(x) for x in row.review_flags or []],
        gates=[GateResult.model_validate(g) for g in row.gate_results_json or [] if isinstance(g, dict)],
        found_by=found_by,  # type: ignore[arg-type]
        content_confidence=row.content_confidence,
        vision=row.vision_json if isinstance(row.vision_json, dict) else None,
    )


def moment_columns(moment: MomentScore | None) -> dict[str, Any]:
    """CandidateRow column values for a moment judgement (all None without one)."""
    if moment is None:
        return {}
    cols: dict[str, Any] = {
        "moment_profile": moment.profile,
        "weights_version": moment.weights_version,
        "archetype": moment.archetype,
        "scores_json": dict(moment.scores),
        "found_by": moment.found_by,
        "content_confidence": moment.content_confidence,
        "vision_json": moment.vision,
        "gate_results_json": [g.model_dump(mode="json") for g in moment.gates],
        "skip_flags": list(moment.skip_flags),
        "review_flags": list(moment.review_flags),
    }
    for key in (*RUBRIC_COLUMNS, *V1_COLUMNS):
        cols[f"score_{key}"] = moment.scores.get(key)
    return cols


def _candidate_model(row: CandidateRow) -> Candidate:
    return Candidate(
        id=row.id,
        source_id=row.source_id,
        start_s=row.start_s,
        end_s=row.end_s,
        score=row.score,
        standalone=row.standalone,
        brief_ok=row.brief_ok,
        reasons=row.reasons,
        prompt_version=row.prompt_version,
        state=row.state,
        moment=moment_from_row(row),
    )


def _redact(settings: Settings, text: str) -> str:
    for secret in settings.secrets.secret_values():
        text = text.replace(secret, "***")
    return alerts.redact_urls(text)


def _failed_checks(report: QAReport) -> str:
    return "; ".join(f"{c.name}: {c.detail}" if c.detail else c.name for c in report.checks if not c.passed)


def fresh_render_time(session: Session, clip_id: int, now: datetime) -> datetime:
    """`rendered_at` for a re-render: `now`, but always later than every decision already recorded for the clip, so an
    earlier approval can never count for the new render even if the bot's and worker's clocks disagree (COMPLIANCE #5).
    """
    latest = session.scalar(select(func.max(ApprovalRow.decided_at)).where(ApprovalRow.clip_id == clip_id))
    if latest is not None and latest >= now:
        return latest + timedelta(seconds=1)
    return now


def _remove_render_dir(settings: Settings, clip_path: str) -> None:
    """Delete a superseded/partial `r<stamp>` render folder, only ever inside `media_dir/clips`."""
    folder = Path(clip_path).parent
    try:
        root = (settings.media_dir / "clips").resolve()
        resolved = folder.resolve()
    except OSError:
        return
    if folder.name.startswith("r") and resolved.is_relative_to(root) and resolved != root:
        shutil.rmtree(resolved, ignore_errors=True)


def _build_claude(settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps) -> ClaudeClient:
    if deps.claude is not None:
        return deps.claude
    return ClaudeClient.from_settings(settings, factory, http_client=deps.claude_http_client)


@dataclass(frozen=True)
class _RenderInputs:
    candidate_id: int
    campaign: Campaign
    campaign_slug: str
    account_set: str
    account: AccountSet
    source_path: Path
    words: list[Word]
    captions: dict[Platform, str]
    title: str | None
    source_id: int = 0
    facecam: FacecamSpec | None = None  # filled by resolve_facecam outside any transaction


def resolve_facecam(
    settings: Settings,
    factory: sessionmaker[Session],
    deps: PipelineDeps,
    source_id: int,
    campaign: Campaign,
    windows: Sequence[tuple[float, float]] | None = None,
) -> FacecamSpec | None:
    """The facecam a game clip of this source is laid out with, or None (crop/fit only).

    Order: the campaign's manual `facecam_box`; the detection stored on the source (current detector version); a new
    detection sampled inside the source's clip windows, or `windows` when given (the keyframe check passes its
    shortlist; no transaction held while it runs), stored with an events row whatever it finds. A detection that
    could not run (no mediapipe, unreadable media) is not stored and is tried again next time."""
    if campaign.content_kind != "game" or campaign.layout in ("crop", "fit"):
        return None
    if campaign.facecam_box is not None:
        return FacecamSpec(origin="campaign", box_pct=campaign.facecam_box)
    with session_scope(factory) as s:
        src = s.get(SourceRow, source_id)
        if src is None or not src.local_path:
            return None
        stored = facecam_from_row(src)
        path = Path(src.local_path)
        if windows is None:
            windows = clip_windows(s, source_id)
    if stored is None:
        started = time.monotonic()
        detection = deps.facecam_fn(path, list(windows))
        if detection is None:
            return None
        with session_scope(factory) as s:
            src = s.get(SourceRow, source_id)
            if src is None:
                return None
            store_facecam(s, src, detection, seconds=time.monotonic() - started)
        stored = detection
    if stored.status != "found" or stored.box is None:
        return None
    return FacecamSpec(origin="detected", box=stored.box)


def _render_and_qa(
    settings: Settings,
    deps: PipelineDeps,
    inputs: _RenderInputs,
    *,
    start_s: float,
    end_s: float,
    hook: str,
    captions: dict[Platform, str] | None = None,
    title: str | None = None,
) -> tuple[RenderManifest, QAReport]:
    """Render into a fresh `r<stamp>` folder and QA the result (no database access)."""
    out_dir = render_dir(settings, inputs.campaign_slug, inputs.account_set, deps.clock())
    req = ClipRequest(
        candidate_id=inputs.candidate_id,
        source_path=inputs.source_path,
        start_s=start_s,
        end_s=end_s,
        words=inputs.words,
        hook=hook,
        content_kind=inputs.campaign.content_kind,
        account_set=inputs.account_set,
        out_dir=out_dir,
        overlay=inputs.campaign.overlay,
        layout=inputs.campaign.layout,
        facecam=inputs.facecam,
    )
    try:
        manifest = render_candidate(req, settings.app, **deps.render_kwargs)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    platforms = compliance.allowed_platforms(inputs.campaign, inputs.account.platforms)
    report = qa_clip(
        manifest,
        inputs.campaign,
        platforms,
        captions if captions is not None else inputs.captions,
        fps=float(settings.app.render.fps),
        enabled=inputs.account.platforms,
        probe_fn=deps.qa_probe,
        youtube_title=title if title is not None else inputs.title,
        face_check_fn=deps.qa_face_check,
    )
    return manifest, report


def _make_proxy(settings: Settings, deps: PipelineDeps, session: Session, clip_id: int, path: str) -> None:
    """ops CR 2: a clip over Telegram's 50 MB bot limit gets `<clip>.preview.mp4` (the name review.previews reads)."""
    try:
        if Path(path).stat().st_size <= TELEGRAM_VIDEO_LIMIT_BYTES:
            return
        deps.preview_fn(Path(path))
    except (RenderError, media.MediaToolError, OSError) as exc:
        alerts.raise_alert(
            session,
            alerts.PREVIEW_TOO_BIG,
            clip_id,
            _redact(settings, f"clip {clip_id}: could not build a preview proxy: {exc}"),
        )


# --------------------------------------------------------------------------------------------------
# Nightly run
# --------------------------------------------------------------------------------------------------


class Pipeline:
    def __init__(self, settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps) -> None:
        self.settings = settings
        self.app = settings.app
        self.factory = factory
        self.deps = deps
        self.report = RunReport(started_at=deps.clock())
        self._claude: ClaudeClient | None = None
        self._capped_sources: set[int] = set()

    # -- control ---------------------------------------------------------------------------------

    @property
    def target_reached(self) -> bool:
        return self.report.ready >= self.app.pipeline.nightly_candidate_target

    def _guard(self) -> None:
        with session_scope(self.factory) as s:
            costs.guard(s, self.app)

    def _pause(self, exc: costs.SpendCapReached) -> None:
        self.report.paused = str(exc)
        with session_scope(self.factory) as s:
            if costs.is_paused(s) is None:
                costs.pause(s, str(exc))
        log.warning("nightly pipeline paused: %s", exc)

    def _problem(self, text: str) -> None:
        clean = _redact(self.settings, text)[:400]
        self.report.problems.append(clean)
        log.warning("pipeline: %s", clean)

    def _isolated(self, stage: str, entity: int | str, fn: Callable[[], Any]) -> Any:
        """Run one stage. Expected failures (`_StageFailed`) were recorded by the stage; anything else is rolled
        back (by the stage's session_scope) and alerted. SpendCapReached propagates to stop the run."""
        try:
            return fn()
        except costs.SpendCapReached:
            raise
        except costs.SourceSpendCapReached as exc:
            self._source_capped(exc)
        except watchdog.WatchdogError:
            raise  # the caller marks the source (or candidate) failed
        except _StageFailed as exc:
            self._problem(f"{stage} {entity}: {exc}")
        except Exception as exc:
            log.exception("pipeline stage %s failed for %s", stage, entity)
            message = _redact(self.settings, f"{stage} failed for {entity}: {type(exc).__name__}: {exc}")[:500]
            self.report.problems.append(message)
            with session_scope(self.factory) as s:
                alerts.raise_alert(s, alerts.PIPELINE_STAGE_FAILED, entity, message)
        return None

    def _source_capped(self, exc: costs.SourceSpendCapReached) -> None:
        """A source reached `per_source_cap_usd`: it fails (raise the cap, then `cf source retry`); the run goes on."""
        message = f"source {exc.source_id} stopped: {exc}. Raise per_source_cap_usd, then `cf source retry`."
        self._problem(message)
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, exc.source_id)
            if src is not None and src.status not in (SourceStatus.FAILED, SourceStatus.PURGED):
                set_source_status(s, src, SourceStatus.FAILED, fail_reason="source_spend_cap", detail=str(exc))
            alerts.raise_alert(s, alerts.SOURCE_SPEND_CAP, exc.source_id, message)

    def run(self) -> RunReport:
        try:
            self._guard()
        except costs.SpendCapReached as exc:
            self._pause(exc)
            self._finish()
            return self.report
        self._isolated("inbox", "scan", self._scan_inbox)
        self._isolated("sweep", "campaigns", self._sweep_inactive)
        self._isolated("sweep", "stranded", self._recover_stranded)
        try:
            night_started = watchdog.CLOCK.now()
            for source_id in self._work_queue():  # Phase A: rank every source of the night first
                if watchdog.CLOCK.now() - night_started >= NIGHT_RANK_BUDGET_S:
                    self._problem(f"ranking budget of {NIGHT_RANK_BUDGET_S / 60:.0f} min used; source {source_id} "
                                  "waits for the next night")  # fmt: skip
                    break
                self.report.sources_seen += 1
                self._process_source(source_id)
            self._isolated("sweep", "stale candidates", self._expire_stale)
            self._render_pending()  # Phase B: the best candidates across sources
        except costs.SpendCapReached as exc:
            self._pause(exc)
        self._isolated("purge", "done sources", self._purge_done)
        self._finish()
        return self.report

    def _expire_stale(self) -> None:
        cutoff = self.deps.clock() - CANDIDATE_TTL
        with session_scope(self.factory) as s:
            rows = s.scalars(
                select(CandidateRow).where(CandidateRow.state == ClipState.CANDIDATE, CandidateRow.created_at < cutoff)
            ).all()
            for cand in rows:
                transition(s, cand, ClipState.EXPIRED, detail=f"not rendered within {CANDIDATE_TTL.days} days")
                self.report.expired_stale += 1

    def _purge_done(self) -> None:
        with session_scope(self.factory) as s:
            ids = list(
                s.scalars(
                    select(SourceRow.id).where(
                        SourceRow.status == SourceStatus.RANKED,
                        SourceRow.local_path.is_not(None),
                    )
                )
            )
        for source_id in ids:
            with session_scope(self.factory) as s:
                if purge_if_done(s, self.settings, source_id, now=self.deps.clock()):
                    self.report.purged += 1

    def _finish(self) -> None:
        summary = self.report.summary()
        with session_scope(self.factory) as s:
            record_event(s, "pipeline", "nightly", None, "done", detail=summary[:2000])
            if self.report.ready == 0 and self.report.paused is None:
                why = "; ".join(self.report.problems[:5]) or "no unprocessed sources on active campaigns"
                alerts.raise_alert(s, alerts.PIPELINE_EMPTY, "nightly", f"Nightly run produced no clips: {why}")
        log.info("%s", summary)

    # -- steps ----------------------------------------------------------------------------------

    def _scan_inbox(self) -> None:
        with session_scope(self.factory) as s:
            self.report.inbox_registered += len(scan_inbox(s, self.settings))

    def _sweep_inactive(self) -> None:
        with session_scope(self.factory) as s:
            for camp in s.scalars(select(CampaignRow).where(CampaignRow.status != CampaignStatus.ACTIVE)):
                self.report.expired += expire_campaign_clips(s, camp.id, f"campaign {camp.status.value}")

    def _recover_stranded(self) -> None:
        now = self.deps.clock()
        with session_scope(self.factory) as s:
            ids = list(s.scalars(select(ClipRow.id).where(ClipRow.state == ClipState.RENDERED)))
        for clip_id in ids:
            with session_scope(self.factory) as s:
                started = get_flag(s, f"{RERENDER_FLAG}{clip_id}")
                if started is not None and now - datetime.fromisoformat(started) < STRANDED_AFTER:
                    continue  # a re-render is still running in the bot
            restore_after_failed_rerender(
                self.settings, self.factory, self.deps, clip_id, "the re-render did not finish (worker recovered it)"
            )
            self.report.recovered += 1

    def _work_queue(self) -> list[int]:
        with session_scope(self.factory) as s:
            rows = s.execute(
                select(SourceRow.id)
                .join(CampaignRow, CampaignRow.id == SourceRow.campaign_id)
                .where(
                    CampaignRow.status == CampaignStatus.ACTIVE,
                    SourceRow.status.in_([SourceStatus.REGISTERED, SourceStatus.DOWNLOADED, SourceStatus.TRANSCRIBED]),
                )
                .order_by(CampaignRow.priority.desc(), SourceRow.id)
                .limit(self.app.pipeline.max_sources_per_night)
            ).all()
        return [r[0] for r in rows]

    def _source_status(self, source_id: int) -> SourceStatus:
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            return src.status

    def _process_source(self, source_id: int) -> None:
        stages: list[tuple[SourceStatus, str, Callable[[int], None]]] = [
            (SourceStatus.REGISTERED, "download", self._download),
            (SourceStatus.DOWNLOADED, "transcribe", self._transcribe),
            (SourceStatus.TRANSCRIBED, "rank", self._rank),
        ]
        timings: list[str] = []
        try:
            with watchdog.stage("source"):
                for status, name, fn in stages:
                    if self._source_status(source_id) is not status:
                        continue
                    started = watchdog.CLOCK.now()
                    try:
                        with costs.for_source(source_id, name), watchdog.stage(name):
                            self._isolated(name, f"source {source_id}", lambda fn=fn: fn(source_id))
                    finally:
                        timings.append(f"{name} {watchdog.CLOCK.now() - started:.0f}s")
                    if self._source_status(source_id) is status:
                        break  # the stage did not advance the source; it is retried next night
        except watchdog.WatchdogError as exc:
            self._watchdog_failed(source_id, exc)
        self._source_stats(source_id, timings)

    def _watchdog_failed(self, source_id: int, exc: watchdog.WatchdogError) -> None:
        """A stage stalled or used its ceiling: the source fails with an alert (`cf source retry` to try again)."""
        reason = "stage_timeout" if isinstance(exc, watchdog.StageTimeout) else "stalled"
        message = f"source {source_id} failed: {exc}. Check the footage, then `cf source retry {source_id}`."
        self._problem(message)
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            if src is not None and src.status not in (SourceStatus.FAILED, SourceStatus.PURGED):
                set_source_status(s, src, SourceStatus.FAILED, fail_reason=reason, detail=str(exc)[:500])
            alerts.raise_alert(s, alerts.PIPELINE_STAGE_FAILED, source_id, message[:500])

    def _source_stats(self, source_id: int, timings: list[str]) -> None:
        """One report line per source: file size, free disk, stage wall times, spend so far."""
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            path = Path(src.local_path) if src is not None and src.local_path else None
            spent = costs.source_spend(s, source_id)
            duration = src.duration_s if src is not None else None
        size = path.stat().st_size if path is not None and path.is_file() else None
        try:
            free = shutil.disk_usage(self.settings.media_dir if self.settings.media_dir.exists() else Path.cwd()).free
        except OSError:
            free = None
        parts = [f"source {source_id}:"]
        if duration:
            parts.append(f"{duration / 3600:.2f} h")
        if size is not None:
            parts.append(f"file {size / 1024**3:.2f} GB")
        if free is not None:
            parts.append(f"disk free {free / 1024**3:.1f} GB")
        parts.append(", ".join(timings) or "no stage ran")
        parts.append(f"spend ${spent:.3f}")
        self.report.source_stats.append(" ".join(parts))

    def _download(self, source_id: int) -> None:
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            try:
                download_source(
                    s,
                    self.settings,
                    src,
                    gdrive=self.deps.gdrive,
                    ytdl_factory=self.deps.ytdl_factory,
                    runner=self.deps.media_runner,
                )
            except (ComplianceError, DownloadError) as exc:
                failure: Exception | None = exc  # status + alert are flushed; session_scope commits them
            else:
                failure = None
                self.report.downloaded += 1
        if failure is not None:
            raise _StageFailed(f"download: {failure}")

    def _transcribe(self, source_id: int) -> None:
        self._guard()
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            try:
                transcribe_source(
                    s,
                    self.settings,
                    src,
                    transcriber=self.deps.transcriber,
                    local=self.deps.local_transcriber,
                    runner=self.deps.media_runner,
                    ledger=self.factory,
                )
            except TranscriberError as exc:
                failure: Exception | None = exc
            else:
                failure = None
                if src.status is SourceStatus.TRANSCRIBED:
                    self.report.transcribed += 1
        if failure is not None:
            raise _StageFailed(f"transcribe: {failure}")

    def _rank(self, source_id: int) -> None:
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            camp_row = s.get(CampaignRow, src.campaign_id)
            assert camp_row is not None
            campaign = campaign_model(camp_row)
            transcript = _latest_transcript(s, source_id)
            fewshots = load_fewshots(s)
            local_path = Path(src.local_path) if src.local_path else None
            used = used_windows(s, source_id)
        profile: MomentProfile | None = None
        if campaign.moment_profile is not None:
            try:
                profile = load_profile(campaign.moment_profile)
            except ProfileError as exc:
                with session_scope(self.factory) as s:
                    alerts.raise_alert(s, alerts.RANK_EMPTY, source_id, f"source {source_id}: {exc}"[:1000])
                raise _StageFailed(f"rank: {exc}") from exc
        picked = account_for(self.settings, campaign)
        if picked is None:
            with session_scope(self.factory) as s:
                alerts.raise_alert(
                    s,
                    alerts.RANK_EMPTY,
                    source_id,
                    f"campaign {campaign.name!r} allows none of the configured account sets' platforms",
                )
            raise _StageFailed("no account set for this campaign")
        _, account = picked
        self._guard()
        try:
            if self._claude is None:
                self._claude = _build_claude(self.settings, self.factory, self.deps)
            if profile is None:
                result = rank_source(transcript, campaign, account.platforms, client=self._claude, fewshots=fewshots)
            else:
                result = self._rank_moments(
                    transcript, campaign, account.platforms, profile, fewshots, local_path, used, source_id
                )
        except (RankError, PromptError, ComplianceError) as exc:
            with session_scope(self.factory) as s:
                alerts.raise_alert(
                    s, alerts.RANK_EMPTY, source_id, _redact(self.settings, f"ranking source {source_id} failed: {exc}")
                )
            raise _StageFailed(f"rank: {exc}") from exc
        result.candidates = [c for c in result.candidates if not overlaps_any((c.start_s, c.end_s), used)]
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            for cand in result.candidates:
                copy = cand.copy_  # None for moment candidates: copy is written right before they render
                row = CandidateRow(
                    source_id=source_id,
                    start_s=cand.start_s,
                    end_s=cand.end_s,
                    score=cand.score,
                    standalone=cand.standalone,
                    brief_ok=cand.brief_ok,
                    reasons=cand.reasons,
                    hook=copy.hook if copy is not None else None,
                    title=copy.youtube_title if copy is not None else None,
                    captions_json={c.platform.value: c.text for c in copy.captions} if copy is not None else None,
                    prompt_version=cand.prompt_version,
                    **moment_columns(cand.moment),
                )
                s.add(row)
                s.flush()
                what = f"score {cand.score}"
                if cand.moment is not None:
                    what += f", {cand.moment.archetype}, found by {cand.moment.found_by or '?'}"
                record_event(
                    s, "candidate", row.id, None, ClipState.CANDIDATE.value, detail=f"{what}, {row.hook or row.reasons}"
                )
            set_source_status(s, src, SourceStatus.RANKED, detail=result.detail()[:1000])
            if not result.candidates:
                alerts.raise_alert(s, alerts.RANK_EMPTY, source_id, f"source {source_id} produced no candidates")
        self.report.ranked += 1
        self.report.candidates_created += len(result.candidates)

    def _rank_moments(
        self,
        transcript: Transcript,
        campaign: Campaign,
        platforms: list[Platform],
        profile: MomentProfile,
        fewshots: list[Fewshot],
        local_path: Path | None,
        used: Sequence[tuple[float, float]] = (),
        source_id: int | None = None,
    ) -> RankResult:
        """Loudness/motion signals (cheap ffmpeg passes, no DB session held), then the moment ranker. Missing or
        failed media signals are not fatal: the ranker runs on the transcript and records it. The keyframe check
        reads frames from the full source file (facecam resolved on the shortlist windows first)."""
        signals = MediaSignals()
        if local_path is None or not local_path.is_file():
            signals.problems.append("source media is not on disk")
        else:
            fn = self.deps.moment_signals
            if fn is None:
                runner = self.deps.media_runner

                def fn(path: Path, rules: SignalRules) -> MediaSignals:
                    return extract_media_signals(path, rules, runner)

            signals = fn(local_path, profile.signals)
        for problem in signals.problems:
            self._problem(f"moment signals for source {transcript.source_id}: {problem}")
        assert self._claude is not None
        return rank_moments(
            transcript,
            campaign,
            platforms,
            profile,
            client=self._claude,
            fewshots=fewshots,
            loudness_db=signals.loudness_db,
            motion=signals.motion,
            exclude=used,
            frames=self._frames_factory(source_id, campaign, local_path, profile),
        )

    def _frames_factory(
        self, source_id: int | None, campaign: Campaign, local_path: Path | None, profile: MomentProfile
    ) -> Callable[[Sequence[tuple[float, float]]], FramesFn | None] | None:
        """For the keyframe check: frames from the source file, with facecam crops when the source has a facecam."""
        if not profile.vision.enabled or source_id is None or local_path is None or not local_path.is_file():
            return None
        path = local_path

        def factory(windows: Sequence[tuple[float, float]]) -> FramesFn | None:
            if self.deps.vision_frames is not None:
                return self.deps.vision_frames(path, windows)
            try:
                info = self.deps.qa_probe(path)
            except ProbeError:
                return None
            box = None
            if profile.vision.facecam_crop:
                spec = resolve_facecam(self.settings, self.factory, self.deps, source_id, campaign, windows)
                if spec is not None and info.width and info.height:
                    box = spec.resolve(info.width, info.height)
            v = profile.vision
            return source_frames(path, width=v.frame_width, quality=v.jpeg_quality, facecam=box)

        return factory

    # -- render ---------------------------------------------------------------------------------

    def _pending_candidates(self) -> list[int]:
        with session_scope(self.factory) as s:
            rows = s.execute(
                select(CandidateRow.id)
                .join(SourceRow, SourceRow.id == CandidateRow.source_id)
                .join(CampaignRow, CampaignRow.id == SourceRow.campaign_id)
                .where(
                    CandidateRow.state == ClipState.CANDIDATE,
                    CampaignRow.status == CampaignStatus.ACTIVE,
                    SourceRow.status == SourceStatus.RANKED,
                )
                .order_by(CampaignRow.priority.desc(), CandidateRow.score.desc(), CandidateRow.id)
            ).all()
        return [r[0] for r in rows]

    def _render_pending(self) -> None:
        for cand_id in self._pending_candidates():
            if self.target_reached:
                self.report.stopped_early = True
                return
            self._isolated("render", f"candidate {cand_id}", lambda c=cand_id: self._render(c))

    def _render_inputs(self, s: Session, cand: CandidateRow) -> _RenderInputs:
        src = s.get(SourceRow, cand.source_id)
        assert src is not None
        camp_row = s.get(CampaignRow, src.campaign_id)
        assert camp_row is not None
        campaign = campaign_model(camp_row)
        picked = account_for(self.settings, campaign)
        if picked is None:
            raise RenderError(f"campaign {campaign.name!r} allows none of the configured account sets' platforms")
        if not src.local_path or not Path(src.local_path).is_file():
            raise RenderError(f"source {src.id} media is missing on disk")
        transcript = _latest_transcript(s, src.id)
        return _RenderInputs(
            candidate_id=cand.id,
            campaign=campaign,
            campaign_slug=camp_row.slug,
            account_set=picked[0],
            account=picked[1],
            source_path=Path(src.local_path),
            words=list(transcript.words),
            captions=_captions(cand),
            title=cand.title,
            source_id=src.id,
        )

    def _render(self, cand_id: int) -> None:
        with session_scope(self.factory) as s:
            cand = s.get(CandidateRow, cand_id)
            assert cand is not None
            if cand.state is not ClipState.CANDIDATE:
                return
            start, end, hook = cand.start_s, cand.end_s, cand.hook or ""
            try:
                inputs = self._render_inputs(s, cand)
            except (RenderError, _StageFailed) as exc:
                failure: Exception | None = exc
            else:
                failure = None
        if failure is not None:
            self._render_failed(cand_id, failure)
            return
        if not hook:  # moment candidates get copy only once they are picked to render (moments-v2)
            try:
                with costs.for_source(inputs.source_id, "copy"), watchdog.stage("copy"):
                    hook, inputs = self._write_copy(cand_id, inputs)
            except (RankError, PromptError, ComplianceError, watchdog.WatchdogError) as exc:
                self._render_failed(cand_id, exc)
                return
            except costs.SourceSpendCapReached as exc:  # skip it (it expires later); the source and its clips stay
                if exc.source_id not in self._capped_sources:
                    self._capped_sources.add(exc.source_id)
                    with session_scope(self.factory) as s:
                        alerts.raise_alert(s, alerts.SOURCE_SPEND_CAP, exc.source_id,
                                           f"source {exc.source_id}: no copy for more candidates: {exc}")  # fmt: skip
                self._problem(f"candidate {cand_id}: {exc}")
                return
        facecam = resolve_facecam(self.settings, self.factory, self.deps, inputs.source_id, inputs.campaign)
        inputs = replace(inputs, facecam=facecam)
        try:
            manifest, report = _render_and_qa(self.settings, self.deps, inputs, start_s=start, end_s=end, hook=hook)
        except RENDER_ERRORS as exc:
            self._render_failed(cand_id, exc)
            return
        now = self.deps.clock()
        with session_scope(self.factory) as s:
            cand = s.get(CandidateRow, cand_id)
            assert cand is not None
            clip = ClipRow(
                candidate_id=cand_id,
                account_set=inputs.account_set,
                style_preset=manifest.style_preset,
                path=manifest.clip_path,
                duration_s=manifest.duration_s,
                sha256=manifest.sha256,
                qa_report_json=report.model_dump(mode="json"),
                rendered_at=now,
                created_at=now,
                layout=manifest.layout,
                render_s=manifest.timings.get("total"),
            )
            s.add(clip)
            s.flush()
            record_event(
                s, "clip", clip.id, None, ClipState.RENDERED.value, detail=f"candidate {cand_id}, layout {clip.layout}"
            )
            transition(s, cand, ClipState.RENDERED, detail=f"clip {clip.id}")
            self.report.rendered += 1
            if report.passed:
                transition(s, clip, ClipState.PENDING_REVIEW, detail="QA passed")
                _make_proxy(self.settings, self.deps, s, clip.id, clip.path)
                self.report.ready += 1
            else:
                transition(s, clip, ClipState.QA_FAILED, detail=_failed_checks(report)[:1000])
                self.report.qa_failed += 1

    def _write_copy(self, cand_id: int, inputs: _RenderInputs) -> tuple[str, _RenderInputs]:
        """Hook, title and captions (Sonnet) for a candidate picked to render; stored on the candidate row."""
        self._guard()
        with session_scope(self.factory) as s:
            cand = s.get(CandidateRow, cand_id)
            assert cand is not None
            model = _candidate_model(cand)
            fewshots = load_fewshots(s)
            clip_text = text_between(inputs.words, cand.start_s, cand.end_s)
        profile: MomentProfile | None = None
        if inputs.campaign.moment_profile is not None:
            try:
                profile = load_profile(inputs.campaign.moment_profile)
            except ProfileError:
                profile = None
        if self._claude is None:
            self._claude = _build_claude(self.settings, self.factory, self.deps)
        copy = write_copy(
            model,
            clip_text,
            inputs.campaign,
            inputs.account.platforms,
            client=self._claude,
            fewshots=fewshots,
            moment=moment_note(model, profile),
        )
        captions = {c.platform: c.text for c in copy.captions}
        with session_scope(self.factory) as s:
            cand = s.get(CandidateRow, cand_id)
            assert cand is not None
            cand.hook = copy.hook
            cand.title = copy.youtube_title
            cand.captions_json = {p.value: t for p, t in captions.items()}
            record_event(s, "candidate", cand_id, None, None, detail=f"copy written: {copy.hook}")
        self.report.copy_written += 1
        return copy.hook, replace(inputs, captions=captions, title=copy.youtube_title)

    def _render_failed(self, cand_id: int, exc: Exception) -> None:
        text = _redact(self.settings, f"{type(exc).__name__}: {exc}")[:300]
        with session_scope(self.factory) as s:
            key = f"render_failures:{cand_id}"
            failures = int(get_flag(s, key) or 0) + 1
            set_flag(s, key, str(failures))
            cand = s.get(CandidateRow, cand_id)
            if failures > RENDER_RETRIES and cand is not None and cand.state is ClipState.CANDIDATE:
                transition(s, cand, ClipState.EXPIRED, detail=f"render failed {failures}x: {text}")
                message = f"candidate {cand_id} dropped: render failed again: {text}"
            else:
                message = f"candidate {cand_id}: render failed (retried next night): {text}"
            alerts.raise_alert(s, alerts.RENDER_FAILED, cand_id, message)
        self._problem(f"render candidate {cand_id}: {text}")


def run_once(settings: Settings, *, deps: PipelineDeps | None = None) -> RunReport:
    """`cf run --once` / the 01:00 job: one nightly run. Migrates the DB first; never publishes anything."""
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        return Pipeline(settings, make_session_factory(engine), deps or PipelineDeps()).run()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------------
# New hook / Shorter (bot buttons): re-render with a fresh approval required (COMPLIANCE #5)
# --------------------------------------------------------------------------------------------------

RerenderKind = Literal["new_hook", "shorter", "relayout"]
REVIEWABLE: frozenset[ClipState] = frozenset({ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED})
RELAYOUT_STATES: frozenset[ClipState] = frozenset({ClipState.PENDING_REVIEW})  # never approved/published/rejected


class RerenderRefused(Exception):
    """The re-render cannot start; the message is shown to the operator."""


@dataclass(frozen=True)
class RerenderJob:
    clip_id: int
    kind: RerenderKind
    inputs: _RenderInputs
    candidate: Candidate
    start_s: float
    end_s: float
    hook: str
    old_path: str
    clip_text: str
    used_hooks: tuple[str, ...] = ()  # every hook this clip has had, current one included ("New hook" avoids them)
    old_layout: str = "fit"


def begin_rerender(
    settings: Settings, factory: sessionmaker[Session], clip_id: int, kind: RerenderKind, now: datetime
) -> RerenderJob:
    """Check the clip, then move it to RENDERED *before* any paid call or render, so neither the dispatcher (which
    only publishes APPROVED/SCHEDULED clips) nor an old preview's Approve button can act on it meanwhile.

    A SCHEDULED clip with no posts is released from its slot first (docs/contract-requests/ops.md item 4). Earlier
    approval rows are left untouched; they go stale when the new render sets a newer `rendered_at`."""
    with session_scope(factory) as s:
        clip = s.get(ClipRow, clip_id)
        if clip is None:
            raise RerenderRefused(f"Clip {clip_id} no longer exists.")
        started = get_flag(s, f"{RERENDER_FLAG}{clip_id}")
        if clip.state is ClipState.RENDERED or (
            started is not None and now - datetime.fromisoformat(started) < STRANDED_AFTER
        ):
            raise RerenderRefused(f"Clip {clip_id} is already being re-rendered; wait for the new preview.")
        if clip.state not in REVIEWABLE:
            raise RerenderRefused(f"Clip {clip_id} is {clip.state.value}; it can no longer be changed.")
        if kind == "relayout" and clip.state not in RELAYOUT_STATES:
            raise RerenderRefused(
                f"Clip {clip_id} is {clip.state.value}; only clips waiting for review are re-laid out."
            )
        if s.scalar(select(PostRow.id).where(PostRow.clip_id == clip_id).limit(1)) is not None:
            raise RerenderRefused(f"Clip {clip_id} already has posts; it can no longer be changed.")
        lin = lineage(s, clip)
        campaign = campaign_model(lin.campaign)
        if campaign.status is not CampaignStatus.ACTIVE:
            raise RerenderRefused(f"Campaign {campaign.name!r} is {campaign.status.value}.")
        account = settings.app.account_sets.get(clip.account_set)
        if account is None:
            raise RerenderRefused(f"Account set {clip.account_set!r} is no longer configured.")
        src = lin.source
        if not src.local_path or not Path(src.local_path).is_file():
            raise RerenderRefused(f"The source footage of clip {clip_id} is no longer on disk.")
        try:
            transcript = _latest_transcript(s, src.id)
        except _StageFailed as exc:
            raise RerenderRefused(str(exc)) from exc
        cand = lin.candidate
        start, end = cand.start_s, cand.end_s
        if kind == "shorter":
            try:
                start, end = shorter_range(
                    transcript.words, cand.start_s, cand.end_s, min_clip_s=settings.app.pipeline.min_clip_s
                )
            except ValueError as exc:
                raise RerenderRefused(f"Clip {clip_id} cannot get shorter: {exc}") from exc
        if clip.state is ClipState.SCHEDULED:
            try:
                release_clip(s, clip, f"released for {kind.replace('_', ' ')}", actor="operator")
            except ValueError as exc:
                raise RerenderRefused(f"Clip {clip_id} is already publishing.") from exc
        transition(s, clip, ClipState.RENDERED, actor="operator", detail=f"re-render: {kind.replace('_', ' ')}")
        set_flag(s, f"{RERENDER_FLAG}{clip_id}", now.isoformat())
        inputs = _RenderInputs(
            candidate_id=cand.id,
            campaign=campaign,
            campaign_slug=lin.campaign.slug,
            account_set=clip.account_set,
            account=account,
            source_path=Path(src.local_path),
            words=list(transcript.words),
            captions=_captions(cand),
            title=cand.title,
            source_id=src.id,
        )
        return RerenderJob(
            clip_id=clip_id,
            kind=kind,
            inputs=inputs,
            candidate=_candidate_model(cand),
            start_s=start,
            end_s=end,
            hook=cand.hook or "",
            old_path=clip.path,
            old_layout=clip.layout or "fit",
            clip_text=text_between(transcript.words, cand.start_s, cand.end_s),
            used_hooks=tuple(h for h in [*(cand.hook_history or []), cand.hook or ""] if h),
        )


def finish_rerender(
    settings: Settings, factory: sessionmaker[Session], job: RerenderJob, deps: PipelineDeps | None = None
) -> str:
    """New copy (New hook, paid) or new range (Shorter), render, QA, then swap the clip to the new file with a new
    `rendered_at` and move it RENDERED -> PENDING_REVIEW. Any failure (including QA) keeps the old file and returns
    the clip to review (see `restore_after_failed_rerender`)."""
    return _finish_rerender(settings, factory, job, deps)[1]


def _finish_rerender(
    settings: Settings, factory: sessionmaker[Session], job: RerenderJob, deps: PipelineDeps | None = None
) -> tuple[bool, str]:
    """`finish_rerender`, also saying whether the new render was swapped in."""
    deps = deps or PipelineDeps()
    new_path: str | None = None
    try:
        facecam = resolve_facecam(settings, factory, deps, job.inputs.source_id, job.inputs.campaign)
        job = replace(job, inputs=replace(job.inputs, facecam=facecam))
        hook, captions, title = job.hook, job.inputs.captions, job.inputs.title
        if job.kind == "new_hook":
            client = _build_claude(settings, factory, deps)
            copy = write_copy(
                job.candidate,
                job.clip_text,
                job.inputs.campaign,
                job.inputs.account.platforms,
                client=client,
                avoid_hooks=job.used_hooks,
            )
            hook, title = copy.hook, copy.youtube_title
            captions = {c.platform: c.text for c in copy.captions}
        manifest, report = _render_and_qa(
            settings, deps, job.inputs, start_s=job.start_s, end_s=job.end_s, hook=hook, captions=captions, title=title
        )
        new_path = manifest.clip_path
        if not report.passed:
            raise RenderError(f"the new render failed QA ({_failed_checks(report)[:300]})")
    except Exception as exc:
        if new_path is not None:
            _remove_render_dir(settings, new_path)
        reason = _redact(settings, f"{type(exc).__name__}: {exc}")[:300]
        restore_after_failed_rerender(settings, factory, deps, job.clip_id, reason)
        what = job.kind.replace("_", " ")
        return False, f"Clip {job.clip_id}: {what} failed ({reason}). The previous version is back for review."

    with session_scope(factory) as s:
        now = deps.clock()  # taken inside the transaction that swaps the file (reviewer hardening)
        clip = s.get(ClipRow, job.clip_id)
        cand = s.get(CandidateRow, job.inputs.candidate_id)
        assert clip is not None
        assert cand is not None
        if clip.state is not ClipState.RENDERED:  # recovered meanwhile by the worker: keep its decision
            _remove_render_dir(settings, manifest.clip_path)
            return False, f"Clip {job.clip_id} changed while re-rendering; nothing was swapped."
        clip.path = manifest.clip_path
        clip.sha256 = manifest.sha256
        clip.duration_s = manifest.duration_s
        clip.qa_report_json = report.model_dump(mode="json")
        clip.rendered_at = fresh_render_time(s, clip.id, now)  # earlier approvals are now stale (COMPLIANCE #5)
        clip.layout = manifest.layout
        clip.render_s = manifest.timings.get("total")
        if job.kind == "relayout":
            detail = f"relayout: {job.old_layout} -> {manifest.layout} ({manifest.timings.get('total', 0):.0f}s render)"
        elif job.kind == "new_hook":
            old_hook = cand.hook
            if old_hook:
                cand.hook_history = [*(cand.hook_history or []), old_hook]  # new list: JSON columns track assignment
            cand.hook = hook
            cand.title = title
            cand.captions_json = {p.value: t for p, t in captions.items()}
            detail = f"new hook: {old_hook!r} -> {hook!r}"
        else:
            detail = f"shorter: {cand.start_s:.2f}-{cand.end_s:.2f} -> {job.start_s:.2f}-{job.end_s:.2f}"
            cand.start_s, cand.end_s = job.start_s, job.end_s
        record_event(s, "candidate", cand.id, None, None, actor="operator", detail=detail)
        done = f"re-rendered ({job.kind}, layout {manifest.layout})"
        transition(s, clip, ClipState.PENDING_REVIEW, actor="operator", detail=done)
        _make_proxy(settings, deps, s, clip.id, clip.path)
        flag = s.get(FlagRow, f"{RERENDER_FLAG}{job.clip_id}")
        if flag is not None:
            s.delete(flag)
    if job.old_path != manifest.clip_path:
        _remove_render_dir(settings, job.old_path)  # never posted: re-renders start only before publishing
    if job.kind == "relayout":
        what = f"layout {job.old_layout} -> {manifest.layout}"
    elif job.kind == "new_hook":
        what = f"new hook: {hook}"
    else:
        what = f"now {job.end_s - job.start_s:.0f}s"
    return True, f"Clip {job.clip_id} re-rendered ({what}). The new preview follows; approve that one."


def restore_after_failed_rerender(
    settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps, clip_id: int, reason: str
) -> None:
    """Return a clip stuck in RENDERED to review with its existing file: QA it again and set `rendered_at = now`, so
    any approval needs a fresh tap on the re-sent preview (COMPLIANCE #5). QA_FAILED if the old file fails QA."""
    with session_scope(factory) as s:
        now = deps.clock()
        clip = s.get(ClipRow, clip_id)
        flag = s.get(FlagRow, f"{RERENDER_FLAG}{clip_id}")
        if flag is not None:
            s.delete(flag)
        if clip is None or clip.state is not ClipState.RENDERED:
            return
        report = _requalify(settings, deps, s, clip)
        clip.qa_report_json = report.model_dump(mode="json")
        clip.rendered_at = fresh_render_time(s, clip.id, now)
        if report.passed:
            transition(s, clip, ClipState.PENDING_REVIEW, detail=f"re-render failed, previous version kept: {reason}")
        else:
            transition(s, clip, ClipState.QA_FAILED, detail=f"re-render failed and old file fails QA: {reason}")
        alerts.raise_alert(s, alerts.RERENDER_FAILED, clip_id, f"clip {clip_id}: re-render failed: {reason}")


def _requalify(settings: Settings, deps: PipelineDeps, s: Session, clip: ClipRow) -> QAReport:
    manifest_path = Path(clip.path).with_suffix(".manifest.json")
    try:
        manifest = RenderManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return QAReport(clip_path=clip.path, checks=[QACheck(name="manifest_file", passed=False, detail=str(exc))])
    lin = lineage(s, clip)
    campaign = campaign_model(lin.campaign)
    account = settings.app.account_sets.get(clip.account_set)
    enabled = account.platforms if account is not None else []
    return qa_clip(
        manifest,
        campaign,
        compliance.allowed_platforms(campaign, enabled),
        _captions(lin.candidate),
        fps=float(settings.app.render.fps),
        enabled=enabled,
        probe_fn=deps.qa_probe,
        youtube_title=lin.candidate.title,
        face_check_fn=deps.qa_face_check,
    )


class ReviewHookRunner:
    """The bot's New hook / Shorter callbacks. Each starts the work in a thread and answers at once: Telegram
    clients show a progress bar until the callback is answered (https://core.telegram.org/bots/api#callbackquery),
    and a render takes minutes. The new preview arrives through the bot's preview task (rendered_at changed)."""

    def __init__(self, settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps | None) -> None:
        self.settings = settings
        self.factory = factory
        self.deps = deps or PipelineDeps()
        self.busy: set[int] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    async def new_hook(self, clip_id: int) -> str:
        return await self._start(clip_id, "new_hook")

    async def shorter(self, clip_id: int) -> str:
        return await self._start(clip_id, "shorter")

    async def _start(self, clip_id: int, kind: RerenderKind) -> str:
        if clip_id in self.busy:
            return f"Clip {clip_id} is already being re-rendered."
        self.busy.add(clip_id)
        try:
            job = await asyncio.to_thread(begin_rerender, self.settings, self.factory, clip_id, kind, self.deps.clock())
        except RerenderRefused as exc:
            self.busy.discard(clip_id)
            return str(exc)
        except BaseException:
            self.busy.discard(clip_id)
            raise
        task = asyncio.create_task(self._finish(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        what = "a new hook" if kind == "new_hook" else "a 20% shorter cut"
        return (
            f"Clip {clip_id}: making {what}. The new preview follows in a few minutes; "
            "the buttons on the old preview no longer work."
        )

    async def _finish(self, job: RerenderJob) -> None:
        try:
            message = await asyncio.to_thread(finish_rerender, self.settings, self.factory, job, self.deps)
            log.info("%s", message)
        except Exception:
            log.exception("re-render of clip %s failed", job.clip_id)
        finally:
            self.busy.discard(job.clip_id)


def make_review_hooks(
    settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps | None = None
) -> ReviewHooks:
    """The New hook / Shorter callbacks `cf bot` uses (P2.3)."""
    runner = ReviewHookRunner(settings, factory, deps)
    return ReviewHooks(new_hook=runner.new_hook, shorter=runner.shorter)


# --------------------------------------------------------------------------------------------------
# `cf clip relayout`: re-render clips waiting for review with the current layout code
# --------------------------------------------------------------------------------------------------


@dataclass
class RelayoutResult:
    clip_id: int
    source_id: int | None = None
    old_layout: str = ""
    new_layout: str = ""
    layout_reason: str = ""
    render_s: float | None = None
    ok: bool = False
    message: str = ""
    rendered_at: datetime | None = None


def _pending_ids(session: Session) -> list[int]:
    return list(
        session.scalars(select(ClipRow.id).where(ClipRow.state == ClipState.PENDING_REVIEW).order_by(ClipRow.id))
    )


def _manifest_of(path: str) -> RenderManifest | None:
    try:
        return RenderManifest.model_validate_json(Path(path).with_suffix(".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def relayout_one(
    settings: Settings, factory: sessionmaker[Session], clip_id: int, deps: PipelineDeps
) -> RelayoutResult:
    """Re-render one PENDING_REVIEW clip (same window, hook, captions) through the New hook / Shorter machinery:
    fresh `rendered_at`, new preview, old approvals stale, the old file kept if anything fails."""
    result = RelayoutResult(clip_id=clip_id)
    try:
        job = begin_rerender(settings, factory, clip_id, "relayout", deps.clock())
    except RerenderRefused as exc:
        result.message = str(exc)
        return result
    result.source_id, result.old_layout = job.inputs.source_id, job.old_layout
    swapped, result.message = _finish_rerender(settings, factory, job, deps)
    with session_scope(factory) as s:
        clip = s.get(ClipRow, clip_id)
        if clip is not None:
            result.ok = swapped and clip.state is ClipState.PENDING_REVIEW
            result.new_layout = clip.layout or ""
            result.render_s = clip.render_s
            result.rendered_at = clip.rendered_at
            manifest = _manifest_of(clip.path) if result.ok else None
            if manifest is not None:
                result.layout_reason = manifest.layout_reason
    return result


def wait_for_previews(
    factory: sessionmaker[Session], results: Sequence[RelayoutResult], timeout_s: float, poll_s: float = 5.0
) -> int:
    """Poll until the bot has sent a preview for each re-rendered clip's current render (or the timeout). Returns how
    many previews were sent. Read-only: the bot's preview task does the sending (review/previews.py)."""
    from clipforge.review.previews import preview_flag

    want = {r.clip_id: r.rendered_at for r in results if r.ok and r.rendered_at is not None}
    deadline = time.monotonic() + timeout_s
    while True:
        with session_scope(factory) as s:
            sent = sum(
                1 for cid, at in want.items() if at is not None and get_flag(s, preview_flag(cid)) == at.isoformat()
            )
        if sent >= len(want) or time.monotonic() >= deadline:
            return sent
        time.sleep(poll_s)


def relayout_clips(
    settings: Settings,
    clip_ids: Sequence[int] = (),
    *,
    all_pending: bool = False,
    dry_run: bool = False,
    wait_previews_s: float = 0.0,
    deps: PipelineDeps | None = None,
) -> list[RelayoutResult]:
    """`cf clip relayout`: re-render PENDING_REVIEW clips one after another and print what happened. Other states are
    refused (approved, scheduled, published, rejected and the rest are never touched). Never publishes anything."""
    from rich.console import Console

    console = Console()
    deps = deps or PipelineDeps()
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        factory = make_session_factory(engine)
        with session_scope(factory) as s:
            ids = list(dict.fromkeys([*clip_ids, *(_pending_ids(s) if all_pending else [])]))
            if dry_run:
                rows: list[RelayoutResult] = []
                for cid in ids:
                    clip = s.get(ClipRow, cid)
                    if clip is None:
                        rows.append(RelayoutResult(clip_id=cid, message="no such clip"))
                        continue
                    lin = lineage(s, clip)
                    ok = clip.state in RELAYOUT_STATES
                    rows.append(
                        RelayoutResult(
                            clip_id=cid,
                            source_id=lin.source.id,
                            old_layout=clip.layout or "fit",
                            ok=ok,
                            message=f"{clip.state.value}; campaign {lin.campaign.name} (layout {lin.campaign.layout})"
                            + ("" if ok else "; would be refused"),
                        )
                    )
        if dry_run:
            for r in rows:
                console.print(f"clip {r.clip_id}: source {r.source_id}, layout {r.old_layout}: {r.message}")
            console.print(f"dry run: {sum(r.ok for r in rows)} of {len(rows)} clip(s) would be re-rendered.")
            return rows
        results = []
        for cid in ids:
            r = relayout_one(settings, factory, cid, deps)
            results.append(r)
            if r.ok:
                console.print(
                    f"clip {cid}: source {r.source_id}, {r.old_layout} -> {r.new_layout}, "
                    f"render {r.render_s or 0:.1f}s" + (f" ({r.layout_reason})" if r.layout_reason else "")
                )
            else:
                console.print(f"[red]clip {cid}: not re-rendered: {r.message}[/red]")
        done = [r for r in results if r.ok]
        console.print(f"{len(done)} of {len(results)} clip(s) re-rendered.")
        if wait_previews_s > 0 and done:
            sent = wait_for_previews(factory, done, wait_previews_s)
            console.print(f"{sent} of {len(done)} preview(s) resent to Telegram.")
        return results
    finally:
        engine.dispose()
