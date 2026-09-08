"""Article operations for note.com API.

Provides functions for creating, updating, and managing articles.
"""

from __future__ import annotations

import html
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from note_mcp.api.client import NoteAPIClient
from note_mcp.api.embeds import resolve_embed_keys
from note_mcp.api.images import _resolve_numeric_note_id
from note_mcp.models import (
    Article,
    ArticleInput,
    ArticleListResult,
    ArticleStatus,
    BulkDeletePreview,
    BulkDeleteResult,
    DeletePreview,
    DeleteResult,
    ErrorCode,
    NoteAPIError,
    Session,
    from_api_response,
)
from note_mcp.utils import markdown_to_html

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# =============================================================================
# Issue #174: Generic API Execution Helper Functions
# =============================================================================


async def _execute_get[T](
    session: Session,
    endpoint: str,
    response_parser: Callable[[dict[str, Any]], T],
    *,
    params: dict[str, Any] | None = None,
) -> T:
    """Execute GET request and parse response.

    Common pattern for API operations that:
    1. Open NoteAPIClient context
    2. Execute GET request
    3. Parse response with provided parser

    Args:
        session: Authenticated session
        endpoint: API endpoint path
        response_parser: Function to parse response dict into result type
        params: Optional query parameters

    Returns:
        Parsed result of type T
    """
    async with NoteAPIClient(session) as client:
        response = await client.get(endpoint, params=params)
    return response_parser(response)


async def _execute_post[T](
    session: Session,
    endpoint: str,
    response_parser: Callable[[dict[str, Any]], T],
    *,
    payload: dict[str, Any] | None = None,
) -> T:
    """Execute POST request and parse response.

    Common pattern for API operations that:
    1. Open NoteAPIClient context
    2. Execute POST request with payload
    3. Parse response with provided parser

    Args:
        session: Authenticated session
        endpoint: API endpoint path
        response_parser: Function to parse response dict into result type
        payload: JSON payload for request (optional, defaults to None)

    Returns:
        Parsed result of type T

    Raises:
        NoteAPIError: If API request fails (401, 403, 404, 429, 5xx)
    """
    async with NoteAPIClient(session) as client:
        response = await client.post(endpoint, json=payload)
    return response_parser(response)


async def _execute_delete(
    session: Session,
    endpoint: str,
) -> None:
    """Execute DELETE request.

    Common pattern for API delete operations that:
    1. Open NoteAPIClient context
    2. Execute DELETE request

    Args:
        session: Authenticated session
        endpoint: API endpoint path

    Returns:
        None

    Raises:
        NoteAPIError: If API request fails (401, 403, 404, 429, 5xx)
    """
    async with NoteAPIClient(session) as client:
        await client.delete(endpoint)


# =============================================================================
# Issue #114: API-only Image Insertion Helper Functions
# =============================================================================

# Default image dimensions used by note.com's editor
NOTE_DEFAULT_IMAGE_WIDTH: int = 620
NOTE_DEFAULT_IMAGE_HEIGHT: int = 457

# =============================================================================
# Issue #141: Delete Draft Constants
# =============================================================================

# Maximum pages to fetch when listing all drafts (safety limit for pagination)
# 1 page = ~10 articles, so 100 pages = ~1000 articles
DELETE_ALL_DRAFTS_MAX_PAGES: int = 100

# Number of articles to show in preview when confirm=False
DELETE_ALL_DRAFTS_PREVIEW_LIMIT: int = 10


def generate_image_html(
    image_url: str,
    caption: str = "",
    width: int = NOTE_DEFAULT_IMAGE_WIDTH,
    height: int = NOTE_DEFAULT_IMAGE_HEIGHT,
) -> str:
    """Generate note.com figure HTML for an image.

    Creates HTML in the format expected by note.com's editor.
    The default dimensions (620x457) match note.com's standard image size.

    Args:
        image_url: CDN URL of the uploaded image
        caption: Optional caption text (default: empty)
        width: Image width in pixels (default: 620)
        height: Image height in pixels (default: 457)

    Returns:
        HTML string: <figure name="..." id="..."><img ...><figcaption>...</figcaption></figure>
    """
    element_id = str(uuid.uuid4())
    # Escape caption and URL to prevent XSS attacks
    escaped_caption = html.escape(caption)
    escaped_url = html.escape(image_url)
    return (
        f'<figure name="{element_id}" id="{element_id}">'
        f'<img src="{escaped_url}" alt="" width="{width}" height="{height}" '
        f'contenteditable="false" draggable="false">'
        f"<figcaption>{escaped_caption}</figcaption></figure>"
    )


