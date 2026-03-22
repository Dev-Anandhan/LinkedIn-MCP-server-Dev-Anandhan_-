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
        # Validate image path if provided early
        if image_path:
            path = Path(image_path)
            if not path.is_file():
                raise ConfigurationError(f"Image file not found: {image_path}")
            if path.suffix.lower() not in _SUPPORTED_IMAGE_EXTENSIONS:
                raise ConfigurationError(
                    f"Unsupported image format '{path.suffix}'. "
                    f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTENSIONS))}"
                )

        try:
            # Optimization: Try direct navigation first to save a full page load
            # BrowserAdapter.navigate will raise SessionExpiredError if we are redirected to login
            try:
                await self._browser.navigate("https://www.linkedin.com/feed/?shareActive=true")
            except Exception:
                # Fallback: full auth check (imports cookies) and then navigate
                await self._auth.ensure_authenticated()
                await self._browser.navigate("https://www.linkedin.com/feed/?shareActive=true")

            await self._browser.create_post(content, image_path=image_path)
        except Exception as e:
            # If a post fails and contains a URL, it might be due to LinkedIn's crawler
            if "http" in content or "www." in content:
                from linkedin_mcp_server.application.diagnose_link import DiagnoseLinkUseCase
                urls = DiagnoseLinkUseCase.extract_urls(content)
                if urls:
                    hint = (
                        "\n\nTIP: This post contains links. If the 'Post' button was disabled "
                        "or a preview error appeared, try running 'diagnose_link' with "
                        f"one of these URLs: {', '.join(urls)}"
                    )
                    # Re-raise with the hint appended to the message
                    if hasattr(e, "message"):
                        e.message += hint # type: ignore
                    else:
                        header = f"{e}"
                        raise type(e)(f"{header}{hint}") from e
            raise

        message = "Post shared successfully"
        if image_path:
            message += " with image"
        return {"status": "success", "message": f"{message}."}

