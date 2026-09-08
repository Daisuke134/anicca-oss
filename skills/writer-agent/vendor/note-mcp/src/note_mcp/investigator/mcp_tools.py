"""Investigator MCP tools for AI-driven API investigation.

Provides MCP tools for browser automation and HTTP traffic analysis.
These tools enable AI agents to investigate note.com's API behavior
through direct browser interaction and traffic capture.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from fastmcp import FastMCP

from note_mcp.investigator.core import CaptureSessionManager

logger = logging.getLogger(__name__)


def register_investigator_tools(mcp: FastMCP) -> None:
    """Register investigator MCP tools with the server.

    Args:
        mcp: FastMCP server instance to register tools with
    """

    @mcp.tool()
    async def investigator_start_capture(
        domain: Annotated[str, "キャプチャ対象ドメイン（例: api.note.com）"],
        port: Annotated[int, "プロキシポート"] = 8080,
    ) -> str:
        """キャプチャセッションを開始します。

        ブラウザとプロキシを起動し、指定ドメインのHTTPトラフィックを
        キャプチャ可能な状態にします。
        """
        try:
            await CaptureSessionManager.get_or_create(domain, port)
            return f"Capture session started for domain: {domain}, port: {port}"
        except RuntimeError as e:
            return f"Error: Failed to start capture session: {e}"
        except Exception as e:
            logger.error(f"Unexpected error starting capture: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_stop_capture() -> str:
        """キャプチャセッションを停止します。

        ブラウザとプロキシを終了し、キャプチャデータを保存します。
        """
        try:
            await CaptureSessionManager.close()
            return "Capture session stopped"
        except Exception as e:
            logger.error(f"Error stopping capture: {e}")
            return f"Error: Failed to stop capture session: {e}"

    @mcp.tool()
    async def investigator_get_status() -> str:
        """現在のキャプチャセッション状態を取得します。

        セッションがアクティブか、キャプチャ中のドメイン等の情報を返します。
        """
        status = CaptureSessionManager.get_status()
        return json.dumps(status, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def investigator_navigate(
        url: Annotated[str, "移動先URL"],
    ) -> str:
        """指定URLに移動します。

        ブラウザを指定URLに移動させ、ページタイトルを返します。
        トラフィックは自動的にキャプチャされます。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return await session.navigate(url)
        except TimeoutError:
            return f"Error: Navigation to {url} timed out."
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_click(
        selector: Annotated[str, "CSSセレクタ"],
    ) -> str:
        """セレクタで指定した要素をクリックします。

        CSSセレクタを使用してページ上の要素を特定し、クリックします。
        クリックにより発生するHTTPリクエストはキャプチャされます。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return await session.click(selector)
        except TimeoutError:
            return f"Error: Click on '{selector}' timed out."
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Click failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_type(
        selector: Annotated[str, "CSSセレクタ"],
        text: Annotated[str, "入力テキスト"],
    ) -> str:
        """指定要素にテキストを入力します。

        CSSセレクタで特定した入力要素にテキストを入力します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return await session.type_text(selector, text)
        except TimeoutError:
            return f"Error: Typing into '{selector}' timed out."
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Type failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_screenshot() -> str:
        """現在のページのスクリーンショットを取得します。

        ページ全体のスクリーンショットをbase64エンコードされたPNG形式で返します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return await session.screenshot()
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_get_page_content() -> str:
        """現在のページのHTMLを取得します。

        ページの完全なHTMLソースを返します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return await session.get_page_content()
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Get page content failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_get_traffic(
        pattern: Annotated[str | None, "URLパターンでフィルタ（正規表現）"] = None,
    ) -> str:
        """キャプチャしたトラフィック一覧を取得します。

        これまでにキャプチャしたHTTPリクエストの一覧をJSON形式で返します。
        パターンを指定すると、URLが一致するリクエストのみ返します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            traffic = session.get_traffic(pattern)
            return json.dumps(traffic, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Get traffic failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_analyze(
        pattern: Annotated[str, "URLパターン（正規表現）"],
        method: Annotated[str | None, "HTTPメソッドでフィルタ"] = None,
    ) -> str:
        """特定パターンのトラフィックを詳細分析します。

        指定したURLパターンに一致するリクエストを集計・分析し、
        レポート形式で返します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return session.analyze_traffic(pattern, method)
        except Exception as e:
            logger.error(f"Analyze traffic failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool()
    async def investigator_export(
        output_path: Annotated[str, "出力ファイルパス"],
    ) -> str:
        """キャプチャデータをJSONファイルにエクスポートします。

        これまでにキャプチャした全トラフィックをJSONファイルに保存します。
        """
        session = await CaptureSessionManager.get_active_session()
        if not session:
            return "Error: No active capture session. Start one first."
        try:
            return session.export_traffic(output_path)
        except OSError as e:
            return f"Error: Failed to write file: {e}"
        except Exception as e:
            logger.error(f"Export failed: {e}")
            return f"Error: {type(e).__name__}: {e}"

