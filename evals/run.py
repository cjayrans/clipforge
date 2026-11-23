"""Ranking eval harness: `uv run python -m evals.run [--live] [--k 5] [--repeat 2]`.

For each transcript, builds windows, scores them through the real `rank.score` code path, selects the top-k
candidates (`rank.select`) and reports:

- schema validity: share of returned score items that are valid `schemas.WindowScore`s for a requested window;
- top-k agreement with `evals/labels.csv`: a candidate matches a label when their IoU is >= `--iou` (0.5).
  precision@k = top-k candidates matching a `good` label; recall@k = `good` labels matched; bad@k = top-k
  candidates matching a `bad` label;
- stability (live mode with `--repeat N`): mean Jaccard of the top-k window ids between run 1 and runs 2..N.
  Claude Haiku 5.5 rejects `temperature`, so there is no temperature-0 determinism to test; this measures how much
  the ranking moves between identical requests instead.

Replay mode (default) answers from `evals/fixtures/replay/*.json` through a mock HTTP transport: offline, free,
deterministic, and it exercises the same request/validation code. Live mode calls the Anthropic API with
`ANTHROPIC_API_KEY`, records costs in the configured database and stops at the monthly cap.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from rich.console import Console
from rich.table import Table

from clipforge import costs
from clipforge.config import ConfigError, Settings, load_settings
from clipforge.db import Base, make_engine, make_session_factory, session_scope
from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.prompts import Fewshot, load_fewshots, load_prompt
from clipforge.rank.score import TOOL_NAME, score_windows
from clipforge.rank.select import select_candidates
from clipforge.rank.windows import build_windows
from clipforge.schemas import CampaignIn, Transcript

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TRANSCRIPTS = (REPO / "tests/fixtures/transcript_talk.json", REPO / "tests/fixtures/transcript_sparse.json")
DEFAULT_CAMPAIGN = REPO / "campaigns/dylan-page-gta6.json"
DEFAULT_LABELS = REPO / "evals/labels.csv"
DEFAULT_REPLAY_DIR = REPO / "evals/fixtures/replay"
REPLAY_MATCH_IOU = 0.3
REPLAY_KEY = "replay-no-network"  # never a real key: replay requests go to the mock transport only

console = Console(soft_wrap=True)
_WINDOW_TAG = re.compile(r'<window id="([^"]+)" start_s="([0-9.]+)" end_s="([0-9.]+)">')


# --------------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    transcript: str
    start_s: float
    end_s: float
    label: str  # good | bad
    note: str = ""


def load_labels(path: Path) -> list[Label]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        label = row["label"].strip().lower()
        if label not in ("good", "bad"):
            raise ValueError(f"{path}: label must be good or bad, got {row['label']!r}")
        out.append(Label(row["transcript"].strip(), float(row["start_s"]), float(row["end_s"]), label, row["note"]))
    return out


def iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def topk_metrics(spans: Sequence[tuple[float, float]], labels: Sequence[Label], k: int, thr: float) -> dict[str, Any]:
    top = list(spans)[:k]
    good = [(lb.start_s, lb.end_s) for lb in labels if lb.label == "good"]
    bad = [(lb.start_s, lb.end_s) for lb in labels if lb.label == "bad"]

    def matches(span: tuple[float, float], targets: list[tuple[float, float]]) -> bool:
        return any(iou(span, t) >= thr for t in targets)

    return {
        "k": k,
        "labels_good": len(good),
        "labels_bad": len(bad),
        "precision_at_k": (sum(matches(s, good) for s in top) / len(top)) if top and good else None,
        "recall_at_k": (sum(any(iou(s, g) >= thr for s in top) for g in good) / len(good)) if good else None,
        "bad_at_k": sum(matches(s, bad) for s in top) if bad else None,
    }


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)


# --------------------------------------------------------------------------------------------------
# Replay transport
# --------------------------------------------------------------------------------------------------


class Replay:
    """Answers `score_windows` requests from canned span scores, keyed by source id."""

    def __init__(self, directory: Path) -> None:
        self.by_source: dict[int, dict[str, Any]] = {}
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.by_source[int(data["source_id"])] = data

    def item_for(self, window_id: str, start_s: float, end_s: float) -> dict[str, Any]:
        source_id = int(window_id.split(":", 1)[0])
        data = self.by_source.get(source_id, {})
        best, best_iou = data.get("default", {"score": 0, "standalone": False, "brief_ok": True,
                                              "hook_potential": 0, "reasons": "no replay data"}), 0.0  # fmt: skip
        for span in data.get("spans", []):
            overlap = iou((start_s, end_s), (span["start_s"], span["end_s"]))
            if overlap >= REPLAY_MATCH_IOU and overlap > best_iou:
                best, best_iou = span, overlap
        fields = ("score", "standalone", "brief_ok", "hook_potential", "reasons")
        item = {"window_id": window_id, "trim_start_s": None, "trim_end_s": None}
        item.update({f: best[f] for f in fields if f in best})
        return item

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        user = body["messages"][0]["content"]
        items = [self.item_for(wid, float(s), float(e)) for wid, s, e in _WINDOW_TAG.findall(user)]
        return httpx2.Response(
            200,
            json={
                "id": "msg_replay",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "tool_use", "id": "toolu_replay", "name": TOOL_NAME, "input": {"scores": items}}],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )


# --------------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------------


@dataclass
class TranscriptReport:
    transcript: str
    windows: int
    calls: int
    items_returned: int
    invalid_items: int
    validity: float
    failed_batches: list[str]
    top: list[dict[str, Any]]
    metrics: dict[str, Any]
    stability: float | None = None
    runs: list[list[str]] = field(default_factory=list)


def _run_once(
    transcript: Transcript, campaign: CampaignIn, client: ClaudeClient, k: int, fewshots: Sequence[Fewshot]
) -> tuple[Any, list[Any], int]:
    cfg = client.app.pipeline
    windows = build_windows(
        transcript, min_s=cfg.window_min_s, max_s=cfg.window_max_s, keep=cfg.windows_scored_per_source
    )
    outcome = score_windows(windows, campaign, client=client, fewshots=fewshots, prompt=load_prompt("score_windows"))
    picked = select_candidates(
        windows,
        outcome.scores,
        words=transcript.words,
        top_n=k,
        min_clip_s=cfg.min_clip_s,
        max_clip_s=cfg.max_clip_s,
        prompt_version="eval",
    )
    return outcome, picked, len(windows)


def evaluate(
    *,
    transcripts: Sequence[Path],
    campaign: CampaignIn,
    labels: Sequence[Label],
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    k: int,
    repeat: int,
    iou_threshold: float,
) -> list[TranscriptReport]:
    reports = []
    for path in transcripts:
        transcript = Transcript.model_validate(json.loads(path.read_text(encoding="utf-8")))
        name = path.stem
        runs: list[list[str]] = []
        first: TranscriptReport | None = None
        for _ in range(max(repeat, 1)):
            outcome, picked, n_windows = _run_once(transcript, campaign, client, k, fewshots)
            runs.append([p.window.window_id for p in picked])
            if first is None:
                spans = [(p.candidate.start_s, p.candidate.end_s) for p in picked]
                first = TranscriptReport(
                    transcript=name,
                    windows=n_windows,
                    calls=outcome.calls,
                    items_returned=outcome.items_returned,
                    invalid_items=outcome.invalid_items,
                    validity=outcome.validity,
                    failed_batches=outcome.failed_batches,
                    top=[
                        {
                            "window_id": p.window.window_id,
                            "start_s": p.candidate.start_s,
                            "end_s": p.candidate.end_s,
                            "score": p.candidate.score,
                        }  # fmt: skip
                        for p in picked
                    ],
                    metrics=topk_metrics(spans, [lb for lb in labels if lb.transcript == name], k, iou_threshold),
                )
        assert first is not None
        first.runs = runs
        if len(runs) > 1:
            first.stability = sum(jaccard(runs[0], r) for r in runs[1:]) / (len(runs) - 1)
        reports.append(first)
    return reports


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def print_report(reports: Sequence[TranscriptReport], mode: str) -> None:
    table = Table(title=f"rank eval ({mode})")
    for col in ("transcript", "windows", "calls", "validity", "P@k", "R@k", "bad@k", "stability", "top-k"):
        table.add_column(col)
    for r in reports:
        top = ", ".join(f"{t['start_s']:.0f}-{t['end_s']:.0f}s:{t['score']}" for t in r.top)
        m = r.metrics
        table.add_row(
            r.transcript, str(r.windows), str(r.calls), _fmt(r.validity), _fmt(m["precision_at_k"]),
            _fmt(m["recall_at_k"]), _fmt(m["bad_at_k"]), _fmt(r.stability), top or "-",
        )  # fmt: skip
    console.print(table)
    for r in reports:
        for failure in r.failed_batches:
            console.print(f"[yellow]{r.transcript}: skipped batch {failure}[/yellow]")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--live", action="store_true", help="call the Anthropic API (costs money; needs ANTHROPIC_API_KEY)")
    p.add_argument("--k", type=int, default=5, help="top-k candidates to compare with labels (default 5)")
    p.add_argument("--repeat", type=int, default=1, help="runs per transcript, for the stability metric (live)")
    p.add_argument("--iou", type=float, default=0.5, help="IoU for a candidate to match a label (default 0.5)")
    p.add_argument("--transcripts", type=Path, nargs="+", default=list(DEFAULT_TRANSCRIPTS))
    p.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    p.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY_DIR)
    p.add_argument("--out", type=Path, default=None, help="report JSON path (default <data dir>/evals/<time>.json)")
    args = p.parse_args(argv)
    if args.k <= 0 or args.repeat <= 0:
        p.error("--k and --repeat must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings: Settings = load_settings()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    campaign = CampaignIn.model_validate(json.loads(args.campaign.read_text(encoding="utf-8")))
    labels = load_labels(args.labels)
    mode = "live" if args.live else "replay"

    if args.live:
        if settings.secrets.anthropic_api_key is None:
            console.print("[red]--live needs ANTHROPIC_API_KEY in the environment; nothing was sent.[/red]")
            return 2
        if not settings.db_path.is_file():
            console.print(f"[red]database not found at {settings.db_path}; run `uv run cf db upgrade` first.[/red]")
            return 2
        engine = make_engine(settings.database_url)
        http_client = None
        key = settings.secrets.anthropic_api_key
    else:
        engine = make_engine("sqlite://")  # throwaway ledger: replay spends nothing
        Base.metadata.create_all(engine)
        http_client = httpx2.Client(transport=httpx2.MockTransport(Replay(args.replay_dir).handler))
        key = None

    factory = make_session_factory(engine)
    try:
        # The client commits each cost row in its own transaction, so spend survives an early stop (COMPLIANCE #12).
        client = ClaudeClient(key or REPLAY_KEY, ledger=factory, app=settings.app, http_client=http_client)
        with session_scope(factory) as session:  # read-only: few-shot examples
            fewshots = load_fewshots(session)
        reports = evaluate(
            transcripts=args.transcripts,
            campaign=campaign,
            labels=labels,
            client=client,
            fewshots=fewshots,
            k=args.k,
            repeat=args.repeat if args.live else 1,
            iou_threshold=args.iou,
        )
        with session_scope(factory) as session:
            spent = costs.month_to_date(session, settings.app) if args.live else 0.0
    except costs.SpendCapReached as exc:
        console.print(f"[red]stopped: {exc}[/red]")
        return 3
    finally:
        engine.dispose()

    print_report(reports, mode)
    out = args.out or settings.data_dir / "evals" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "at": datetime.now(UTC).isoformat(),
        "campaign": campaign.name,
        "prompt": load_prompt("score_windows").tag,
        "model": settings.app.models.rank,
        "month_to_date_usd": spent,
        "transcripts": [asdict(r) for r in reports],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
