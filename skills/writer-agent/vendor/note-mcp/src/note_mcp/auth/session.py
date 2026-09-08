"""Session management with keyring storage.

Provides secure session storage using the system keyring.
Includes diagnostic information when keyring is not available.

For Docker/headless environments where keyring is not available,
set USE_FILE_SESSION=1 to use file-based session storage.
"""

from __future__ import annotations

import json
import os
import platform
from typing import TYPE_CHECKING

import keyring
from keyring.errors import PasswordDeleteError

from note_mcp.auth.file_session import FileBasedSessionManager
from note_mcp.models import Session

if TYPE_CHECKING:
    pass


class KeyringError(Exception):
    """Exception raised when keyring operations fail.

    Includes diagnostic information to help users troubleshoot
    keyring configuration issues.

    Attributes:
        message: Human-readable error message
        os_info: Operating system information
        backend_info: Keyring backend information
        setup_instructions: Steps to configure keyring
    """

    def __init__(
        self,
        message: str,
        os_info: str,
        backend_info: str | None = None,
        setup_instructions: list[str] | None = None,
    ) -> None:
        """Initialize KeyringError with diagnostic information.

        Args:
            message: Human-readable error message
            os_info: Operating system information
            backend_info: Keyring backend information (if available)
            setup_instructions: Steps to configure keyring
        """
        self.message = message
        self.os_info = os_info
        self.backend_info = backend_info
        self.setup_instructions = setup_instructions or []
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format the error message with diagnostic information."""
        parts = [self.message]
        parts.append(f"\nOS: {self.os_info}")
        if self.backend_info:
            parts.append(f"Backend: {self.backend_info}")
        if self.setup_instructions:
            parts.append("\nSetup instructions:")
            for instruction in self.setup_instructions:
                parts.append(f"  - {instruction}")
        return "\n".join(parts)


def _get_os_info() -> str:
    """Get operating system information for diagnostics."""
    return f"{platform.system()} {platform.release()}"


def _get_backend_info() -> str | None:
    """Get keyring backend information for diagnostics."""
    try:
        backend = keyring.get_keyring()
        return type(backend).__name__
    except Exception:
        return None


def _get_setup_instructions() -> list[str]:
    """Get setup instructions based on the current platform."""
    system = platform.system()
    instructions: list[str] = []

    if system == "Linux":
        instructions = [
            "Install a keyring backend: sudo apt-get install gnome-keyring",
            "Or install libsecret: sudo apt-get install libsecret-1-0",
            "For headless servers, consider using keyrings.alt: pip install keyrings.alt",
            "Then configure with: python -c 'import keyring; print(keyring.get_keyring())'",
        ]
    elif system == "Darwin":
        instructions = [
            "macOS should use Keychain automatically",
            "If issues persist, try: security unlock-keychain",
            "Check Keychain Access app for any locked keychains",
        ]
    elif system == "Windows":
        instructions = [
            "Windows should use Windows Credential Locker automatically",
            "If issues persist, check Windows Credential Manager",
            "Try running as administrator if access is denied",
        ]
    else:
        instructions = [
            "Install a compatible keyring backend",
            "See: https://pypi.org/project/keyring/",
        ]

    return instructions


class SessionManager:
    """Manages session storage using keyring or file-based backend.

    Provides secure storage of session data in the system keyring.
    When keyring is not available, provides clear diagnostic information.

    For Docker/headless environments, set USE_FILE_SESSION=1 to use
    file-based session storage instead of keyring.

    Attributes:
        service_name: The keyring service name used for storage
    """

    DEFAULT_SERVICE_NAME = "note-mcp"
    SESSION_KEY = "session"

    def __init__(self, service_name: str | None = None) -> None:
        """Initialize SessionManager.

        Args:
            service_name: Keyring service name (default: "note-mcp")
        """
        self.service_name = service_name or self.DEFAULT_SERVICE_NAME

        # Use file-based backend when USE_FILE_SESSION=1
        if os.environ.get("USE_FILE_SESSION") == "1":
            self._backend: FileBasedSessionManager | None = FileBasedSessionManager()
        else:
            self._backend = None  # Use keyring directly

    def save(self, session: Session) -> None:
        """Save session to keyring or file backend.

        Args:
            session: Session object to save

        Raises:
            KeyringError: If keyring operation fails (when using keyring)
            OSError: If file operation fails (when using file backend)
        """
        if self._backend:
            self._backend.save(session)
            return

        try:
            session_data = session.model_dump()
            session_json = json.dumps(session_data)
            keyring.set_password(self.service_name, self.SESSION_KEY, session_json)
        except Exception as e:
            raise KeyringError(
                message=f"Failed to save session to keyring: {e}",
                os_info=_get_os_info(),
                backend_info=_get_backend_info(),
                setup_instructions=_get_setup_instructions(),
            ) from e

    def load(self) -> Session | None:
        """Load session from keyring or file backend.

        Returns:
            Session object if found and valid, None otherwise

        Raises:
            KeyringError: If keyring operation fails (when using keyring)
        """
        if self._backend:
            return self._backend.load()

        try:
            session_json = keyring.get_password(self.service_name, self.SESSION_KEY)
        except Exception as e:
            raise KeyringError(
                message=f"Failed to load session from keyring: {e}",
                os_info=_get_os_info(),
                backend_info=_get_backend_info(),
                setup_instructions=_get_setup_instructions(),
            ) from e

        if session_json is None:
            return None

        try:
            session_data = json.loads(session_json)
            return Session(**session_data)
        except (json.JSONDecodeError, TypeError, ValueError):
            # Invalid JSON or invalid session data
            return None

    def clear(self) -> None:
        """Clear session from keyring or file backend.

        Does not raise an error if no session exists.
        """
        if self._backend:
            self._backend.clear()
            return

        try:
            keyring.delete_password(self.service_name, self.SESSION_KEY)
        except PasswordDeleteError:
            # Session didn't exist, which is fine
            pass
        except Exception:
            # Silently ignore other errors on clear
            pass

    def has_session(self) -> bool:
        """Check if a session exists in keyring or file backend.

        Returns:
            True if a valid session exists, False otherwise
        """
        if self._backend:
            return self._backend.has_session()

        try:
            session_json = keyring.get_password(self.service_name, self.SESSION_KEY)
            return session_json is not None
        except Exception:
            return False

