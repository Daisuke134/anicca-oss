"""HTTP traffic investigation module for note.com API analysis.

This module provides tools for capturing and analyzing HTTP traffic
to understand note.com API behavior.

Usage:
    # Interactive capture
    uv run python -m note_mcp.investigator capture

    # Analyze captured traffic
    uv run python -m note_mcp.investigator analyze traffic.flow

    # Export to JSON
    uv run python -m note_mcp.investigator export traffic.flow
"""

from note_mcp.investigator.core import (
    CapturedRequest,
    CaptureSession,
    CaptureSessionManager,
    ProxyManager,
    run_capture_session,
)
from note_mcp.investigator.mcp_tools import register_investigator_tools

__all__ = [
    "CapturedRequest",
    "CaptureSession",
    "CaptureSessionManager",
    "ProxyManager",
    "register_investigator_tools",
    "run_capture_session",
]

