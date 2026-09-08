"""Browser-based article preview using API access token.

Provides functionality to show article preview in browser via API.
This approach is faster and more stable than the editor-based approach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from note_mcp.api.articles import build_preview_url, get_preview_access_token
from note_mcp.browser.manager import BrowserManager

if TYPE_CHECKING:
    from note_mcp.models import Session


async def show_preview(
    session: Session,
    article_key: str,
) -> None:
    """Show article preview in browser via API.

    Gets preview access token via API and navigates directly
    to preview URL. Faster and more stable than editor-based approach.

    Args:
        session: Authenticated session with username
        article_key: Article key (e.g., "n1234567890ab")

    Raises:
        NoteAPIError: If token fetch fails
        RuntimeError: If browser navigation fails
    """
    # Get preview access token via API
    access_token = await get_preview_access_token(session, article_key)

    # Build preview URL
    preview_url = build_preview_url(article_key, access_token)

    # Get browser page
    manager = BrowserManager.get_instance()
    page = await manager.get_page()

    # Inject session cookies into browser context
    playwright_cookies: list[dict[str, Any]] = []
    for name, value in session.cookies.items():
        playwright_cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".note.com",
                "path": "/",
            }
        )
    await page.context.add_cookies(playwright_cookies)  # type: ignore[arg-type]

    # Navigate directly to preview URL (no editor involved)
    try:
        await page.goto(preview_url, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Preview page load timed out: {preview_url}") from e
    except PlaywrightError as e:
        raise RuntimeError(f"Browser navigation failed: {e}") from e

