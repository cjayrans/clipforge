"""`cf` command line.

Phase 0 owns this file and defines every command and its arguments. Commands owned by a later phase delegate to a
fixed `module:function` (see `TARGETS`); until that function exists the command exits with a message naming the
phase. Phase 1 branches implement the target function in their own package and never edit this file.

Real in Phase 0: `status`, `costs`, `resume`, `backup`, `db upgrade`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge import costs, heartbeat
from clipforge.config import ConfigError, Settings, load_settings
from clipforge.db import (
    CampaignRow,
    ClipRow,
    CostRow,
    PostRow,
    make_engine,
    make_session_factory,
    session_scope,
    utcnow,
)
from clipforge.db.backup import backup_db
from clipforge.db.migrate import upgrade
from clipforge.logsetup import setup_logging
from clipforge.schemas import CampaignStatus

console = Console()
err = Console(stderr=True)


@dataclass(frozen=True)
class Target:
    module: str
    func: str
    owner: str

    @property
    def dotted(self) -> str:
        return f"clipforge.{self.module}.{self.func}"


TARGETS: dict[str, Target] = {
    "campaign add": Target("ingest.registry", "add_campaign_from_json", "Phase 1 A (feat/ingest-transcribe)"),
    "campaign list": Target("ingest.registry", "list_campaigns", "Phase 1 A (feat/ingest-transcribe)"),
    "campaign drain": Target("ingest.registry", "set_campaign_status", "Phase 1 A (feat/ingest-transcribe)"),
    "campaign end": Target("ingest.registry", "set_campaign_status", "Phase 1 A (feat/ingest-transcribe)"),
    "campaign check": Target("ingest.registry", "check_campaign", "Phase 1 A (feat/ingest-transcribe)"),
    "source add": Target("ingest.registry", "add_source", "Phase 1 A (feat/ingest-transcribe)"),
    "source retry": Target("ingest.registry", "retry_source", "Phase 2 (P2.1)"),
    "worker": Target("scheduler.worker", "run_worker", "Phase 1 D (feat/ops)"),
    "bot": Target("review.bot", "run_bot", "Phase 1 D (feat/ops)"),
    "report weekly": Target("track.reports", "weekly_report", "Phase 1 D (feat/ops)"),
    "export-reconcile": Target("track.reconcile", "export_csv", "Phase 1 D (feat/ops)"),
    "import-reconcile": Target("track.reconcile", "import_csv", "Phase 1 D (feat/ops)"),
    "run --once": Target("pipeline", "run_once", "Phase 2 (P2.2)"),
}

EXIT_NOT_IMPLEMENTED = 3
EXIT_CONFIG = 2
EXIT_CHECK_FAILED = 1


class NotImplementedYet(RuntimeError):
    pass


def resolve(command: str) -> Callable[..., Any]:
    """Import the target function for `command`, or raise NotImplementedYet naming the owning phase."""
    target = TARGETS[command]
    module_name = f"clipforge.{target.module}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only "the module doesn't exist yet" means not implemented; a missing dependency inside it is a real error.
        if exc.name is not None and module_name.startswith(exc.name):
            raise NotImplementedYet(_not_implemented_msg(command, target)) from exc
        raise
    fn = getattr(module, target.func, None)
    if not callable(fn):
        raise NotImplementedYet(_not_implemented_msg(command, target))
    return fn  # type: ignore[no-any-return]


def _not_implemented_msg(command: str, target: Target) -> str:
    return f"`cf {command}` is implemented in {target.owner} (`{target.dotted}`). It is not available yet."


# --------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------


def _settings(ctx: typer.Context) -> Settings:
    settings = ctx.obj
    assert isinstance(settings, Settings)
    return settings


@contextmanager
def _db(settings: Settings) -> Iterator[Session]:
    """Migrate to head (cheap, idempotent) and yield a session that commits on success."""
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with session_scope(make_session_factory(engine)) as session:
            yield session
    finally:
        engine.dispose()


def _delegate(ctx: typer.Context, command: str, *args: Any, with_session: bool = True) -> Any:
    try:
        fn = resolve(command)
    except NotImplementedYet as exc:
        err.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(EXIT_NOT_IMPLEMENTED) from exc
    settings = _settings(ctx)
    if not with_session:
        return fn(settings, *args)
    with _db(settings) as session:
        return fn(session, settings, *args)


# --------------------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------------------

app = typer.Typer(no_args_is_help=True, add_completion=False, help="clipforge operator CLI.")
campaign_app = typer.Typer(no_args_is_help=True, help="Campaign registry.")
source_app = typer.Typer(no_args_is_help=True, help="Footage sources.")
report_app = typer.Typer(no_args_is_help=True, help="Reports.")
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(campaign_app, name="campaign")
app.add_typer(source_app, name="source")
app.add_typer(report_app, name="report")
app.add_typer(db_app, name="db")


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option(help="Config YAML (default: $CLIPFORGE_CONFIG).")] = None,
) -> None:
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_CONFIG) from exc
    setup_logging(settings.secrets.secret_values())
    ctx.obj = settings


# --- campaigns and sources (Phase 1 A) ---------------------------------------------------------------


@campaign_app.command("add")
def campaign_add(
    ctx: typer.Context,
    from_json: Annotated[Path, typer.Option("--from-json", exists=True, dir_okay=False, help="CampaignIn JSON.")],
) -> None:
    """Register a campaign from a CampaignIn JSON file."""
    _delegate(ctx, "campaign add", from_json)


@campaign_app.command("list")
def campaign_list(ctx: typer.Context) -> None:
    """List campaigns."""
    _delegate(ctx, "campaign list")


@campaign_app.command("drain")
def campaign_drain(ctx: typer.Context, campaign_id: int) -> None:
    """Mark a campaign drained (budget gone): no more downloads or posts."""
    _delegate(ctx, "campaign drain", campaign_id, CampaignStatus.DRAINED)


@campaign_app.command("end")
def campaign_end(ctx: typer.Context, campaign_id: int) -> None:
    """Mark a campaign ended."""
    _delegate(ctx, "campaign end", campaign_id, CampaignStatus.ENDED)


@campaign_app.command("check")
def campaign_check(ctx: typer.Context, campaign_id: int) -> None:
    """Apply the campaign filter in config (campaign_filter) to a registered campaign. Exits 1 if any check fails."""
    checks = _delegate(ctx, "campaign check", campaign_id)
    if checks and not all(c.passed for c in checks):
        raise typer.Exit(EXIT_CHECK_FAILED)


@source_app.command("add")
def source_add(ctx: typer.Context, campaign_id: int, url: str) -> None:
    """Register a footage URL (or folder link) on a campaign. Only registered URLs can be downloaded."""
    _delegate(ctx, "source add", campaign_id, url)


@source_app.command("retry")
def source_retry(ctx: typer.Context, source_id: int) -> None:
    """Put a failed source back in the queue; the next nightly run downloads it again."""
    _delegate(ctx, "source retry", source_id)


# --- long-running processes and reports (Phase 1 D / Phase 2) -----------------------------------------


@app.command("worker")
def worker(ctx: typer.Context) -> None:
    """Run the scheduler worker (nightly pipeline, slots, polling, reports)."""
    _delegate(ctx, "worker", with_session=False)


@app.command("bot")
def bot(ctx: typer.Context) -> None:
    """Run the Telegram review bot."""
    _delegate(ctx, "bot", with_session=False)


@app.command("run")
def run(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once", help="Run the nightly pipeline once and exit.")] = False,
) -> None:
    """Run the nightly pipeline."""
    if not once:
        err.print("Use `cf run --once` (the scheduled run happens inside `cf worker`).")
        raise typer.Exit(EXIT_CONFIG)
    report = _delegate(ctx, "run --once", with_session=False)
    summary = getattr(report, "summary", None)
    if callable(summary):
        console.print(summary())


@report_app.command("weekly")
def report_weekly(ctx: typer.Context) -> None:
    """Write the weekly digest to data/reports/ and print its path."""
    path = _delegate(ctx, "report weekly")
    if path is not None:
        typer.echo(str(path))


@app.command("export-reconcile")
def export_reconcile(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option(help="CSV to write.")] = Path("data/reconcile.csv"),
) -> None:
    """Export open submissions to CSV (schemas.ReconcileRow)."""
    _delegate(ctx, "export-reconcile", out)


@app.command("import-reconcile")
def import_reconcile(ctx: typer.Context, file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Import an edited reconcile CSV."""
    _delegate(ctx, "import-reconcile", file)


