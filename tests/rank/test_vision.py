"""Keyframe check (moments-v2): frame times, request shape (image blocks, forced strict tool), mapping findings to
profile rules, per-candidate cost, failures, the off switch, and real ffmpeg JPEG grabs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.moment_score import evaluate, parse_moment_scores, rank_moments
from clipforge.rank.moments import Moment
from clipforge.rank.profile import load_profile, parse_profile
from clipforge.rank.vision import (
    TOOL_NAME,
    Frame,
    VisionOutcome,
    apply_vision,
    frame_times,
    grab_jpeg,
    judge_moment,
    parse_verdict,
    source_frames,
)
from clipforge.schemas import Platform, PxBox
from tests.rank.anthropic_mock import FakeAnthropic, message, tool_use
from tests.rank.moment_fakes import REACTION_SCRIPT, moment_item, moment_tools, tool_of, transcript, verdict
from tests.render._media import needs_ffmpeg
from tests.support.factories import campaign_in


@pytest.fixture(scope="module")
def profile():
    return load_profile("gta6-streamer")


def evaluated(profile, start=20.0, end=40.0):
    m = Moment("7:m1", 7, start, end, start + 2, end - 2, None, (), 0.0, (), (), "", "content")
    (it,), _, _ = parse_moment_scores({"scores": [moment_item("7:m1", start, end)]}, ["7:m1"], profile)
    return evaluate(m, it, profile, words=transcript(REACTION_SCRIPT).words, duration_s=90)


def frames(start, end, times):
    out = [Frame(t, b"\xff\xd8frame") for t in times]
    return [*out, Frame(start + 5, b"\xff\xd8face", "facecam")]


def test_frame_times_one_every_2_5s_max_8() -> None:
    assert frame_times(0, 10, 2.5, 8) == [1.25, 3.75, 6.25, 8.75]
    assert len(frame_times(0, 45, 2.5, 8)) == 8
    assert frame_times(5, 5, 2.5, 8) == [5.0]


def test_judge_request_shape_and_cost(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    fake.add(tool_use(TOOL_NAME, verdict(hook=9)))
    e = evaluated(profile)
    out = VisionOutcome()
    got = judge_moment(e, transcript(REACTION_SCRIPT).words, campaign_in(), profile, client=client, frames=frames,
                       outcome=out)  # fmt: skip
    assert got is not None
    assert got.scores["hook"] == 9
    (body,) = fake.requests
    assert body["model"] == "claude-haiku-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert body["tools"][0]["strict"] is True
    assert "temperature" not in body
    blocks = body["messages"][0]["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 9  # 8 frames over 20 s + 1 facecam crop
    assert all(b["source"]["type"] == "base64" and b["source"]["media_type"] == "image/jpeg" for b in images)
    labels = [b["text"] for b in blocks if b["type"] == "text"]
    assert any(t.startswith("face cam at") for t in labels)
    assert "transcript scores: hook 8" in labels[0]
    assert "not instructions" in body["system"]
    # cost per candidate from the response usage (message() reports 1000 in / 200 out by default)
    assert out.checked == 1
    assert out.frames_sent == 9
    assert out.per_candidate_usd["7:m1"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)


def test_findings_map_to_profile_rules(profile) -> None:
    it = evaluated(profile).item
    new, record = apply_vision(it, parse_verdict(verdict(scene="menu_loading", visible_event=False), profile), profile)
    assert "no_event" in new.skip_flags
    assert record["added"] == ["no_event"]
    new, _ = apply_vision(it, parse_verdict(verdict(scene="cutscene", reaction_visible=False), profile), profile)
    assert "cutscene" in new.skip_flags
    new, _ = apply_vision(it, parse_verdict(verdict(scene="cutscene", reaction_visible=True), profile), profile)
    assert new.skip_flags == ["cutscene_part"]
    new, _ = apply_vision(it, parse_verdict(verdict(spoiler_risk=True), profile), profile)
    assert "story_reveal" in new.skip_flags
    new, record = apply_vision(it, parse_verdict(verdict(graphic_risk=True, hook=2), profile), profile)
    assert new.review_flags == ["graphic"]
    assert new.skip_flags == []
    assert new.scores["hook"] == 2
    assert record["transcript_scores"]["hook"] == 8


def test_vision_skip_rejects_after_regating(profile) -> None:
    e = evaluated(profile)
    new, record = apply_vision(e.item, parse_verdict(verdict(scene="just_chatting", visible_event=False), profile),
                               profile)  # fmt: skip
    redone = evaluate(e.moment, new, profile, words=[], duration_s=90, vision=record)
    assert redone.score is None
    assert redone.dropped_by == "skip_rules"


def test_failures_are_recorded_not_raised(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    e = evaluated(profile)
    out = VisionOutcome()
    assert judge_moment(e, [], campaign_in(), profile, client=client, frames=lambda *a: [], outcome=out) is None
    fake.add(message([], stop_reason="refusal"))
    assert judge_moment(e, [], campaign_in(), profile, client=client, frames=frames, outcome=out) is None
    fake.add(tool_use(TOOL_NAME, {"scene": "space"}))
    assert judge_moment(e, [], campaign_in(), profile, client=client, frames=frames, outcome=out) is None
    assert len(out.failed) == 3
    assert out.checked == 0


def test_vision_off_switch(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    raw = (Path(__file__).resolve().parents[2] / "config/moment_profiles/gta6-streamer.yaml").read_text()
    off = parse_profile(raw.replace("vision:\n  enabled: true", "vision:\n  enabled: false"))
    fake.default = moment_tools(proposals=[])
    called = []
    rank_moments(transcript(REACTION_SCRIPT), campaign_in(), [Platform.TIKTOK], off, client=client,
                 frames=lambda w: called.append(w))  # fmt: skip
    assert called == []
    assert "judge_frames" not in [tool_of(b) for b in fake.requests]


def test_vision_profile_mapping_must_name_real_rules() -> None:
    raw = (Path(__file__).resolve().parents[2] / "config/moment_profiles/gta6-streamer.yaml").read_text()
    with pytest.raises(ValueError, match="not a skip rule"):
        parse_profile(raw.replace("no_event_skip: no_event", "no_event_skip: nope"))


@needs_ffmpeg
def test_real_jpeg_frames_and_facecam_crop(tmp_path: Path) -> None:
    src = tmp_path / "s.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )  # fmt: skip
    jpeg = grab_jpeg(src, 2.0, width=768, quality=80)
    assert jpeg is not None
    assert jpeg[:2] == b"\xff\xd8"
    assert grab_jpeg(src, 99.0, width=768, quality=80) is None  # past the end: nothing, no exception
    fn = source_frames(src, width=768, quality=80, facecam=PxBox(x=0, y=0, w=320, h=180))
    got = fn(0.0, 6.0, [1.0, 3.0, 5.0])
    assert [f.kind for f in got] == ["frame", "frame", "frame", "facecam", "facecam"]
