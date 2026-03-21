"""Share a post on the user's LinkedIn feed."""

from linkedin_mcp_server.ports.auth import AuthPort
from linkedin_mcp_server.ports.browser import BrowserPort


class SharePostUseCase:
    """Share a post on the user's LinkedIn feed."""

    def __init__(self, browser: BrowserPort, auth: AuthPort, *, debug: bool = False):
        self._browser = browser
        self._auth = auth
        self._debug = debug

    async def execute(self, content: str) -> dict[str, str]:
        """Execute the share post use case."""
        await self._auth.ensure_authenticated()
        await self._browser.create_post(content)
        return {"status": "success", "message": "Post shared successfully."}
