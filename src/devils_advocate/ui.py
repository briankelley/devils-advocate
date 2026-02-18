"""Rich console singleton for the entire package."""

from rich.console import Console

console = Console()


def print_panel(title: str, content: str, style: str = "blue") -> None:
    from rich.panel import Panel
    console.print(Panel(content, title=title, style=style))
