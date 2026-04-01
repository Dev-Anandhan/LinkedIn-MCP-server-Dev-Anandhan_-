"""Diagnose a link for LinkedIn crawler compatibility."""

import re
from typing import Any

from linkedin_mcp_server.ports.browser import BrowserPort

_URL_PATTERN = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+')


class DiagnoseLinkUseCase:
    """Diagnose a link for LinkedIn crawler compatibility."""

    def __init__(self, browser: BrowserPort):
        self._browser = browser

    async def execute(self, url: str) -> dict[str, Any]:
        """Execute the diagnose link use case.

        Args:
            url: The URL to diagnose.

        Returns:
            A dictionary containing accessibility status and recommendations.
        """
        result = await self._browser.check_url_accessibility(url)

        # Add analysis/recommendations based on the result
        recommendations = []
        if not result.get("ok"):
            if result.get("is_local"):
                recommendations.append(
                    "LinkedIn CANNOT access localhost or private URLs. "
                    "Deploy your site to a public server (Vercel, Netlify, etc.)."
                )
            elif result.get("status") in (401, 403, 429):
                recommendations.append(
                    "Your site might be blocking the LinkedIn bot. "
                    "Ensure 'LinkedInBot' is whitelisted in your Cloudflare/WAF settings."
                )
            elif result.get("status") == 0:
                recommendations.append(
                    f"Connection failed: {result.get('error')}. "
                    "Ensure the URL is public and HTTPS is preferred."
                )
            else:
                recommendations.append(
                    f"The URL returned status {result.get('status')}. "
                    "LinkedIn might fail to scrape this content."
                )

        og_tags = result.get("og_tags", {})
        missing = [tag for tag, present in og_tags.items() if not present]
        if missing:
            recommendations.append(
                f"Missing or broken Open Graph tags: {', '.join(missing)}. "
                "LinkedIn depends heavily on og:title, og:image, and og:description."
            )

        if result.get("is_blocked"):
            recommendations.append(
                "Bot protection detected. LinkedIn's crawler will likely be blocked."
            )

        result["recommendations"] = recommendations
        return result

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Extract URLs from a text string."""
        return _URL_PATTERN.findall(text)
