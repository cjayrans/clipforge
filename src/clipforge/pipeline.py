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
5. Sources of active campaigns (priority, then oldest), at most `pipeline.max_sources_per_night`: download, then
   transcribe, then rank (each paid stage after `costs.guard()`), then render the new candidates.
6. Stop early once `pipeline.nightly_candidate_target` clips passed QA *in this run*; candidates not rendered stay
   CANDIDATE for the next night.

Database discipline:
- Every step runs in its own short `session_scope`. Nothing holds a write transaction while a paid call, a download
  or a render runs: the bot writes to the same SQLite file, and the Claude/Groq cost ledgers commit on their own
  (docs/contract-requests/rank.md item 7).
- Every state change goes through `db.transition()` / `set_source_status()` and writes an `events` row.

Error isolation: an expected failure of one stage (refused host, download error, transcription error, rank error,
render/QA error) is already recorded by that module (status + alert); the run commits it and moves on to the next
source or candidate. An unexpected exception is rolled back, written as a `pipeline_stage_failed` alert, and the run
moves on. `SpendCapReached` from any paid call stops the whole run and pauses it (COMPLIANCE #12).

Render output: `media/clips/<campaign slug>/<account set>/r<UTC stamp>/clip_<candidate id>.mp4` (+ .ass, .manifest.json,
and `<clip>.preview.mp4` when the clip is over Telegram's 50 MB limit). A re-render writes a new `r<stamp>` folder,
so a file the database still points at is never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
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
from clipforge.ingest import media
from clipforge.ingest.download import DownloadError, GDriveClient, download_source
from clipforge.ingest.inbox import scan_inbox
from clipforge.ingest.models import set_source_status
from clipforge.publish.dispatcher import expire_campaign_clips, release_clip
from clipforge.publish.queries import campaign_model, lineage
from clipforge.qa.checks import qa_clip
from clipforge.qa.preview import TELEGRAM_VIDEO_LIMIT_BYTES, preview_for_telegram
from clipforge.qa.probe import ProbeError, ProbeResult, probe
from clipforge.rank.claude_client import ClaudeClient, RankError
from clipforge.rank.copy import rank_source, write_copy
from clipforge.rank.prompts import PromptError, load_fewshots
from clipforge.rank.select import shorter_range, text_between
from clipforge.render.assets import AssetNotFoundError
from clipforge.render.clip import ClipRequest, render_candidate
from clipforge.render.encode import RenderError
from clipforge.review import alerts
from clipforge.review.service import ReviewHooks, get_flag, set_flag
from clipforge.schemas import (
    Campaign,
    CampaignStatus,
    Candidate,
    ClipState,
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


# --------------------------------------------------------------------------------------------------
# Dependencies and report
# --------------------------------------------------------------------------------------------------


RenderFn = Callable[[ClipRequest], RenderManifest]


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
    stopped_early: bool = False
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"nightly run: {self.ready} clip(s) ready for review"
            + (" (target reached, stopped early)" if self.stopped_early else ""),
            f"sources {self.sources_seen}: downloaded {self.downloaded}, transcribed {self.transcribed}, "
            f"ranked {self.ranked}; candidates {self.candidates_created}; rendered {self.rendered}, "
            f"QA failed {self.qa_failed}",
            f"inbox {self.inbox_registered} new; expired {self.expired}; recovered {self.recovered}",
        ]
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
            session, alerts.PREVIEW_TOO_BIG, clip_id, f"clip {clip_id}: could not build a preview proxy: {exc}"
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
        except _StageFailed as exc:
            self._problem(f"{stage} {entity}: {exc}")
        except Exception as exc:
            log.exception("pipeline stage %s failed for %s", stage, entity)
            message = _redact(self.settings, f"{stage} failed for {entity}: {type(exc).__name__}: {exc}")[:500]
            self.report.problems.append(message)
            with session_scope(self.factory) as s:
                alerts.raise_alert(s, alerts.PIPELINE_STAGE_FAILED, entity, message)
        return None

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
            self._render_pending()
            for source_id in self._work_queue():
                if self.target_reached:
                    self.report.stopped_early = True
                    break
                self.report.sources_seen += 1
                self._process_source(source_id)
        except costs.SpendCapReached as exc:
            self._pause(exc)
        self._finish()
        return self.report

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
        for status, name, fn in stages:
            if self._source_status(source_id) is not status:
                continue
            self._isolated(name, f"source {source_id}", lambda fn=fn: fn(source_id))
            if self._source_status(source_id) is status:
                return  # the stage did not advance the source; it is retried next night
        self._render_pending()

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
            result = rank_source(transcript, campaign, account.platforms, client=self._claude, fewshots=fewshots)
        except (RankError, PromptError, ComplianceError) as exc:
            with session_scope(self.factory) as s:
                alerts.raise_alert(s, alerts.RANK_EMPTY, source_id, f"ranking source {source_id} failed: {exc}")
            raise _StageFailed(f"rank: {exc}") from exc
        with session_scope(self.factory) as s:
            src = s.get(SourceRow, source_id)
            assert src is not None
            for cand in result.candidates:
                copy = cand.copy_
                assert copy is not None
                row = CandidateRow(
                    source_id=source_id,
                    start_s=cand.start_s,
                    end_s=cand.end_s,
                    score=cand.score,
                    standalone=cand.standalone,
                    brief_ok=cand.brief_ok,
                    reasons=cand.reasons,
                    hook=copy.hook,
                    title=copy.youtube_title,
                    captions_json={c.platform.value: c.text for c in copy.captions},
                    prompt_version=cand.prompt_version,
                )
                s.add(row)
                s.flush()
                record_event(
                    s, "candidate", row.id, None, ClipState.CANDIDATE.value, detail=f"score {cand.score}, {row.hook}"
                )
            detail = (
                f"{len(result.candidates)} candidate(s) from {result.windows_built} windows; "
                f"{len(result.score.failed_batches)} failed batch(es), {len(result.copy_failures)} copy failure(s)"
            )
            set_source_status(s, src, SourceStatus.RANKED, detail=detail)
            if not result.candidates:
                alerts.raise_alert(s, alerts.RANK_EMPTY, source_id, f"source {source_id} produced no candidates")
        self.report.ranked += 1
        self.report.candidates_created += len(result.candidates)

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
            )
            s.add(clip)
            s.flush()
            record_event(s, "clip", clip.id, None, ClipState.RENDERED.value, detail=f"candidate {cand_id}")
            transition(s, cand, ClipState.RENDERED, detail=f"clip {clip.id}")
            self.report.rendered += 1
            if report.passed:
                transition(s, clip, ClipState.PENDING_REVIEW, detail="QA passed")
                _make_proxy(self.settings, self.deps, s, clip.id, clip.path)
                self.report.ready += 1
            else:
                transition(s, clip, ClipState.QA_FAILED, detail=_failed_checks(report)[:1000])
                self.report.qa_failed += 1

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

RerenderKind = Literal["new_hook", "shorter"]
REVIEWABLE: frozenset[ClipState] = frozenset({ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED})


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
            clip_text=text_between(transcript.words, cand.start_s, cand.end_s),
        )


