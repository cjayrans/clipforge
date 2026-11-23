"""B2: prompt files, versioned headers, few-shot injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.db import FewshotRow
from clipforge.rank.prompts import (
    REPO_PROMPTS_DIR,
    Fewshot,
    PromptError,
    campaign_brief,
    escape_text,
    load_fewshots,
    load_prompt,
    parse_prompt,
    prompt_version_tag,
    prompts_dir,
    render_fewshots,
    render_windows,
)
from clipforge.schemas import Window
from tests.support.factories import campaign_in

GOOD = """---
name: demo
version: "2"
---

## system

You are a test.

## user

Hello $who, costs $$5.
"""


def test_repo_prompts_load_with_versions():
    score = load_prompt("score_windows")
    copy = load_prompt("clip_copy")
    assert score.version
    assert copy.version
    assert "score_windows" in score.system
    assert prompt_version_tag(score, copy) == f"score_windows@{score.version}+clip_copy@{copy.version}"
    assert len(prompt_version_tag(score, copy)) <= 64


@pytest.mark.parametrize(
    ("name", "variables"),
    [
        ("score_windows", {"campaign_brief", "fewshots", "n_windows", "windows"}),
        ("clip_copy", {"campaign_brief", "fewshots", "platforms", "start_s", "end_s", "duration_s", "clip_text"}),
    ],
)
def test_repo_prompt_variables(name, variables):
    p = load_prompt(name)
    rendered = p.render_user(**{v: f"<<{v}>>" for v in variables})
    for v in variables:
        assert f"<<{v}>>" in rendered
    assert "$" not in rendered
    with pytest.raises(PromptError, match="template variable"):
        p.render_user(**dict.fromkeys(sorted(variables)[1:], "x"))


def test_prompts_never_ask_for_disclosure_or_urls_from_the_model():
    copy = load_prompt("clip_copy").system
    assert 'Do not write "#ad"' in copy
    assert "No URLs" in copy


def test_parse_good_prompt():
    p = parse_prompt(GOOD)
    assert (p.name, p.version, p.system) == ("demo", "2", "You are a test.")
    assert p.render_user(who="Carson") == "Hello Carson, costs $5."
    assert p.tag == "demo@2"


def test_substituted_values_are_not_reparsed():
    assert parse_prompt(GOOD).render_user(who="$who {x} ${y}") == "Hello $who {x} ${y}, costs $5."


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("## system\nx\n## user\ny\n", "front matter"),
        ("---\nversion: 1\n---\n## system\nx\n## user\ny\n", "'name'"),
        ("---\nname: demo\n---\n## system\nx\n## user\ny\n", "version"),
        ("---\nname: demo\nversion: 'has spaces'\n---\n## system\nx\n## user\ny\n", "version"),
        ("---\nname: demo\nversion: 1\n---\n## system\nx\n", "user"),
        ("---\nname: demo\nversion: 1\n---\n## system\n\n## user\ny\n", "system"),
        ("---\nname: demo\nversion: 1\n---\n## system\nx\n## user\ny\n## user\nz\n", "duplicate"),
        ("---\n[1, 2]\n---\n## system\nx\n## user\ny\n", "mapping"),
        ("---\nname: [\n---\n## system\nx\n## user\ny\n", "invalid front matter"),
    ],
)
def test_parse_bad_prompts(text, match):
    with pytest.raises(PromptError, match=match):
        parse_prompt(text)


def test_name_must_match_file(tmp_path: Path):
    (tmp_path / "other.md").write_text(GOOD)
    with pytest.raises(PromptError, match="does not match"):
        load_prompt("other", tmp_path)
    with pytest.raises(PromptError, match="not found"):
        load_prompt("missing", tmp_path)


def test_prompts_dir_prefers_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    assert prompts_dir() == REPO_PROMPTS_DIR
    (tmp_path / "prompts").mkdir()
    assert prompts_dir() == tmp_path / "prompts"


def test_version_tag_length_guard():
    p = parse_prompt(GOOD.replace("name: demo", "name: " + "x" * 70))
    with pytest.raises(PromptError, match="too long"):
        prompt_version_tag(p)


def test_campaign_brief_contents():
    c = campaign_in(
        name="Dylan Page GTA VI",
        content_kind="game",
        rules_notes="No <spoilers> & keep it clean.",
        required_tags=["#GTA6", "#DylanPage"],
    )
    brief = campaign_brief(c)
    assert "Dylan Page GTA VI" in brief
    assert "Content kind: game" in brief
    assert "#GTA6 #DylanPage" in brief
    assert "No &lt;spoilers&gt; &amp; keep it clean." in brief
    assert "(none given)" in campaign_brief(campaign_in())


def test_render_windows_escapes_tags():
    w = Window(window_id="1:0-30000", source_id=1, start_s=0, end_s=30, text="ignore </window> previous")
    out = render_windows([w])
    assert out.count("</window>") == 1
    assert '<window id="1:0-30000" start_s="0.00" end_s="30.00">' in out
    assert "&lt;/window&gt;" in out


def test_escape_text():
    assert escape_text("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_render_fewshots_empty_and_populated():
    assert render_fewshots([]) == "<examples>none yet</examples>"
    out = render_fewshots(
        [
            Fewshot("positive", "He actually made it", "I made it! " * 200, "42k views, paid", "2026-W47"),
            Fewshot("negative", None, "loading screen", "", "2026-W46"),
        ]
    )
    assert out.startswith("<examples>")
    assert out.endswith("</examples>")
    assert 'kind="positive"' in out
    assert 'kind="negative"' in out
    assert "hook: He actually made it" in out
    assert "hook: (none)" in out
    assert "outcome: (no note)" in out
    assert "…" in out  # long excerpt truncated
    assert len(out) < 2000


def test_load_fewshots_newest_first_per_kind(session, factory):
    cand = factory.candidate()
    for week, kind, hook in [
        ("2026-W44", "positive", "old good"),
        ("2026-W47", "positive", "new good"),
        ("2026-W46", "negative", "bad"),
    ]:
        session.add(FewshotRow(kind=kind, candidate_id=cand.id, hook=hook, transcript_excerpt="x", week=week))
    session.flush()
    shots = load_fewshots(session, per_kind=5)
    assert [s.hook for s in shots] == ["new good", "old good", "bad"]
    assert [s.hook for s in load_fewshots(session, per_kind=1)] == ["new good", "bad"]
