"""CLI interface for HTTP traffic investigation.

Provides commands for capturing and analyzing HTTP traffic
to investigate note.com API behavior.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="investigator",
    help="note.com API investigation tool - capture and analyze HTTP traffic",
)


@app.command()
def capture(
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Output file for captured traffic (.flow format)",
        ),
    ] = Path("traffic.flow"),
    url: Annotated[
        str,
        typer.Option(
            "--url",
            "-u",
            help="Initial URL to navigate to",
        ),
    ] = "https://note.com",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Proxy server port",
        ),
    ] = 8080,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            "-d",
            help="Filter traffic by domain (e.g., 'api.note.com')",
        ),
    ] = None,
    no_session: Annotated[
        bool,
        typer.Option(
            "--no-session",
            help="Don't restore saved session (require fresh login)",
        ),
    ] = False,
) -> None:
    """Start interactive traffic capture session.

    Opens a browser with proxy configured to capture all HTTP traffic.
    By default, restores saved session cookies from note_login.
    Perform your investigation in the browser, then close it to save the capture.

    Example:
        uv run python -m note_mcp.investigator capture --domain api.note.com
    """
    from note_mcp.investigator.core import run_capture_session

    typer.echo("🔍 Starting traffic capture...")
    typer.echo(f"   Output: {output}")
    typer.echo(f"   Proxy port: {port}")
    if domain:
        typer.echo(f"   Domain filter: {domain}")
    typer.echo(f"   Session restore: {'disabled' if no_session else 'enabled'}")
    typer.echo()
    typer.echo("📋 Instructions:")
    typer.echo("   1. Browser will open with proxy configured")
    if not no_session:
        typer.echo("   2. If logged in, session will be restored automatically")
    typer.echo("   3. If auto-navigation fails, manually enter the URL")
    typer.echo("   4. Perform the actions you want to investigate")
    typer.echo("   5. Close the browser when done")
    typer.echo()

    try:
        asyncio.run(
            run_capture_session(
                output=output,
                initial_url=url,
                proxy_port=port,
                domain_filter=domain,
                restore_session=not no_session,
            )
        )
        typer.echo(f"✅ Capture saved to: {output}")
        typer.echo()
        typer.echo("Next steps:")
        typer.echo(f"  uv run python -m note_mcp.investigator analyze {output}")

    except RuntimeError as e:
        typer.echo(f"❌ Error: {e}", err=True)
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        typer.echo("\n⚠️  Capture interrupted")
        raise typer.Exit(0) from None


@app.command()
def analyze(
    file: Annotated[
        Path,
        typer.Argument(
            help="Traffic capture file to analyze (.flow format)",
        ),
    ],
    pattern: Annotated[
        str | None,
        typer.Option(
            "--pattern",
            "-p",
            help="Regex pattern to search in requests/responses",
        ),
    ] = None,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            "-d",
            help="Filter by domain",
        ),
    ] = None,
    method: Annotated[
        str | None,
        typer.Option(
            "--method",
            "-m",
            help="Filter by HTTP method (GET, POST, PUT, etc.)",
        ),
    ] = None,
    show_body: Annotated[
        bool,
        typer.Option(
            "--body",
            "-b",
            help="Show request/response bodies",
        ),
    ] = False,
) -> None:
    """Analyze captured traffic file.

    Reads a .flow file captured by mitmproxy and displays matching requests.

    Example:
        uv run python -m note_mcp.investigator analyze traffic.flow --pattern citation
    """
    if not file.exists():
        typer.echo(f"❌ File not found: {file}", err=True)
        raise typer.Exit(1)

    typer.echo(f"📊 Analyzing: {file}")
    if pattern:
        typer.echo(f"   Pattern: {pattern}")
    if domain:
        typer.echo(f"   Domain: {domain}")
    if method:
        typer.echo(f"   Method: {method}")
    typer.echo()

    # Build mitmdump filter
    filters: list[str] = []
    if domain:
        filters.append(f"~d {domain}")
    if method:
        filters.append(f"~m {method.upper()}")

    # Use mitmdump to read and display the flow
    cmd = ["mitmdump", "-n", "-r", str(file)]
    if filters:
        cmd.extend(["--set", f"flow_filter={'&'.join(filters)}"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        output_lines = result.stdout.strip().split("\n") if result.stdout else []

        if pattern:
            # Filter lines by pattern
            regex = re.compile(pattern, re.IGNORECASE)
            matched_lines: list[str] = []
            for line in output_lines:
                if regex.search(line):
                    matched_lines.append(line)
            output_lines = matched_lines

        if not output_lines or (len(output_lines) == 1 and not output_lines[0]):
            typer.echo("No matching requests found.")
            return

        typer.echo(f"Found {len(output_lines)} matching request(s):\n")
        for line in output_lines:
            typer.echo(line)

        if result.stderr:
            typer.echo(f"\n⚠️  Warnings: {result.stderr}", err=True)

    except FileNotFoundError:
        typer.echo("❌ mitmdump not found. Install mitmproxy first.", err=True)
        raise typer.Exit(1) from None


@app.command()
def export(
    file: Annotated[
        Path,
        typer.Argument(
            help="Traffic capture file to export (.flow format)",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Output file (JSON format)",
        ),
    ] = Path("traffic.json"),
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            "-d",
            help="Filter by domain",
        ),
    ] = None,
) -> None:
    """Export captured traffic to JSON format.

    Converts mitmproxy .flow format to readable JSON for further analysis.

    Example:
        uv run python -m note_mcp.investigator export traffic.flow -o api_calls.json
    """
    if not file.exists():
        typer.echo(f"❌ File not found: {file}", err=True)
        raise typer.Exit(1)

    typer.echo(f"📤 Exporting: {file} -> {output}")

    # Use mitmproxy Python API to read flows
    try:
        from mitmproxy import io as mio
        from mitmproxy.http import HTTPFlow
    except ImportError:
        typer.echo("❌ mitmproxy not installed. Run: uv sync --group dev", err=True)
        raise typer.Exit(1) from None

    import json

    flows_data: list[dict[str, object]] = []

    with open(file, "rb") as f:
        reader = mio.FlowReader(f)
        for flow in reader.stream():
            if not isinstance(flow, HTTPFlow):
                continue

            # Apply domain filter
            if domain and domain not in flow.request.host:
                continue

            flow_data: dict[str, object] = {
                "request": {
                    "method": flow.request.method,
                    "url": flow.request.url,
                    "headers": dict(flow.request.headers),
                    "body": flow.request.get_text(strict=False),
                },
            }

            if flow.response:
                flow_data["response"] = {
                    "status_code": flow.response.status_code,
                    "headers": dict(flow.response.headers),
                    "body": flow.response.get_text(strict=False),
                }

            flows_data.append(flow_data)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(flows_data, f, indent=2, ensure_ascii=False)

    typer.echo(f"✅ Exported {len(flows_data)} request(s) to: {output}")


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()

