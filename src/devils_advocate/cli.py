"""Click CLI definition for ``dvad``.

Commands: review, history, config, override.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import click
from rich.markdown import Markdown
from rich.table import Table

from devils_advocate import __version__
from .config import find_config, init_config, load_config, validate_config, get_models_by_role
from .orchestrator import run_plan_review, run_code_review, run_integration_review, run_spec_review
from .revision import run_revision
from .storage import StorageManager
from .types import (
    APIError,
    ConfigError,
    CostLimitError,
    CostTracker,
    Resolution,
    StorageError,
)
from .ui import console


# ─── CLI Group ────────────────────────────────────────────────────────────────


@click.group()
@click.version_option(version=__version__, prog_name="dvad")
def cli():
    """Devil's Advocate -- Cost-aware multi-LLM adversarial review engine."""


# ─── review ───────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--mode",
    type=click.Choice(["plan", "code", "integration", "spec"]),
    required=True,
    help="Review mode: plan, code, integration, or spec",
)
@click.option(
    "--input",
    "input_path",
    multiple=True,
    help="Input file(s) to review",
)
@click.option(
    "--spec",
    "spec_path",
    default=None,
    help="Specification file (for code or integration review mode)",
)
@click.option(
    "--project",
    required=True,
    help="Project name/identifier",
)
@click.option(
    "--max-cost",
    type=float,
    default=None,
    help="Maximum cost in USD -- abort if estimated cost exceeds this",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show planned API calls without executing them",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to models.yaml",
)
@click.option(
    "--project-dir",
    "project_dir",
    default=None,
    help="Project directory (for integration mode spec discovery)",
)
def review(mode, input_path, spec_path, project, max_cost, dry_run, config_path, project_dir):
    """Run a review."""
    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    issues = validate_config(config)
    errors = [msg for level, msg in issues if level == "error"]
    warnings = [msg for level, msg in issues if level == "warn"]

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    if errors:
        for e in errors:
            console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if mode in ("plan", "code", "spec"):
        if not input_path:
            console.print(
                f"[red]Error:[/red] --input is required for {mode} reviews."
            )
            sys.exit(1)
        for p in input_path:
            if not Path(p).exists():
                console.print(f"[red]Error:[/red] Input file not found: {p}")
                sys.exit(1)

    # Build the coroutine
    if mode == "plan":
        input_files = [Path(p) for p in input_path]
        main_coro = run_plan_review(config, input_files, project, max_cost, dry_run)
    elif mode == "code":
        spec_file = Path(spec_path) if spec_path else None
        if spec_file and not spec_file.exists():
            console.print(f"[red]Error:[/red] Spec file not found: {spec_file}")
            sys.exit(1)
        main_coro = run_code_review(
            config, Path(input_path[0]), project, spec_file, max_cost, dry_run
        )
    elif mode == "integration":
        input_files_list = list(input_path) if input_path else None
        spec_file = Path(spec_path) if spec_path else None
        proj_dir = Path(project_dir) if project_dir else None
        main_coro = run_integration_review(
            config,
            project,
            input_files=input_files_list,
            spec_file=spec_file,
            project_dir=proj_dir,
            max_cost=max_cost,
            dry_run=dry_run,
        )
    elif mode == "spec":
        input_files = [Path(p) for p in input_path]
        main_coro = run_spec_review(config, input_files, project, max_cost, dry_run)
    else:
        sys.exit(1)

    # Signal handling (Bug 9 fix): install handlers around asyncio.run()
    # Use loop.add_signal_handler on POSIX; fall back to signal.signal elsewhere.
    storage = StorageManager(Path.cwd())

    def _cleanup():
        try:
            storage.release_lock()
        except Exception:
            pass
        try:
            storage.close()
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # POSIX signal handler for SIGTERM
        try:
            loop.add_signal_handler(signal.SIGTERM, _cleanup)
        except NotImplementedError:
            # Fallback for platforms where add_signal_handler is unavailable
            # (e.g., Windows). Note: signal.signal handlers may not interact
            # cleanly with asyncio on all platforms.
            signal.signal(signal.SIGTERM, lambda s, f: _cleanup())

        # SIGINT is handled by KeyboardInterrupt propagation
        loop.run_until_complete(main_coro)
    except KeyboardInterrupt:
        _cleanup()
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except (APIError, CostLimitError) as e:
        _cleanup()
        console.print(f"\n[red]Aborted:[/red] {e}")
        sys.exit(1)
    finally:
        loop.close()


# ─── history ──────────────────────────────────────────────────────────────────


