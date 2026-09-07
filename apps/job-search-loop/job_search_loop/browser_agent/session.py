from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .contracts import SessionHandleV1
from .direct_cdp import DirectCDPPage


Connector = Callable[[str], Awaitable[Any]]


class BrowserSession:
    """Own one tagged row page inside the existing authenticated CDP browser."""

    def __init__(self, connector: Connector | None = None) -> None:
        self._connector = connector
        self._drivers: list[Any] = []
        self._pages: dict[str, Any] = {}
        self._leases: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _lease_script() -> Path:
        configured = os.environ.get("JOB_SEARCH_CDP_LEASE_SCRIPT")
        candidates = [
            Path(configured) if configured else None,
        ]
        for candidate in candidates:
            if candidate is not None and candidate.is_file():
                return candidate
        raise RuntimeError("Daily Driver CDP context lease script is unavailable")

    async def _lease_command(self, *arguments: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["CLOAK_CDP_BASE_URL"] = "http://127.0.0.1:9222"
        env["CLOAK_SESSION_VAULT_FILE"] = str(
            Path.home() / ".cloak/vault/job-search-daily/auth-state.json"
        )
        process = await asyncio.create_subprocess_exec(
            os.environ.get("JOB_SEARCH_PYTHON", "/opt/homebrew/bin/python3"),
            str(self._lease_script()),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        if process.returncode != 0:
            raise RuntimeError(
                "Daily Driver lease command failed: "
                + stderr.decode("utf-8", errors="replace")[-300:]
            )
        result = json.loads(stdout)
        if not result.get("ok"):
            raise RuntimeError("Daily Driver lease command returned failure")
        return result

    async def _leased_page(self) -> tuple[DirectCDPPage, dict[str, Any]]:
        lease = await self._lease_command("acquire", "job-search-daily")
        if (
            not lease.get("ws")
            or not lease.get("target_id")
            or not lease.get("context_id")
            or not isinstance(lease.get("token"), str)
            or not isinstance(lease.get("generation"), int)
        ):
            raise RuntimeError("Daily Driver returned an invalid page lease")
        page = DirectCDPPage(str(lease["ws"]), str(lease["target_id"]))
        try:
            await page.connect()
        except Exception:
            await self._release_lease(lease)
            raise
        return page, lease

    async def _release_lease(self, lease: dict[str, Any]) -> None:
        await self._lease_command(
            "release",
            "job-search-daily",
            "--token",
            str(lease["token"]),
            "--generation",
            str(lease["generation"]),
        )

    async def _bind_leased_page(
        self, marker: str
    ) -> tuple[DirectCDPPage, dict[str, Any], bool]:
        page, lease = await self._leased_page()
        try:
            recovered = await self._marker(page) == marker
            if not recovered:
                await page.evaluate("marker => { window.name = marker }", marker)
        except Exception:
            await self._release_lease(lease)
            raise
        self._pages[marker] = page
        self._leases[marker] = lease
        return page, lease, recovered

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        value = endpoint.rstrip("/")
        parsed = urlparse(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 9222
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("BrowserSession may attach only to local CDP :9222")
        return value

    async def _connect(self, endpoint: str) -> Any:
        if self._connector is not None:
            return await self._connector(endpoint)
        from playwright.async_api import async_playwright

        last_error: Exception | None = None
        for attempt in range(3):
            driver = await async_playwright().start()
            try:
                browser = await driver.chromium.connect_over_cdp(
                    endpoint, timeout=10_000
                )
            except Exception as error:
                last_error = error
                await driver.stop()
                if attempt < 2:
                    await asyncio.sleep(2)
                continue
            self._drivers.append(driver)
            return browser
        raise RuntimeError("existing CDP owner did not accept a bounded attach") from last_error

    @staticmethod
    async def _marker(page: Any) -> str | None:
        try:
            value = await asyncio.wait_for(
                page.evaluate("() => window.name"), timeout=2.0
            )
        except (Exception, asyncio.TimeoutError):
            return None
        return value if isinstance(value, str) else None

    async def _recover_or_create(self, browser: Any, marker: str) -> tuple[Any, bool]:
        if not browser.contexts:
            raise RuntimeError("existing CDP owner has no default browser context")
        context = browser.contexts[0]
        for page in context.pages:
            if not page.is_closed() and await self._marker(page) == marker:
                return page, True
        page = await context.new_page()
        await page.evaluate("marker => { window.name = marker }", marker)
        return page, False

    async def attach(self, endpoint: str, row_run_id: str) -> SessionHandleV1:
        endpoint = self._validate_endpoint(endpoint)
        if not row_run_id:
            raise ValueError("row_run_id is required")
        marker = f"anicca-job-search:{row_run_id}"
        if self._connector is None:
            await self._bind_leased_page(marker)
            return SessionHandleV1(1, endpoint, row_run_id, marker, 1)
        browser = await self._connect(endpoint)
        recovered = await self._recover_or_create(browser, marker)
        page = recovered[0] if isinstance(recovered, tuple) else recovered
        self._pages[marker] = page
        return SessionHandleV1(1, endpoint, row_run_id, marker, 1)

    async def reconnect(self, handle: SessionHandleV1) -> SessionHandleV1:
        endpoint = self._validate_endpoint(handle.endpoint)
        page = self._pages.get(handle.page_marker)
        if page is not None and not page.is_closed():
            return handle
        if self._connector is None:
            await self._bind_leased_page(handle.page_marker)
            return SessionHandleV1(
                1, endpoint, handle.row_run_id, handle.page_marker, handle.generation + 1
            )
        browser = await self._connect(endpoint)
        recovered = await self._recover_or_create(browser, handle.page_marker)
        page = recovered[0] if isinstance(recovered, tuple) else recovered
        self._pages[handle.page_marker] = page
        return SessionHandleV1(
            1,
            endpoint,
            handle.row_run_id,
            handle.page_marker,
            handle.generation + 1,
        )

    async def resume(self, handle: SessionHandleV1) -> tuple[SessionHandleV1, bool]:
        endpoint = self._validate_endpoint(handle.endpoint)
        if self._connector is None:
            _page, _lease, recovered = await self._bind_leased_page(handle.page_marker)
            return (
                SessionHandleV1(
                    1,
                    endpoint,
                    handle.row_run_id,
                    handle.page_marker,
                    handle.generation + 1,
                ),
                recovered,
            )
        browser = await self._connect(endpoint)
        page, recovered = await self._recover_or_create(browser, handle.page_marker)
        self._pages[handle.page_marker] = page
        return (
            SessionHandleV1(
                1, endpoint, handle.row_run_id, handle.page_marker, handle.generation + 1
            ),
            recovered,
        )

    async def close_owned(self, handle: SessionHandleV1) -> None:
        page = self._pages.get(handle.page_marker)
        lease = self._leases.get(handle.page_marker)
        if page is not None and not page.is_closed():
            if await self._marker(page) != handle.page_marker:
                raise RuntimeError("refusing to close a page not owned by this row")
            await page.close()
        if lease is not None:
            await self._release_lease(lease)
        self._pages.pop(handle.page_marker, None)
        self._leases.pop(handle.page_marker, None)

    def page(self, handle: SessionHandleV1) -> Any:
        page = self._pages.get(handle.page_marker)
        if page is None or page.is_closed():
            raise RuntimeError("row page is not attached")
        return page
