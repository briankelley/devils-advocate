"""CLI entry point — stub for Phase 0 verification."""

import click

from devils_advocate import __version__


@click.group()
@click.version_option(version=__version__, prog_name="dvad")
def cli():
    """Devil's Advocate — Cost-aware multi-LLM adversarial review engine."""


@cli.command()
def review():
    """Run a review (not yet implemented)."""
    click.echo("Not yet implemented.")
