import unittest
from unittest.mock import AsyncMock, patch

from job_search_loop.browser_agent.contracts import SessionHandleV1
from job_search_loop.browser_agent.session import BrowserSession


LEASE = {
    "ok": True,
    "context_id": "CONTEXT_A",
    "target_id": "TARGET_A",
    "ws": "ws://127.0.0.1:9222/devtools/page/TARGET_A",
    "token": "a" * 32,
    "generation": 7,
}


class _Page:
    def __init__(self, *_args):
        self.closed = False
        self.marker = None

    async def connect(self):
        return None

    async def evaluate(self, _expression, marker=None):
        if marker is not None:
            self.marker = marker
            return None
        return self.marker

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class BrowserSessionLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_returns_the_exact_context_fence(self):
        session = BrowserSession()
        session._lease_command = AsyncMock(return_value=LEASE.copy())
        with patch("job_search_loop.browser_agent.session.DirectCDPPage", _Page):
            handle = await session.attach("http://127.0.0.1:9222", "row-1")
            await session.close_owned(handle)

        self.assertEqual(
            session._lease_command.await_args_list[-1].args,
            (
                "release", "job-search-daily", "--token", "a" * 32,
                "--generation", "7",
            ),
        )

    async def test_foreign_marker_keeps_page_and_lease_owned(self):
        session = BrowserSession()
        page = _Page()
        page.marker = "another-row"
        handle = SessionHandleV1(
            1, "http://127.0.0.1:9222", "row-1", "anicca-job-search:row-1", 1
        )
        session._pages[handle.page_marker] = page
        session._leases[handle.page_marker] = LEASE.copy()
        session._lease_command = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "not owned"):
            await session.close_owned(handle)

        self.assertIn(handle.page_marker, session._pages)
        self.assertIn(handle.page_marker, session._leases)
        session._lease_command.assert_not_awaited()

    async def test_marker_failure_releases_the_exact_context_fence(self):
        class BrokenPage(_Page):
            async def evaluate(self, _expression, marker=None):
                if marker is not None:
                    raise RuntimeError("marker failed")
                return None

        session = BrowserSession()
        session._lease_command = AsyncMock(return_value=LEASE.copy())
        with patch("job_search_loop.browser_agent.session.DirectCDPPage", BrokenPage):
            with self.assertRaisesRegex(RuntimeError, "marker failed"):
                await session.attach("http://127.0.0.1:9222", "row-1")
        self.assertEqual(session._lease_command.await_args_list[-1].args[0], "release")

    async def test_release_failure_retains_fence_for_safe_retry(self):
        session = BrowserSession()
        page = _Page()
        page.marker = "anicca-job-search:row-1"
        handle = SessionHandleV1(
            1, "http://127.0.0.1:9222", "row-1", page.marker, 1
        )
        session._pages[handle.page_marker] = page
        session._leases[handle.page_marker] = LEASE.copy()
        session._lease_command = AsyncMock(side_effect=RuntimeError("release failed"))

        with self.assertRaisesRegex(RuntimeError, "release failed"):
            await session.close_owned(handle)
        self.assertIn(handle.page_marker, session._leases)
        self.assertTrue(page.is_closed())


if __name__ == "__main__":
    unittest.main()
