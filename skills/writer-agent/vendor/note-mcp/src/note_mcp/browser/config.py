"""Browser configuration utilities.

Provides configuration functions for browser automation.
"""

import os

# Environment variable name for headless mode configuration
HEADLESS_ENV_VAR = "NOTE_MCP_TEST_HEADLESS"


def get_headless_mode() -> bool:
    """Get headless mode from NOTE_MCP_TEST_HEADLESS environment variable.

    Default: True (headless mode for CI/CD stability)
    Set NOTE_MCP_TEST_HEADLESS=false to show browser window for debugging.

    Returns:
        True if headless mode is enabled (default)
    """
    return os.environ.get(HEADLESS_ENV_VAR, "true").lower() != "false"

