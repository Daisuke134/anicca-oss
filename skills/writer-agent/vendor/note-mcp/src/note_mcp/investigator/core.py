"""HTTP traffic investigation core module.

Provides proxy management and capture session handling for API investigation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TextIO

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

# Timeout constants (Article 6: named constants for all timeouts)
PROXY_STOP_TIMEOUT_SEC: int = 5  # Timeout for stopping proxy process
PAGE_CLOSE_WAIT_TIMEOUT_MS: int = 0  # Immediate timeout for page close check
MITMDUMP_READ_TIMEOUT_SEC: int = 30  # Timeout for reading mitmdump output
PAGE_NAVIGATION_TIMEOUT_MS: int = 30000  # Timeout for page navigation


@dataclass
class CapturedRequest:
    """Represents a captured HTTP request/response pair."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    response_status: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str | None = None


class ProxyManager:
    """Manages mitmproxy (mitmdump) process lifecycle.

    Attributes:
        port: Proxy server port
        process: Subprocess running mitmdump
        output_file: Path to traffic capture file
    """

    def __init__(self, port: int = 8080) -> None:
        """Initialize proxy manager.

        Args:
            port: Port number for the proxy server
        """
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.output_file: Path | None = None
        self._log_handle: TextIO | None = None

    def start(self, output: Path, domain_filter: str | None = None) -> None:
        """Start mitmproxy in dump mode.

        Args:
            output: Path to save captured traffic
            domain_filter: Optional domain to filter (e.g., "note.com")
        """
        self.output_file = output

        # Use uv run to execute mitmdump within the project's virtual environment
        cmd = [
            "uv",
            "run",
            "mitmdump",
            "--mode",
            f"regular@{self.port}",
            "--set",
            "flow_detail=3",
            "-w",
            str(output),
        ]

        # Add domain filter if specified
        if domain_filter:
            cmd.extend(["--set", f"filter=~d {domain_filter}"])

        # Don't capture stdout/stderr with PIPE - it causes blocking issues
        # mitmproxy needs to write output freely without buffer pressure
        # Log to file for debugging, stdout to DEVNULL
        log_file = Path("/tmp/mitmproxy_debug.log")
        try:
            self._log_handle = open(log_file, "w")  # noqa: SIM115
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._log_handle,
            )

            # Wait for proxy to start - mitmproxy needs time to initialize
            time.sleep(3.0)

            if self.process.poll() is not None:
                msg = f"Failed to start mitmproxy on port {self.port}. Check if the port is already in use."
                raise RuntimeError(msg)
        except Exception:
            # Clean up log handle on failure
            if self._log_handle:
                self._log_handle.close()
                self._log_handle = None
            raise

    def stop(self) -> None:
        """Stop mitmproxy process."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=PROXY_STOP_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    def is_running(self) -> bool:
        """Check if mitmproxy is running.

        Returns:
            True if process is running
        """
        return self.process is not None and self.process.poll() is None


class CaptureSession:
    """Manages a traffic capture session with browser.

    Combines ProxyManager with Playwright browser for interactive
    traffic investigation.
    """

    def __init__(self, proxy_port: int = 8080) -> None:
        """Initialize capture session.

        Args:
            proxy_port: Port for the proxy server
        """
        self.proxy = ProxyManager(proxy_port)
        self.proxy_port = proxy_port
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def _verify_proxy_ready(self, timeout: float = 5.0) -> bool:
        """Verify that the proxy is accepting connections.

        Args:
            timeout: Maximum time to wait for proxy

        Returns:
            True if proxy is ready, False otherwise
        """
        import socket

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    result = sock.connect_ex(("127.0.0.1", self.proxy_port))
                    if result == 0:
                        logger.info(f"Proxy is ready on port {self.proxy_port}")
                        return True
            except OSError:
                pass
            time.sleep(0.5)

        logger.error(f"Proxy did not become ready on port {self.proxy_port}")
        return False

    async def start(
        self,
        output: Path,
        domain_filter: str | None = None,
        restore_session: bool = True,
    ) -> Page:
        """Start capture session with browser.

        Args:
            output: Path to save captured traffic
            domain_filter: Optional domain to filter
            restore_session: Whether to restore saved session cookies

        Returns:
            Playwright Page instance for interaction
        """
        # Start proxy first
        self.proxy.start(output, domain_filter)

        # Verify proxy is accepting connections before launching browser
        if not self._verify_proxy_ready():
            raise RuntimeError(f"Proxy is not accepting connections on port {self.proxy_port}")

        # Start browser with proxy settings
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        # Use Playwright's bundled Chromium with certificate bypass flags
        # This avoids certificate trust issues with mitmproxy
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            proxy={"server": f"http://127.0.0.1:{self.proxy_port}"},
            args=[
                "--no-sandbox",  # Required for some WSL2 configurations
                "--ignore-certificate-errors",  # Bypass all certificate validation
                "--allow-insecure-localhost",  # Allow insecure localhost connections
            ],
        )
        self._context = await self._browser.new_context(
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()

        # Restore session if available and requested
        if restore_session:
            await self._restore_session()

        return self._page

    async def _restore_session(self) -> bool:
        """Restore saved session cookies to browser context.

        Returns:
            True if session was restored, False otherwise
        """
        try:
            from note_mcp.auth.session import SessionManager

            session_manager = SessionManager()
            saved_session = session_manager.load()

            if saved_session and not saved_session.is_expired() and saved_session.cookies:
                logger.info("Restoring saved session cookies...")

                # Convert saved cookies to Playwright format
                playwright_cookies: list[dict[str, Any]] = []
                for name, value in saved_session.cookies.items():
                    playwright_cookies.append(
                        {
                            "name": name,
                            "value": value,
                            "domain": ".note.com",
                            "path": "/",
                        }
                    )

                if self._context:
                    await self._context.add_cookies(playwright_cookies)  # type: ignore[arg-type]
                    logger.info(f"Restored {len(playwright_cookies)} cookies for user: {saved_session.username}")
                    return True
                else:
                    logger.warning("No browser context available for session restore")
                    return False

            logger.info("No valid saved session found - manual login required")
            return False

        except ImportError as e:
            logger.error(f"Session module import failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"Failed to restore session (will require manual login): {e}")
            return False

    async def wait_for_close(self) -> None:
        """Wait for browser to be closed by user."""
        if self._page:
            # Wait indefinitely for page close (suppress if page already closed)
            with contextlib.suppress(Exception):
                await self._page.wait_for_event("close", timeout=PAGE_CLOSE_WAIT_TIMEOUT_MS)

    async def close(self) -> None:
        """Close browser and stop proxy."""
        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self.proxy.stop()

        self._context = None
        self._page = None

    # =========================================================================
    # Browser operation methods for MCP tools
    # =========================================================================

    async def navigate(self, url: str) -> str:
        """Navigate to specified URL.

        Args:
            url: Target URL to navigate to

        Returns:
            Navigation result with page title

        Raises:
            RuntimeError: If session not started
        """
        if not self._page:
            raise RuntimeError("Session not started")
        await self._page.goto(url, wait_until="domcontentloaded")
        title = await self._page.title()
        return f"Navigated to {url}, title: {title}"

    async def click(self, selector: str) -> str:
        """Click element by CSS selector.

        Args:
            selector: CSS selector for target element

        Returns:
            Click result message

        Raises:
            RuntimeError: If session not started
        """
        if not self._page:
            raise RuntimeError("Session not started")
        await self._page.click(selector)
        return f"Clicked {selector}"

    async def type_text(self, selector: str, text: str) -> str:
        """Type text into specified element.

        Args:
            selector: CSS selector for input element
            text: Text to type

        Returns:
            Type result message

        Raises:
            RuntimeError: If session not started
        """
        if not self._page:
            raise RuntimeError("Session not started")
        await self._page.fill(selector, text)
        return f"Typed text into {selector}"

    async def screenshot(self) -> str:
        """Take screenshot of current page.

        Returns:
            Base64-encoded PNG screenshot

        Raises:
            RuntimeError: If session not started
        """
        if not self._page:
            raise RuntimeError("Session not started")
        screenshot_bytes = await self._page.screenshot()
        return base64.b64encode(screenshot_bytes).decode()

    async def get_page_content(self) -> str:
        """Get current page HTML content.

        Returns:
            Full HTML content of current page

        Raises:
            RuntimeError: If session not started
        """
        if not self._page:
            raise RuntimeError("Session not started")
        return await self._page.content()

    # =========================================================================
    # Traffic analysis methods
    # =========================================================================

    def get_traffic(self, pattern: str | None = None) -> list[dict[str, Any]]:
        """Get captured traffic as list of request/response pairs.

        Args:
            pattern: Optional regex pattern to filter URLs

        Returns:
            List of traffic entries with method, url, status, etc.
        """
        if not self.proxy.output_file or not self.proxy.output_file.exists():
            return []

        traffic: list[dict[str, Any]] = []
        try:
            # Read mitmproxy flow file using mitmdump
            # Note: flow_detail=1 produces concise output, flow_detail=0 produces nothing
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "mitmdump",
                    "-r",
                    str(self.proxy.output_file),
                    "-n",  # No upstream connection
                    "--set",
                    "flow_detail=1",
                ],
                capture_output=True,
                text=True,
                timeout=MITMDUMP_READ_TIMEOUT_SEC,
            )

            # Parse output lines
            # Format with flow_detail=1:
            # "127.0.0.1:49898: GET https://note.com/ HTTP/2.0"
            # "     << HTTP/2.0 200 OK 38.5k"
            current_entry: dict[str, Any] | None = None
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                # Request line: contains method and URL
                methods = [": GET ", ": POST ", ": PUT ", ": DELETE ", ": PATCH "]
                if any(m in line for m in methods):
                    # Parse request line
                    parts = line.split(": ", 1)
                    if len(parts) >= 2:
                        request_part = parts[1]
                        req_parts = request_part.split()
                        if len(req_parts) >= 2:
                            method = req_parts[0]
                            url = req_parts[1]

                            # Apply pattern filter
                            if pattern and not re.search(pattern, url):
                                current_entry = None
                                continue

                            current_entry = {
                                "method": method,
                                "url": url,
                                "status": 0,
                            }

                # Response line: contains status code
                elif line.strip().startswith("<<") and current_entry:
                    # Parse response status
                    # Format: "<< HTTP/2.0 200 OK 38.5k" or "<< 200 OK 105b"
                    resp_parts = line.strip().split()
                    for part in resp_parts:
                        if part.isdigit():
                            current_entry["status"] = int(part)
                            break

                    traffic.append(current_entry)
                    current_entry = None

        except subprocess.TimeoutExpired:
            logger.warning("Traffic read timed out after 30 seconds")
        except subprocess.CalledProcessError as e:
            logger.error(f"mitmdump failed with exit code {e.returncode}: {e.stderr}")
        except FileNotFoundError:
            logger.error("mitmdump not found - ensure mitmproxy is installed")
        except Exception as e:
            logger.error(f"Failed to read traffic: {type(e).__name__}: {e}")

        return traffic

    def analyze_traffic(self, pattern: str, method: str | None = None) -> str:
        """Analyze traffic matching pattern.

        Args:
            pattern: Regex pattern to match URLs
            method: Optional HTTP method filter

        Returns:
            Analysis result as formatted string
        """
        traffic = self.get_traffic(pattern)

        if method:
            traffic = [t for t in traffic if t["method"].upper() == method.upper()]

        if not traffic:
            return f"No traffic matching pattern: {pattern}"

        # Build analysis report
        lines = [f"Traffic Analysis for pattern: {pattern}"]
        if method:
            lines.append(f"  Method filter: {method}")
        lines.append(f"  Total requests: {len(traffic)}")
        lines.append("")

        # Group by URL
        url_counts: dict[str, int] = {}
        for t in traffic:
            url = t["url"]
            url_counts[url] = url_counts.get(url, 0) + 1

        lines.append("Requests by URL:")
        for url, count in sorted(url_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  [{count}x] {url}")

        return "\n".join(lines)

    def export_traffic(self, output_path: str) -> str:
        """Export captured traffic to JSON file.

        Args:
            output_path: Path to output JSON file

        Returns:
            Export result message
        """
        traffic = self.get_traffic()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        with open(output, "w", encoding="utf-8") as f:
            json.dump(traffic, f, ensure_ascii=False, indent=2)

        return f"Exported {len(traffic)} requests to {output_path}"


class CaptureSessionManager:
    """Singleton manager for sharing CaptureSession across MCP tools.

    Ensures only one capture session is active at a time
    and provides thread-safe access.
    """

    _instance: ClassVar[CaptureSession | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _domain: ClassVar[str | None] = None
    _output_file: ClassVar[Path | None] = None

    @classmethod
    async def get_or_create(
        cls,
        domain: str,
        port: int = 8080,
    ) -> CaptureSession:
        """Get existing session or create new one.

        Args:
            domain: Domain filter for traffic capture
            port: Proxy port number

        Returns:
            Active CaptureSession instance
        """
        async with cls._lock:
            if cls._instance is None:
                cls._instance = CaptureSession(port)
                cls._domain = domain
                # Use APP_DATA_DIR environment variable with fallback to /app/data
                data_dir = Path(os.environ.get("APP_DATA_DIR", "/app/data"))
                data_dir.mkdir(parents=True, exist_ok=True)
                cls._output_file = data_dir / f"capture_{int(time.time())}.flow"
                await cls._instance.start(
                    output=cls._output_file,
                    domain_filter=domain,
                    restore_session=True,
                )
            return cls._instance

    @classmethod
    async def get_active_session(cls) -> CaptureSession | None:
        """Get active session if exists (thread-safe).

        Returns:
            Active CaptureSession instance or None if no session is active
        """
        async with cls._lock:
            return cls._instance

    @classmethod
    async def close(cls) -> None:
        """Close active session if exists."""
        async with cls._lock:
            if cls._instance:
                await cls._instance.close()
                cls._instance = None
                cls._domain = None
                cls._output_file = None

    @classmethod
    def get_status(cls) -> dict[str, Any]:
        """Get current session status.

        Returns:
            Status dict with active flag, domain, and output file
        """
        if cls._instance is None:
            return {"active": False}

        return {
            "active": True,
            "domain": cls._domain,
            "output_file": str(cls._output_file) if cls._output_file else None,
            "proxy_running": cls._instance.proxy.is_running(),
        }


async def run_capture_session(
    output: Path,
    initial_url: str = "https://note.com",
    proxy_port: int = 8080,
    domain_filter: str | None = None,
    restore_session: bool = True,
) -> None:
    """Run an interactive capture session.

    Starts proxy and browser, navigates to initial URL,
    then waits for user to close browser.

    Args:
        output: Path to save captured traffic
        initial_url: URL to navigate to initially
        proxy_port: Port for proxy server
        domain_filter: Optional domain to filter traffic
        restore_session: Whether to restore saved session cookies
    """
    session = CaptureSession(proxy_port)

    try:
        page = await session.start(output, domain_filter, restore_session)

        # Try to navigate, but don't fail if it times out
        # User can manually navigate if automatic navigation fails
        try:
            await page.goto(initial_url, timeout=PAGE_NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as nav_error:
            import logging

            logging.warning(f"Auto-navigation failed: {nav_error}")
            logging.info("Please navigate manually in the browser")

        # Wait for user to close browser
        await session.wait_for_close()

    finally:
        await session.close()

