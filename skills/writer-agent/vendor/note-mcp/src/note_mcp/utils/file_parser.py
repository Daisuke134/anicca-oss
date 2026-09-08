"""Markdown file parser for note.com article creation.

Parses Markdown files with YAML frontmatter support,
extracts titles from headings, and detects local images.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LocalImage:
    """Represents a local image found in Markdown content.

    Attributes:
        markdown_path: The path as written in Markdown (e.g., ./images/test.png)
        absolute_path: The resolved absolute path to the image file
        alt_text: The alt text for the image (empty string if not provided)
    """

    markdown_path: str
    absolute_path: Path
    alt_text: str = ""


@dataclass
class ParsedArticle:
    """Represents a parsed Markdown article.

    Attributes:
        title: The article title (from frontmatter or heading)
        body: The article body content (Markdown)
        tags: List of tags for the article
        local_images: List of local images detected in the content
        source_path: Path to the source Markdown file
        eyecatch: Path to the eyecatch (header) image, if specified
    """

    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    local_images: list[LocalImage] = field(default_factory=list)
    source_path: Path | None = None
    eyecatch: Path | None = None


# Pattern to match YAML frontmatter (must start at beginning of file)
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

# Pattern to match Markdown images: ![alt](path)
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Patterns that indicate a URL (not a local file)
URL_PREFIXES = ("http://", "https://", "data:")

# Patterns to match H1 and H2 title headings
_H1_TITLE_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2_TITLE_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def parse_markdown_file(file_path: Path | str) -> ParsedArticle:
    """Parse a Markdown file and extract article components.

    The function extracts:
    - Title: From YAML frontmatter, or first H1, or first H2 as fallback
    - Tags: From YAML frontmatter (optional)
    - Body: The Markdown content (frontmatter stripped, title heading removed)
    - Local images: Paths to local image files (not URLs)
    - Eyecatch: Path to eyecatch image from YAML frontmatter (optional)

    Args:
        file_path: Path to the Markdown file (str or Path)

    Returns:
        ParsedArticle with extracted components

    Raises:
        FileNotFoundError: If the file does not exist
        ValueError: If no title can be extracted
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path

    if not path.exists():
        raise FileNotFoundError(f"ファイルが見つかりません: {path}")

    content = path.read_text(encoding="utf-8")

    # Try to extract YAML frontmatter
    frontmatter_data = _extract_frontmatter(content)
    body = _strip_frontmatter(content)

    # Get title from frontmatter or headings
    title = _get_title(frontmatter_data, body)
    if not title:
        raise ValueError(f"タイトルが見つかりません: {path}")

    # If title came from heading (not frontmatter), remove it from body
    if not _has_frontmatter_title(frontmatter_data):
        body = _remove_title_heading(body, title)

    # Get tags from frontmatter
    tags = _get_tags(frontmatter_data)

    # Get eyecatch image path from frontmatter
    eyecatch = _get_eyecatch(frontmatter_data, path.parent)

    # Detect local images
    local_images = _detect_local_images(body, path.parent)

    # Normalize body whitespace
    body = body.strip()

    return ParsedArticle(
        title=title,
        body=body,
        tags=tags,
        local_images=local_images,
        source_path=path,
        eyecatch=eyecatch,
    )


def _extract_frontmatter(content: str) -> dict[str, Any]:
    """Extract YAML frontmatter from content.

    Args:
        content: The full file content

    Returns:
        Parsed YAML as dict, or empty dict if no valid frontmatter
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}

    yaml_content = match.group(1)
    try:
        data = yaml.safe_load(yaml_content)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from content.

    Args:
        content: The full file content

    Returns:
        Content with frontmatter removed
    """
    match = FRONTMATTER_PATTERN.match(content)
    if match:
        return content[match.end() :]
    return content


def _has_frontmatter_title(frontmatter: dict[str, Any]) -> bool:
    """Check if frontmatter has a non-empty title.

    Args:
        frontmatter: Parsed YAML frontmatter

    Returns:
        True if frontmatter has a non-empty title
    """
    title = frontmatter.get("title", "")
    return bool(title and str(title).strip())


def _get_title(frontmatter: dict[str, Any], body: str) -> str | None:
    """Extract title from frontmatter or body headings.

    Priority:
    1. YAML frontmatter 'title' field (if non-empty)
    2. First H1 heading
    3. First H2 heading

    Args:
        frontmatter: Parsed YAML frontmatter
        body: The body content (frontmatter stripped)

    Returns:
        The extracted title, or None if not found
    """
    # Try frontmatter first
    if _has_frontmatter_title(frontmatter):
        return str(frontmatter["title"]).strip()

    # Try H1 heading
    h1_match = _H1_TITLE_PATTERN.search(body)
    if h1_match:
        return h1_match.group(1).strip()

    # Try H2 heading as fallback
    h2_match = _H2_TITLE_PATTERN.search(body)
    if h2_match:
        return h2_match.group(1).strip()

    return None


def _remove_title_heading(body: str, title: str) -> str:
    """Remove the title heading from body content.

    Removes the first H1 or H2 heading that matches the title.

    Args:
        body: The body content
        title: The title that was extracted

    Returns:
        Body with the title heading removed
    """
    # Escape special regex characters in title
    escaped_title = re.escape(title)

    # Try to remove H1 first
    h1_pattern = re.compile(rf"^#\s+{escaped_title}\s*\n?", re.MULTILINE)
    new_body, count = h1_pattern.subn("", body, count=1)
    if count > 0:
        return new_body

    # Try to remove H2
    h2_pattern = re.compile(rf"^##\s+{escaped_title}\s*\n?", re.MULTILINE)
    new_body, count = h2_pattern.subn("", body, count=1)
    if count > 0:
        return new_body

    return body


def _get_tags(frontmatter: dict[str, Any]) -> list[str]:
    """Extract tags from frontmatter.

    Handles both list and single string formats.

    Args:
        frontmatter: Parsed YAML frontmatter

    Returns:
        List of tags (empty list if none)
    """
    tags = frontmatter.get("tags", [])

    if isinstance(tags, str):
        return [tags] if tags.strip() else []

    if isinstance(tags, list):
        return [str(tag).strip() for tag in tags if tag]

    return []


def _get_eyecatch(frontmatter: dict[str, Any], base_dir: Path) -> Path | None:
    """Extract eyecatch image path from frontmatter.

    Args:
        frontmatter: Parsed YAML frontmatter
        base_dir: Base directory for resolving relative paths

    Returns:
        Resolved absolute path to eyecatch image, or None if not specified
    """
    eyecatch = frontmatter.get("eyecatch", "")

    if not eyecatch or not str(eyecatch).strip():
        return None

    eyecatch_str = str(eyecatch).strip()
    return (base_dir / eyecatch_str).resolve()


def _detect_local_images(body: str, base_dir: Path) -> list[LocalImage]:
    """Detect local image references in Markdown content.

    Finds all image references that are local files (not URLs).

    Args:
        body: The Markdown body content
        base_dir: Base directory for resolving relative paths

    Returns:
        List of LocalImage objects
    """
    images: list[LocalImage] = []

    for match in IMAGE_PATTERN.finditer(body):
        alt_text = match.group(1)
        image_path = match.group(2)

        # Skip URLs and data URIs
        if any(image_path.startswith(prefix) for prefix in URL_PREFIXES):
            continue

        # Resolve the absolute path
        absolute_path = (base_dir / image_path).resolve()

        images.append(
            LocalImage(
                markdown_path=image_path,
                absolute_path=absolute_path,
                alt_text=alt_text,
            )
        )

    return images

