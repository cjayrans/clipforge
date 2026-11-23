from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from clipforge.compliance import normalize_caption
from clipforge.qa.checks import (
    caption_check,
    duration_check,
    file_integrity_check,
    qa_clip,
    run_qa,
)
from clipforge.qa.probe import ProbeError, ProbeResult
from clipforge.render.encode import sha256_file
from clipforge.schemas import CampaignStatus, Platform, QAReport, RenderManifest
from tests.compliance._helpers import campaign, manifest

GOOD = ProbeResult(
    duration_s=35.02, size_bytes=1, width=1080, height=1920, fps=30.0, video_codec="h264", audio_codec="aac",
    audio_streams=1, video_streams=1,
)  # fmt: skip
PLATFORMS = [Platform.TIKTOK, Platform.INSTAGRAM]


def captions(c: object = None, platforms: list[Platform] = PLATFORMS) -> dict[Platform, str]:
    camp = c or campaign(allowed_platforms=list(Platform))
    return {p: normalize_caption("Wild GTA moment", p, camp) for p in platforms}  # type: ignore[arg-type]


def report(p: ProbeResult = GOOD, m: RenderManifest | None = None, **kw: object) -> QAReport:
    camp = kw.pop("camp", None) or campaign(allowed_platforms=list(Platform))
    platforms = kw.pop("platforms", PLATFORMS)
    caps = kw.pop("caps", None) or captions(camp, platforms)  # type: ignore[arg-type]
    return run_qa(m or manifest(), p, camp, platforms, caps, **kw)  # type: ignore[arg-type]


def failed(r: QAReport) -> set[str]:
    return {c.name for c in r.checks if not c.passed}


def test_good_clip_passes() -> None:
    r = report()
    assert r.passed, [c for c in r.checks if not c.passed]
    names = {c.name for c in r.checks}
    assert {"duration", "resolution", "fps", "video_codec", "audio", "manifest", "campaign_active"} <= names
    assert {"platform:tiktok", "caption:tiktok", "platform:instagram", "caption:instagram"} <= names


@pytest.mark.parametrize(("d", "ok"), [(10.0, True), (90.0, True), (9.9, False), (95.0, False)])
def test_duration_check(d: float, ok: bool) -> None:
    assert duration_check(d).passed is ok


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"duration_s": 95.0}, "duration"),
        ({"duration_s": 33.0}, "duration_matches_manifest"),
        ({"width": 1920, "height": 1080}, "resolution"),
        ({"fps": 29.97}, "fps"),
        ({"video_codec": "hevc"}, "video_codec"),
        ({"audio_streams": 0, "audio_codec": None}, "audio"),
        ({"audio_streams": 2}, "audio"),
    ],
)
def test_probe_failures(change: dict[str, object], check: str) -> None:
    r = report(dataclasses.replace(GOOD, **change))  # type: ignore[arg-type]
    assert not r.passed
    assert check in failed(r)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"has_ad_label": False}, "no 'Ad' label"),
        ({"ad_label_end_s": 2.0}, ">= 3.0"),
        ({"audio_source_only": False}, "audio"),
    ],
)
def test_manifest_failures(change: dict[str, object], fragment: str) -> None:
    r = report(m=manifest(**change))
    check = next(c for c in r.checks if c.name == "manifest")
    assert not check.passed
    assert fragment in check.detail


@pytest.mark.parametrize("status", [CampaignStatus.DRAINED, CampaignStatus.ENDED])
def test_inactive_campaign_fails(status: CampaignStatus) -> None:
    camp = campaign(status=status, allowed_platforms=list(Platform))
    assert "campaign_active" in failed(report(camp=camp))


def test_platform_not_allowed_by_campaign_fails() -> None:
    camp = campaign(allowed_platforms=[Platform.TIKTOK])
    r = report(camp=camp)
    assert failed(r) == {"platform:instagram"}


def test_political_campaign_blocked_on_tiktok() -> None:
    camp = campaign(allowed_platforms=list(Platform), is_political=True)
    assert "platform:tiktok" in failed(report(camp=camp))


