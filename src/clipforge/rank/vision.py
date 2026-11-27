"""Keyframe check on the shortlist (moments-v2): Haiku looks at frames from the candidate window.

For each shortlisted candidate: about one frame every `vision.frame_every_s` (at most `vision.max_frames`), scaled to
`vision.frame_width` and sent as JPEG image blocks (https://platform.claude.com/docs/en/build-with-claude/vision),
plus one or two crops of the facecam box when the source has one. The forced tool `judge_frames` says whether an
event, the payoff and the streamer's reaction are visible, what the scene is (gameplay, menu/loading, cutscene, just
chatting, mixed), spoiler and graphic risk, whether the setup is clear, and returns corrected rubric scores.

`apply_vision` replaces the transcript scores with the corrected ones and maps findings to profile rules (all
optional, from `vision.*` in the profile): no visible event in a menu/loading/just-chatting scene → `no_event_skip`;
a cutscene without a visible reaction → `cutscene_skip`, with one → `cutscene_part_skip`; spoiler risk →
`spoiler_skip`; graphic risk → the `graphic_review` review flag (never a reject). The spend caps are respected (the
client guards every call) and the cost of each check is recorded per candidate.

Image token cost is about width x height / 750 per image (vision docs), so 8 frames at 768x432 is about 3.5k tokens.
"""

from __future__ import annotations

import base64
import logging
import math
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from clipforge import costs
from clipforge.rank.catalogue import catalogue_values
from clipforge.rank.claude_client import ClaudeClient, RankError, strict_object
from clipforge.rank.moments import timestamped_lines
from clipforge.rank.prompts import Prompt, campaign_brief, escape_text, load_prompt
from clipforge.schemas import CampaignIn, PxBox, Word

if TYPE_CHECKING:
    from clipforge.rank.moment_score import Evaluated, MomentItem
    from clipforge.rank.profile import MomentProfile

log = logging.getLogger(__name__)

TOOL_NAME = "judge_frames"
TOOL_DESCRIPTION = "Record what the keyframes show and the corrected rubric scores."
MAX_TOKENS = 2048
NOTE_MAX = 200
GRAB_TIMEOUT_S = 60
FACECAM_CROPS = 2
Scene = Literal["gameplay", "menu_loading", "cutscene", "just_chatting", "mixed"]
SCENES: tuple[str, ...] = ("gameplay", "menu_loading", "cutscene", "just_chatting", "mixed")


@dataclass(frozen=True)
class Frame:
    t: float
    jpeg: bytes
    kind: Literal["frame", "facecam"] = "frame"


FramesFn = Callable[[float, float, Sequence[float]], list[Frame]]
# Called once with the shortlist windows (so a facecam can be detected on exactly those) -> a FramesFn, or None when
# no frames can be read (the check is then skipped and recorded).
FramesFactory = Callable[[Sequence[tuple[float, float]]], FramesFn | None]


class VisionVerdict(BaseModel):
    visible_event: bool
    visible_payoff: bool
    reaction_visible: bool
    scene: Scene
    spoiler_risk: bool
    graphic_risk: bool
    setup_clear: bool
    note: str
    scores: dict[str, int]


@dataclass
class VisionOutcome:
    checked: int = 0
    failed: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    per_candidate_usd: dict[str, float] = field(default_factory=dict)
    frames_sent: int = 0

    def summary(self) -> str:
        if not self.checked and not self.failed:
            return "vision: not run"
        avg = self.cost_usd / self.checked if self.checked else 0.0
        return (
            f"vision: {self.checked} checked ({self.frames_sent} images, ${self.cost_usd:.4f}, "
            f"${avg:.4f}/candidate)" + (f", {len(self.failed)} failed" if self.failed else "")
        )


def frame_times(start_s: float, end_s: float, every_s: float, max_frames: int) -> list[float]:
    """About one time every `every_s` across [start, end] (at most `max_frames`), at the middle of equal slices."""
    dur = max(end_s - start_s, 0.0)
    n = max(1, min(max_frames, math.ceil(dur / every_s))) if dur > 0 else 1
    return [round(start_s + (k + 0.5) * dur / n, 3) for k in range(n)]


