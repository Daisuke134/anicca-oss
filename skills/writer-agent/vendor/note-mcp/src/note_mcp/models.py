"""Pydantic data models for note-mcp.

This module defines all data models used throughout the note-mcp server,
including session management, article handling, and error types.
"""

from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel


class Session(BaseModel):
    """User authentication session.

    Stores authentication state including cookies, user information,
    and session expiration.

    Attributes:
        cookies: note.com authentication cookies (note_gql_auth_token, _note_session_v5)
        user_id: note.com user ID
        username: note.com username (used in URL paths)
        expires_at: Session expiration timestamp (Unix timestamp), None if no expiry
        created_at: Session creation timestamp (Unix timestamp)
    """

    cookies: dict[str, str]
    user_id: str
    username: str
    expires_at: int | None = None
    created_at: int

    def is_expired(self) -> bool:
        """Check if the session has expired.

        Returns:
            True if session has expired, False otherwise.
            Returns False if expires_at is None (no expiry set).
        """
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class ArticleStatus(str, Enum):
    """Article publication status."""

    DRAFT = "draft"
    PUBLISHED = "published"
    PRIVATE = "private"
    DELETED = "deleted"


class ImageType(str, Enum):
    """Image upload type.

    Determines which note.com API endpoint to use for image upload.
    """

    EYECATCH = "eyecatch"  # Header/eyecatch image (見出し画像)
    BODY = "body"  # Inline/body image (記事内埋め込み画像)


class Article(BaseModel):
    """A note.com article.

    Represents an article with all its metadata as stored on note.com.

    Attributes:
        id: Article ID (note.com internal ID)
        key: Article key (used in URL path)
        title: Article title
        body: Article body content (HTML format)
        status: Publication status
        tags: List of hashtags (without # prefix)
        eyecatch_image_key: Eyecatch image key (if set)
        prev_access_key: Preview access key for draft articles
        created_at: Creation timestamp (ISO 8601)
        updated_at: Last update timestamp (ISO 8601)
        published_at: Publication timestamp (ISO 8601)
        url: Full article URL
    """

    id: str
    key: str
    title: str
    body: str
    status: ArticleStatus
    tags: list[str] = []
    eyecatch_image_key: str | None = None
    prev_access_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None
    url: str | None = None


class ArticleInput(BaseModel):
    """Input data for creating or updating an article.

    Attributes:
        title: Article title
        body: Article body content (Markdown format)
        tags: List of hashtags (# prefix optional, will be normalized)
        eyecatch_image_path: Local path to eyecatch image (optional)
    """

    title: str
    body: str
    tags: list[str] = []
    eyecatch_image_path: str | None = None


class Image(BaseModel):
    """An uploaded image.

    Attributes:
        key: note.com image key (None for eyecatch images as API doesn't return it)
        url: Image URL on note.com
        original_path: Original local file path
        size_bytes: File size in bytes (optional)
        uploaded_at: Upload timestamp (Unix timestamp)
        image_type: Type of image (eyecatch or body)
    """

    key: str | None = None
    url: str
    original_path: str
    size_bytes: int | None = None
    uploaded_at: int
    image_type: ImageType = ImageType.EYECATCH


class Tag(BaseModel):
    """A hashtag for articles.

    Attributes:
        name: Tag name (without # prefix)
    """

    name: str

    @classmethod
    def normalize(cls, tag: str) -> str:
        """Normalize a tag by removing leading # characters.

        Args:
            tag: Tag string, possibly with # prefix

        Returns:
            Tag string without # prefix
        """
        return tag.lstrip("#")


class ArticleListResult(BaseModel):
    """Result of listing articles.

    Attributes:
        articles: List of articles
        total: Total number of articles matching the query
        page: Current page number (1-indexed)
        has_more: Whether there are more articles to fetch
    """

    articles: list[Article]
    total: int
    page: int
    has_more: bool


