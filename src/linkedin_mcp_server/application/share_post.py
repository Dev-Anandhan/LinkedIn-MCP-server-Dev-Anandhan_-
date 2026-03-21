"""Share a post on the user's LinkedIn feed."""

from pathlib import Path

from linkedin_mcp_server.domain.exceptions import ConfigurationError
from linkedin_mcp_server.ports.auth import AuthPort
from linkedin_mcp_server.ports.browser import BrowserPort

_SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}


class SharePostUseCase:
    """Share a post on the user's LinkedIn feed."""

    def __init__(self, browser: BrowserPort, auth: AuthPort, *, debug: bool = False):
        self._browser = browser
        self._auth = auth
        self._debug = debug

    async def execute(self, content: str, image_path: str | None = None) -> dict[str, str]:
        """Execute the share post use case.

        Args:
            content: The text content of the post.
            image_path: Optional absolute path to an image to attach.
        """
        await self._auth.ensure_authenticated()

        # Validate image path if provided
        if image_path:
            path = Path(image_path)
            if not path.is_file():
                raise ConfigurationError(f"Image file not found: {image_path}")
            if path.suffix.lower() not in _SUPPORTED_IMAGE_EXTENSIONS:
                raise ConfigurationError(
                    f"Unsupported image format '{path.suffix}'. "
                    f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTENSIONS))}"
                )

        await self._browser.create_post(content, image_path=image_path)
        message = "Post shared successfully"
        if image_path:
            message += " with image"
        return {"status": "success", "message": f"{message}."}