def judge_schema(profile: MomentProfile) -> dict[str, Any]:
    props: dict[str, Any] = {
        "visible_event": {"type": "boolean"},
        "visible_payoff": {"type": "boolean"},
        "reaction_visible": {"type": "boolean"},
        "scene": {"type": "string", "enum": list(SCENES)},
        "spoiler_risk": {"type": "boolean"},
        "graphic_risk": {"type": "boolean"},
        "setup_clear": {"type": "boolean"},
    }
    for c in profile.rubric:
        props[c.id] = {"type": "integer", "description": f"{c.label}, 0-10."}
    props["note"] = {"type": "string", "description": f"One line, at most {NOTE_MAX} characters."}
    return strict_object(props)


def parse_verdict(raw: Any, profile: MomentProfile) -> VisionVerdict | None:
    from clipforge.rank.moment_score import rubric_scores

    if not isinstance(raw, dict):
        return None
    scores = rubric_scores(raw, profile)
    if scores is None:
        return None
    fields = {k: raw.get(k) for k in VisionVerdict.model_fields if k != "scores"}
    try:
        verdict = VisionVerdict.model_validate({**fields, "scores": scores})
    except ValidationError:
        return None
    return verdict.model_copy(update={"note": " ".join(verdict.note.split())[:NOTE_MAX]})