def append_image_to_body(existing_body: str, image_html: str) -> str:
    """Append image HTML to article body.

    Simply appends the image HTML to the end of the existing body.
    Use this when inserting images via API without browser automation.

    Args:
        existing_body: Current HTML body of the article
        image_html: Generated figure HTML to append

    Returns:
        Updated HTML body with image appended at the end
    """
    return existing_body + image_html


async def get_article_raw_html(
    session: Session,
    article_id: str,
) -> Article:
    """Get article with raw HTML body (no conversion to Markdown).

    Unlike get_article(), this returns the HTML body as-is without
    converting to Markdown. Use this when you need to manipulate
    the HTML content directly (e.g., appending image HTML).

    Args:
        session: Authenticated session
        article_id: Article key (e.g., "n1234567890ab").
            Note: Key format is required due to note.com API limitations.
            The /v3/notes/ endpoint does not support numeric IDs.

    Returns:
        Article object with raw HTML body

    Raises:
        NoteAPIError: If API request fails or numeric ID is provided
    """
    # Issue #154: /v3/notes/ endpoint does not support numeric IDs
    if article_id.isdigit():
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=(
                f"Numeric article ID '{article_id}' is not supported. "
                "Please use the article key format (e.g., 'n1234567890ab'). "
                "You can get the article key from create_draft() or list_articles()."
            ),
            details={"article_id": article_id},
        )

    article = await _execute_get(
        session,
        f"/v3/notes/{article_id}",
        _parse_article_response,
    )

    # Issue #209: Check if article was deleted
    # note.com API returns status='deleted' instead of 404 for deleted articles.
    # We treat this as ARTICLE_NOT_FOUND because the content is no longer accessible.
    if article.status == ArticleStatus.DELETED:
        raise NoteAPIError(
            code=ErrorCode.ARTICLE_NOT_FOUND,
            message="Article has been deleted (status='deleted')",
            details={"article_id": article_id},
        )

    return article


async def update_article_raw_html(
    session: Session,
    article_id: str,
    title: str,
    html_body: str,
    tags: list[str] | None = None,
) -> Article:
    """Update article with raw HTML body (no Markdown conversion).

    Unlike update_article(), this saves the HTML body directly without
    converting from Markdown. Use this when the body is already in HTML
    format (e.g., after appending image HTML).

    Args:
        session: Authenticated session
        article_id: ID of the article to update
        title: Article title
        html_body: HTML body content (not Markdown)
        tags: Optional list of tags

    Returns:
        Updated Article object

    Raises:
        NoteAPIError: If API request fails
    """
    # Resolve to numeric ID (API requirement)
    numeric_id = await _resolve_numeric_note_id(session, article_id)

    # Build payload with raw HTML body (no conversion)
    payload: dict[str, Any] = {
        "name": title,
        "body": html_body,
        "body_length": len(html_body),
        "index": False,
        "is_lead_form": False,
    }

    # Add tags if provided
    hashtags = _normalize_tags(tags)
    if hashtags:
        payload["hashtags"] = hashtags

    return await _execute_post(
        session,
        f"/v1/text_notes/draft_save?id={numeric_id}&is_temp_saved=true",
        _create_draft_save_parser(article_id, numeric_id, title, html_body),
        payload=payload,
    )


def _parse_article_response(response: dict[str, Any]) -> Article:
    """Parse API response and convert to Article.

    Handles the common pattern of extracting article data from
    API response and converting it to Article model.

    Args:
        response: Raw API response dict with "data" key

    Returns:
        Article object parsed from response data

    Raises:
        NoteAPIError: If "data" key is missing from response
    """
    article_data = response.get("data")
    if article_data is None:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Invalid API response: missing 'data' key",
            details={"response": response},
        )
    return from_api_response(article_data)


def _normalize_tags(tags: list[str] | None) -> list[dict[str, Any]] | None:
    """Normalize tags to API format for draft_save.

    Removes leading '#' and converts to hashtag dict format.
    This format is used by POST /v1/text_notes/draft_save.

    Args:
        tags: List of tags (may include '#' prefix)

    Returns:
        List of hashtag dicts for API, or None if no tags
    """
    if not tags:
        return None
    normalized = [tag.lstrip("#") for tag in tags]
    return [{"hashtag": {"name": tag}} for tag in normalized]


