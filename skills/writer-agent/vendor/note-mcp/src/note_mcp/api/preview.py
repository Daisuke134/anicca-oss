"""Preview API functions for note.com.

Provides functionality to get preview access tokens
and fetch preview page HTML.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from note_mcp.api.articles import build_preview_url, get_preview_access_token
from note_mcp.models import ErrorCode, NoteAPIError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from note_mcp.models import Session


# Re-export for convenience
__all__ = ["get_preview_access_token", "build_preview_url", "get_preview_html"]

# Common User-Agent string for API requests
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"

# Retry configuration for transient errors
MAX_TRANSIENT_RETRIES = 3  # Maximum retries for transient errors (502/503/504)
BASE_DELAY = 0.5  # Initial backoff delay in seconds
MAX_DELAY = 4.0  # Maximum backoff delay in seconds


async def get_preview_html(
    session: Session,
    article_key: str,
) -> str:
    """Fetch preview page HTML for an article.

    Gets preview access token via API and fetches the preview page HTML.
    Useful for E2E testing and content verification.

    Retry behavior:
    - Authentication errors (401/403): Retries once with a fresh token
    - Transient server errors (502/503/504): Retries with exponential backoff

    Args:
        session: Authenticated session
        article_key: Article key (e.g., "n1234567890ab")

    Returns:
        Preview page HTML as string

    Raises:
        NoteAPIError: If token fetch or HTML fetch fails after all retries
    """
    # Build cookie header
    cookie_parts = [f"{k}={v}" for k, v in session.cookies.items()]
    cookies_header = "; ".join(cookie_parts)

    # HTTP headers for requests
    headers = {
        "Cookie": cookies_header,
        "User-Agent": USER_AGENT,
    }

    # Auth error status codes that trigger token refresh retry
    auth_error_codes = {401, 403}

    # Transient server error codes that trigger backoff retry
    transient_error_codes = {502, 503, 504}

    last_response: httpx.Response | None = None
    auth_retry_used = False
    transient_retry_count = 0

    while True:
        # Get preview access token via API
        access_token = await get_preview_access_token(session, article_key)

        # Build preview URL
        preview_url = build_preview_url(article_key, access_token)

        # Fetch HTML via httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(
                preview_url,
                headers=headers,
                follow_redirects=True,
            )

        if response.is_success:
            return response.text

        last_response = response
        status_code = response.status_code

        # Handle auth errors: retry once with fresh token
        if status_code in auth_error_codes and not auth_retry_used:
            logger.warning(
                "Preview HTML fetch got auth error %d, retrying with fresh token",
                status_code,
            )
            auth_retry_used = True
            continue

        # Handle transient server errors: retry with exponential backoff
        if status_code in transient_error_codes and transient_retry_count < MAX_TRANSIENT_RETRIES:
            delay = min(BASE_DELAY * (2**transient_retry_count), MAX_DELAY)
            logger.warning(
                "Preview HTML fetch got transient error %d, retrying in %.1fs (%d/%d)",
                status_code,
                delay,
                transient_retry_count + 1,
                MAX_TRANSIENT_RETRIES,
            )
            await asyncio.sleep(delay)
            transient_retry_count += 1
            continue

        # No more retries available
        break

    # All attempts failed
    assert last_response is not None

    # Use NOT_AUTHENTICATED for 401 errors, API_ERROR for others
    error_code = ErrorCode.NOT_AUTHENTICATED if last_response.status_code == 401 else ErrorCode.API_ERROR

    raise NoteAPIError(
        code=error_code,
        message=f"Failed to fetch preview HTML. Status: {last_response.status_code}",
        details={
            "article_key": article_key,
            "status_code": last_response.status_code,
            "response_text": last_response.text[:500] if last_response.text else "(empty)",
            "auth_retry_used": auth_retry_used,
            "transient_retry_count": transient_retry_count,
        },
    )

