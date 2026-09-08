"""Image upload operations for note.com API.

Provides functionality for uploading images to note.com.
Supports both eyecatch (header) images and body (inline) images.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from note_mcp.api.client import NoteAPIClient
from note_mcp.models import ErrorCode, Image, ImageType, NoteAPIError, Session

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def _resolve_numeric_note_id(session: Session, note_id: str) -> str:
    """Resolve note ID to numeric format.

    The image upload API requires numeric note IDs.
    This function converts key format IDs (e.g., "ne1c111d2073c") to numeric IDs.

    Args:
        session: Authenticated session
        note_id: Note ID in either numeric or key format

    Returns:
        Numeric note ID as string

    Raises:
        NoteAPIError: If ID resolution fails
    """
    # If already numeric, return as-is
    if note_id.isdigit():
        return note_id

    # Key format IDs start with "n" followed by alphanumeric characters
    if not re.match(r"^n[a-z0-9]+$", note_id):
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=f"Invalid note ID format: {note_id}",
            details={"note_id": note_id},
        )

    # Fetch article details to get numeric ID
    async with NoteAPIClient(session) as client:
        response = await client.get(f"/v3/notes/{note_id}")

    # Extract numeric ID from response
    data = response.get("data", {})
    numeric_id = data.get("id")

    if not numeric_id:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message=f"Failed to resolve note ID: {note_id}",
            details={"note_id": note_id, "response": response},
        )

    return str(numeric_id)


# Allowed image file extensions
ALLOWED_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Maximum file size in bytes (10MB)
MAX_FILE_SIZE: int = 10 * 1024 * 1024

# Content-type mapping for image files (single source of truth - DRY)
CONTENT_TYPE_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# API endpoints for different image types
# Note: Body images use the same endpoint as eyecatch images.
# The returned URL can be embedded in article body using Markdown syntax.
IMAGE_UPLOAD_ENDPOINTS: dict[ImageType, str] = {
    ImageType.EYECATCH: "/v1/image_upload/note_eyecatch",
    ImageType.BODY: "/v1/image_upload/note_eyecatch",  # Same endpoint - URL works for body embedding
}


def validate_image_file(file_path: str) -> None:
    """Validate image file before upload.

    Args:
        file_path: Path to the image file

    Raises:
        NoteAPIError: If file is invalid (not found, wrong format, too large)
    """
    path = Path(file_path)

    # Check file exists
    if not path.exists():
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=f"File not found: {file_path}",
            details={"file_path": file_path},
        )

    # Check file extension
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=(f"Invalid file format: {path.suffix}. Allowed formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}"),
            details={"file_path": file_path, "extension": path.suffix},
        )

    # Check file size
    file_size = path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=(f"File size ({file_size} bytes) exceeds maximum allowed size ({MAX_FILE_SIZE} bytes)"),
            details={"file_path": file_path, "size": file_size, "max_size": MAX_FILE_SIZE},
        )


async def _upload_image_internal(
    session: Session,
    file_path: str,
    note_id: str,
    image_type: ImageType,
) -> Image:
    """Internal function for uploading an image to note.com.

    Validates the file format and size before uploading.
    Uses multipart/form-data for the upload.

    Args:
        session: Authenticated session
        file_path: Path to the image file
        note_id: The note ID to associate the image with (numeric or key format)
        image_type: Type of image (eyecatch or body)

    Returns:
        Image object with upload result

    Raises:
        NoteAPIError: If validation fails or API request fails
    """
    # Validate file before upload
    validate_image_file(file_path)

    # Resolve note ID to numeric format (API requirement)
    numeric_note_id = await _resolve_numeric_note_id(session, note_id)

    path = Path(file_path)
    file_size = path.stat().st_size

    # Prepare file for multipart upload
    with open(file_path, "rb") as f:
        file_content = f.read()

    # Determine content type based on extension
    content_type = CONTENT_TYPE_MAP.get(path.suffix.lower(), "application/octet-stream")

    # Prepare files for multipart request
    files = {
        "file": (path.name, file_content, content_type),
    }

    # note_id is required by the API (must be numeric)
    data = {"note_id": numeric_note_id}

    # Get endpoint for the image type
    endpoint = IMAGE_UPLOAD_ENDPOINTS[image_type]

    async with NoteAPIClient(session) as client:
        response = await client.post(endpoint, files=files, data=data)

    # Parse response - Article 6: validate required fields, no fallback
    image_data = response.get("data", {})

    # Note: The eyecatch upload endpoint (/v1/image_upload/note_eyecatch) returns
    # only 'url' in the response, not 'key'. This is expected behavior based on
    # API testing - body images (via presigned_post) return 'key', eyecatch does not.
    image_key = image_data.get("key")

    image_url = image_data.get("url")
    if not image_url:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Image upload failed: API response missing required field 'url'",
            details={"response": response},
        )

    return Image(
        key=str(image_key) if image_key else None,
        url=str(image_url),
        original_path=file_path,
        size_bytes=file_size,
        uploaded_at=int(time.time()),
        image_type=image_type,
    )


async def upload_eyecatch_image(
    session: Session,
    file_path: str,
    note_id: str,
) -> Image:
    """Upload an eyecatch (header) image to note.com.

    Validates the file format and size before uploading.
    Uses multipart/form-data for the upload.

    Args:
        session: Authenticated session
        file_path: Path to the image file
        note_id: The note ID to associate the image with (required by API)

    Returns:
        Image object with upload result

    Raises:
        NoteAPIError: If validation fails or API request fails
    """
    return await _upload_image_internal(session, file_path, note_id, ImageType.EYECATCH)


async def upload_body_image(
    session: Session,
    file_path: str,
    note_id: str,
) -> Image:
    """Upload a body (inline) image to note.com.

    Uses the presigned_post flow to upload directly to S3.
    This does NOT update the eyecatch image (unlike the eyecatch endpoint).
    The returned URL can be embedded in article body using Markdown syntax:
    ![alt text](returned_url)

    Args:
        session: Authenticated session
        file_path: Path to the image file
        note_id: The note ID to associate the image with (for metadata only)

    Returns:
        Image object with upload result

    Raises:
        NoteAPIError: If validation fails or API request fails
    """
    import httpx

    # Validate file before upload
    validate_image_file(file_path)

    path = Path(file_path)
    file_size = path.stat().st_size

    # Step 1: Get presigned POST URL from note.com
    async with NoteAPIClient(session) as client:
        response = await client.post(
            "/v3/images/upload/presigned_post",
            data={"filename": path.name},
        )

    presigned_data = response.get("data", {})
    s3_url = presigned_data.get("action")
    image_url = presigned_data.get("url")
    post_fields = presigned_data.get("post", {})

    if not s3_url or not image_url or not post_fields:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="Failed to get presigned URL for image upload",
            details={"response": response},
        )

    # Step 2: Upload file directly to S3
    with open(file_path, "rb") as f:
        file_content = f.read()

    # Article 6: Validate required S3 presigned POST fields
    required_s3_fields = [
        "key",
        "policy",
        "x-amz-credential",
        "x-amz-algorithm",
        "x-amz-date",
        "x-amz-signature",
    ]
    for field in required_s3_fields:
        if not post_fields.get(field):
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message=f"Presigned POST missing required S3 field: {field}",
                details={"response": response, "missing_field": field},
            )

    # Build multipart form data with S3 required fields
    # Order matters for S3 - policy fields first, then file
    files_data: dict[str, tuple[None, str] | tuple[str, bytes, str]] = {
        "key": (None, str(post_fields["key"])),
        "acl": (None, str(post_fields.get("acl", ""))),
        "Expires": (None, str(post_fields.get("Expires", ""))),
        "policy": (None, str(post_fields["policy"])),
        "x-amz-credential": (None, str(post_fields["x-amz-credential"])),
        "x-amz-algorithm": (None, str(post_fields["x-amz-algorithm"])),
        "x-amz-date": (None, str(post_fields["x-amz-date"])),
        "x-amz-signature": (None, str(post_fields["x-amz-signature"])),
    }

    # Determine content type
    content_type = CONTENT_TYPE_MAP.get(path.suffix.lower(), "application/octet-stream")

    # Add file last (S3 requirement)
    files_data["file"] = (path.name, file_content, content_type)

    async with httpx.AsyncClient() as http_client:
        s3_response = await http_client.post(s3_url, files=files_data)

        if not s3_response.is_success:
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message=f"Failed to upload image to S3: {s3_response.status_code}",
                details={"status": s3_response.status_code, "response": s3_response.text},
            )

    # key is validated above in required_s3_fields check
    return Image(
        key=str(post_fields["key"]),
        url=image_url,
        original_path=file_path,
        size_bytes=file_size,
        uploaded_at=int(time.time()),
        image_type=ImageType.BODY,
    )


async def insert_image_via_api(
    session: Session,
    article_id: str,
    file_path: str,
    caption: str | None = None,
) -> dict[str, Any]:
    """Insert an image into an article via API.

    Fully API-based implementation without Playwright dependency.
    This is faster and more reliable than browser-based insertion.

    Flow:
    1. Validate image file
    2. Get article with raw HTML body
    3. Upload image to S3 via API
    4. Generate figure HTML
    5. Append to existing body
    6. Update article via draft_save API

    Args:
        session: Authenticated session
        article_id: Article key (e.g., "n1234567890ab").
            Note: Key format is required due to note.com API limitations.
            The /v3/notes/ endpoint does not support numeric IDs.
            Use the article key returned from create_draft() or list_articles().
        file_path: Path to the image file to insert
        caption: Optional caption for the image

    Returns:
        Dictionary with the following keys:
        - success: Always True on success (raises on failure)
        - article_id: Numeric article ID
        - article_key: Article key (e.g., "n1234567890ab")
        - file_path: Path to the uploaded file
        - image_url: URL of the uploaded image on note.com CDN
        - caption: Caption text (if provided)
        - fallback_used: Always False (no browser fallback in API-only mode)

    Raises:
        NoteAPIError: If image insertion fails
    """
    # Import here to avoid circular imports
    from note_mcp.api.articles import (
        append_image_to_body,
        generate_image_html,
        get_article_raw_html,
        update_article_raw_html,
    )

    # Step 1: Validate file (existence, extension, and size)
    validate_image_file(file_path)

    # Step 2: Validate article_id format
    # Issue #147: /v3/notes/ endpoint does not support numeric IDs
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

    # Step 3: Get article with raw HTML body
    try:
        article = await get_article_raw_html(session, article_id)
    except NoteAPIError as e:
        raise NoteAPIError(
            code=ErrorCode.INVALID_INPUT,
            message=f"Invalid article ID: {article_id}. Please verify the article exists and you have access.",
            details={"article_id": article_id, "original_error": str(e)},
        ) from e

    article_key = article.key
    numeric_id = article.id
    logger.debug(f"Article validated: key={article_key}, numeric_id={numeric_id}")

    # Step 3: Upload image via API
    image = await upload_body_image(session, file_path, numeric_id)
    logger.info(f"Image uploaded via API: {image.url[:50]}...")

    # Step 4: Generate image HTML in note.com format
    image_html = generate_image_html(
        image_url=image.url,
        caption=caption or "",
    )
    logger.debug(f"Generated image HTML: {image_html[:100]}...")

    # Step 5: Append image to existing body
    new_body_html = append_image_to_body(article.body or "", image_html)
    logger.debug(f"New body length: {len(new_body_html)} chars")

    # Step 6: Update article via API (draft_save)
    await update_article_raw_html(
        session=session,
        article_id=numeric_id,
        title=article.title,
        html_body=new_body_html,
    )
    logger.info("Article updated via API")

    return {
        "success": True,
        "article_id": numeric_id,
        "article_key": article_key,
        "file_path": file_path,
        "image_url": image.url,
        "caption": caption,
        "fallback_used": False,  # No fallback in API-only mode
    }

