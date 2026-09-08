"""Secure logging configuration for note-mcp.

Provides logging setup with cookie value masking for security.
Cookie values are completely masked in all log output.
"""

import logging
import re


class CookieMaskingFilter(logging.Filter):
    """Logging filter that masks cookie values for security.

    All cookie values are replaced with [MASKED] to prevent
    credential leakage in logs.
    """

    # Patterns to match cookie values in various formats
    COOKIE_PATTERNS = [
        # Match note_gql_auth_token=VALUE or _note_session_v5=VALUE
        re.compile(r"(note_gql_auth_token|_note_session_v5)[=:]\s*([^\s;,}\"']+)"),
        # Match cookie dict format {"name": "value"}
        re.compile(r'(["\']?(?:note_gql_auth_token|_note_session_v5)["\']?\s*[=:]\s*["\'])([^"\']+)(["\'])'),
        # Match Cookie header format
        re.compile(r"(Cookie:\s*[^;]*?(?:note_gql_auth_token|_note_session_v5)=)([^;\s]+)"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter and mask cookie values in log records.

        Args:
            record: Log record to process

        Returns:
            Always True (record is always passed through, just modified)
        """
        if record.msg:
            record.msg = self._mask_cookies(str(record.msg))
        if record.args:
            # Handle args that might contain sensitive data
            new_args: list[object] = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(self._mask_cookies(arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        return True

    def _mask_cookies(self, text: str) -> str:
        """Mask all cookie values in text.

        Args:
            text: Text potentially containing cookie values

        Returns:
            Text with cookie values replaced by [MASKED]
        """
        result = text
        for pattern in self.COOKIE_PATTERNS:
            # Replace the value part (group 2) with [MASKED]
            def mask_match(m: re.Match[str]) -> str:
                suffix = m.group(3) if len(m.groups()) > 2 else ""
                return m.group(1) + "[MASKED]" + suffix

            result = pattern.sub(mask_match, result)
        return result


def setup_logging(level: int = logging.INFO, name: str | None = None) -> logging.Logger:
    """Set up logging with cookie masking.

    Configures a logger with the CookieMaskingFilter to prevent
    credential leakage in log output.

    Args:
        level: Logging level (default: INFO)
        name: Logger name (default: "note_mcp")

    Returns:
        Configured logger instance
    """
    logger_name = name or "note_mcp"
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # Create console handler with formatting
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    # Add cookie masking filter
    handler.addFilter(CookieMaskingFilter())

    logger.addHandler(handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a logger instance with cookie masking.

    Gets or creates a child logger under the note_mcp namespace.
    All loggers created this way inherit the cookie masking filter.

    Args:
        name: Logger name suffix (e.g., "api" for "note_mcp.api")

    Returns:
        Logger instance
    """
    if name:
        return logging.getLogger(f"note_mcp.{name}")
    return logging.getLogger("note_mcp")