class BrowserArticleResult(BaseModel):
    """Result of browser-based article creation/update.

    Includes the article and optional TOC/alignment/embed/image insertion results for user notification.

    Attributes:
        article: The created/updated article
        toc_inserted: True if TOC was successfully inserted, False if failed, None if not attempted
        toc_error: Error message if TOC insertion failed
        alignments_applied: Number of text alignments successfully applied, None if not attempted
        alignment_error: Error message if text alignment application failed
        embeds_inserted: Number of embeds successfully inserted, None if not attempted
        embed_error: Error message if embed insertion failed
        images_inserted: Number of images successfully inserted, None if not attempted
        image_error: Error message if image insertion failed
        debug_info: Debug information for troubleshooting (temporary)
    """

    article: Article
    toc_inserted: bool | None = None
    toc_error: str | None = None
    alignments_applied: int | None = None
    alignment_error: str | None = None
    embeds_inserted: int | None = None
    embed_error: str | None = None
    images_inserted: int | None = None
    image_error: str | None = None
    debug_info: str | None = None


class ErrorCode(str, Enum):
    """Error codes for note-mcp API errors."""

    NOT_AUTHENTICATED = "not_authenticated"
    SESSION_EXPIRED = "session_expired"
    ARTICLE_NOT_FOUND = "article_not_found"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"
    UPLOAD_FAILED = "upload_failed"
    INVALID_INPUT = "invalid_input"


