"""Post-related MCP tool registrations."""

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.adapters.driving.error_mapping import map_domain_error
from linkedin_mcp_server.application.share_post import SharePostUseCase


def register_post_tools(
    mcp: FastMCP,
    share_post_uc: SharePostUseCase,
) -> None:
    """Register post-related MCP tools."""

    @mcp.tool(
        name="share_post",
        description=(
            "Create a new post on the user's LinkedIn feed.\n\n"
            "Args:\n"
            "    content: The text content of the post.\n"
            "    image_path: Optional absolute file path to an image "
            "(jpg, png, gif) to attach to the post."
        ),
    )
    async def share_post(
        content: str,
        ctx: Context,
        image_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await share_post_uc.execute(content, image_path=image_path)
        except Exception as e:
            map_domain_error(e, "share_post")