# --- implemented in Phase 0 ---------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply database migrations (also runs automatically before every DB command)."""
    settings = _settings(ctx)
    upgrade(settings.database_url)
    typer.echo(f"database at head: {settings.db_path}")


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Publish mode, pause state, heartbeats, spend and pipeline counts."""
    settings = _settings(ctx)
    with _db(settings) as session:
        mtd = costs.month_to_date(session, settings.app)
        paused = costs.is_paused(session)
        campaigns = dict(session.execute(select(CampaignRow.status, func.count()).group_by(CampaignRow.status)).all())
        clips = dict(session.execute(select(ClipRow.state, func.count()).group_by(ClipRow.state)).all())
        posts = dict(session.execute(select(PostRow.state, func.count()).group_by(PostRow.state)).all())

    table = Table(show_header=False, box=None)
    table.add_row("publish mode", str(settings.publish_mode))
    table.add_row("database", str(settings.db_path))
    table.add_row("pipeline", f"[red]paused: {paused}[/red]" if paused else "running")
    table.add_row("spend (month)", f"${mtd:.2f} of ${settings.app.monthly_api_cap_usd:.2f}")
    for name in ("worker", "bot"):
        age = heartbeat.age_s(settings.heartbeat_dir, name)
        table.add_row(f"{name} heartbeat", "never" if age is None else f"{age:.0f}s ago")
    table.add_row("campaigns", _counts(campaigns))
    table.add_row("clips", _counts(clips))
    table.add_row("posts", _counts(posts))
    console.print(table)


def _counts(counts: dict[Any, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))) or "none"


@app.command("costs")
def costs_cmd(ctx: typer.Context) -> None:
    """Month-to-date API spend by service and model, against the cap."""
    settings = _settings(ctx)
    with _db(settings) as session:
        start = costs.month_start_utc(utcnow(), settings.app.tz)
        rows = session.execute(
            select(CostRow.service, CostRow.model, func.sum(CostRow.usd))
            .where(CostRow.at >= start)
            .group_by(CostRow.service, CostRow.model)
            .order_by(CostRow.service, CostRow.model)
        ).all()
        mtd = costs.month_to_date(session, settings.app)
    table = Table("service", "model", "usd")
    for service, model, usd in rows:
        table.add_row(service, model, f"{usd:.4f}")
    console.print(table)
    console.print(f"month to date: ${mtd:.2f} of ${settings.app.monthly_api_cap_usd:.2f} cap")


@app.command("resume")
def resume(ctx: typer.Context) -> None:
    """Clear the spend-cap pause. If spend is still at the cap, raise monthly_api_cap_usd first."""
    settings = _settings(ctx)
    with _db(settings) as session:
        was_paused = costs.resume(session)
        mtd = costs.month_to_date(session, settings.app)
    if not was_paused:
        console.print("pipeline was not paused")
        return
    console.print("pipeline resumed")
    if mtd >= settings.app.monthly_api_cap_usd:
        err.print(
            f"[yellow]spend ${mtd:.2f} is still at/over the ${settings.app.monthly_api_cap_usd:.2f} cap; "
            "the next paid stage will pause again unless you raise monthly_api_cap_usd.[/yellow]"
        )


@app.command("backup")
def backup(ctx: typer.Context) -> None:
    """Back up the SQLite database (keeps retention.backups_keep copies)."""
    settings = _settings(ctx)
    upgrade(settings.database_url)
    path = backup_db(settings.db_path, settings.backups_dir, settings.app.retention.backups_keep)
    typer.echo(str(path))


if __name__ == "__main__":  # pragma: no cover
    app()
