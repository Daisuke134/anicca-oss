"""HTML to Markdown conversion utility.

Converts note.com HTML format (ProseMirror) back to Markdown.
This is the reverse operation of markdown_to_html.
"""

import html
import re
from collections.abc import Callable

# Pre-compiled regex patterns for basic elements
# Match <pre><code>...</code></pre> with or without class="codeBlock"
_CODE_BLOCK_PATTERN = re.compile(
    r"<pre[^>]*><code>(.*?)</code></pre>",
    re.DOTALL | re.IGNORECASE,
)
# Match <pre>...</pre> without <code> tag (fallback for some note.com formats)
_PRE_ONLY_PATTERN = re.compile(
    r"<pre[^>]*>(?!<code>)(.*?)</pre>",
    re.DOTALL | re.IGNORECASE,
)
_HEADING_PATTERN = re.compile(
    r"<(h[1-6])[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_PARAGRAPH_PATTERN = re.compile(
    r"<p[^>]*>(.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_HR_PATTERN = re.compile(r"<hr[^>]*/?>", re.IGNORECASE)

# Patterns for complex elements
_BLOCKQUOTE_FIGURE_PATTERN = re.compile(
    r"<figure[^>]*>\s*<blockquote[^>]*>(.*?)</blockquote>\s*"
    r"<figcaption>(.*?)</figcaption>\s*</figure>",
    re.DOTALL | re.IGNORECASE,
)
_BR_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
_FIGCAPTION_LINK_PATTERN = re.compile(
    r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_IMAGE_FIGURE_PATTERN = re.compile(
    r'<figure[^>]*>\s*<img[^>]*src="([^"]+)"[^>]*alt="([^"]*)"[^>]*>\s*'
    r"<figcaption>(.*?)</figcaption>\s*</figure>",
    re.DOTALL | re.IGNORECASE,
)
# Alternative pattern for img with alt before src
_IMAGE_FIGURE_PATTERN_ALT = re.compile(
    r'<figure[^>]*>\s*<img[^>]*alt="([^"]*)"[^>]*src="([^"]+)"[^>]*>\s*'
    r"<figcaption>(.*?)</figcaption>\s*</figure>",
    re.DOTALL | re.IGNORECASE,
)
_UL_PATTERN = re.compile(r"<ul[^>]*>(.*?)</ul>", re.DOTALL | re.IGNORECASE)
_OL_PATTERN = re.compile(r"<ol[^>]*>(.*?)</ol>", re.DOTALL | re.IGNORECASE)

# Patterns for inline elements
_LINK_PATTERN = re.compile(
    r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_STRONG_PATTERN = re.compile(r"<strong>(.*?)</strong>", re.DOTALL | re.IGNORECASE)
_EM_PATTERN = re.compile(r"<em>(.*?)</em>", re.DOTALL | re.IGNORECASE)
_INLINE_CODE_PATTERN = re.compile(r"<code>(.*?)</code>", re.DOTALL | re.IGNORECASE)
_STRIKETHROUGH_PATTERN = re.compile(r"<s>(.*?)</s>", re.DOTALL | re.IGNORECASE)

# TOC element pattern (note.com uses TableOfContents class)
# Match elements with class containing "TableOfContents"
_TOC_ELEMENT_PATTERN = re.compile(
    r'<[^>]*class="[^"]*TableOfContents[^"]*"[^>]*>.*?</(?:div|section|nav)>',
    re.DOTALL | re.IGNORECASE,
)
# Also match self-closing or empty TOC elements
_TOC_ELEMENT_SIMPLE_PATTERN = re.compile(
    r'<[^>]*class="[^"]*TableOfContents[^"]*"[^>]*/?>',
    re.IGNORECASE,
)

# Text alignment pattern (Issue #40)
# Match <p ... style="text-align: center/right/left" ...> with possible other style properties
_TEXT_ALIGN_P_PATTERN = re.compile(
    r'<p([^>]*style="[^"]*text-align:\s*(center|right|left)[^"]*"[^>]*)>(.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)

# Cleanup patterns
_UUID_ATTR_PATTERN = re.compile(
    r'\s(?:name|id)="[a-f0-9-]{36}"',
    re.IGNORECASE,
)


def _strip_fence_markers(code: str) -> str:
    """Strip fence markers from code block content.

    Handles various formats:
    - ```python\\ncode\\n```
    - ```\\ncode\\n```
    - code with fence markers at boundaries
    """
    # Remove opening fence marker (``` or ```language)
    if code.startswith("```"):
        # Find the end of the first line (after language identifier)
        newline_pos = code.find("\n")
        if newline_pos != -1:
            code = code[newline_pos + 1 :]
        else:
            # No newline, remove just the opening ``` and optional language
            first_word_end = 3  # Skip ```
            while first_word_end < len(code) and code[first_word_end].isalnum():
                first_word_end += 1
            code = code[first_word_end:]

    # Remove closing fence marker (```)
    code = code.rstrip()
    if code.endswith("```"):
        code = code[:-3]

    return code.strip()


def _create_code_block_extractor(code_blocks: list[str]) -> Callable[[re.Match[str]], str]:
    """Create a code block extractor closure with local storage.

    Args:
        code_blocks: List to store extracted code blocks (mutated in place)

    Returns:
        A function that extracts code blocks and returns placeholders
    """

    def extract_code_block(match: re.Match[str]) -> str:
        """Extract code block and replace with placeholder."""
        code = match.group(1)
        code = html.unescape(code)
        # Remove any remaining fence markers (``` at start/end)
        code = _strip_fence_markers(code)
        # Include trailing newlines for proper paragraph separation
        block = f"```\n{code}\n```\n\n"
        code_blocks.append(block)
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    return extract_code_block


def _convert_text_align_paragraph(match: re.Match[str]) -> str:
    """Convert text-aligned paragraph to Markdown format with alignment markers.

    Args:
        match: Regex match with groups (attrs, alignment, content)

    Returns:
        Markdown with alignment markers:
        - center: ->text<-
        - right: ->text
        - left: <-text
    """
    alignment = match.group(2).lower()
    content = match.group(3).strip()

    alignment_formats = {
        "center": f"->{content}<-\n\n",
        "right": f"->{content}\n\n",
        "left": f"<-{content}\n\n",
    }
    return alignment_formats.get(alignment, f"{content}\n\n")


def _convert_heading(match: re.Match[str]) -> str:
    """Convert heading to Markdown format."""
    level = int(match.group(1)[1])  # h1 -> 1, h2 -> 2, etc.
    text = match.group(2).strip()
    return f"{'#' * level} {text}\n\n"


def _convert_paragraph(match: re.Match[str]) -> str:
    """Convert paragraph to Markdown format."""
    content = match.group(1).strip()
    return f"{content}\n\n"


def _convert_blockquote_figure(match: re.Match[str]) -> str:
    """Convert blockquote figure to Markdown format."""
    content = match.group(1)
    figcaption = match.group(2).strip()

    # Remove <p> tags from content
    content = re.sub(r"<p[^>]*>(.*?)</p>", r"\1", content, flags=re.DOTALL | re.IGNORECASE)

    # Convert <br> to newlines
    content = _BR_PATTERN.sub("\n", content)

    # Build blockquote lines
    lines = content.strip().split("\n")
    quote_lines = [f"> {line.strip()}" for line in lines if line.strip()]

    # Add citation if present
    if figcaption:
        # Check for link in figcaption
        link_match = _FIGCAPTION_LINK_PATTERN.search(figcaption)
        if link_match:
            url = link_match.group(1)
            text = link_match.group(2)
            quote_lines.append(f"> — {text} ({url})")
        else:
            quote_lines.append(f"> — {figcaption}")

    return "\n".join(quote_lines) + "\n\n"


def _convert_image_figure(match: re.Match[str], alt_first: bool = False) -> str:
    """Convert image figure to Markdown format."""
    if alt_first:
        alt = match.group(1)
        src = match.group(2)
        caption = match.group(3).strip()
    else:
        src = match.group(1)
        alt = match.group(2)
        caption = match.group(3).strip()

    if caption:
        return f'![{alt}]({src} "{caption}")\n\n'
    return f"![{alt}]({src})\n\n"


def _find_matching_tags(
    html_content: str,
    tag_name: str,
) -> list[tuple[str, int, int]]:
    """Find all top-level matching tag pairs with proper nesting support.

    Args:
        html_content: HTML string to search
        tag_name: Tag name to find (e.g., "li", "ul")

    Returns:
        List of (content, start_pos, end_pos) tuples for each match
    """
    results: list[tuple[str, int, int]] = []
    open_tag = f"<{tag_name}"
    close_tag = f"</{tag_name}>"
    pos = 0

    while pos < len(html_content):
        # Find opening tag
        tag_start = html_content.find(open_tag, pos)
        if tag_start == -1:
            break

        # Find the > that closes the opening tag
        tag_end = html_content.find(">", tag_start)
        if tag_end == -1:
            break

        # Track depth to find matching close tag
        depth = 1
        search_pos = tag_end + 1

        while depth > 0 and search_pos < len(html_content):
            next_open = html_content.find(open_tag, search_pos)
            next_close = html_content.find(close_tag, search_pos)

            if next_close == -1:
                break

            if next_open != -1 and next_open < next_close:
                depth += 1
                search_pos = next_open + len(open_tag)
            else:
                depth -= 1
                if depth == 0:
                    content = html_content[tag_end + 1 : next_close]
                    results.append((content, tag_start, next_close + len(close_tag)))
                search_pos = next_close + len(close_tag)

        pos = search_pos

    return results


def _find_matching_li_tags(html_content: str) -> list[str]:
    """Find all top-level <li> elements, properly handling nested lists."""
    return [content for content, _, _ in _find_matching_tags(html_content, "li")]


def _convert_list(html_content: str, ordered: bool = False, indent_level: int = 0) -> str:
    """Convert list to Markdown format with nested list support.

    Args:
        html_content: HTML content of list
        ordered: True for ordered list, False for unordered
        indent_level: Current indentation level (0 = top level)

    Returns:
        Markdown formatted list
    """
    indent = "  " * indent_level  # 2 spaces per level
    lines: list[str] = []
    counter = 1

    # Use proper tag matching instead of regex
    li_contents = _find_matching_li_tags(html_content)

    for li_content in li_contents:
        # Extract text from first <p> tag if present (before any nested lists)
        p_match = re.search(r"<p[^>]*>(.*?)</p>", li_content, re.DOTALL | re.IGNORECASE)
        if p_match:
            text = p_match.group(1).strip()
        else:
            # Remove any nested lists before extracting text
            text = _UL_PATTERN.sub("", li_content)
            text = _OL_PATTERN.sub("", text)
            text = text.strip()

        # Clean up any remaining HTML tags from text
        text = re.sub(r"<[^>]+>", "", text).strip()

        # Add list item
        if text:  # Only add if there's text content
            if ordered:
                lines.append(f"{indent}{counter}. {text}")
                counter += 1
            else:
                lines.append(f"{indent}- {text}")

        # Process nested lists
        nested_ul = _UL_PATTERN.search(li_content)
        nested_ol = _OL_PATTERN.search(li_content)
        if nested_ul:
            nested_md = _convert_list(nested_ul.group(1), ordered=False, indent_level=indent_level + 1)
            lines.append(nested_md.rstrip())
        if nested_ol:
            nested_md = _convert_list(nested_ol.group(1), ordered=True, indent_level=indent_level + 1)
            lines.append(nested_md.rstrip())

    return "\n".join(lines) + "\n"


def _find_matching_tag_content(html_content: str, tag_name: str) -> tuple[str, int, int] | None:
    """Find the content of a tag, properly handling nested same-name tags.

    Returns (content, start_pos, end_pos) or None if not found.
    """
    results = _find_matching_tags(html_content, tag_name)
    return results[0] if results else None


def _convert_all_lists(html_content: str) -> str:
    """Convert all lists in the HTML content, properly handling nesting."""
    result = html_content

    # Process lists repeatedly until no more are found
    # We process from innermost to outermost by repeatedly finding and replacing
    max_iterations = 100  # Prevent infinite loops
    for _ in range(max_iterations):
        # Try to find a ul or ol
        ul_match = _find_matching_tag_content(result, "ul")
        ol_match = _find_matching_tag_content(result, "ol")

        # Find which one comes first
        if ul_match is None and ol_match is None:
            break

        if ul_match is not None and (ol_match is None or ul_match[1] < ol_match[1]):
            # Process ul
            content, start, end = ul_match
            md = _convert_list(content, ordered=False)
            result = result[:start] + md + result[end:]
        elif ol_match is not None:
            # Process ol
            content, start, end = ol_match
            md = _convert_list(content, ordered=True)
            result = result[:start] + md + result[end:]

    return result


def _convert_link(match: re.Match[str]) -> str:
    """Convert link to Markdown format."""
    url = match.group(1)
    text = match.group(2).strip()
    return f"[{text}]({url})"


def _convert_inline_elements(text: str) -> str:
    """Convert inline elements to Markdown format."""
    result = text

    # Links
    result = _LINK_PATTERN.sub(_convert_link, result)

    # Bold
    result = _STRONG_PATTERN.sub(r"**\1**", result)

    # Italic
    result = _EM_PATTERN.sub(r"*\1*", result)

    # Strikethrough
    result = _STRIKETHROUGH_PATTERN.sub(r"~~\1~~", result)

    # Inline code (must be after code block extraction to avoid false matches)
    result = _INLINE_CODE_PATTERN.sub(r"`\1`", result)

    return result


def html_to_markdown(html_content: str) -> str:
    """Convert note.com HTML to Markdown.

    Args:
        html_content: HTML string from note.com editor (ProseMirror format)

    Returns:
        Markdown formatted text
    """
    if not html_content or not html_content.strip():
        return ""

    # Use local storage for code blocks (thread-safe)
    code_blocks: list[str] = []
    extract_code_block = _create_code_block_extractor(code_blocks)

    result = html_content

    # 1. コードブロック（プレースホルダーで保護）
    result = _CODE_BLOCK_PATTERN.sub(extract_code_block, result)
    # Also handle <pre> without <code> tag (some note.com formats)
    result = _PRE_ONLY_PATTERN.sub(extract_code_block, result)

    # 2. TOC要素を[TOC]マーカーに変換
    result = _TOC_ELEMENT_PATTERN.sub("[TOC]\n\n", result)
    result = _TOC_ELEMENT_SIMPLE_PATTERN.sub("[TOC]\n\n", result)

    # 3. figure要素（blockquoteとimageを先に処理）
    result = _BLOCKQUOTE_FIGURE_PATTERN.sub(_convert_blockquote_figure, result)
    result = _IMAGE_FIGURE_PATTERN.sub(lambda m: _convert_image_figure(m, alt_first=False), result)
    result = _IMAGE_FIGURE_PATTERN_ALT.sub(lambda m: _convert_image_figure(m, alt_first=True), result)

    # 4. 見出し
    result = _HEADING_PATTERN.sub(_convert_heading, result)

    # 5. リスト（ネスト対応 - 適切なタグマッチングを使用）
    result = _convert_all_lists(result)

    # 6. 水平線
    result = _HR_PATTERN.sub("\n---\n\n", result)

    # 7. インライン要素（リンク、太字、斜体、インラインコード）
    result = _convert_inline_elements(result)

    # 8. テキスト配置を持つ段落（通常の段落変換より先に処理）
    result = _TEXT_ALIGN_P_PATTERN.sub(_convert_text_align_paragraph, result)

    # 9. 段落（他の要素処理後に適用）
    result = _PARAGRAPH_PATTERN.sub(_convert_paragraph, result)

    # === 最終処理 ===

    # プレースホルダー復元（コードブロック）
    for i, block in enumerate(code_blocks):
        result = result.replace(f"__CODE_BLOCK_{i}__", block)

    # UUID属性削除（残存する場合のクリーンアップ）
    result = _UUID_ATTR_PATTERN.sub("", result)

    # 残存するHTMLタグを削除（エンティティデコード前に実行）
    # これにより、ユーザーコンテンツ内の &lt;tag&gt; が保護される
    result = re.sub(r"<[^>]+>", "", result)

    # HTMLエンティティデコード
    result = html.unescape(result)

    # 連続する空行を正規化
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()