class NoteAPIError(Exception):
    """Exception for note.com API errors.

    Attributes:
        code: Error code
        message: Human-readable error message
        details: Additional error details
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            code: Error code from ErrorCode enum
            message: Human-readable error message
            details: Additional context (e.g., status code, response body)
        """
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class LoginError(Exception):
    """ログイン処理でのエラー。

    reCAPTCHA検出、2FA要求、認証情報エラー時に送出される。
    手動ログインへのフォールバックは行わず、明確なエラーで通知する。

    Attributes:
        code: エラーコード（RECAPTCHA_DETECTED, TWO_FACTOR_REQUIRED,
              INVALID_CREDENTIALS, LOGIN_TIMEOUT）
        message: エラーメッセージ
        resolution: 推奨される対処法
    """

    def __init__(
        self,
        code: str,
        message: str,
        resolution: str | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            code: エラーコード
            message: エラーメッセージ
            resolution: 推奨される対処法（オプション）
        """
        self.code = code
        self.message = message
        self.resolution = resolution
        super().__init__(message)


# =============================================================================
# Issue #141: Delete Draft Models
# =============================================================================

# Delete operation error messages (T020)
DELETE_ERROR_PUBLISHED_ARTICLE = "公開済み記事は削除できません。下書きのみ削除可能です。"
DELETE_ERROR_NO_ACCESS = "この記事へのアクセス権がありません。"
DELETE_ERROR_NOT_FOUND = "記事が見つかりません。キー: {article_key}"
DELETE_CONFIRM_REQUIRED = "削除を実行するには confirm=True を指定してください。"


class ArticleSummary(BaseModel):
    """Summary information of an article.

    Used in bulk delete preview/result to show article information
    without the full body content.

    Attributes:
        article_id: Article ID (note.com internal ID)
        article_key: Article key (used in URL path)
        title: Article title
    """

    article_id: str
    article_key: str
    title: str


class FailedArticle(BaseModel):
    """Information about a failed deletion.

    Used in BulkDeleteResult to provide details about articles
    that could not be deleted and why.

    Attributes:
        article_id: Article ID
        article_key: Article key
        title: Article title
        error: Error message explaining the failure
    """

    article_id: str
    article_key: str
    title: str
    error: str


class DeleteResult(BaseModel):
    """Result of a single delete operation.

    Returned when a delete operation completes (success or failure).

    Attributes:
        success: Whether the deletion was successful
        article_id: ID of the deleted article
        article_key: Key of the deleted article
        article_title: Title of the deleted article
        message: Result message for the user
    """

    success: bool
    article_id: str
    article_key: str
    article_title: str
    message: str


class DeletePreview(BaseModel):
    """Preview information before deletion.

    Returned when confirm=False to show what will be deleted
    and prompt for confirmation.

    Attributes:
        article_id: ID of the article to be deleted
        article_key: Key of the article to be deleted
        article_title: Title of the article to be deleted
        status: Current status of the article
        message: Confirmation prompt message
    """

    article_id: str
    article_key: str
    article_title: str
    status: ArticleStatus
    message: str


class BulkDeletePreview(BaseModel):
    """Preview information for bulk deletion.

    Returned when confirm=False to show all drafts that will be deleted.

    Attributes:
        total_count: Total number of drafts to be deleted
        articles: List of articles to be deleted
        message: Confirmation prompt message
    """

    total_count: int
    articles: list[ArticleSummary]
    message: str


class BulkDeleteResult(BaseModel):
    """Result of bulk delete operation.

    Provides detailed information about the bulk deletion,
    including counts and lists of successful/failed deletions.

    Attributes:
        success: Whether all deletions were successful
        total_count: Total number of articles targeted
        deleted_count: Number of successfully deleted articles
        failed_count: Number of failed deletions
        deleted_articles: List of successfully deleted articles
        failed_articles: List of articles that failed to delete
        message: Summary message
    """

    success: bool
    total_count: int
    deleted_count: int
    failed_count: int
    deleted_articles: list[ArticleSummary]
    failed_articles: list[FailedArticle]
    message: str


class DeleteDraftInput(BaseModel):
    """Input for note_delete_draft MCP tool.

    Attributes:
        article_key: Key of the article to delete (format: nXXXXXXXXXXXX)
        confirm: Confirmation flag (must be True to execute deletion)
    """

    article_key: str
    confirm: bool = False


class DeleteAllDraftsInput(BaseModel):
    """Input for note_delete_all_drafts MCP tool.

    Attributes:
        confirm: Confirmation flag (must be True to execute deletion)
    """

    confirm: bool = False


def from_api_response(data: dict[str, object]) -> Article:
    """Create an Article from note.com API response.

    Article 6 (Data Accuracy Mandate) compliant:
    - Required fields (id, key, status) must be present, no implicit fallbacks
    - Missing required fields raise NoteAPIError

    Args:
        data: Raw API response dictionary

    Returns:
        Article instance

    Raises:
        NoteAPIError: If required fields (id, key, status) are missing or invalid
    """
    # Article 6: Validate required field 'id' - no fallback
    article_id = data.get("id")
    if not article_id:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="API response missing required field: id",
            details={"response": data},
        )

    # Article 6: Validate required field 'key' - no fallback
    article_key = data.get("key")
    if not article_key:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="API response missing required field: key",
            details={"response": data},
        )

    # Article 6: Validate required field 'status' - no fallback or guessing
    status_str = data.get("status")
    if not isinstance(status_str, str) or not status_str:
        raise NoteAPIError(
            code=ErrorCode.API_ERROR,
            message="API response missing or invalid required field: status",
            details={"response": data, "status_value": status_str},
        )

    # Extract hashtag names from the hashtags array
    # Empty hashtags list is valid - this is not a fallback
    hashtags = data.get("hashtags", [])
    tags: list[str] = []
    if isinstance(hashtags, list):
        for ht in hashtags:
            if isinstance(ht, dict):
                hashtag_obj = ht.get("hashtag", {})
                if isinstance(hashtag_obj, dict):
                    # Skip hashtags without name - no fallback to empty string
                    name = hashtag_obj.get("name")
                    if name:
                        tags.append(str(name))

    # Extract title: use "name" field, fallback to "noteDraft.name" for drafts
    title = data.get("name")
    if not title:
        note_draft = data.get("noteDraft")
        if isinstance(note_draft, dict):
            title = note_draft.get("name")
    title_str = str(title) if title else ""

    # body can be empty string - this is a valid value, not a missing field
    body = data.get("body")
    body_str = str(body) if body is not None else ""

    return Article(
        id=str(article_id),
        key=str(article_key),
        title=title_str,
        body=body_str,
        status=ArticleStatus(status_str),
        tags=tags,
        eyecatch_image_key=str(data.get("eyecatch_image_key")) if data.get("eyecatch_image_key") else None,
        prev_access_key=str(data.get("prev_access_key")) if data.get("prev_access_key") else None,
        created_at=str(data.get("created_at")) if data.get("created_at") else None,
        updated_at=str(data.get("updated_at")) if data.get("updated_at") else None,
        published_at=str(data.get("publish_at")) if data.get("publish_at") else None,
        url=str(data.get("noteUrl")) if data.get("noteUrl") else None,
    )


def to_api_request(article_input: ArticleInput, html_body: str) -> dict[str, object]:
    """Convert ArticleInput to note.com API request format.

    Args:
        article_input: Input data from user
        html_body: HTML-converted body content

    Returns:
        Dictionary suitable for note.com API request
    """
    return {
        "name": article_input.title,
        "body": html_body,
        "status": "draft",
    }