def _normalize_tags_for_publish(tags: list[str] | None) -> list[str] | None:
    """Normalize tags to API format for publish.

    Ensures tags have '#' prefix as required by PUT /v1/text_notes/{id}.
    This format is used when publishing articles.

    Args:
        tags: List of tags (may or may not include '#' prefix)

    Returns:
        List of hashtag strings with '#' prefix, or None if no tags
    """
    if not tags:
        return None
    return [f"#{tag.lstrip('#')}" for tag in tags]


def _build_article_payload(
    article_input: ArticleInput,
    html_body: str | None = None,
    include_body: bool = True,
) -> dict[str, Any]:
    """Build common article payload for API requests.

    Args:
        article_input: Article content and metadata
        html_body: Pre-converted HTML body (optional)
        include_body: Whether to include body in payload

    Returns:
        Payload dict for note.com API
    """
    payload: dict[str, Any] = {
        "name": article_input.title,
        "index": False,
        "is_lead_form": False,
    }

    if include_body and html_body is not None:
        payload["body"] = html_body
        payload["body_length"] = len(html_body)

    hashtags = _normalize_tags(article_input.tags)
    if hashtags:
        payload["hashtags"] = hashtags

    return payload


def _is_article_key_format(article_id: str) -> bool:
    """Check if article_id is in key format (e.g., "n12345abcdef").

    Key format starts with "n" followed by alphanumeric characters.
    Pure numeric IDs (e.g., "12345") are NOT considered keys.

    Args:
        article_id: Article identifier to check

    Returns:
        True if article_id is in key format, False otherwise
    """
    return article_id.startswith("n") and not article_id.isdigit()


def _validate_draft_save_response(
    response: dict[str, Any],
    article_id: str,
) -> None:
    """Validate draft_save API response.

    Issue #155: draft_save returns {result, note_days_count, updated_at},
    not full article data. We validate by checking for "result" field.

    Args:
        response: Raw API response dict
        article_id: Article ID for error context

    Raises:
        NoteAPIError: If response is invalid or missing required fields
    """
    article_data = response.get("data", {})
    if not article_data or "result" not in article_data:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Article update failed: API returned empty response",
            details={"article_id": article_id, "response": response},
        )


def _create_draft_save_parser(
    article_id: str,
    numeric_id: str,
    title: str,
    html_body: str,
    article_key: str = "",
) -> Callable[[dict[str, Any]], Article]:
    """Create a parser for draft_save response.

    Issue #174: Factory function to create response parser with context.
    draft_save returns minimal response, so we construct Article from inputs.

    Args:
        article_id: Original article ID (for error context)
        numeric_id: Resolved numeric ID
        title: Article title
        html_body: HTML body content
        article_key: Article key (optional, derived from article_id if not provided)

    Returns:
        Parser function that validates response and returns Article
    """

    def parser(response: dict[str, Any]) -> Article:
        _validate_draft_save_response(response, article_id)
        key = article_key if article_key else (article_id if _is_article_key_format(article_id) else "")
        return Article(
            id=numeric_id,
            key=key,
            title=title,
            body=html_body,
            status=ArticleStatus.DRAFT,
        )

    return parser


