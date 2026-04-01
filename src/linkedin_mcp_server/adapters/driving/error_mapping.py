"""Domain exception → MCP ToolError mapping.

This is the ONLY file that imports from fastmcp.exceptions.
All domain errors are translated to structured JSON ToolError messages here.
"""

import json
import logging
from datetime import UTC, datetime
from typing import NoReturn

from fastmcp.exceptions import ToolError

from linkedin_mcp_server.domain.exceptions import (
    AuthenticationError,
    ConfigurationError,
    LinkedInMCPError,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)


def _format_error(code: str, message: str, retryable: bool) -> str:
    """Format the error as a structured JSON string."""
    return json.dumps({
        "error": code,
        "message": message,
        "retryable": retryable,
        "timestamp": datetime.now(UTC).isoformat()
    })


def map_domain_error(exception: Exception, context: str = "") -> NoReturn:
    """Map domain exceptions to ToolError for MCP clients.

    Args:
        exception: The caught exception
        context: Optional context string (e.g. tool name)

    Raises:
        ToolError: Always, with a user-friendly structured JSON message
    """
    prefix = f"[{context}] " if context else ""

    if isinstance(exception, SessionExpiredError):
        raise ToolError(_format_error(
            "SESSION_EXPIRED",
            f"{prefix}LinkedIn session expired during operation. Please re-authenticate by running the server with --login.",
            retryable=False
        )) from exception

    if isinstance(exception, AuthenticationError):
        raise ToolError(_format_error(
            "AUTH_REQUIRED",
            f"{prefix}Authentication required. Please run the server with --login to authenticate first.",
            retryable=False
        )) from exception

    if isinstance(exception, RateLimitError):
        wait_mins = getattr(exception, "suggested_wait_time", 300) // 60
        raise ToolError(_format_error(
            "RATE_LIMIT_EXCEEDED",
            f"{prefix}LinkedIn rate limit detected. Please wait ~{wait_mins} minutes before retrying.",
            retryable=True
        )) from exception

    if isinstance(exception, ProfileNotFoundError):
        raise ToolError(_format_error(
            "NOT_FOUND",
            f"{prefix}Profile or resource not found. Please check the provided ID/URL.",
            retryable=False
        )) from exception

    if isinstance(exception, NetworkError):
        raise ToolError(_format_error(
            "NETWORK_ERROR",
            f"{prefix}Network error. Please check your connection and try again.",
            retryable=True
        )) from exception

    if isinstance(exception, ScrapingError):
        raise ToolError(_format_error(
            "SCRAPING_FAILED",
            f"{prefix}Failed to extract or interact with data on the page. The page structure may have changed.",
            retryable=False
        )) from exception

    if isinstance(exception, ConfigurationError):
        raise ToolError(_format_error(
            "CONFIG_ERROR",
            f"{prefix}Configuration error: {exception}",
            retryable=False
        )) from exception

    if isinstance(exception, LinkedInMCPError):
        raise ToolError(_format_error(
            "DOMAIN_ERROR",
            f"{prefix}{exception}",
            retryable=False
        )) from exception

    # Unknown exception — log and re-raise with masked details
    logger.exception("Unexpected error in %s", context or "unknown context")
    raise ToolError(_format_error(
        "INTERNAL_ERROR",
        f"{prefix}An unexpected error occurred. Check server logs for details.",
        retryable=False
    )) from exception