def test_platform_not_enabled_on_account_fails() -> None:
    assert "platform:instagram" in failed(report(enabled=[Platform.TIKTOK]))


def test_no_platforms_fails() -> None:
    assert "platforms" in failed(report(platforms=[], caps={Platform.TIKTOK: "#ad x"}))


def test_missing_caption_fails() -> None:
    r = report(caps={Platform.TIKTOK: "#ad hi"})
    assert failed(r) == {"caption:instagram"}


@pytest.mark.parametrize(
    ("platform", "caption", "fragment"),
    [
        (Platform.TIKTOK, "great clip #ad", "start with '#ad '"),
        (Platform.TIKTOK, "#ad", "start with '#ad '"),
        (Platform.TIKTOK, "#ad   ", "nothing after"),
        (Platform.X, "#ad watch twitch.tv/someone", "URL"),
        (Platform.X, "#ad " + "a" * 300, "limit"),
    ],
)
def test_caption_rules(platform: Platform, caption: str, fragment: str) -> None:
    check = caption_check(platform, caption)
    assert not check.passed
    assert fragment in check.detail


def test_file_integrity(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    assert not file_integrity_check(manifest(clip_path=str(clip))).passed
    clip.write_bytes(b"data")
    assert file_integrity_check(manifest(clip_path=str(clip), sha256=sha256_file(clip))).passed
    assert not file_integrity_check(manifest(clip_path=str(clip))).passed  # sha mismatch


def test_qa_clip_end_to_end_with_fake_probe(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"data")
    m = manifest(clip_path=str(clip), sha256=sha256_file(clip))
    camp = campaign(allowed_platforms=list(Platform))
    r = qa_clip(m, camp, PLATFORMS, captions(camp), probe_fn=lambda _: GOOD)
    assert r.passed
    assert r.checks[0].name == "file"


def test_qa_clip_probe_error_fails(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"data")
    m = manifest(clip_path=str(clip), sha256=sha256_file(clip))

    def broken(_: Path) -> ProbeResult:
        raise ProbeError("moov atom not found")

    r = qa_clip(m, campaign(allowed_platforms=list(Platform)), PLATFORMS, captions(), probe_fn=broken)
    assert not r.passed
    assert "probe" in failed(r)


def test_qa_clip_missing_file_fails(tmp_path: Path) -> None:
    r = qa_clip(manifest(clip_path=str(tmp_path / "nope.mp4")), campaign(), PLATFORMS, captions())
    assert failed(r) == {"file"}


def test_required_tags_checked() -> None:
    assert caption_check(Platform.TIKTOK, "#ad big win #GTA6", ["#gta6"]).passed
    missing = caption_check(Platform.TIKTOK, "#ad big win #GTA6s", ["#GTA6", "@rockstar"])
    assert not missing.passed
    assert "#GTA6, @rockstar" in missing.detail


def test_run_qa_uses_campaign_required_tags() -> None:
    camp = campaign(allowed_platforms=list(Platform), required_tags=["#DylanPage"])
    good = report(camp=camp)  # captions built with normalize_caption, which appends the tag
    assert good.passed
    bad = report(camp=camp, caps=dict.fromkeys(PLATFORMS, "#ad no tag here"))
    assert failed(bad) == {"caption:tiktok", "caption:instagram"}


@pytest.mark.parametrize(
    ("title", "ok"),
    [("Lamar roasts Franklin", True), (None, False), ("   ", False), ("x" * 96, True), ("x" * 97, False)],
)
def test_youtube_title_checked_when_youtube_is_a_target(title: str | None, ok: bool) -> None:
    """Render PR note: the 100-char YouTube title limit (with the '#ad ' prefix) is checked before YouTube is on."""
    r = report(platforms=[Platform.YOUTUBE], youtube_title=title)
    assert ("youtube_title" in failed(r)) is not ok


def test_youtube_title_not_checked_without_youtube() -> None:
    r = report(youtube_title=None)
    assert "youtube_title" not in {c.name for c in r.checks}
