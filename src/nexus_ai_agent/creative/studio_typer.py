"""Typer wiring for ``nexus studio init | doctor | serve``.

Kept separate from :mod:`nexus_ai_agent.creative.studio_cli` so the operations
stay importable and testable without the CLI framework. ``serve`` starts the
existing dashboard application; it does not yet expose artifact downloads.
"""

from __future__ import annotations

from pathlib import Path

import typer

from nexus_ai_agent.creative.studio_cli import StudioCliError, studio_doctor, studio_init

studio_app = typer.Typer(help="NAGAR One local studio: init, doctor, serve")

_WORKSPACE_OPTION = typer.Option(
    "./studio-workspace",
    "--workspace",
    help="Local studio workspace directory.",
)


@studio_app.command("init")
def init_command(workspace: str = _WORKSPACE_OPTION) -> None:
    """Create the local studio directories (projects, assets, artifacts, queue)."""
    try:
        created = studio_init(Path(workspace))
    except StudioCliError as exc:
        typer.echo(f"✗ studio init refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if created:
        typer.echo(f"✓ created {len(created)} director{'y' if len(created) == 1 else 'ies'}")
    else:
        typer.echo("✓ workspace already initialized")


@studio_app.command("doctor")
def doctor_command(workspace: str = _WORKSPACE_OPTION) -> None:
    """Check ffmpeg, ffprobe, atomic writes, the queue database and the compiler."""
    report = studio_doctor(Path(workspace))
    for check in report.checks:
        mark = "✓" if check.ok else "✗"
        typer.echo(f"{mark} {check.name}: {check.detail}")
    if not report.healthy:
        typer.echo("✗ studio doctor: unhealthy", err=True)
    raise typer.Exit(code=report.exit_code)


@studio_app.command("serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """Start the local studio web server (dashboard application)."""
    import os

    import uvicorn

    os.environ["NEXUS_STUDIO_WORKSPACE"] = str(Path(workspace).resolve())
    uvicorn.run("nexus_ai_agent.api.app:app", host=host, port=port)
