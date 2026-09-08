"""Decorators for MCP tool handlers.

Provides common functionality for MCP tool handlers:
- Session validation
- API error handling

Decorator Order:
    When combining decorators, apply in this order (outermost first):

        @handle_api_error   # Catches NoteAPIError from the inner function
        @require_session    # Validates session before calling handler
        async def handler(session: Session, ...) -> str:
            ...

    This ensures session validation happens first, then API errors
    are caught and formatted.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Concatenate, ParamSpec

from note_mcp.auth.session import SessionManager
from note_mcp.models import NoteAPIError

if TYPE_CHECKING:
    from note_mcp.models import Session

logger = logging.getLogger(__name__)
P = ParamSpec("P")

# Session manager instance for decorators
# (SessionManager uses persistent storage, so multiple instances are safe)
_session_manager = SessionManager()


def require_session(
    func: Callable[Concatenate[Session, P], Awaitable[str]],
) -> Callable[P, Awaitable[str]]:
    """Decorator to validate session before executing handler.

    Loads the session from SessionManager. If the session is valid,
    it is passed as the first argument to the decorated function.
    If the session is invalid or expired, returns an error message.

    Usage:
        @require_session
        async def my_handler(session: Session, arg1: str) -> str:
            # session is automatically injected
            return await do_something(session, arg1)

    Args:
        func: The async function to wrap. Must accept Session as first argument.

    Returns:
        Wrapped function that validates session before calling the original.
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        session = _session_manager.load()
        if session is None or session.is_expired():
            return "セッションが無効です。note_loginでログインしてください。"
        return await func(session, *args, **kwargs)

    # Remove 'session' parameter from the signature so it's not exposed in MCP schema
    # See: https://github.com/drillan/note-mcp/issues/238
    original_sig = inspect.signature(func)
    new_params = [param for name, param in original_sig.parameters.items() if name != "session"]
    wrapper.__signature__ = original_sig.replace(parameters=new_params)  # type: ignore[attr-defined]

    return wrapper


def handle_api_error(
    func: Callable[P, Awaitable[str]],
) -> Callable[P, Awaitable[str]]:
    """Decorator to catch and format NoteAPIError exceptions.

    Wraps the function in a try-except block. If NoteAPIError is raised,
    returns a formatted error message. Other exceptions are propagated.

    Usage:
        @handle_api_error
        async def my_handler(session: Session, arg1: str) -> str:
            result = await api_call(session, arg1)
            return f"Success: {result}"

    Args:
        func: The async function to wrap.

    Returns:
        Wrapped function that catches NoteAPIError exceptions.
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return await func(*args, **kwargs)
        except NoteAPIError as e:
            logger.error(
                "NoteAPIError in %s: code=%s, message=%s",
                func.__name__,
                e.code.value,
                e.message,
                exc_info=True,
            )
            return f"エラー [{e.code.value}]: {e.message}"

    return wrapper
