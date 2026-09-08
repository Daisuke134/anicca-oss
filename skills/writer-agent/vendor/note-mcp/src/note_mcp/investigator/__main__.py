"""Entry point for running investigator as a module.

Usage:
    uv run python -m note_mcp.investigator capture
    uv run python -m note_mcp.investigator analyze traffic.flow
"""

from note_mcp.investigator.cli import main

if __name__ == "__main__":
    main()

