"""Diagnostic-related MCP tool registrations."""

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.adapters.driving.error_mapping import map_domain_error
from linkedin_mcp_server.application.diagnose_link import DiagnoseLinkUseCase


def register_diagnostic_tools(
    mcp: FastMCP,
    diagnose_link_uc: DiagnoseLinkUseCase,
) -> None:
    """Register diagnostic-related MCP tools."""

    @mcp.tool(
        name="diagnose_link",
        description=(
            "Verify if a URL is accessible by LinkedIn's crawler (bot).\n\n"
            "This checks for missing Open Graph (OG) tags, bot blocking (WAF/Cloudflare), "
            "and other common issues that cause link preview failures on LinkedIn.\n\n"
            "Args:\n"
            "    url: The absolute URL to diagnose (e.g., https://example.com)."
        ),
    )
    async def diagnose_link(
        url: str,
        ctx: Context,
    ) -> dict[str, Any]:
        try:
            return await diagnose_link_uc.execute(url)
        except Exception as e:
            map_domain_error(e, "diagnose_link")
