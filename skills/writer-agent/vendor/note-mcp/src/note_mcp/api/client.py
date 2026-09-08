"""note.com API client using httpx.

Provides authenticated access to note.com API endpoints
with rate limiting and error handling.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import httpx

from note_mcp.models import ErrorCode, NoteAPIError, Session

if TYPE_CHECKING:
    pass


# API base URL
NOTE_API_BASE = "https://note.com/api"

# Editor origin and referer for mutating API requests
NOTE_EDITOR_ORIGIN = "https://editor.note.com"
NOTE_EDITOR_REFERER = "https://editor.note.com/"

# Common User-Agent string for API requests
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"

# Rate limiting configuration
RATE_LIMIT_REQUESTS = 10  # Maximum requests per minute
RATE_LIMIT_WINDOW = 60  # Window size in seconds

# Default timeout for requests (seconds)
DEFAULT_TIMEOUT = 30


class NoteAPIClient:
    """HTTP client for note.com API.

    Provides authenticated requests with rate limiting and error handling.
    Use as async context manager for proper resource management.

    Attributes:
        session: User session with authentication cookies
    """

    def __init__(self, session: Session | None = None) -> None:
        """Initialize API client.

        Args:
            session: User session for authentication (optional for public endpoints)
        """
        self.session = session
        self._client: httpx.AsyncClient | None = None
        self._request_times: list[float] = []

    async def __aenter__(self) -> Self:
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            base_url=NOTE_API_BASE,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context manager."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_headers(self, include_xsrf: bool = False) -> dict[str, str]:
        """Build request headers with authentication.

        Args:
            include_xsrf: Whether to include X-XSRF-TOKEN header (for POST/PUT/DELETE)

        Returns:
            Headers dictionary with Accept and Cookie if session exists
        """
        headers: dict[str, str] = {
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
        }

        if self.session is not None:
            cookie_parts = [f"{k}={v}" for k, v in self.session.cookies.items()]
            headers["Cookie"] = "; ".join(cookie_parts)

            # Add XSRF token and editor headers for mutating requests
            if include_xsrf:
                xsrf_token = self.session.cookies.get("XSRF-TOKEN")
                if xsrf_token:
                    headers["X-XSRF-TOKEN"] = xsrf_token
                # Required headers for editor API
                headers["Origin"] = NOTE_EDITOR_ORIGIN
                headers["Referer"] = NOTE_EDITOR_REFERER
                headers["X-Requested-With"] = "XMLHttpRequest"
                # Sec-Fetch headers (browser security headers)
                headers["Sec-Fetch-Site"] = "same-site"
                headers["Sec-Fetch-Mode"] = "cors"
                headers["Sec-Fetch-Dest"] = "empty"

        return headers

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting.

        Cleans up old timestamps and checks if we've exceeded the limit.
        """
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW

        # Clean up old timestamps
        self._request_times = [t for t in self._request_times if t > window_start]

        # Note: We don't actively wait here, we just track
        # The actual rate limit error will come from the server
        # This is mainly for client-side tracking

    def _track_request(self) -> None:
        """Track a request for rate limiting."""
        self._request_times.append(time.time())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        include_xsrf: bool = False,
    ) -> dict[str, Any]:
        """Make an HTTP request to the API.

        Centralizes: init check, rate limit, tracking, headers, error handling.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            path: API endpoint path
            params: Query parameters
            json: JSON body
            data: Form data
            files: Files to upload
            include_xsrf: Whether to include X-XSRF-TOKEN header

        Returns:
            JSON response as dictionary

        Raises:
            RuntimeError: If client not initialized
            NoteAPIError: If request fails
        """
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        await self._check_rate_limit()
        self._track_request()

        headers = self._build_headers(include_xsrf=include_xsrf)

        # Set Content-Type for JSON requests (not multipart)
        if json is not None and files is None:
            headers["Content-Type"] = "application/json"

        request_method = getattr(self._client, method.lower())

        # Build kwargs based on method - GET doesn't support json/data/files
        kwargs: dict[str, Any] = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if method.upper() != "GET":
            if json is not None:
                kwargs["json"] = json
            if data is not None:
                kwargs["data"] = data
            if files is not None:
                kwargs["files"] = files

        response = await request_method(path, **kwargs)

        if not response.is_success:
            self._handle_error_response(response)

        result: dict[str, Any] = response.json()
        return result

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Handle error responses from the API.

        Args:
            response: HTTP response object

        Raises:
            NoteAPIError: With appropriate error code
        """
        status = response.status_code

        if status == 401:
            raise NoteAPIError(
                code=ErrorCode.NOT_AUTHENTICATED,
                message="Authentication required. Please log in first.",
                details={"status_code": status},
            )
        elif status == 403:
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message="Access denied.",
                details={"status_code": status, "response": response.text},
            )
        elif status == 404:
            raise NoteAPIError(
                code=ErrorCode.ARTICLE_NOT_FOUND,
                message="Resource not found.",
                details={"status_code": status, "response": response.text},
            )
        elif status == 429:
            raise NoteAPIError(
                code=ErrorCode.RATE_LIMITED,
                message="Rate limit exceeded. Please wait before making more requests.",
                details={"status_code": status},
            )
        elif status >= 500:
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message="Server error. Please try again later.",
                details={"status_code": status, "response": response.text},
            )
        else:
            # Include response text in error message for debugging
            raise NoteAPIError(
                code=ErrorCode.API_ERROR,
                message=f"API request failed with status {status}. Response: {response.text[:500]}",
                details={"status_code": status, "response": response.text},
            )

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a GET request to the API.

        Args:
            path: API endpoint path (e.g., "/v1/articles")
            params: Query parameters

        Returns:
            JSON response as dictionary

        Raises:
            NoteAPIError: If request fails
        """
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a POST request to the API.

        Args:
            path: API endpoint path
            json: JSON body
            data: Form data
            files: Files to upload

        Returns:
            JSON response as dictionary

        Raises:
            NoteAPIError: If request fails
        """
        return await self._request("POST", path, json=json, data=data, files=files, include_xsrf=True)

    async def put(
        self,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a PUT request to the API.

        Args:
            path: API endpoint path
            json: JSON body

        Returns:
            JSON response as dictionary

        Raises:
            NoteAPIError: If request fails
        """
        return await self._request("PUT", path, json=json, include_xsrf=True)

    async def delete(self, path: str) -> dict[str, Any]:
        """Make a DELETE request to the API.

        Args:
            path: API endpoint path

        Returns:
            JSON response as dictionary

        Raises:
            NoteAPIError: If request fails
        """
        return await self._request("DELETE", path, include_xsrf=True)

