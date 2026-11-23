"""Versioned prompt templates (`prompts/*.md`) and the pieces rendered into them.

A prompt file is YAML front matter (`name`, `version`, optional notes) followed by `## system` and `## user`
sections. Templates use `string.Template` (`$var`), so braces or dollar signs inside transcript text are harmless:
substituted values are never re-parsed. Every candidate stores `prompt_version_tag(...)` (PRD F12).

Few-shot examples come from the `fewshots` table, written weekly by track/learning (Phase 1 D). This module only
reads it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml  # type: ignore[import-untyped]  # no types-PyYAML in the lockfile
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import FewshotRow
from clipforge.schemas import CampaignIn, Window

REPO_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
FEWSHOTS_PER_KIND = 5
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,16}$")
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
_SECTION_RE = re.compile(r"^## (system|user)[ \t]*$", re.M)


class PromptError(ValueError):
    """A prompt file is missing, malformed or a template variable is missing."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"

    def render_user(self, **values: object) -> str:
        try:
            return Template(self.user).substitute({k: str(v) for k, v in values.items()})
        except (KeyError, ValueError) as exc:
            raise PromptError(f"prompt {self.tag}: bad or missing template variable {exc}") from exc


def prompts_dir() -> Path:
    """`./prompts` when running from the repo root or the Docker WORKDIR, else the repo checkout's `prompts/`."""
    cwd = Path.cwd() / "prompts"
    return cwd if cwd.is_dir() else REPO_PROMPTS_DIR


def parse_prompt(text: str, *, expected_name: str | None = None) -> Prompt:
    match = _FRONT_MATTER_RE.match(text.replace("\r\n", "\n"))
    if not match:
        raise PromptError("prompt file must start with '---' front matter")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise PromptError(f"invalid front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise PromptError("front matter must be a mapping")
    name = str(meta.get("name", "")).strip()
    version = str(meta.get("version", "")).strip()
    if not name:
        raise PromptError("front matter needs 'name'")
    if expected_name is not None and name != expected_name:
        raise PromptError(f"front matter name {name!r} does not match file {expected_name!r}")
    if not _VERSION_RE.match(version):
        raise PromptError(f"prompt {name}: front matter needs a short 'version' (got {version!r})")

    body = match.group(2)
    parts = _SECTION_RE.split(body)
    # parts = [preamble, "system", text, "user", text]
    sections: dict[str, str] = {}
    for key, value in zip(parts[1::2], parts[2::2], strict=True):
        if key in sections:
            raise PromptError(f"prompt {name}: duplicate '## {key}' section")
        sections[key] = value.strip()
    missing = [k for k in ("system", "user") if not sections.get(k)]
    if missing:
        raise PromptError(f"prompt {name}: missing or empty section(s): {', '.join(missing)}")
    return Prompt(name=name, version=version, system=sections["system"], user=sections["user"])


def load_prompt(name: str, directory: Path | None = None) -> Prompt:
    path = (directory or prompts_dir()) / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"prompt file not found: {path}")
    return parse_prompt(path.read_text(encoding="utf-8"), expected_name=name)


def prompt_version_tag(*prompts: Prompt) -> str:
    """e.g. 'score_windows@1+clip_copy@1' (stored on each candidate; the DB column is 64 chars)."""
    tag = "+".join(p.tag for p in prompts)
    if len(tag) > 64:
        raise PromptError(f"prompt version tag too long for the candidates column: {tag!r}")
    return tag


# --------------------------------------------------------------------------------------------------
# Rendered pieces
# --------------------------------------------------------------------------------------------------


def escape_text(text: str) -> str:
    """Keep transcript text from closing or opening our XML-ish tags."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def campaign_brief(campaign: CampaignIn) -> str:
    lines = [
        f"Campaign: {campaign.name}",
        f"Content kind: {campaign.content_kind}",
        f"Political campaign: {'yes' if campaign.is_political else 'no'}",
    ]
    if campaign.required_tags:
        lines.append(f"Required tags (added automatically): {' '.join(campaign.required_tags)}")
    lines.append("Rules and brief notes:")
    lines.append(escape_text(campaign.rules_notes.strip()) or "(none given)")
    return "\n".join(lines)


def render_windows(windows: Sequence[Window]) -> str:
    return "\n".join(
        f'<window id="{w.window_id}" start_s="{w.start_s:.2f}" end_s="{w.end_s:.2f}">\n{escape_text(w.text)}\n</window>'
        for w in windows
    )


@dataclass(frozen=True)
class Fewshot:
    kind: str  # positive | negative
    hook: str | None
    excerpt: str
    outcome_note: str
    week: str


def load_fewshots(session: Session, per_kind: int = FEWSHOTS_PER_KIND) -> list[Fewshot]:
    """Newest examples first: up to `per_kind` positives, then up to `per_kind` negatives."""
    out: list[Fewshot] = []
    for kind in ("positive", "negative"):
        rows = session.scalars(
            select(FewshotRow)
            .where(FewshotRow.kind == kind)
            .order_by(FewshotRow.week.desc(), FewshotRow.id.desc())
            .limit(per_kind)
        )
        out.extend(Fewshot(r.kind, r.hook, r.transcript_excerpt, r.outcome_note, r.week) for r in rows)
    return out


def render_fewshots(fewshots: Iterable[Fewshot], *, max_excerpt_chars: int = 600) -> str:
    items = list(fewshots)
    if not items:
        return "<examples>none yet</examples>"
    blocks = []
    for f in items:
        excerpt = f.excerpt.strip()
        if len(excerpt) > max_excerpt_chars:
            excerpt = excerpt[:max_excerpt_chars].rsplit(" ", 1)[0] + " …"
        blocks.append(
            f'<example kind="{f.kind}" week="{escape_text(f.week)}">\n'
            f"hook: {escape_text(f.hook or '(none)')}\n"
            f"outcome: {escape_text(f.outcome_note or '(no note)')}\n"
            f"excerpt: {escape_text(excerpt)}\n</example>"
        )
    return "<examples>\n" + "\n".join(blocks) + "\n</examples>"