@cli.command("history")
@click.option("--project", required=True, help="Project name")
@click.option(
    "--review-id",
    default=None,
    help="Show details for a specific review",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to models.yaml",
)
@click.option(
    "--project-dir",
    "project_dir",
    default=None,
    help="Project directory to search for reviews",
)
def history(project, review_id, config_path, project_dir):
    """Show review history for a project."""
    base_dir = Path(project_dir) if project_dir else Path.cwd()
    storage = StorageManager(base_dir)

    if review_id:
        data = storage.load_review(review_id)
        if not data:
            console.print(f"[red]Error:[/red] Review {review_id} not found.")
            sys.exit(1)
        # Print the report
        report_path = storage.reviews_dir / review_id / "dvad-report.md"
        if report_path.exists():
            console.print(Markdown(report_path.read_text()))
        else:
            console.print_json(json.dumps(data, indent=2))
        return

    reviews = storage.list_reviews()
    if not reviews:
        console.print("No reviews found for this project.")
        return

    table = Table(title=f"Review History -- {project}")
    table.add_column("Review ID", style="cyan", no_wrap=True)
    table.add_column("Mode")
    table.add_column("Input")
    table.add_column("Date")
    table.add_column("Points", justify="right")
    table.add_column("Cost", justify="right")

    for r in reviews:
        table.add_row(
            r["review_id"],
            r["mode"],
            str(r["input_file"])[:40],
            str(r["timestamp"])[:19],
            str(r["total_points"]),
            f"${r['total_cost']:.4f}",
        )
    console.print(table)


# ─── config ───────────────────────────────────────────────────────────────────


@cli.command("config")
@click.option("--show", is_flag=True, help="Show current configuration")
@click.option("--init", "do_init", is_flag=True, help="Create example config directory")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to models.yaml",
)
def config_cmd(show, do_init, config_path):
    """Show, validate, or initialize configuration."""
    if do_init:
        status, path = init_config()
        if status == "exists":
            console.print(
                f"[yellow]Config already exists:[/yellow] {path}\n"
                "  Edit it directly or delete it to regenerate."
            )
        else:
            console.print(f"[green]Config created:[/green] {path}")
            console.print("  Edit the file to configure your models and API keys.")
            env_example = path.parent / ".env.example"
            if env_example.exists():
                console.print(f"  Rename [bold]{env_example}[/bold] to .env and add your API keys.")
        return

    if not show:
        console.print("Use --show to display configuration or --init to create example config.")
        return

    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    console.print(f"[bold]Config file:[/bold] {config['config_path']}")
    console.print()

    table = Table(title="Configured Models")
    table.add_column("Name", style="cyan")
    table.add_column("Provider")
    table.add_column("Model ID")
    table.add_column("Role")
    table.add_column("Flags")
    table.add_column("Context Window", justify="right")
    table.add_column("Timeout", justify="right")
    table.add_column("API Key", style="green")

    display_models = config.get("all_models", config["models"])
    for name, m in display_models.items():
        flags = []
        if m.deduplication:
            flags.append("dedup")
        if m.integration_reviewer:
            flags.append("integration")
        active = name in config["models"]
        key_status = "set" if m.api_key else "[red]MISSING[/red]"
        if not active:
            key_status = "[dim]--[/dim]"
        ctx_str = (
            f"{m.context_window:,}" if m.context_window else "[dim]unset[/dim]"
        )
        style = None if active else "dim"
        table.add_row(
            name,
            m.provider,
            m.model_id,
            ", ".join(sorted(m.roles)) or "[dim]--[/dim]",
            ", ".join(flags) or "[dim]--[/dim]",
            ctx_str,
            f"{m.timeout}s",
            key_status,
            style=style,
        )
    console.print(table)

    # Validate
    issues = validate_config(config)
    if issues:
        console.print()
        for level, msg in issues:
            tag = "[red]ERROR[/red]" if level == "error" else "[yellow]WARN[/yellow]"
            console.print(f"  {tag}: {msg}")
    else:
        console.print("\n[green]Configuration is valid.[/green]")


# ─── override ─────────────────────────────────────────────────────────────────


@cli.command("override")
@click.option("--project", required=True, help="Project name")
@click.option(
    "--review",
    "review_id",
    required=True,
    help="Review ID",
)
@click.option(
    "--point",
    "point_id",
    required=True,
    help="Point or group ID to override",
)
@click.option(
    "--resolution",
    type=click.Choice(["uphold", "dismiss", "escalate"]),
    required=True,
    help=(
        "uphold: reviewer's finding is valid. "
        "dismiss: author's position holds. "
        "escalate: keep flagged for human review."
    ),
)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to models.yaml",
)
@click.option(
    "--project-dir",
    "project_dir",
    default=None,
    help="Project directory containing .dvad/",
)
def override(project, review_id, point_id, resolution, config_path, project_dir):
    """Resolve an escalated governance decision.

    \b
    uphold  -- The reviewer was right. The finding is valid and should be addressed.
    dismiss -- The author was right. The finding does not warrant action.
    escalate -- Keep flagged for human review (no change).
    """
    resolution_map = {
        "uphold": Resolution.OVERRIDDEN.value,
        "dismiss": Resolution.AUTO_DISMISSED.value,
        "escalate": Resolution.ESCALATED.value,
    }
    base_dir = Path(project_dir) if project_dir else Path.cwd()
    storage = StorageManager(base_dir)
    try:
        storage.update_point_override(
            review_id, point_id, resolution_map[resolution]
        )
        console.print(f"[green]Override applied:[/green] {point_id} -> {resolution}")
        console.print(
            f"  Updated: {storage.reviews_dir / review_id / 'review-ledger.json'}"
        )
    except StorageError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ─── revise ──────────────────────────────────────────────────────────────────


