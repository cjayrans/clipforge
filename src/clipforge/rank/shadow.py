"""`cf source shadow-rank <id> --report PATH [--full] [--compare-clips 24-31] [--ledger-url URL]` (moments-v2).

Ranks one registered source with the current moment logic and writes a markdown report, WITHOUT writing candidates,
clips or posts and without changing the source. Used to compare the new logic with the clips picked before.

1. Footage: with `--full` and a Twitch source, the whole registered VOD is downloaded again into
   `media/scratch/shadow-<id>/` (registered URL only, compliance check first, disk-space check from the VOD's
   metadata) and deleted at the end whatever happens. Otherwise the source's file on disk is used.
2. Transcript: with `--full`, the scratch file is transcribed (Groq, silence-aware 45 s-overlap chunks; nothing
   stored); otherwise the stored transcript is used.
3. Loudness/motion signals over the whole file, then `rank_moments` (content pass, signals, rubric scoring, keyframe
   check) with the top 10 kept. Paid calls go through the normal spend caps (`costs.for_source`).
4. Report: the top 10 (description, archetype, six scores, found by, vision verdict) next to the clips picked before,
   the score distribution by archetype, the 10 best windows that were discarded and why, the spend by stage, and
   download size, disk use and processing time.

`--ledger-url`: when this runs on a copy of the database (so the live one is never touched by new-schema code), the
cost rows recorded during the run are also appended to the live database's `costs` table (base columns only), so the
monthly cap there stays true.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from clipforge import compliance, costs
from clipforge.config import Settings
from clipforge.db import (
    CampaignRow,
    CandidateRow,
    ClipRow,
    CostRow,
    SourceRow,
    TranscriptRow,
    make_engine,
    make_session_factory,
    session_scope,
    utcnow,
)
from clipforge.db.migrate import upgrade
from clipforge.ingest import disk, media
from clipforge.publish.queries import campaign_model
from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.media_signals import MediaSignals
from clipforge.rank.media_signals import extract as extract_media_signals
from clipforge.rank.moment_score import Evaluated, MomentRun, overlaps_any, rank_moments, weighted_score
from clipforge.rank.profile import MomentProfile, load_profile
from clipforge.rank.prompts import load_fewshots
from clipforge.rank.vision import FramesFn, source_frames
from clipforge.schemas import Candidate, SourceHost, Transcript
from clipforge.transcribe.service import Transcriber, load_transcript, transcribe_media

log = logging.getLogger(__name__)

TOP_N = 10
GB = 1024**3


@dataclass
class ShadowDeps:
    """External pieces, replaceable in tests."""

    ytdl_factory: Callable[[dict[str, object]], object] | None = None
    transcriber: Transcriber | None = None
    runner: media.Runner = media.run
    claude_http_client: httpx2.Client | None = None
    signals: Callable[[Path, Any], MediaSignals] | None = None
    frames: Callable[[Path, Sequence[tuple[float, float]]], FramesFn | None] | None = None


@dataclass
class ShadowStats:
    duration_s: float = 0.0
    download_bytes: int | None = None
    free_before: int | None = None
    free_after_download: int | None = None
    timings: dict[str, float] = field(default_factory=dict)
    spend_by_stage: dict[str, float] = field(default_factory=dict)
    scratch: str = ""
    transcript_words: int = 0


def parse_clip_ids(spec: str) -> list[int]:
    """'24-31' or '24,25,30' -> ids."""
    out: list[int] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out.extend(range(min(a, b), max(a, b) + 1))
        else:
            out.append(int(part))
    return out


# --------------------------------------------------------------------------------------------------
# Footage and transcript
# --------------------------------------------------------------------------------------------------


def _download_full(settings: Settings, source: SourceRow, campaign: CampaignRow, scratch: Path, deps: ShadowDeps,
                   stats: ShadowStats) -> Path:  # fmt: skip
    from clipforge.ingest import twitch

    with session_scope(make_session_factory(make_engine(settings.database_url))) as s:
        registered = list(s.scalars(select(SourceRow.url).where(SourceRow.campaign_id == campaign.id)))
    compliance.assert_registered_source(source.url, campaign_model(campaign), registered)  # COMPLIANCE #1 first
    if not campaign.allow_stream_download:
        raise compliance.ComplianceError(1, f"campaign {campaign.id} does not allow stream downloads")
    stats.free_before = disk.free_bytes(scratch)

    def disk_check(meta: dict[str, Any]) -> None:
        disk.check_free(scratch, disk.estimate_bytes(meta), settings.app.min_free_gb)

    started = time.monotonic()
    path = twitch.download(
        source.url, scratch, str(source.id), ytdl_factory=deps.ytdl_factory, channels=campaign.stream_channels,
        max_minutes=None, disk_check=disk_check,
    )  # fmt: skip
    stats.timings["download"] = time.monotonic() - started
    stats.download_bytes = path.stat().st_size
    stats.free_after_download = disk.free_bytes(scratch)
    return path


def _stored_transcript(factory: sessionmaker[Session], source_id: int) -> Transcript:
    with session_scope(factory) as s:
        row = s.scalar(
            select(TranscriptRow).where(TranscriptRow.source_id == source_id).order_by(TranscriptRow.id.desc())
        )
        if row is None:
            raise RuntimeError(f"source {source_id} has no stored transcript; use --full")
        return load_transcript(row.words_json_path)


# --------------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------------


def _scores(scores: dict[str, int], profile: MomentProfile) -> str:
    return " ".join(f"{scores.get(k, '-')}" for k in profile.rubric_ids)


def _ts(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _vision(v: dict[str, Any] | None) -> str:
    if not v:
        return "-"
    flags = "".join(c for c, k in (("E", "visible_event"), ("P", "visible_payoff"), ("R", "reaction_visible"))
                    if v.get(k))  # fmt: skip
    return f"{v.get('scene', '?')} {flags or '-'}"


def _md(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _old_clips(factory: sessionmaker[Session], clip_ids: Sequence[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with session_scope(factory) as s:
        for cid in clip_ids:
            clip = s.get(ClipRow, cid)
            if clip is None:
                continue
            cand = s.get(CandidateRow, clip.candidate_id)
            if cand is None:
                continue
            v1 = [cand.score_reaction, cand.score_event, cand.score_setup, cand.score_absurdity]
            out.append(
                {
                    "clip": cid,
                    "state": clip.state.value,
                    "start": cand.start_s,
                    "end": cand.end_s,
                    "score": cand.score,
                    "archetype": cand.archetype or "-",
                    "v1": "/".join("-" if x is None else str(x) for x in v1),
                    "hook": cand.hook or "",
                    "reason": cand.reasons,
                }
            )
    return out


def _discard_reason(e: Evaluated, picks: Sequence[Evaluated]) -> str:
    if e.dropped_by is not None:
        gate = next((g for g in reversed(e.gates) if g.name == e.dropped_by), None)
        return f"{e.dropped_by}: {gate.detail if gate else ''}".strip()
    if any(overlaps_any(e.span, [p.span]) for p in picks):
        return "overlaps a better pick"
    return f"below the top {TOP_N}"


def render_shadow_report(
    *,
    source_id: int,
    profile: MomentProfile,
    run: MomentRun,
    candidates: Sequence[Candidate],
    old: Sequence[dict[str, Any]],
    stats: ShadowStats,
    started_at: datetime,
) -> str:
    rubric = " ".join(profile.rubric_ids)
    lines = [
        f"# Shadow rank of source {source_id} ({profile.weights_version})",
        "",
        f"Run {started_at:%Y-%m-%d %H:%M} UTC. Source {stats.duration_s / 3600:.2f} h, "
        f"{stats.transcript_words:,} transcript words. No candidates were written.",
        "",
        "## Top 10 with the new logic",
        "",
        f"Scores are {rubric} (0-10). Found by: content pass, audio/transcript signals, or both. Frames: scene and "
        "what the keyframes show (E event, P payoff, R reaction).",
        "",
        "| # | window | score | archetype | scores | found by | frames | what happens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(candidates, 1):
        m = c.moment
        assert m is not None
        old_hit = [o["clip"] for o in old if overlaps_any((c.start_s, c.end_s), [(o["start"], o["end"])])]
        note = f" (= old clip {', '.join(map(str, old_hit))})" if old_hit else ""
        lines.append(
            f"| {i} | {_ts(c.start_s)}-{_ts(c.end_s)} ({c.end_s - c.start_s:.0f}s) | {c.score} | {m.archetype} | "
            f"{_scores(m.scores, profile)} | {m.found_by or '-'} | {_vision(m.vision)} | {_md(m.reason)}{note} |"
        )
    if old:
        lines += [
            "",
            "## Clips picked before",
            "",
            "Scored with v1 (reaction/event/setup/absurdity).",
            "",
            "| clip | state | window | score | archetype | v1 scores | hook | in new top 10? |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for o in old:
            hit = [
                i for i, c in enumerate(candidates, 1) if overlaps_any((c.start_s, c.end_s), [(o["start"], o["end"])])
            ]
            lines.append(
                f"| {o['clip']} | {o['state']} | {_ts(o['start'])}-{_ts(o['end'])} | {o['score']} | {o['archetype']} | "
                f"{o['v1']} | {_md(o['hook'])} | {'#' + ', #'.join(map(str, hit)) if hit else 'no'} |"
            )
    # distribution by archetype (every scored candidate, kept or not)
    by_arch: dict[str, list[float]] = {}
    kept: dict[str, int] = {}
    found: dict[str, dict[str, int]] = {}
    for e in run.evaluated:
        w = weighted_score(e.item.scores, profile)
        by_arch.setdefault(e.item.archetype, []).append(w)
        kept[e.item.archetype] = kept.get(e.item.archetype, 0) + int(e.score is not None)
        f = found.setdefault(e.item.archetype, {})
        f[e.moment.found_by] = f.get(e.moment.found_by, 0) + 1
    lines += [
        "",
        "## Score distribution by archetype",
        "",
        f"Weighted score before skip-rule penalties, every scored candidate. Kept = passed every gate (min score "
        f"{profile.gates.min_score:g}).",
        "",
        "| archetype | scored | kept | mean | median | min | max | found by |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arch, vals in sorted(by_arch.items(), key=lambda kv: -statistics.fmean(kv[1])):
        fb = ", ".join(f"{k} {v}" for k, v in sorted(found[arch].items()))
        lines.append(
            f"| {arch} | {len(vals)} | {kept.get(arch, 0)} | {statistics.fmean(vals):.0f} | "
            f"{statistics.median(vals):.0f} | {min(vals):.0f} | {max(vals):.0f} | {fb} |"
        )
    dropped = run.dropped_counts()
    lines += ["", "Dropped by gate: " + (", ".join(f"{k} {v}" for k, v in dropped.items() if v) or "none") + "."]
    # best discarded
    picked = {id(e) for e in run.picks}
    rest = [e for e in run.evaluated if id(e) not in picked]
    rest.sort(key=lambda e: -weighted_score(e.item.scores, profile))
    lines += [
        "",
        "## The 10 best windows that were discarded",
        "",
        "| window | raw score | archetype | scores | found by | why | what happens |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in rest[:10]:
        lines.append(
            f"| {_ts(e.window.start_s)}-{_ts(e.window.end_s)} | {weighted_score(e.item.scores, profile):.0f} | "
            f"{e.item.archetype} | {_scores(e.item.scores, profile)} | {e.moment.found_by} | "
            f"{_md(_discard_reason(e, run.picks))[:160]} | {_md(e.item.reason)} |"
        )
    # spend and resources
    total = sum(stats.spend_by_stage.values())
    v = run.vision
    per = v.cost_usd / v.checked if v.checked else 0.0
    lines += [
        "",
        "## Spend and resources",
        "",
        "| stage | USD |",
        "|---|---|",
        *[f"| {k} | {usd:.4f} |" for k, usd in sorted(stats.spend_by_stage.items())],
        f"| **total** | **{total:.4f}** |",
        "",
        f"Keyframe check: {v.checked} candidate(s), {v.frames_sent} image(s), ${v.cost_usd:.4f} "
        f"(${per:.4f} per candidate)" + (f"; {len(v.failed)} failed" if v.failed else "") + ".",
        "",
        f"Run detail: {run.summary(profile.weights_version)}",
        "",
    ]
    if stats.download_bytes is not None:
        peak = (stats.free_before or 0) - (stats.free_after_download or 0)
        lines.append(
            f"Full VOD download: {stats.download_bytes / GB:.2f} GB; disk free {(stats.free_before or 0) / GB:.1f} GB "
            f"before, {(stats.free_after_download or 0) / GB:.1f} GB after (about {max(peak, 0) / GB:.2f} GB used); "
            f"scratch deleted afterwards."
        )
    timing = ", ".join(f"{k} {t / 60:.1f} min" for k, t in stats.timings.items())
    lines.append(f"Processing time: {timing}.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------


def _mirror_costs(src_factory: sessionmaker[Session], after_id: int, ledger_url: str) -> int:
    """Append the run's cost rows to another database's `costs` table (base columns, works on older schemas)."""
    with session_scope(src_factory) as s:
        rows = s.execute(
            text("SELECT at, service, model, units, unit_kind, usd FROM costs WHERE id > :after ORDER BY id"),
            {"after": after_id},
        ).all()
    if not rows:
        return 0
    engine = create_engine(ledger_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO costs (at, service, model, units, unit_kind, usd) "
                    "VALUES (:at, :service, :model, :units, :unit_kind, :usd)"
                ),
                [dict(r._mapping) for r in rows],
            )
    finally:
        engine.dispose()
    return len(rows)


def live_guard(
    settings: Settings, ledger_url: str, copy_factory: sessionmaker[Session], after_id: int
) -> Callable[[], None]:
    """A check for `costs.extra_guard`: the live database's pause flag, and its month-to-date spend plus what this run
    has spent on the copy (not yet mirrored), against `monthly_api_cap_usd`. Reads only columns every schema has."""
    live_engine = make_engine(ledger_url)
    live = make_session_factory(live_engine)

    def check() -> None:
        with session_scope(copy_factory) as s:
            run_spend = float(s.scalar(select(func.coalesce(func.sum(CostRow.usd), 0.0)).where(CostRow.id > after_id))
                              or 0.0)  # fmt: skip
        with session_scope(live) as s:
            mtd = costs.month_to_date(s, settings.app)
            reason = costs.is_paused(s)
        cap = settings.app.monthly_api_cap_usd
        if reason is not None:
            raise costs.SpendCapReached(mtd + run_spend, cap, paused_reason=reason)
        if mtd + run_spend >= cap:
            raise costs.SpendCapReached(mtd + run_spend, cap)

    return check


def shadow_rank(
    settings: Settings,
    source_id: int,
    *,
    report: Path,
    full: bool = False,
    compare_clips: str = "",
    ledger_url: str | None = None,
    deps: ShadowDeps | None = None,
) -> Path:
    deps = deps or ShadowDeps()
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    started_at = utcnow()
    first_cost = 0
    stats = ShadowStats()
    scratch = settings.media_dir / "scratch" / f"shadow-{source_id}"
    stats.scratch = str(scratch)
    stack = contextlib.ExitStack()
    try:
        with session_scope(factory) as s:
            source = s.get(SourceRow, source_id)
            if source is None:
                raise RuntimeError(f"source {source_id} not found")
            camp = s.get(CampaignRow, source.campaign_id)
            assert camp is not None
            campaign = campaign_model(camp)
            fewshots = load_fewshots(s)
            first_cost = int(s.scalar(select(func.coalesce(func.max(CostRow.id), 0))) or 0)
        if campaign.moment_profile is None:
            raise RuntimeError(f"campaign {camp.id} has no moment_profile; shadow-rank needs one")
        profile = load_profile(campaign.moment_profile)
        if ledger_url:  # running on a copy: the live database's cap and pause flag still apply
            check = live_guard(settings, ledger_url, factory, first_cost)
            check()  # before downloading anything
            stack.enter_context(costs.extra_guard(check))

        with costs.for_source(source_id, "shadow-download"):
            if full and compliance.classify_source_host(source.url) is SourceHost.TWITCH:
                path = _download_full(settings, source, camp, scratch, deps, stats)
            elif source.local_path and Path(source.local_path).is_file():
                path = Path(source.local_path)
            else:
                raise RuntimeError(f"source {source_id} has no file on disk; use --full for a Twitch VOD")

        started = time.monotonic()
        if full:
            with session_scope(factory) as s, costs.for_source(source_id, "transcribe"):
                src = s.get(SourceRow, source_id)
                assert src is not None
                transcript, _ = transcribe_media(
                    s, settings, src, path, transcriber=deps.transcriber, local=None, runner=deps.runner,
                    ledger=factory,
                )  # fmt: skip
        else:
            transcript = _stored_transcript(factory, source_id)
        stats.timings["transcribe"] = time.monotonic() - started
        stats.duration_s = transcript.duration_s
        stats.transcript_words = len(transcript.words)

        started = time.monotonic()
        sig_fn = deps.signals or (lambda p, rules: extract_media_signals(p, rules, deps.runner))
        signals = sig_fn(path, profile.signals)
        stats.timings["signals"] = time.monotonic() - started

        def frames(windows: Sequence[tuple[float, float]]) -> FramesFn | None:
            if deps.frames is not None:
                return deps.frames(path, windows)
            return _frames(path, windows, profile)

        client = ClaudeClient.from_settings(settings, factory, http_client=deps.claude_http_client)
        started = time.monotonic()
        with costs.for_source(source_id, "rank"):
            result = rank_moments(
                transcript, campaign, [], profile, client=client, fewshots=fewshots,
                loudness_db=signals.loudness_db, motion=signals.motion, frames=frames, top_n=TOP_N,
            )  # fmt: skip
        stats.timings["rank (content pass, scoring, vision)"] = time.monotonic() - started

        with session_scope(factory) as s:
            for stage, usd in s.execute(
                select(CostRow.stage, func.sum(CostRow.usd)).where(CostRow.id > first_cost).group_by(CostRow.stage)
            ).all():
                stats.spend_by_stage[stage or "other"] = float(usd or 0.0)
        old = _old_clips(factory, parse_clip_ids(compare_clips)) if compare_clips else []
        run = result.moment_run
        assert isinstance(run, MomentRun)
        text_out = render_shadow_report(
            source_id=source_id, profile=profile, run=run, candidates=result.candidates, old=old, stats=stats,
            started_at=started_at,
        )  # fmt: skip
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text_out, encoding="utf-8")
        print(f"wrote {report}")
        return report
    finally:
        stack.close()
        shutil.rmtree(scratch, ignore_errors=True)
        if ledger_url:  # even after a failure: whatever was spent must reach the live ledger
            try:
                n = _mirror_costs(factory, first_cost, ledger_url)
                print(f"shadow-rank: {n} cost row(s) appended to the ledger database")
            except Exception:
                log.exception("shadow-rank: could not append cost rows to the ledger database")
        engine.dispose()


def _frames(path: Path, windows: Sequence[tuple[float, float]], profile: MomentProfile) -> FramesFn | None:
    """Keyframes from the file, with facecam crops when a facecam is found in the shortlist windows (not stored)."""
    from clipforge.qa.probe import ProbeError, probe
    from clipforge.render.facecam import default_detect

    try:
        info = probe(path)
    except ProbeError:
        return None
    box = None
    if profile.vision.facecam_crop and info.width and info.height:
        try:
            detection = default_detect(path, list(windows))
        except Exception:  # mediapipe missing (Mac) or unreadable media: frames without the crop
            detection = None
        if detection is not None and detection.status == "found" and detection.box is not None:
            box = detection.box
    v = profile.vision
    return source_frames(path, width=v.frame_width, quality=v.jpeg_quality, facecam=box)
