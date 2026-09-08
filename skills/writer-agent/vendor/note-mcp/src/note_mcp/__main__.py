"""Entry point for note-mcp MCP server.

Run with: uv run python -m note_mcp
Or via fastmcp: uv run fastmcp run note_mcp.server:mcp

Investigator mode (stdio):
    uv run python -m note_mcp --investigator
    Or set INVESTIGATOR_MODE=1 environment variable

Investigator mode (HTTP for remote access):
    uv run python -m note_mcp --investigator --http --port 9000
    This exposes MCP tools via HTTP transport for Claude Code on host.
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="note-mcp MCP server")
    parser.add_argument(
        "--investigator",
        action="store_true",
        help="Enable investigator mode for API investigation",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP transport instead of stdio (for remote access)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind HTTP server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port for HTTP transport (default: 9000)",
    )
    args = parser.parse_args()

    if args.investigator:
        os.environ["INVESTIGATOR_MODE"] = "1"

    # Import server after setting environment variable
    from note_mcp.server import mcp

    if args.http:
        # stateless_http=True to avoid session ID management issues
        # when the server is restarted (e.g., container restart).
        # Application-level session state (CaptureSessionManager) is
        # independent of HTTP transport session management.
        mcp.run(transport="http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