def _user_blocks(e: Evaluated, words: Sequence[Word], frames: Sequence[Frame], brief: str) -> list[dict[str, Any]]:
    start, end = e.span
    scores = ", ".join(f"{k} {v}" for k, v in e.item.scores.items())
    intro = (
        f"<campaign_brief>\n{brief}\n</campaign_brief>\n\n"
        f'<candidate id="{e.moment.moment_id}" start_s="{start:.2f}" end_s="{end:.2f}">\n'
        f"transcript judgement: [{e.item.archetype}] {escape_text(e.item.reason)}\n"
        f"transcript scores: {scores}\n"
        f"clip transcript:\n{escape_text(timestamped_lines(words, start, end))}\n</candidate>\n\n"
        f"Keyframes in time order follow (absolute seconds), then face cam crops if there is a face cam."
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": intro}]
    for f in frames:
        label = f"face cam at {f.t:.1f}s:" if f.kind == "facecam" else f"frame at {f.t:.1f}s:"
        blocks.append({"type": "text", "text": label})
        data = base64.standard_b64encode(f.jpeg).decode("ascii")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    blocks.append({"type": "text", "text": "Call the judge_frames tool once for this candidate."})
    return blocks


def judge_moment(
    e: Evaluated,
    words: Sequence[Word],
    campaign: CampaignIn,
    profile: MomentProfile,
    *,
    client: ClaudeClient,
    frames: FramesFn,
    outcome: VisionOutcome,
    prompt: Prompt | None = None,
) -> VisionVerdict | None:
    """One keyframe check. A failure (no frames, refusal, bad output) is recorded and returns None; spend caps and
    API errors after retries propagate."""
    cid = e.moment.moment_id
    v = profile.vision
    start, end = e.span
    try:
        images = frames(start, end, frame_times(start, end, v.frame_every_s, v.max_frames))
    except OSError as exc:
        outcome.failed.append(f"{cid}: frames: {exc}")
        return None
    if not any(f.kind == "frame" for f in images):
        outcome.failed.append(f"{cid}: no frames could be read")
        return None
    prompt = prompt or load_prompt("judge_frames")
    values = {**catalogue_values(profile), "campaign_brief": campaign_brief(campaign)}
    system = Prompt("system", "0", "", prompt.system).render_user(**values)
    try:
        result = client.forced_tool(
            model=client.app.models.rank,
            system=system,
            user=_user_blocks(e, words, images, campaign_brief(campaign)),
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            input_schema=judge_schema(profile),
            max_tokens=MAX_TOKENS,
        )
    except RankError as exc:
        outcome.failed.append(f"{cid}: {exc}")
        return None
    usd = costs.price_usd(client.app, client.app.models.rank, result.input_tokens, "input_tokens") + costs.price_usd(
        client.app, client.app.models.rank, result.output_tokens, "output_tokens"
    )
    outcome.cost_usd += usd
    outcome.per_candidate_usd[cid] = round(usd, 6)
    outcome.frames_sent += len(images)
    verdict = parse_verdict(result.data, profile)
    if verdict is None:
        outcome.failed.append(f"{cid}: invalid judge_frames output")
        return None
    outcome.checked += 1
    return verdict


def apply_vision(item: MomentItem, verdict: VisionVerdict, profile: MomentProfile) -> tuple[MomentItem, dict[str, Any]]:
    """The item with the corrected scores and any skip rules / review flags the frames call for, plus the record that
    is stored on the candidate (`vision_json`)."""
    v = profile.vision
    skips = list(item.skip_flags)
    flags = list(item.review_flags)
    added: list[str] = []

    def skip(rule_id: str | None) -> None:
        if rule_id is not None and rule_id not in skips:
            skips.append(rule_id)
            added.append(rule_id)

    if verdict.scene in ("menu_loading", "just_chatting") and not verdict.visible_event:
        skip(v.no_event_skip)
    if verdict.scene == "cutscene":
        skip(v.cutscene_part_skip if verdict.reaction_visible else v.cutscene_skip)
    if verdict.spoiler_risk:
        skip(v.spoiler_skip)
    if verdict.graphic_risk and v.graphic_review is not None and v.graphic_review not in flags:
        flags.append(v.graphic_review)
        added.append(v.graphic_review)
    record = {
        **verdict.model_dump(exclude={"scores"}),
        "scores": dict(verdict.scores),
        "transcript_scores": dict(item.scores),
        "added": added,
    }
    new = item.model_copy(update={"scores": dict(verdict.scores), "skip_flags": skips, "review_flags": flags})
    return new, record


# --------------------------------------------------------------------------------------------------
# Frames from the source file (ffmpeg -> JPEG)
# --------------------------------------------------------------------------------------------------


def _jpeg_args(path: Path, t: float, vf: str, quality: int) -> list[str]:
    q = max(2, min(31, round(31 - (quality / 100) * 29)))  # ffmpeg mjpeg qscale: 2 best .. 31 worst
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
        "-an", "-sn", "-dn", "-vf", vf, "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", str(q), "-",
    ]  # fmt: skip


def grab_jpeg(path: Path, t: float, *, width: int, quality: int, crop: PxBox | None = None) -> bytes | None:
    """One frame at `t` as JPEG bytes (fast seek), scaled to `width` (optionally cropped first). None if unreadable."""
    vf = f"scale={width}:-2"
    if crop is not None:
        vf = f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y},scale='min({width // 2},iw)':-2"
    try:
        done = subprocess.run(_jpeg_args(path, t, vf, quality), check=True, capture_output=True, timeout=GRAB_TIMEOUT_S)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    data = done.stdout
    return data if data[:2] == b"\xff\xd8" else None


def source_frames(path: Path, *, width: int, quality: int, facecam: PxBox | None = None) -> FramesFn:
    """A FramesFn over one source file: the keyframes, then up to two facecam crops (around 1/3 and 2/3 in)."""

    def frames(start_s: float, end_s: float, times: Sequence[float]) -> list[Frame]:
        out = [Frame(t, j) for t in times if (j := grab_jpeg(path, t, width=width, quality=quality)) is not None]
        if facecam is not None:
            for k in range(FACECAM_CROPS):
                t = start_s + (end_s - start_s) * (k + 1) / (FACECAM_CROPS + 1)
                j = grab_jpeg(path, t, width=width, quality=quality, crop=facecam)
                if j is not None:
                    out.append(Frame(round(t, 3), j, "facecam"))
        return out

    return frames
