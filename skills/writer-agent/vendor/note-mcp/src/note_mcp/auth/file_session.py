"""File-based session management for Docker/headless environments.

Provides session storage using JSON files when keyring is not available.
This is a fallback mechanism for environments where system keyring
cannot be accessed (Docker containers, headless servers, etc.).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from note_mcp.models import Session

logger = logging.getLogger(__name__)

# Default session filename
SESSION_FILENAME = "session.json"


def _get_default_data_dir() -> Path:
    """Get the default data directory for session storage.

    Returns:
        Path to data directory. Priority:
        1. NOTE_MCP_DATA_DIR environment variable
        2. /app/data (Docker)
        3. ~/.note-mcp (local)
    """
    # Check for environment variable first (Important #4)
    env_dir = os.environ.get("NOTE_MCP_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    # Check for Docker environment
    if Path("/app/data").exists() and os.access("/app/data", os.W_OK):
        return Path("/app/data")

    # Fall back to home directory
    home = Path.home()
    return home / ".note-mcp"


class FileBasedSessionManager:
    """File-based session management for Docker/headless environments.

    Stores session data in a JSON file instead of system keyring.
    Suitable for environments where keyring is not available.

    Attributes:
        data_dir: Directory where session file is stored
        session_file: Full path to the session JSON file
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        """Initialize FileBasedSessionManager.

        Args:
            data_dir: Session file storage directory.
                      Default: /app/data (Docker) or ~/.note-mcp (local)
        """
        self.data_dir = data_dir or _get_default_data_dir()
        self.session_file = self.data_dir / SESSION_FILENAME

    def save(self, session: Session) -> None:
        """Save session to JSON file.

        Args:
            session: Session object to save

        Raises:
            OSError: If file cannot be written
        """
        # Ensure directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Write session data to file
        session_data = session.model_dump()
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)

        logger.debug(f"Session saved to {self.session_file}")

    def load(self) -> Session | None:
        """Load session from JSON file.

        Returns:
            Session object if found and valid, None otherwise
        """
        if not self.session_file.exists():
            logger.debug("No session file found")
            return None

        try:
            with open(self.session_file, encoding="utf-8") as f:
                session_data = json.load(f)

            # Import here to avoid circular imports
            from note_mcp.models import Session

            return Session(**session_data)
        except json.JSONDecodeError as e:
            # File is corrupted - distinct from "no session"
            logger.error(f"Session file corrupted (invalid JSON): {e}")
            logger.info(f"Consider deleting corrupted file: {self.session_file}")
            return None
        except (TypeError, ValueError) as e:
            # Invalid data structure
            logger.error(f"Session file has invalid data structure: {e}")
            logger.info(f"Consider deleting invalid file: {self.session_file}")
            return None
        except OSError as e:
            logger.warning(f"Failed to read session file: {e}")
            return None

    def clear(self) -> bool:
        """Clear session by deleting the session file.

        Returns:
            True if session was cleared successfully, False if deletion failed.
            Returns True if no session file exists (nothing to clear).
        """
        if not self.session_file.exists():
            logger.debug("No session file to clear")
            return True

        try:
            self.session_file.unlink()
            logger.debug(f"Session file deleted: {self.session_file}")
            return True
        except OSError as e:
            logger.error(f"Failed to delete session file (security concern): {e}")
            return False

    def has_session(self) -> bool:
        """Check if a session file exists.

        Returns:
            True if session file exists, False otherwise
        """
        return self.session_file.exists()

