"""Markdown to HTML conversion utility.

Uses markdown-it-py for CommonMark-compliant conversion.
"""

import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from markdown_it import MarkdownIt

from note_mcp.api.embeds import (
    generate_embed_html,
    get_embed_service,
)

# Pre-compiled regex patterns for performance
# TOC pattern: [TOC] must be alone on a line
_TOC_PATTERN = re.compile(r"^\[TOC\]$", re.MULTILINE)
# TOC placeholder (text marker, not HTML comment)
# Must match TOC_PLACEHOLDER in toc_helpers.py
_TOC_PLACEHOLDER = "§§TOC§§"
# Pattern to match TOC placeholder wrapped in paragraph tags (after Markdown conversion)
_TOC_PLACEHOLDER_HTML_PATTERN = re.compile(
    r'<p\s+name="([^"]+)"\s+id="([^"]+)">' + re.escape(_TOC_PLACEHOLDER) + r"</p>",
    re.IGNORECASE,
)

# Note: <li> and <blockquote> are excluded because note.com doesn't add name/id to these tags
_TAG_PATTERN = re.compile(
    r"<(p|h[1-6]|ul|ol|code|hr|div|span)(\s[^>]*)?>",
    re.IGNORECASE,
)
_PRE_PATTERN = re.compile(r"<pre([^>]*)>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
_IMG_IN_P_PATTERN = re.compile(
    r'<p>\s*<img\s+src="([^"]+)"\s+alt="([^"]*)"(?:\s+title="([^"]*)")?\s*/?\s*>\s*</p>',
    re.IGNORECASE,
)
_LANGUAGE_CLASS_PATTERN = re.compile(r'<code[^>]*class="language-[^"]*"[^>]*>')
# Pattern to match li elements with direct text content (not already wrapped in p)
# This matches <li ...>content</li> where content doesn't start with <p
_LI_CONTENT_PATTERN = re.compile(
    r"(<li[^>]*>)(?!<p)([^<]+|(?:(?!</li>).)*?)(</li>)",
    re.IGNORECASE | re.DOTALL,
)
# Pattern to match blockquote elements and their content
_BLOCKQUOTE_PATTERN = re.compile(
    r"(<blockquote[^>]*>)(.*?)(</blockquote>)",
    re.DOTALL | re.IGNORECASE,
)
# Pattern to detect citation line: em-dash followed by space at start of line or after <br>
# Matches: "— Text" or "<br>— Text" at the end of content
_CITATION_PATTERN = re.compile(
    r"(?:^|<br>)(—\s+.+?)(?:</p>|$)",
    re.IGNORECASE,
)
# Pattern to extract URL from citation: "Text (URL)"
_CITATION_URL_PATTERN = re.compile(r"^(.+?)\s+\((\S+)\)\s*$")

# Text alignment patterns (Issue #40)
# Center: ->text<- (must be at start of line, ends with <- at end of line)
# Right: ->text (must be at start of line, no closing marker)
# Left: <-text (must be at start of line)
# Order matters: center must be checked before right to avoid partial matches
_TEXT_ALIGN_CENTER_PATTERN = re.compile(r"^->(.+)<-$", re.MULTILINE)
_TEXT_ALIGN_RIGHT_PATTERN = re.compile(r"^->(.+)$", re.MULTILINE)
_TEXT_ALIGN_LEFT_PATTERN = re.compile(r"^<-(.+)$", re.MULTILINE)

# Pattern to find URLs that are alone on a line (potential embed URLs)
_STANDALONE_URL_PATTERN = re.compile(r"^(https?://\S+)$", re.MULTILINE)

# Stock notation patterns (Issue #216)
# Japanese stocks: ^5243 (4-5 digit security code) - must be alone on a line
_STOCK_JP_PATTERN = re.compile(r"^\^(\d{4,5})$", re.MULTILINE)
# US stocks: $GOOG (uppercase ticker) - must be alone on a line
_STOCK_US_PATTERN = re.compile(r"^\$([A-Z]+)$", re.MULTILINE)

# Pattern to match paragraphs containing alignment placeholders (for html_transformer)
_ALIGN_P_PATTERN = re.compile(
    r"<p([^>]*)>§§ALIGN_(CENTER|RIGHT|LEFT)§§(.*?)§§/ALIGN§§</p>",
    re.DOTALL | re.IGNORECASE,
)

# Pattern to match <p> content inside blockquotes (for html_transformer)
_P_IN_BLOCKQUOTE_PATTERN = re.compile(
    r"(<blockquote[^>]*>.*?)(<p[^>]*>)(.*?)(</p>)(.*?</blockquote>)",
    re.DOTALL | re.IGNORECASE,
)

# Pattern to match blockquote elements for figure wrapping (for html_transformer)
_BLOCKQUOTE_FIGURE_PATTERN = re.compile(
    r"<blockquote[^>]*>(.*?)</blockquote>",
    re.DOTALL | re.IGNORECASE,
)


@contextmanager
def _protect_code_blocks(content: str, prefix: str = "CODE_BLOCK") -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Context manager for protecting code blocks during content processing.

    Temporarily replaces fenced (```) and inline (`) code blocks with placeholders.
    This prevents code block content from being processed by other transformations.

    Args:
        content: Content to protect code blocks in.
        prefix: Prefix for placeholder names. Use unique prefixes to avoid conflicts.

    Yields:
        Tuple of (protected_content, code_blocks_list) where:
        - protected_content: Content with code blocks replaced by placeholders
        - code_blocks_list: List of (placeholder, original_code) tuples for restoration

    Example:
        with _protect_code_blocks(content, "ALIGN") as (protected, blocks):
            # Process the protected content (reassign to protected)
            protected = some_pattern.sub(replacement, protected)
        return _restore_code_blocks(protected, blocks)
    """
    code_blocks: list[tuple[str, str]] = []

    def protect(match: re.Match[str]) -> str:
        placeholder = f"__{prefix}_{len(code_blocks)}__"
        code_blocks.append((placeholder, match.group(0)))
        return placeholder

    # Protect fenced code blocks first (```)
    protected = re.sub(r"```[\s\S]*?```", protect, content)
    # Then protect inline code (`)
    protected = re.sub(r"`[^`]+`", protect, protected)

    yield protected, code_blocks


def html_transformer(
    pattern: re.Pattern[str],
    transformer: Callable[[re.Match[str]], str],
) -> Callable[[str], str]:
    """Create an HTML transformation function.

    This Higher-Order Function reduces code duplication for pattern-based
    HTML transformations that follow the pattern:
        result = pattern.sub(transform_func, html)

    Args:
        pattern: Compiled regex pattern to match.
        transformer: Function that takes a Match object and returns replacement string.

    Returns:
        Function that applies the transformation to HTML content.

    Example:
        >>> pattern = re.compile(r"<em>(.*?)</em>")
        >>> strong_transformer = html_transformer(pattern, lambda m: f"<strong>{m.group(1)}</strong>")
        >>> strong_transformer("<em>text</em>")
        '<strong>text</strong>'
    """

    def transform(html: str) -> str:
        return pattern.sub(transformer, html)

    return transform


def _restore_code_blocks(content: str, blocks: list[tuple[str, str]]) -> str:
    """Restore code blocks from placeholders.

    Args:
        content: Content with placeholders.
        blocks: List of (placeholder, original) tuples from _protect_code_blocks.

    Returns:
        Content with placeholders replaced by original code blocks.
    """
    for placeholder, original in blocks:
        content = content.replace(placeholder, original)
    return content


def protected_content_transformer(
    prefix: str,
) -> Callable[[Callable[[str], str]], Callable[[str], str]]:
    """Create a decorator that protects code blocks during transformation.

    This Higher-Order Function reduces code duplication for pattern-based
    Markdown transformations that need to preserve code blocks.

    The decorator:
    1. Protects fenced (```) and inline (`) code blocks with placeholders
    2. Applies the transformation function
    3. Restores the original code blocks

    Args:
        prefix: Unique prefix for placeholders (e.g., "STOCK", "ALIGN", "TOC").
                Use unique prefixes to avoid conflicts between transformations.

    Returns:
        Decorator function that wraps a transformation function.

    Example:
        >>> @protected_content_transformer("STOCK")
        ... def convert_stock(content: str) -> str:
        ...     return pattern.sub(replacement, content)
        >>> convert_stock("text with `^5243` in code")  # code block preserved
    """
    import functools

    def decorator(transform: Callable[[str], str]) -> Callable[[str], str]:
        @functools.wraps(transform)
        def wrapper(content: str) -> str:
            with _protect_code_blocks(content, prefix) as (protected, blocks):
                result = transform(protected)
            return _restore_code_blocks(result, blocks)

        return wrapper

    return decorator


def has_embed_url(content: str) -> bool:
    """Check if content contains URLs that should be embedded.

    Detects YouTube, Twitter/X, note.com article, GitHub Gist, GitHub Repository,
    noteマネー, Zenn.dev, Google Slides, and SpeakerDeck URLs that appear alone on
    a line (indicating they should be embedded, not linked).

    Uses get_embed_service() for URL detection (Issue #235: DRY principle).

    Args:
        content: Markdown content to check.

    Returns:
        True if content contains embed-worthy URLs.
    """
    # Find all standalone URLs (URLs alone on their own line)
    for match in _STANDALONE_URL_PATTERN.finditer(content):
        url = match.group(1)
        # Check if this URL matches any embed pattern (using api.embeds patterns)
        if get_embed_service(url) is not None:
            return True
    return False


# Pattern to match standalone embed URLs in HTML paragraphs
# Matches: <p name="..." id="...">https://youtube.com/watch?v=xxx</p>
# Uses negative lookbehind to exclude paragraphs inside list items
_STANDALONE_EMBED_URL_IN_HTML_PATTERN = re.compile(
    r'(?<!<li>)<p\s+name="[^"]+"\s+id="[^"]+">(\s*)(https?://\S+?)(\s*)</p>',
    re.IGNORECASE,
)


def _convert_standalone_embed_urls(html: str) -> str:
    """Convert standalone embed URLs to figure elements.

    Detects standalone URLs (URLs that are alone in a paragraph) and converts
    supported embed URLs (YouTube, Twitter, note.com, GitHub Gist, GitHub Repository,
    noteマネー, Zenn.dev, Google Slides, SpeakerDeck) to figure elements.

    This function should be called after markdown conversion and UUID addition,
    but before code block processing.

    Args:
        html: HTML content with paragraphs containing potential embed URLs.

    Returns:
        HTML with embed URLs converted to figure elements.
    """

    def replace_embed_url(match: re.Match[str]) -> str:
        url = match.group(2).strip()

        # Check if this URL is a supported embed URL
        service = get_embed_service(url)
        if service is None:
            # Not an embed URL, keep original paragraph
            return match.group(0)

        # Generate embed HTML
        return generate_embed_html(url, service)

    return _STANDALONE_EMBED_URL_IN_HTML_PATTERN.sub(replace_embed_url, html)


def _generate_uuid() -> str:
    """Generate a UUID for note.com element IDs."""
    return str(uuid.uuid4())


def _extract_citation(blockquote_content: str) -> tuple[str, str]:
    """Extract citation from blockquote content.

    Detects citation lines starting with em-dash (—) followed by space.
    Supports optional URL in parentheses: "— Source (https://example.com)"

    Args:
        blockquote_content: HTML content inside <blockquote> tags

    Returns:
        Tuple of (modified_content, figcaption_html):
        - modified_content: blockquote content with citation line removed
        - figcaption_html: HTML for figcaption element content (may be empty)

    Examples:
        >>> _extract_citation("<p>Quote<br>— Source</p>")
        ('<p>Quote</p>', 'Source')
        >>> _extract_citation("<p>Quote<br>— Source (https://example.com)</p>")
        ('<p>Quote</p>', '<a href="https://example.com">Source</a>')
    """
    # Look for citation pattern: "<br>— text" or "— text" at end of content
    # The pattern searches within <p> tags
    match = _CITATION_PATTERN.search(blockquote_content)
    if not match:
        return blockquote_content, ""

    citation_with_dash = match.group(1)  # "— Text" or "— Text (URL)"
    citation_text = citation_with_dash[2:].strip()  # Remove "— " prefix

    # Empty citation text
    if not citation_text:
        return blockquote_content, ""

    # Remove the citation line from blockquote content
    # Handle both "<br>— text" and standalone "— text"
    full_match = match.group(0)
    modified_content = blockquote_content.replace(full_match, "</p>")

    # Check for URL pattern: "Text (URL)"
    url_match = _CITATION_URL_PATTERN.match(citation_text)
    if url_match:
        text = url_match.group(1).strip()
        url = url_match.group(2)
        figcaption_html = f'<a href="{url}">{text}</a>'
    else:
        figcaption_html = citation_text

    return modified_content, figcaption_html


@protected_content_transformer("STOCK")
def _convert_stock_notation(content: str) -> str:
    """Convert stock notation to noteマネー URLs.

    Converts stock notation markers BEFORE markdown conversion:
    - ^5243 (Japanese stock) → https://money.note.com/companies/5243
    - $GOOG (US stock) → https://money.note.com/us-companies/GOOG

    Only converts notations that are alone on a line.
    Code blocks are protected from conversion via decorator.

    Issue #216: Support stock chart embedding via notation.

    Args:
        content: Markdown content with stock notations

    Returns:
        Content with stock notations converted to URLs
    """
    # Japanese stocks: ^5243 → https://money.note.com/companies/5243
    content = _STOCK_JP_PATTERN.sub(r"https://money.note.com/companies/\1", content)
    # US stocks: $GOOG → https://money.note.com/us-companies/GOOG
    content = _STOCK_US_PATTERN.sub(r"https://money.note.com/us-companies/\1", content)
    return content


@protected_content_transformer("ALIGN")
def _convert_text_alignment(content: str) -> str:
    """Convert text alignment Markdown notation to internal placeholders.

    This function processes text alignment markers BEFORE markdown conversion.
    It converts the custom notation to placeholders that will be converted
    to proper HTML after markdown processing.

    Code blocks are protected from conversion via decorator.

    Notation:
        ->text<- : center alignment
        ->text   : right alignment
        <-text   : left alignment

    Args:
        content: Markdown content with alignment markers

    Returns:
        Content with alignment markers converted to placeholders
    """
    # Convert alignment markers to placeholders
    # Order matters: center first (more specific), then right/left
    content = _TEXT_ALIGN_CENTER_PATTERN.sub(r"§§ALIGN_CENTER§§\1§§/ALIGN§§", content)
    content = _TEXT_ALIGN_RIGHT_PATTERN.sub(r"§§ALIGN_RIGHT§§\1§§/ALIGN§§", content)
    content = _TEXT_ALIGN_LEFT_PATTERN.sub(r"§§ALIGN_LEFT§§\1§§/ALIGN§§", content)
    return content


def _apply_alignment(match: re.Match[str]) -> str:
    """Transform alignment placeholder to styled paragraph.

    Args:
        match: Regex match with groups:
            - group(1): HTML attributes (e.g., ' name="..." id="..."')
            - group(2): Alignment type (CENTER, RIGHT, or LEFT)
            - group(3): Paragraph content

    Returns:
        HTML paragraph with text-align style applied.
    """
    attrs = match.group(1)
    alignment = match.group(2).lower()
    content = match.group(3)

    # Add style attribute for text-align
    style = f"text-align: {alignment}"

    # If there are existing attributes, append style
    if attrs and 'style="' in attrs:
        # Append to existing style (unlikely but handle it)
        attrs = attrs.replace('style="', f'style="{style}; ')
    else:
        # Add new style attribute before other attrs
        attrs = f' style="{style}"' + (attrs or "")

    return f"<p{attrs}>{content}</p>"


_apply_text_alignment_to_html = html_transformer(_ALIGN_P_PATTERN, _apply_alignment)
"""Convert text alignment placeholders to HTML style attributes.

This function processes the alignment placeholders created by
_convert_text_alignment and converts them to proper HTML paragraphs
with text-align styles.

Args:
    html: HTML content with alignment placeholders

Returns:
    HTML with proper text-align styles applied
"""


def _wrap_li_content_in_p(html: str) -> str:
    """Wrap list item content in paragraph tags.

    ProseMirror (used by note.com) expects list items to contain
    block content like paragraphs, not just inline text.

    Converts: <li>Item text</li>
    To: <li><p>Item text</p></li>

    Args:
        html: HTML string with list items

    Returns:
        HTML with list item content wrapped in <p> tags
    """

    def wrap_content(match: re.Match[str]) -> str:
        li_open = match.group(1)  # <li ...>
        content = match.group(2)  # text content
        li_close = match.group(3)  # </li>

        # Skip if content is empty or whitespace only
        if not content or not content.strip():
            return match.group(0)

        return f"{li_open}<p>{content.strip()}</p>{li_close}"

    return _LI_CONTENT_PATTERN.sub(wrap_content, html)


def _convert_p_newlines(match: re.Match[str]) -> str:
    """Transform paragraph newlines to <br> tags inside blockquotes.

    Args:
        match: Regex match with groups:
            - group(1): Content before <p> tag (including <blockquote>)
            - group(2): Opening <p> tag (e.g., '<p name="..." id="...">')
            - group(3): Paragraph content (text inside <p>)
            - group(4): Closing </p> tag
            - group(5): Content after </p> tag (including </blockquote>)

    Returns:
        Blockquote HTML with newlines converted to <br> tags.
    """
    before_p = match.group(1)  # <blockquote...> and anything before <p>
    p_open = match.group(2)  # <p ...>
    p_content = match.group(3)  # content inside <p>
    p_close = match.group(4)  # </p>
    after_p = match.group(5)  # anything after </p> including </blockquote>

    # Convert newlines to <br> tags (note.com uses <br> without slash)
    p_content = p_content.replace("\n", "<br>")

    return f"{before_p}{p_open}{p_content}{p_close}{after_p}"


_convert_blockquote_newlines_to_br = html_transformer(_P_IN_BLOCKQUOTE_PATTERN, _convert_p_newlines)
"""Convert newlines inside blockquote paragraphs to <br> tags.

note.com's browser editor uses <br> tags for line breaks inside blockquotes.
This function converts newlines to <br> tags to match that format.

Converts:
    <blockquote><p>Line 1
    Line 2</p></blockquote>
To:
    <blockquote><p>Line 1<br>Line 2</p></blockquote>

Note: While this generates correct HTML with <br> tags, note.com's API
sanitizes <br> tags from blockquote content. This is a server-side
limitation. Content created via browser editor preserves <br> tags,
but API-submitted content has them stripped.

Workaround for users: Use separate blockquotes for each line:
    > Line 1

    > Line 2

Args:
    html: HTML string with blockquotes

Returns:
    HTML with blockquote paragraph newlines converted to <br> tags
"""


def _wrap_in_figure(match: re.Match[str]) -> str:
    """Transform blockquote to note.com figure format.

    Args:
        match: Regex match with groups:
            - group(1): Blockquote inner content (HTML between <blockquote> tags)

    Returns:
        Blockquote wrapped in figure element with citation extracted.
    """
    blockquote_content = match.group(1)
    element_id = _generate_uuid()

    # Extract citation if present
    modified_content, figcaption_html = _extract_citation(blockquote_content)

    return (
        f'<figure name="{element_id}" id="{element_id}">'
        f"<blockquote>{modified_content}</blockquote>"
        f"<figcaption>{figcaption_html}</figcaption></figure>"
    )


_convert_blockquotes_to_note_format = html_transformer(_BLOCKQUOTE_FIGURE_PATTERN, _wrap_in_figure)
"""Convert blockquotes to note.com figure format.

note.com expects blockquotes to be wrapped in <figure> elements:
<figure name="UUID" id="UUID">
  <blockquote><p name="UUID" id="UUID">content</p></blockquote>
  <figcaption>citation</figcaption>
</figure>

Citation is extracted from lines starting with em-dash (—):
- "— Source" becomes <figcaption>Source</figcaption>
- "— Source (URL)" becomes <figcaption><a href="URL">Source</a></figcaption>

This format is required for the API to preserve <br> tags inside blockquotes.

Args:
    html: HTML string with blockquotes

Returns:
    HTML with blockquotes wrapped in figure elements
"""


def _add_uuid_to_elements(html: str) -> str:
    """Add name attribute (UUID) to HTML elements.

    note.com requires elements to have unique name attributes.
    Note: <pre> tags are handled separately by _convert_code_blocks_to_note_format.
    Note: <li> tags are excluded because note.com doesn't add name to <li> tags.

    Args:
        html: HTML string

    Returns:
        HTML with name attributes added to elements
    """

    def add_uuid(match: re.Match[str]) -> str:
        tag_name = match.group(1)
        attrs = match.group(2) or ""

        # Skip if already has name attribute
        if 'name="' in attrs:
            return match.group(0)

        element_id = _generate_uuid()
        # note.com requires both 'name' and 'id' attributes for proper content handling
        return f'<{tag_name} name="{element_id}" id="{element_id}"{attrs}>'

    return _TAG_PATTERN.sub(add_uuid, html)


def _convert_images_to_note_format(html: str) -> str:
    """Convert standard HTML img tags to note.com figure format.

    note.com expects images in this format:
    <figure name="UUID" id="UUID">
      <img src="URL" alt="" width="620" height="457"
           contenteditable="false" draggable="false">
      <figcaption></figcaption>
    </figure>

    Args:
        html: HTML string with standard img tags

    Returns:
        HTML with img tags converted to figure format
    """

    def replace_img(match: re.Match[str]) -> str:
        src = match.group(1)
        alt = match.group(2)
        caption = match.group(3) or ""  # titleがなければ空文字
        element_id = _generate_uuid()
        # note.com requires both 'name' and 'id' attributes for proper content handling
        return (
            f'<figure name="{element_id}" id="{element_id}">'
            f'<img src="{src}" alt="{alt}" width="620" height="457" '
            f'contenteditable="false" draggable="false">'
            f"<figcaption>{caption}</figcaption></figure>"
        )

    return _IMG_IN_P_PATTERN.sub(replace_img, html)


def _convert_code_blocks_to_note_format(html: str) -> str:
    """Convert code blocks to note.com format and handle newlines.

    note.com requires:
    - <pre class="codeBlock"> with name and id attributes
    - <code> without language class
    - Actual newlines preserved inside code blocks
    - Newlines removed from other HTML elements

    Uses placeholder approach to preserve newlines in code blocks
    while removing them from the rest of the HTML.

    Args:
        html: HTML string with code blocks

    Returns:
        HTML with code blocks in note.com format
    """
    pre_blocks: list[str] = []

    def convert_pre_block(match: re.Match[str]) -> str:
        """Convert pre block to note.com format and store for later restoration."""
        content = match.group(2)

        # Generate fresh UUIDs for code blocks
        element_id = _generate_uuid()

        # Remove language class from <code> tag
        # markdown-it-py adds class="language-xxx" which note.com doesn't use
        content = _LANGUAGE_CLASS_PATTERN.sub("<code>", content)

        # Build note.com format: <pre name="..." id="..." class="codeBlock">
        # note.com requires both 'name' and 'id' attributes for proper content handling
        pre_block = f'<pre name="{element_id}" id="{element_id}" class="codeBlock">{content}</pre>'
        pre_blocks.append(pre_block)

        return f"__PRE_BLOCK_{len(pre_blocks) - 1}__"

    # Replace pre blocks with placeholders
    result = _PRE_PATTERN.sub(convert_pre_block, html)

    # Remove newlines from the rest of the HTML
    result = result.replace("\n", "")

    # Restore pre blocks with their preserved newlines
    for i, block in enumerate(pre_blocks):
        result = result.replace(f"__PRE_BLOCK_{i}__", block)

    return result


def _has_toc_placeholder(content: str) -> bool:
    """Check if content contains [TOC] placeholder.

    Args:
        content: Markdown content to check.

    Returns:
        True if [TOC] placeholder exists on its own line.
    """
    return bool(_TOC_PATTERN.search(content))


def _replace_toc_markers(content: str) -> str:
    """Replace [TOC] markers with placeholder (first only, rest removed).

    Internal function that handles the stateful replacement logic.
    Only the first [TOC] is converted to placeholder, subsequent ones are removed.

    Args:
        content: Content with potential [TOC] markers.

    Returns:
        Content with [TOC] markers processed.
    """
    first_replaced = False

    def replace_toc(match: re.Match[str]) -> str:
        nonlocal first_replaced
        if not first_replaced:
            first_replaced = True
            return _TOC_PLACEHOLDER
        return ""  # Remove subsequent [TOC]s

    return _TOC_PATTERN.sub(replace_toc, content)


@protected_content_transformer("TOC")
def _convert_toc_to_placeholder(content: str) -> str:
    """Convert first [TOC] to HTML placeholder.

    Only the first [TOC] is converted. Subsequent ones are removed.
    [TOC] inside code blocks is not processed via decorator.

    Args:
        content: Markdown content with potential [TOC] markers.

    Returns:
        Content with [TOC] converted to placeholder.
    """
    return _replace_toc_markers(content)


def _convert_toc_placeholder_to_html(html: str) -> str:
    """Convert TOC placeholder in HTML to <table-of-contents> element.

    Replaces <p name="..." id="...">§§TOC§§</p> with
    <table-of-contents name="..." id="..."></table-of-contents>

    This is called after markdown conversion and UUID addition to convert
    the placeholder to the actual custom element that note.com preserves via API.

    Issue #117: This enables TOC via API without browser automation.

    Args:
        html: HTML content with potential TOC placeholder

    Returns:
        HTML with TOC placeholder converted to <table-of-contents> element
    """

    def replace_with_toc(match: re.Match[str]) -> str:
        name = match.group(1)
        element_id = match.group(2)
        return f'<table-of-contents name="{name}" id="{element_id}"></table-of-contents>'

    return _TOC_PLACEHOLDER_HTML_PATTERN.sub(replace_with_toc, html)


def markdown_to_html(content: str) -> str:
    """Convert Markdown content to HTML.

    Uses markdown-it-py for CommonMark-compliant conversion.
    Converts images to note.com's figure format.

    Args:
        content: Markdown formatted text

    Returns:
        HTML formatted text. Returns empty string for empty input.

    Example:
        >>> markdown_to_html("# Hello")
        '<h1>Hello</h1>\\n'
    """
    if not content or not content.strip():
        return ""

    # 1. Convert [TOC] to placeholder FIRST (before any processing)
    content = _convert_toc_to_placeholder(content)

    # 2. Convert stock notation to URLs (Issue #216)
    # Must be before markdown conversion so URLs can be processed as embeds
    content = _convert_stock_notation(content)

    # 3. Convert text alignment markers to placeholders BEFORE markdown conversion
    content = _convert_text_alignment(content)

    # 4. Markdown conversion
    md = MarkdownIt().enable("strikethrough")
    result: str = md.render(content)

    # Convert images to note.com format
    result = _convert_images_to_note_format(result)

    # Wrap list item content in p tags (ProseMirror requirement)
    result = _wrap_li_content_in_p(result)

    # Convert blockquote newlines to <br> tags (note.com browser editor format)
    result = _convert_blockquote_newlines_to_br(result)

    # Add UUID to all elements (note.com requirement)
    result = _add_uuid_to_elements(result)

    # Convert TOC placeholder to <table-of-contents> element (Issue #117)
    # Must be after UUID addition to preserve name/id attributes
    result = _convert_toc_placeholder_to_html(result)

    # Apply text alignment styles to paragraphs (must be after UUID addition)
    result = _apply_text_alignment_to_html(result)

    # Convert blockquotes to note.com figure format
    # This is required for the API to preserve <br> tags inside blockquotes
    result = _convert_blockquotes_to_note_format(result)

    # Convert standalone embed URLs to figure elements (Issue #116)
    # YouTube, Twitter, note.com URLs alone in a paragraph become embeds
    result = _convert_standalone_embed_urls(result)

    # Convert code blocks to note.com format and handle newlines
    result = _convert_code_blocks_to_note_format(result)

    return result

