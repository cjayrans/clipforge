"""`cf source shadow-rank`: ranks a source with the moments-v2 logic, writes a report, writes no candidates, deletes
its scratch footage, and can mirror its cost rows into an older-schema ledger database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from clipforge.config import Settings, load_settings
from clipforge.db import (
    CampaignRow,
    CandidateRow,
    ClipRow,
    SourceRow,
    TranscriptRow,
    make_engine,
    make_session_factory,
    session_scope,
    transition,
)
from clipforge.db.migrate import upgrade
from clipforge.ingest.models import campaign_row_values
from clipforge.rank.media_signals import MediaSignals
from clipforge.rank.shadow import ShadowDeps, parse_clip_ids, shadow_rank
from clipforge.rank.vision import Frame
from clipforge.schemas import ClipState, SourceStatus
from tests.ingest.test_twitch_limits import MetaYDL
from tests.rank.anthropic_mock import FakeAnthropic
from tests.rank.moment_fakes import REACTION_SCRIPT, loudness, moment_tools, motion, proposal, transcript
from tests.support.factories import campaign_in
from tests.support.pipeline_fakes import media_runner
from tests.transcribe.helpers import FakeTranscriber


@pytest.fixture
def shadow_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    return load_settings()


def seed(settings: Settings, *, url: str = "https://www.twitch.tv/videos/2", with_file: bool = True) -> tuple[int, int]:
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    with session_scope(make_session_factory(engine)) as s:
        c = campaign_in(
            moment_profile="gta6-streamer", allow_stream_download=True, stream_channels=["fakestreamer"],
            content_kind="game", source_urls=[url],
        )  # fmt: skip
        camp = CampaignRow(slug=c.slug, **campaign_row_values(c))
        s.add(camp)
        s.flush()
        media = settings.media_dir / "sources" / "2.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        if with_file:
            media.write_bytes(b"video")
        src = SourceRow(campaign_id=camp.id, url=url, status=SourceStatus.RANKED, local_path=str(media),
                        duration_s=90.0)  # fmt: skip
        s.add(src)
        s.flush()
        tpath = settings.media_dir / "transcripts" / f"{src.id}.json"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text(transcript(REACTION_SCRIPT, source_id=src.id).model_dump_json())
        s.add(TranscriptRow(source_id=src.id, provider="groq", model="whisper-large-v3-turbo",
                            words_json_path=str(tpath)))  # fmt: skip
        v1 = {"score_reaction": 8, "score_event": 7, "score_setup": 7, "score_absurdity": 8}
        old = CandidateRow(source_id=src.id, start_s=24.0, end_s=40.0, score=70, standalone=True, brief_ok=True,
                           reasons="[blunder] old pick", hook="Old hook", prompt_version="score_moments@1+clip_copy@3",
                           archetype="blunder", **v1)  # fmt: skip
        s.add(old)
        s.flush()
        transition(s, old, ClipState.RENDERED)
        clip = ClipRow(candidate_id=old.id, account_set="gaming-1", style_preset="bold-yellow", path="/x.mp4",
                       duration_s=16.0, sha256="b" * 64)  # fmt: skip
        s.add(clip)
        s.flush()
        transition(s, clip, ClipState.PENDING_REVIEW)
        ids = (src.id, clip.id)
    engine.dispose()
    return ids


def deps(fake: FakeAnthropic, **kw) -> ShadowDeps:
    fake.default = moment_tools(proposals=[proposal(44.0, 62.0, archetype="bit")])
    return ShadowDeps(
        claude_http_client=fake.http_client(),
        signals=lambda p, rules: MediaSignals(loudness_db=loudness(spikes={31: -8.0, 66: -9.0}),
                                              motion=motion(spikes={30.0: 25.0})),
        frames=lambda path, windows: (lambda s, e, times: [Frame(t, b"\xff\xd8jpeg") for t in times]),
        **kw,
    )  # fmt: skip


def test_parse_clip_ids() -> None:
    assert parse_clip_ids("24-31") == list(range(24, 32))
    assert parse_clip_ids("3, 5,7-8") == [3, 5, 7, 8]


def test_shadow_rank_writes_a_report_and_no_candidates(shadow_settings: Settings, tmp_path: Path) -> None:
    source_id, clip_id = seed(shadow_settings)
    fake = FakeAnthropic()
    out = shadow_rank(shadow_settings, source_id, report=tmp_path / "r.md", compare_clips=str(clip_id),
                      deps=deps(fake))  # fmt: skip
    text = out.read_text()
    assert "## Top 10 with the new logic" in text
    assert "hook arc arousal sendability clarity density" in text
    assert "## Clips picked before" in text
    assert "Old hook" in text
    assert "## Score distribution by archetype" in text
    assert "## The 10 best windows that were discarded" in text
    assert "## Spend and resources" in text
    assert "| content |" in text
    assert "| score |" in text
    assert "Keyframe check:" in text
    engine = make_engine(shadow_settings.database_url)
    with session_scope(make_session_factory(engine)) as s:
        assert len(s.scalars(select(CandidateRow)).all()) == 1  # only the old one: nothing written
        assert s.get(SourceRow, source_id).status is SourceStatus.RANKED  # source untouched
    engine.dispose()
    assert not (shadow_settings.media_dir / "scratch" / f"shadow-{source_id}").exists()


def test_full_download_is_registered_only_deleted_after_and_costs_mirror(
    shadow_settings: Settings, tmp_path: Path
) -> None:
    source_id, clip_id = seed(shadow_settings)
    ledger = tmp_path / "live.db"
    with sqlite3.connect(ledger) as conn:  # an older-schema costs table (no source_id / stage)
        conn.execute("CREATE TABLE costs (id INTEGER PRIMARY KEY, at TEXT, service TEXT, model TEXT, units REAL, "
                     "unit_kind TEXT, usd REAL)")  # fmt: skip
        conn.execute("CREATE TABLE flags (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    MetaYDL.instances.clear()
    MetaYDL.meta = {"uploader_id": "fakestreamer", "is_live": False, "filesize": 1000}
    fake = FakeAnthropic()
    out = shadow_rank(
        shadow_settings, source_id, report=tmp_path / "r.md", full=True, ledger_url=f"sqlite:///{ledger}",
        deps=deps(fake, ytdl_factory=MetaYDL, transcriber=FakeTranscriber(), runner=media_runner()),
    )  # fmt: skip
    (ydl,) = MetaYDL.instances
    assert ydl.opts.get("download_ranges") is None  # the whole VOD, not the first N minutes
    assert ydl.calls == [False, True]
    text = out.read_text()
    assert "Full VOD download:" in text
    assert "scratch deleted afterwards" in text
    assert not (shadow_settings.media_dir / "scratch" / f"shadow-{source_id}").exists()
    with sqlite3.connect(ledger) as conn:
        services = [r[0] for r in conn.execute("SELECT service FROM costs")]
    assert "anthropic" in services
    assert "groq" not in services


def test_no_download_when_the_campaign_lost_stream_permission(shadow_settings: Settings, tmp_path: Path) -> None:
    from clipforge.compliance import ComplianceError

    source_id, _ = seed(shadow_settings)
    engine = make_engine(shadow_settings.database_url)
    with session_scope(make_session_factory(engine)) as s:  # the campaign loses its permission
        s.scalars(select(CampaignRow)).one().allow_stream_download = False
    engine.dispose()
    MetaYDL.instances.clear()
    with pytest.raises(ComplianceError):
        shadow_rank(shadow_settings, source_id, report=tmp_path / "r.md", full=True,
                    deps=deps(FakeAnthropic(), ytdl_factory=MetaYDL))  # fmt: skip
    assert MetaYDL.instances == []


def test_live_ledger_at_the_cap_stops_before_any_paid_call(shadow_settings: Settings, tmp_path: Path) -> None:
    """On a database copy, the live database's month-to-date spend (and pause flag) still apply (COMPLIANCE #12)."""
    from clipforge import costs
    from clipforge.db import CostRow

    source_id, _ = seed(shadow_settings)
    live = tmp_path / "live.db"
    url = f"sqlite:///{live}"
    upgrade(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        usd = shadow_settings.app.monthly_api_cap_usd
        s.add(CostRow(service="groq", model="whisper-large-v3-turbo", units=1, unit_kind="audio_seconds", usd=usd))
    engine.dispose()
    fake = FakeAnthropic()
    MetaYDL.instances.clear()
    d = deps(fake, ytdl_factory=MetaYDL, transcriber=FakeTranscriber(), runner=media_runner())
    with pytest.raises(costs.SpendCapReached):
        shadow_rank(shadow_settings, source_id, report=tmp_path / "r.md", full=True, ledger_url=url, deps=d)
    assert fake.requests == []
    assert MetaYDL.instances == []  # not even downloaded


def _live_db(tmp_path: Path, *, paused: bool = False) -> str:
    from clipforge.db import FlagRow

    url = f"sqlite:///{tmp_path / 'live2.db'}"
    upgrade(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        if paused:
            s.add(FlagRow(key="pipeline_paused", value="operator pause"))
    engine.dispose()
    return url


def test_live_pause_flag_stops_the_shadow_run(shadow_settings: Settings, tmp_path: Path) -> None:
    from clipforge import costs

    source_id, _ = seed(shadow_settings)
    fake = FakeAnthropic()
    d = deps(fake)
    with pytest.raises(costs.SpendCapReached, match="paused"):
        shadow_rank(shadow_settings, source_id, report=tmp_path / "r.md", ledger_url=_live_db(tmp_path, paused=True),
                    deps=d)  # fmt: skip
    assert fake.requests == []


def test_live_spend_crossing_the_cap_mid_run_stops_the_next_paid_call(
    shadow_settings: Settings, tmp_path: Path
) -> None:
    from clipforge import costs
    from clipforge.db import CostRow

    source_id, _ = seed(shadow_settings)
    url = _live_db(tmp_path)
    fake = FakeAnthropic()
    d = deps(fake)
    inner = d.signals

    def signals_then_live_spend(path, rules):  # the live worker spends meanwhile
        engine = make_engine(url)
        with session_scope(make_session_factory(engine)) as s:
            s.add(CostRow(service="anthropic", model="claude-haiku-5-5", units=1, unit_kind="input_tokens",
                          usd=shadow_settings.app.monthly_api_cap_usd))  # fmt: skip
        engine.dispose()
        return inner(path, rules)

    d.signals = signals_then_live_spend
    with pytest.raises(costs.SpendCapReached):
        shadow_rank(shadow_settings, source_id, report=tmp_path / "r.md", ledger_url=url, deps=d)
    assert fake.requests == []  # the content pass never called the model
