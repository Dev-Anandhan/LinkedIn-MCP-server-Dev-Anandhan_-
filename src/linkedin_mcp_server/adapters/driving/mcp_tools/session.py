"""Session-related MCP tool registrations."""

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.adapters.driving.error_mapping import map_domain_error
from linkedin_mcp_server.application.manage_session import ManageSessionUseCase


def register_session_tools(
    mcp: FastMCP,
    manage_session_uc: ManageSessionUseCase,
) -> None:
    """Register session-related MCP tools."""

    @mcp.tool(
        name="close_browser",
        description="Close the browser instance and release resources. Credentials are preserved.",
    )
    async def close_browser(ctx: Context) -> dict[str, Any]:
        try:
            result = await manage_session_uc.close_browser()
            return {
                "is_valid": result.is_valid,
                "message": result.message,
            }
        except Exception as e:
            map_domain_error(e, "close_browser")

    @mcp.tool(
        name="check_session_status",
        description="Check if the current LinkedIn session is authenticated and valid.",
    )
    async def check_session_status(ctx: Context) -> dict[str, Any]:
        try:
            result = await manage_session_uc.check_status()
            return {
                "is_valid": result.is_valid,
                "message": result.message,
            }
        except Exception as e:
            map_domain_error(e, "check_session_status")

    @mcp.tool(
        name="start_login",
        description=(
            "Launch an interactive browser window to log in to LinkedIn. "
            "The user will complete the login in the pop-up window."
        ),
    )
    async def start_login(ctx: Context) -> dict[str, Any]:
        try:
            # Fast login with minimal warmup for the one-shot flow
            result = await manage_session_uc.login(warm_up=False)
            return {
                "is_valid": result.is_valid,
                "message": result.message,
            }
        except Exception as e:
            map_domain_error(e, "start_login")

    @mcp.tool(
        name="logout_and_cleanup",
        description=(
            "Close the browser and completely wipe all stored LinkedIn "
            "credentials, cookies, and profile data for privacy."
        ),
    )
    async def logout_and_cleanup(ctx: Context) -> dict[str, Any]:
        try:
            await manage_session_uc.close_browser()
            result = manage_session_uc.logout()
            return {
                "is_valid": result.is_valid,
                "message": "Session closed and completely wiped from disk.",
            }
        except Exception as e:
            map_domain_error(e, "logout_and_cleanup")
