"""Browser module for note-mcp.

Provides Playwright-based browser automation for login and preview.
"""

from note_mcp.browser.config import HEADLESS_ENV_VAR, get_headless_mode
from note_mcp.browser.manager import BrowserManager
from note_mcp.browser.preview import show_preview

__all__ = ["BrowserManager", "show_preview", "get_headless_mode", "HEADLESS_ENV_VAR"]