def finish_rerender(
    settings: Settings, factory: sessionmaker[Session], job: RerenderJob, deps: PipelineDeps | None = None
) -> str:
    """New copy (New hook, paid) or new range (Shorter), render, QA, then swap the clip to the new file with a new
    `rendered_at` and move it RENDERED -> PENDING_REVIEW. Any failure (including QA) keeps the old file and returns
    the clip to review (see `restore_after_failed_rerender`)."""
    deps = deps or PipelineDeps()
    new_path: str | None = None
    try:
        hook, captions, title = job.hook, job.inputs.captions, job.inputs.title
        if job.kind == "new_hook":
            client = _build_claude(settings, factory, deps)
            copy = write_copy(
                job.candidate, job.clip_text, job.inputs.campaign, job.inputs.account.platforms, client=client
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
        return f"Clip {job.clip_id}: {what} failed ({reason}). The previous version is back for review."

    now = deps.clock()
    with session_scope(factory) as s:
        clip = s.get(ClipRow, job.clip_id)
        cand = s.get(CandidateRow, job.inputs.candidate_id)
        assert clip is not None
        assert cand is not None
        if clip.state is not ClipState.RENDERED:  # recovered meanwhile by the worker: keep its decision
            _remove_render_dir(settings, manifest.clip_path)
            return f"Clip {job.clip_id} changed while re-rendering; nothing was swapped."
        clip.path = manifest.clip_path
        clip.sha256 = manifest.sha256
        clip.duration_s = manifest.duration_s
        clip.qa_report_json = report.model_dump(mode="json")
        clip.rendered_at = fresh_render_time(s, clip.id, now)  # earlier approvals are now stale (COMPLIANCE #5)
        if job.kind == "new_hook":
            old_hook = cand.hook
            cand.hook = hook
            cand.title = title
            cand.captions_json = {p.value: t for p, t in captions.items()}
            detail = f"new hook: {old_hook!r} -> {hook!r}"
        else:
            detail = f"shorter: {cand.start_s:.2f}-{cand.end_s:.2f} -> {job.start_s:.2f}-{job.end_s:.2f}"
            cand.start_s, cand.end_s = job.start_s, job.end_s
        record_event(s, "candidate", cand.id, None, None, actor="operator", detail=detail)
        transition(s, clip, ClipState.PENDING_REVIEW, actor="operator", detail=f"re-rendered ({job.kind})")
        _make_proxy(settings, deps, s, clip.id, clip.path)
        flag = s.get(FlagRow, f"{RERENDER_FLAG}{job.clip_id}")
        if flag is not None:
            s.delete(flag)
    if job.old_path != manifest.clip_path:
        _remove_render_dir(settings, job.old_path)  # never posted: re-renders start only before publishing
    what = f"new hook: {hook}" if job.kind == "new_hook" else f"now {job.end_s - job.start_s:.0f}s"
    return f"Clip {job.clip_id} re-rendered ({what}). The new preview follows; approve that one."


def restore_after_failed_rerender(
    settings: Settings, factory: sessionmaker[Session], deps: PipelineDeps, clip_id: int, reason: str
) -> None:
    """Return a clip stuck in RENDERED to review with its existing file: QA it again and set `rendered_at = now`, so
    any approval needs a fresh tap on the re-sent preview (COMPLIANCE #5). QA_FAILED if the old file fails QA."""
    now = deps.clock()
    with session_scope(factory) as s:
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