@cli.command("revise")
@click.option("--project", required=True, help="Project name")
@click.option(
    "--review",
    "review_id",
    required=True,
    help="Review ID to revise from",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to models.yaml",
)
@click.option(
    "--project-dir",
    "project_dir",
    default=None,
    help="Project directory containing .dvad/",
)
@click.option(
    "--max-cost",
    type=float,
    default=None,
    help="Maximum cost in USD for this revision (independent budget)",
)
@click.option(
    "--input",
    "input_override",
    default=None,
    help="Override: path to artifact file (default: uses saved original_content.txt)",
)
def revise(project, review_id, config_path, project_dir, max_cost, input_override):
    """Generate a revised artifact from a completed review.

    Uses the governance outcomes from the specified review to produce a
    revised plan, diff, or remediation plan. Run this after ``dvad override``
    to incorporate manual overrides into the revised artifact.
    """
    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    roles = get_models_by_role(config)
    revision_model = roles["revision"]

    base_dir = Path(project_dir) if project_dir else Path.cwd()
    storage = StorageManager(base_dir)

    # Load ledger
    ledger_data = storage.load_review(review_id)
    if not ledger_data:
        console.print(f"[red]Error:[/red] Review {review_id} not found.")
        sys.exit(1)

    mode = ledger_data.get("mode", "plan")
    rd = storage.reviews_dir / review_id

    # Load original content
    if input_override:
        input_path = Path(input_override)
        if not input_path.exists():
            console.print(f"[red]Error:[/red] Input file not found: {input_path}")
            sys.exit(1)
        original_content = input_path.read_text()
    else:
        oc_path = rd / "original_content.txt"
        if not oc_path.exists():
            console.print(
                f"[red]Error:[/red] original_content.txt not found in {rd}. "
                "Use --input to specify the artifact file."
            )
            sys.exit(1)
        original_content = oc_path.read_text()

    # Output filename per mode
    output_names = {
        "plan": "revised-plan.md",
        "code": "revised-diff.patch",
        "integration": "remediation-plan.md",
    }
    output_name = output_names.get(mode, "revised-plan.md")

    cost_tracker = CostTracker(max_cost=max_cost)

    async def _run():
        import httpx
        async with httpx.AsyncClient() as client:
            return await run_revision(
                client,
                revision_model,
                original_content,
                ledger_data,
                mode=mode,
                cost_tracker=cost_tracker,
                storage=storage,
                review_id=review_id,
            )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        revised = loop.run_until_complete(_run())
        if revised:
            storage._atomic_write(rd / output_name, revised)
            console.print(f"[green]Revision complete.[/green]")
            console.print(f"  Output: {rd / output_name}")
            console.print(f"  Cost: ${cost_tracker.total_usd:.4f}")
        else:
            console.print("[yellow]No revised artifact produced.[/yellow]")
    except (APIError, CostLimitError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[yellow]Warning: Revision failed: {e}[/yellow]")
        storage.log(f"Revision command failed (non-fatal): {e}")
    finally:
        loop.close()


# ─── gui ──────────────────────────────────────────────────────────────────


@cli.command("gui")
@click.option("--port", default=8411, help="Port to listen on")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--config", "config_path", default=None, help="Path to models.yaml")
@click.option(
    "--allow-nonlocal",
    is_flag=True,
    default=False,
    help="Allow binding to non-localhost interfaces (unsafe; requires token header for POST).",
)
def gui_cmd(port, host, config_path, allow_nonlocal):
    """Launch the Devil's Advocate web GUI."""
    from .gui import create_app

    # Preflight port bind for clean UX
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
    except OSError:
        console.print(f"[red]Error:[/red] Port {port} is already in use.")
        console.print(f"  Try: dvad gui --port {port + 1}")
        sys.exit(1)
    finally:
        try:
            s.close()
        except Exception:
            pass

    if host != "127.0.0.1" and not allow_nonlocal:
        console.print("[red]Refusing to bind to non-localhost without --allow-nonlocal.[/red]")
        console.print("  This GUI has mutating endpoints and is intended for local use only.")
        sys.exit(1)

    if host != "127.0.0.1":
        console.print("[yellow]Warning:[/yellow] Binding to a non-local interface exposes review/config endpoints.")
        console.print("  This tool is intended for local-only use. Ensure you understand the risks.")

    import uvicorn
    app = create_app(config_path=config_path)

    # Check for first-run / config issues
    try:
        from .config import load_config, get_config_health
        config = load_config(Path(config_path) if config_path else None)
        has_errors, error_summary = get_config_health(config)
        if has_errors:
            console.print(
                f"[yellow]Setup incomplete[/yellow] — {error_summary}. "
                f"Open http://{host}:{port}/config to configure models and API keys."
            )
    except FileNotFoundError:
        console.print(
            f"[yellow]First run detected[/yellow] — open http://{host}:{port}/config to set up your models."
        )
    except Exception as exc:
        console.print(
            f"[yellow]Configuration error[/yellow] — {exc}. Open http://{host}:{port}/config to fix."
        )

    uvicorn.run(app, host=host, port=port, log_level="warning")


# ─── install ─────────────────────────────────────────────────────────────


@cli.command("install")
@click.option("--port", default=8411, help="Port for the GUI service")
@click.option("--no-start", is_flag=True, default=False, help="Enable but don't start the service")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing service file without prompting")
def install_cmd(port, no_start, force):
    """Install the dvad GUI as a systemd user service."""
    from .service import (
        check_platform,
        detect_dvad_binary,
        read_existing_service,
        render_service_unit,
        service_exists,
        systemctl_daemon_reload,
        systemctl_enable,
        systemctl_start,
        write_service_file,
    )

    # 1. Platform check
    err = check_platform()
    if err:
        console.print(f"[red]Error:[/red] {err}")
        sys.exit(1)

    # 2. Binary detection
    try:
        dvad_bin = detect_dvad_binary()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # 4. Config init
    status, config_path = init_config()
    if status == "created":
        console.print(f"[green]Config created:[/green] {config_path}")
        console.print("  Edit the file to add your API keys before using reviews.")
    else:
        console.print(f"Config: {config_path}")

    # 5. Existing service check
    if service_exists() and not force:
        existing = read_existing_service()
        new_content = render_service_unit(dvad_bin, port)
        if existing == new_content:
            console.print("[green]Service already installed[/green] with identical configuration.")
            sys.exit(0)
        if not click.confirm("Service file already exists with different content. Overwrite?"):
            console.print("Aborted.")
            sys.exit(0)

    # 6. Write service file
    content = render_service_unit(dvad_bin, port)
    path = write_service_file(content)
    console.print(f"Service file written: {path}")

    # 7. systemctl operations
    try:
        systemctl_daemon_reload()
        systemctl_enable()
        if not no_start:
            systemctl_start()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # 8. Success
    if no_start:
        console.print("[green]Service installed and enabled.[/green] Start manually with:")
        console.print(f"  systemctl --user start dvad-gui.service")
    else:
        console.print(f"[green]Service installed, enabled, and started.[/green]")
    console.print(f"  GUI: http://localhost:{port}")


# ─── uninstall ───────────────────────────────────────────────────────────


@cli.command("uninstall")
def uninstall_cmd():
    """Uninstall the dvad GUI systemd user service."""
    from .service import (
        check_platform,
        remove_service_file,
        service_exists,
        systemctl_daemon_reload,
        systemctl_disable,
        systemctl_is_active,
        systemctl_is_enabled,
        systemctl_stop,
    )

    # 1. Platform check
    err = check_platform()
    if err:
        console.print(f"[red]Error:[/red] {err}")
        sys.exit(1)

    # 2. Service exists check
    if not service_exists():
        console.print("Service is not installed. Nothing to do.")
        sys.exit(0)

    # 3. Stop
    try:
        if systemctl_is_active():
            systemctl_stop()
            console.print("Service stopped.")
        else:
            console.print("Service already stopped.")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] Failed to stop service: {e}")
        sys.exit(1)

    # 4. Disable
    try:
        if systemctl_is_enabled():
            systemctl_disable()
            console.print("Service disabled.")
        else:
            console.print("Service already disabled.")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] Failed to disable service: {e}")
        sys.exit(1)

    # 5. Remove service file
    remove_service_file()
    console.print("Service file removed.")

    # 6. daemon-reload
    try:
        systemctl_daemon_reload()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] Failed to reload daemon: {e}")
        sys.exit(1)

    # 7. Success
    console.print("[green]Service uninstalled.[/green]")
    console.print("  Config at ~/.config/devils-advocate/ was preserved.")
