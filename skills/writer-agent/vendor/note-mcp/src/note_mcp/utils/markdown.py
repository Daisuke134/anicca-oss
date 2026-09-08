"""Backward compatibility alias for markdown modules.

This module re-exports functions for backward compatibility.
New code should import directly from:
- note_mcp.utils.markdown_to_html
- note_mcp.utils.html_to_markdown
"""

from note_mcp.utils.html_to_markdown import html_to_markdown
from note_mcp.utils.markdown_to_html import markdown_to_html

__all__ = ["html_to_markdown", "markdown_to_html"]