def _parse_create_response(response: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Parse response from article creation endpoint.

    Issue #174: Extract article_id and article_key from create response.

    Args:
        response: Raw API response from /v1/text_notes

    Returns:
        Tuple of (article_id, article_key, article_data)

    Raises:
        NoteAPIError: If required fields are missing
    """
    article_data = response.get("data", {})
    article_id = article_data.get("id")
    article_key = article_data.get("key")

    if not article_id:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Article creation failed: API returned no article ID",
            details={"response": response},
        )
    if not article_key:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Article creation failed: API returned no article key",
            details={"article_id": article_id, "response": response},
        )

    return str(article_id), str(article_key), article_data


async def create_draft(
    session: Session,
    article_input: ArticleInput,
) -> Article:
    """Create a new draft article.

    Uses the note.com API to create the draft directly.
    Converts Markdown body to HTML as required by the API.

    Note: This function performs multiple API calls:
    1. POST /v1/text_notes - Creates the article entry (without body)
    2. GET /v2/embed_by_external_api - For each embed URL, fetches server key
    3. POST /v1/text_notes/draft_save - Saves the body content with resolved keys

    The body is sent only via draft_save to preserve HTML structure.
    Embed URLs (YouTube, Twitter, note.com) are processed to obtain
    server-registered keys required for iframe rendering.

    Args:
        session: Authenticated session
        article_input: Article content and metadata

    Returns:
        Created Article object

    Raises:
        NoteAPIError: If API request fails
    """
    # Convert Markdown to HTML for API (embeds get random keys initially)
    html_body = markdown_to_html(article_input.body)

    # Step 1 payload: without body to avoid sanitization
    create_payload = _build_article_payload(article_input, include_body=False)

    # Step 1: Create the article entry (without body)
    # The body is saved separately via draft_save to preserve <br> tags
    article_id, article_key, article_data = await _execute_post(
        session,
        "/v1/text_notes",
        _parse_create_response,
        payload=create_payload,
    )

    # Step 2: Resolve embed keys via API
    # Replace random keys with server-registered keys for iframe rendering
    resolved_html = await resolve_embed_keys(session, html_body, article_key)

    # Step 3: Save the body content with draft_save
    # Use resolved HTML with server-registered embed keys
    save_payload = _build_article_payload(article_input, resolved_html)

    async with NoteAPIClient(session) as client:
        await client.post(
            f"/v1/text_notes/draft_save?id={article_id}&is_temp_saved=true",
            json=save_payload,
        )

    # Parse response
    # Note: POST /v1/text_notes returns empty 'status' field for newly created articles.
    # Since this function specifically creates drafts, we set status to 'draft' explicitly.
    # This is Article 6 compliant: we know the expected state from the function's semantics.
    status_str = article_data.get("status")
    if not status_str:
        logger.warning(
            "create_draft API returned empty status, setting to 'draft'. Response: %s",
            article_data,
        )
        article_data["status"] = ArticleStatus.DRAFT.value

    return from_api_response(article_data)


async def update_article(
    session: Session,
    article_id: str,
    article_input: ArticleInput,
) -> Article:
    """Update an existing article.

    Uses the note.com API to update the article.
    Converts Markdown body to HTML as required by the API.
    Embed URLs (YouTube, Twitter, note.com) are processed to obtain
    server-registered keys required for iframe rendering.

    Args:
        session: Authenticated session
        article_id: ID of the article to update (numeric or key format)
        article_input: New article content and metadata

    Returns:
        Updated Article object

    Raises:
        NoteAPIError: If API request fails
    """
    from note_mcp.api.embeds import _EMBED_FIGURE_PATTERN

    # Resolve to numeric ID (API requirement)
    numeric_id = await _resolve_numeric_note_id(session, article_id)

    # Convert Markdown to HTML for API (embeds get random keys initially)
    html_body = markdown_to_html(article_input.body)

    # Check if HTML contains embeds that need key resolution
    # Issue #146: Only fetch article key when embeds are present
    has_embeds = bool(_EMBED_FIGURE_PATTERN.search(html_body))

    # Determine final HTML and article key for result construction
    final_html = html_body
    article_key_for_result = article_id if _is_article_key_format(article_id) else ""

    if has_embeds:
        # Resolve article key for embed resolution
        article_key = article_id if _is_article_key_format(article_id) else ""

        if not article_key:
            # Numeric ID: fetch article to get key since draft_save doesn't return it
            # Issue #155: draft_save returns {result, note_days_count, updated_at}, not article data
            fetched_article = await get_article_via_api(session, str(numeric_id))
            article_key = fetched_article.key
            # Preserve fetched key in result (Issue #155 review feedback)
            article_key_for_result = article_key

        if article_key:
            # Resolve embed keys via API
            # Replace random keys with server-registered keys for iframe rendering
            final_html = await resolve_embed_keys(session, html_body, str(article_key))
        else:
            # Fallback: proceed without embed resolution if key not available
            logger.warning(
                "Embed resolution skipped: article does not have a key. Embeds in article %s may not render correctly.",
                article_id,
                extra={"article_id": article_id},
            )

    # Build payload and save via draft_save endpoint
    payload = _build_article_payload(article_input, final_html)

    return await _execute_post(
        session,
        f"/v1/text_notes/draft_save?id={numeric_id}&is_temp_saved=true",
        _create_draft_save_parser(
            article_id,
            numeric_id,
            article_input.title,
            final_html,
            article_key_for_result,
        ),
        payload=payload,
    )


async def get_article_via_api(
    session: Session,
    article_id: str,
) -> Article:
    """Get article content by ID via API.

    Retrieves article content directly from the note.com API.
    Faster and more reliable than browser-based retrieval.

    Args:
        session: Authenticated session
        article_id: Article key (e.g., "n1234567890ab").
            Note: Key format is required due to note.com API limitations.
            The /v3/notes/ endpoint does not support numeric IDs.

    Returns:
        Article object with title, body (as Markdown), and status

    Raises:
        NoteAPIError: If API request fails or numeric ID is provided
    """
    # Issue #154: /v3/notes/ endpoint does not support numeric IDs
    if article_id.isdigit():
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=(
                f"Numeric article ID '{article_id}' is not supported. "
                "Please use the article key format (e.g., 'n1234567890ab'). "
                "You can get the article key from create_draft() or list_articles()."
            ),
            details={"article_id": article_id},
        )

    from note_mcp.utils.html_to_markdown import html_to_markdown

    article = await _execute_get(
        session,
        f"/v3/notes/{article_id}",
        _parse_article_response,
    )

    # Issue #209: Check if article was deleted
    # note.com API returns status='deleted' instead of 404 for deleted articles.
    # We treat this as ARTICLE_NOT_FOUND because the content is no longer accessible.
    if article.status == ArticleStatus.DELETED:
        raise NoteAPIError(
            code=ErrorCode.ARTICLE_NOT_FOUND,
            message="Article has been deleted (status='deleted')",
            details={"article_id": article_id},
        )

    # Convert HTML body to Markdown for consistent output
    if article.body:
        article = Article(
            id=article.id,
            key=article.key,
            title=article.title,
            body=html_to_markdown(article.body),
            status=article.status,
            tags=article.tags,
            eyecatch_image_key=article.eyecatch_image_key,
            prev_access_key=article.prev_access_key,
            created_at=article.created_at,
            updated_at=article.updated_at,
            published_at=article.published_at,
            url=article.url,
        )

    return article


async def get_article(
    session: Session,
    article_id: str,
) -> Article:
    """Get article content by ID.

    Retrieves article content via API.
    Use this to retrieve existing content before editing.

    Recommended workflow:
    1. get_article(article_id) - retrieve current content
    2. Edit content as needed
    3. update_article(article_id, ...) - save changes

    Args:
        session: Authenticated session
        article_id: ID of the article to retrieve

    Returns:
        Article object with title, body (as Markdown), and status

    Raises:
        NoteAPIError: If API request fails
    """
    return await get_article_via_api(session, article_id)


async def list_articles(
    session: Session,
    status: ArticleStatus | None = None,
    page: int = 1,
    limit: int = 10,
) -> ArticleListResult:
    """List articles for the authenticated user.

    Uses the note_list/contents endpoint which returns both drafts and
    published articles for the authenticated user.

    Args:
        session: Authenticated session
        status: Filter by article status (draft, published, or None for all)
        page: Page number (1-indexed)
        limit: Number of articles per page (max 10)

    Returns:
        ArticleListResult containing articles and pagination info

    Raises:
        NoteAPIError: If API request fails
    """
    # Build query parameters for note_list endpoint
    # This endpoint returns both drafts and published articles
    params: dict[str, Any] = {
        "page": page,
    }

    # Add status filter if specified
    # Note: The note_list endpoint uses "publish_status" parameter
    if status is not None:
        params["publish_status"] = status.value

    # Use note_list/contents endpoint for authenticated user's articles
    # This endpoint requires authentication and returns both drafts and published
    async with NoteAPIClient(session) as client:
        response = await client.get("/v2/note_list/contents", params=params)

    # Parse response
    data = response.get("data", {})

    # The endpoint returns notes (not contents) in data
    contents = data.get("notes", [])
    total_count = data.get("totalCount", len(contents))
    is_last_page = data.get("isLastPage", True)

    # Convert each article
    articles: list[Article] = []
    for item in contents:
        article = from_api_response(item)
        articles.append(article)

    # Apply limit client-side if needed
    articles = articles[:limit]

    return ArticleListResult(
        articles=articles,
        total=total_count,
        page=page,
        has_more=not is_last_page,
    )


async def publish_article(
    session: Session,
    article_id: str | None = None,
    article_input: ArticleInput | None = None,
    tags: list[str] | None = None,
) -> Article:
    """Publish an article.

    Either publishes an existing draft or creates and publishes a new article.

    Args:
        session: Authenticated session
        article_id: ID of existing draft to publish (mutually exclusive with article_input)
        article_input: New article content to create and publish (mutually exclusive with article_id)
        tags: Tags to set on the article when publishing an existing draft (optional).
            For new articles, use article_input.tags instead.

    Returns:
        Published Article object

    Raises:
        ValueError: If neither or both article_id and article_input are provided
        NoteAPIError: If API request fails
    """
    if article_id is None and article_input is None:
        raise ValueError("Either article_id or article_input must be provided")

    if article_id is not None and article_input is not None:
        raise ValueError("Cannot provide both article_id and article_input")

    if article_id is not None:
        # Publish existing draft
        # Issue #250: Validate article_id format BEFORE making any API calls
        # to prevent data inconsistency (article published but error returned)
        if article_id.isdigit():
            raise NoteAPIError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    f"Numeric article ID '{article_id}' is not supported. "
                    "Please use the article key format (e.g., 'n1234567890ab')."
                ),
                details={"article_id": article_id},
            )

        # Issue #250: Use PUT /v1/text_notes/{numeric_id} instead of
        # non-existent POST /v3/notes/{id}/publish endpoint
        numeric_id = await _resolve_numeric_note_id(session, article_id)

        async with NoteAPIClient(session) as client:
            # Fetch article title (required for both draft_save and PUT)
            article_response = await client.get(f"/v3/notes/{article_id}")
            article_data = article_response.get("data", {})
            # For drafts, title is in note_draft.name; for published, it's in name
            article_title = article_data.get("name", "")
            if not article_title:
                note_draft = article_data.get("note_draft")
                if isinstance(note_draft, dict):
                    article_title = note_draft.get("name", "")
            # For drafts, prefer note_draft.body which has full HTML including headings
            # data.body may be a stripped/sanitized version
            # Use `or ""` to handle None values (key exists but value is None)
            note_draft = article_data.get("note_draft")
            if isinstance(note_draft, dict) and note_draft.get("body"):
                article_body = note_draft.get("body") or ""
            else:
                article_body = article_data.get("body") or ""

            # Publish the article
            # Issue #252: PUT /v1/text_notes/{id} requires 'free_body' (not 'body')
            # and hashtags in ["#tag1", "#tag2"] format (not dict format)
            payload: dict[str, Any] = {
                "name": article_title,
                "free_body": article_body,
                "body_length": len(article_body),
                "status": "published",
                "index": False,
            }

            # Add tags if provided (using publish format with # prefix)
            if tags:
                hashtags = _normalize_tags_for_publish(tags)
                if hashtags:
                    payload["hashtags"] = hashtags

            response = await client.put(f"/v1/text_notes/{numeric_id}", json=payload)

        # Validate API response for logical failure
        data = response.get("data", {})
        if data.get("result") is False:
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message="Failed to publish article: API returned failure",
                details={"article_id": article_id, "response": response},
            )

        return await get_article_via_api(session, article_id)

    # Create and publish new article
    assert article_input is not None  # Type narrowing
    html_body = markdown_to_html(article_input.body)

    new_article_payload: dict[str, Any] = {
        "name": article_input.title,
        "body": html_body,
        "status": "published",
    }

    # Add tags if present (using dict format for /v3/notes endpoint)
    new_article_hashtags = _normalize_tags(article_input.tags)
    if new_article_hashtags:
        new_article_payload["hashtags"] = new_article_hashtags

    return await _execute_post(
        session,
        "/v3/notes",
        _parse_article_response,
        payload=new_article_payload,
    )


# =============================================================================
# Issue #134: Preview Access Token Functions
# =============================================================================


async def get_preview_access_token(
    session: Session,
    article_key: str,
) -> str:
    """Get preview access token for a draft article.

    Calls the note.com API to obtain a preview access token that allows
    viewing draft articles without editor access.

    Args:
        session: Authenticated session
        article_key: Article key (e.g., "n1234567890ab")

    Returns:
        32-character hex preview access token

    Raises:
        NoteAPIError: If API request fails or token is missing from response

    Example:
        token = await get_preview_access_token(session, "n1234567890ab")
        url = build_preview_url("n1234567890ab", token)
    """
    async with NoteAPIClient(session) as client:
        response = await client.post(
            f"/v2/notes/{article_key}/access_tokens",
            json={"key": article_key},
        )

    data = response.get("data", {})
    token = data.get("preview_access_token")

    if not token:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message=(
                "Failed to get preview access token. "
                "Possible causes: article does not exist, article is already published, "
                "or insufficient permissions."
            ),
            details={"article_key": article_key, "response": response},
        )

    return str(token)


def build_preview_url(article_key: str, preview_access_token: str) -> str:
    """Build direct preview URL from access token.

    Constructs a URL that allows direct access to the draft article preview
    without going through the editor UI.

    Args:
        article_key: Article key (e.g., "n1234567890ab")
        preview_access_token: 32-character hex token from API

    Returns:
        Direct preview URL

    Example:
        url = build_preview_url("n123abc", "token123...")
        # url = "https://note.com/preview/n123abc?prev_access_key=token123..."
    """
    return f"https://note.com/preview/{article_key}?prev_access_key={preview_access_token}"


# =============================================================================
# Issue #141: Delete Draft Functions
# =============================================================================


async def delete_draft(
    session: Session,
    article_key: str,
    *,
    confirm: bool = False,
) -> DeleteResult | DeletePreview:
    """Delete a draft article.

    Deletes a draft article from note.com. Only draft articles can be deleted;
    published articles will raise an error.

    This function implements a two-step confirmation flow:
    1. When confirm=False: Returns a DeletePreview with article info
    2. When confirm=True: Actually deletes the article

    Args:
        session: Authenticated session
        article_key: Key of the article to delete (format: nXXXXXXXXXXXX)
        confirm: Confirmation flag (must be True to execute deletion)

    Returns:
        DeletePreview when confirm=False (shows what will be deleted)
        DeleteResult when confirm=True (deletion result)

    Raises:
        NoteAPIError: If article is published, not found, or API fails

    Example:
        # Step 1: Preview what will be deleted
        preview = await delete_draft(session, "n1234567890ab", confirm=False)
        print(f"Will delete: {preview.article_title}")

        # Step 2: Actually delete
        result = await delete_draft(session, "n1234567890ab", confirm=True)
        print(f"Deleted: {result.message}")
    """
    # Import here to avoid circular imports
    from note_mcp.models import (
        DELETE_ERROR_PUBLISHED_ARTICLE,
        DeletePreview,
        DeleteResult,
    )

    # Step 1: Fetch article info to validate and get details
    article = await _execute_get(
        session,
        f"/v3/notes/{article_key}",
        _parse_article_response,
    )

    # Check if article is published (cannot delete published articles)
    if article.status == ArticleStatus.PUBLISHED:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message=DELETE_ERROR_PUBLISHED_ARTICLE,
            details={"article_key": article_key, "status": article.status.value},
        )

    # Issue #209: Check if article was already deleted
    # note.com API returns status='deleted' instead of 404 for deleted articles.
    # We treat this as ARTICLE_NOT_FOUND because attempting to delete an
    # already deleted article is nonsensical.
    if article.status == ArticleStatus.DELETED:
        raise NoteAPIError(
            code=ErrorCode.ARTICLE_NOT_FOUND,
            message="Article has been deleted (status='deleted')",
            details={"article_key": article_key},
        )

    # If confirm=False, return preview without deleting
    if not confirm:
        return DeletePreview(
            article_id=article.id,
            article_key=article.key,
            article_title=article.title,
            status=article.status,
            message=f"下書き記事「{article.title}」を削除しますか？confirm=True を指定して再度呼び出してください。",
        )

    # Step 2: Execute deletion (confirm=True)
    # Note: The delete endpoint requires /n/ prefix before the article key
    await _execute_delete(session, f"/v1/notes/n/{article_key}")

    return DeleteResult(
        success=True,
        article_id=article.id,
        article_key=article.key,
        article_title=article.title,
        message=f"下書き記事「{article.title}」({article.key})を削除しました。",
    )


async def delete_all_drafts(
    session: Session,
    *,
    confirm: bool = False,
) -> BulkDeleteResult | BulkDeletePreview:
    """Delete all draft articles.

    Deletes all draft articles for the authenticated user.
    Implements a two-step confirmation flow for safety.

    This function:
    1. Fetches all drafts using list_articles(status=DRAFT)
    2. When confirm=False: Returns a BulkDeletePreview listing all drafts
    3. When confirm=True: Sequentially deletes each draft

    Args:
        session: Authenticated session
        confirm: Confirmation flag (must be True to execute deletion)

    Returns:
        BulkDeletePreview when confirm=False (shows what will be deleted)
        BulkDeleteResult when confirm=True (deletion results with success/failure counts)

    Example:
        # Step 1: Preview what will be deleted
        preview = await delete_all_drafts(session, confirm=False)
        print(f"Will delete {preview.total_count} drafts")

        # Step 2: Actually delete all
        result = await delete_all_drafts(session, confirm=True)
        print(f"Deleted: {result.deleted_count}, Failed: {result.failed_count}")
    """
    from note_mcp.models import (
        ArticleSummary,
        BulkDeletePreview,
        BulkDeleteResult,
        FailedArticle,
    )

    # Step 1: Get all drafts (paginate through all pages)
    article_summaries: list[ArticleSummary] = []
    page = 1

    async with NoteAPIClient(session) as client:
        while page <= DELETE_ALL_DRAFTS_MAX_PAGES:
            response = await client.get(
                "/v2/note_list/contents",
                params={"publish_status": "draft", "page": page},
            )

            data = response.get("data", {})
            notes = data.get("notes", [])

            # No more notes, stop pagination
            if not notes:
                break

            # Build article summaries for this page
            # Article 6: Required fields (id, key) must be present, skip invalid notes
            for note in notes:
                note_id = note.get("id")
                note_key = note.get("key")

                # Skip notes with missing required fields (Article 6 compliance)
                if not note_id or not note_key:
                    logger.warning(
                        "Skipping note with missing required field(s)",
                        extra={
                            "note_id": note_id,
                            "note_key": note_key,
                            "note_name": note.get("name"),
                        },
                    )
                    continue

                article_summaries.append(
                    ArticleSummary(
                        article_id=str(note_id),
                        article_key=str(note_key),
                        # title is display-only, empty string is valid
                        title=str(note.get("name") or ""),
                    )
                )

            page += 1

    total_count = len(article_summaries)

    # If no drafts, return early
    if total_count == 0:
        if not confirm:
            return BulkDeletePreview(
                total_count=0,
                articles=[],
                message="削除対象の下書きがありません。",
            )
        return BulkDeleteResult(
            success=True,
            total_count=0,
            deleted_count=0,
            failed_count=0,
            deleted_articles=[],
            failed_articles=[],
            message="削除対象の下書きがありません。",
        )

    # If confirm=False, return preview
    if not confirm:
        return BulkDeletePreview(
            total_count=total_count,
            articles=article_summaries[:DELETE_ALL_DRAFTS_PREVIEW_LIMIT],
            message=f"{total_count}件の下書き記事を削除しますか？confirm=True を指定して再度呼び出してください。",
        )

    # Step 2: Execute deletion (confirm=True)
    deleted_articles: list[ArticleSummary] = []
    failed_articles: list[FailedArticle] = []

    async with NoteAPIClient(session) as client:
        for summary in article_summaries:
            try:
                await client.delete(f"/v1/notes/n/{summary.article_key}")
                deleted_articles.append(summary)
            except NoteAPIError as e:
                failed_articles.append(
                    FailedArticle(
                        article_id=summary.article_id,
                        article_key=summary.article_key,
                        title=summary.title,
                        error=e.message,
                    )
                )

    deleted_count = len(deleted_articles)
    failed_count = len(failed_articles)
    success = failed_count == 0

    # Build result message
    if failed_count == 0:
        message = f"{deleted_count}件の下書き記事を削除しました。"
    else:
        message = (
            f"{total_count}件中{deleted_count}件の下書き記事を削除しました。{failed_count}件の削除に失敗しました。"
        )

    return BulkDeleteResult(
        success=success,
        total_count=total_count,
        deleted_count=deleted_count,
        failed_count=failed_count,
        deleted_articles=deleted_articles,
        failed_articles=failed_articles,
        message=message,
    )

