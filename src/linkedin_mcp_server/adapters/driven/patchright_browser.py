"""Patchright browser adapter — BrowserPort implementation.

Handles browser lifecycle, page navigation, scrolling, modal dismissal,
rate limit detection, and HTML extraction.
"""

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from linkedin_mcp_server.domain.exceptions import (
    NetworkError,
    RateLimitError,
    ScrapingError,
    SessionExpiredError,
)
from linkedin_mcp_server.domain.value_objects import BrowserConfig, PageContent
from linkedin_mcp_server.ports.browser import BrowserPort

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = [
    "we've detected unusual activity",
    "you've reached the limit",
    "too many requests",
]

# URL patterns that indicate the session has expired mid-operation
_AUTH_REDIRECT_PATTERNS = [
    "/login",
    "/authwall",
    "/checkpoint",
    "/challenge",
    "/uas/login",
    "/uas/consumer-email-challenge",
]

# Realistic Chrome user agents — one is picked randomly per session
# when no custom user_agent is configured.
_UA_CHROME = "AppleWebKit/537.36 (KHTML, like Gecko)"
_USER_AGENT_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"{_UA_CHROME} Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"{_UA_CHROME} Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"{_UA_CHROME} Chrome/130.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"{_UA_CHROME} Chrome/130.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        f"{_UA_CHROME} Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        f"{_UA_CHROME} Chrome/130.0.0.0 Safari/537.36"
    ),
]


class PatchrightBrowserAdapter(BrowserPort):
    """BrowserPort implementation using Patchright persistent browser."""

    def __init__(self, config: BrowserConfig):
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def _ensure_browser(self) -> Page:
        """Lazy-initialize the browser on first use."""
        if self._page is not None:
            return self._page

        self._playwright = await async_playwright().start()

        user_data_dir = str(Path(self._config.user_data_dir).expanduser())

        # Use configured user agent or pick a random realistic one
        user_agent = self._config.user_agent or random.choice(_USER_AGENT_POOL)
        logger.info("Using user agent: %s", user_agent)

        launch_args: dict = {
            "headless": self._config.headless,
            "slow_mo": self._config.slow_mo,
        }

        if self._config.chrome_path:
            launch_args["executable_path"] = self._config.chrome_path

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir,
            **launch_args,
            viewport={
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            user_agent=user_agent,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.set_default_timeout(self._config.default_timeout)

        logger.info("Browser started with profile: %s", user_data_dir)
        return self._page

    # ── BrowserPort implementation ────────────────────────────────────────────

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        page = await self._ensure_browser()
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                await page.goto(url, wait_until=wait_until)
                # Detect mid-navigation auth redirects (session expired)
                self._check_auth_redirect(page.url, url)
                return
            except SessionExpiredError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "Navigation attempt %d/3 failed for %s: %s",
                    attempt,
                    url,
                    e,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)

        raise NetworkError(f"Navigation failed after 3 attempts: {url}") from last_error

    async def extract_page_html(self, url: str) -> PageContent:
        """Navigate, scroll, extract <main> innerHTML."""
        page = await self._ensure_browser()
        await self.navigate(url)

        await self._detect_rate_limit(page)
        await self._handle_modal_close(page)
        await self._wait_for_main(page)
        await self._scroll_to_bottom(page)

        html = await page.evaluate("""
            () => {
                const main = document.querySelector('main');
                return main ? main.innerHTML : document.body.innerHTML;
            }
        """)

        return PageContent(url=page.url, html=html or "")

    async def extract_overlay_html(self, url: str) -> PageContent:
        """Navigate, wait for dialog/modal, extract overlay innerHTML."""
        page = await self._ensure_browser()
        await self.navigate(url)

        try:
            await page.wait_for_selector(
                '[role="dialog"]',
                timeout=self._config.default_timeout,
            )
        except Exception:
            logger.warning("Overlay dialog not found for %s", url)

        html = await page.evaluate("""
            () => {
                const dialog = document.querySelector('[role="dialog"]');
                return dialog ? dialog.innerHTML : '';
            }
        """)

        return PageContent(url=page.url, html=html or "")

    async def extract_search_page_html(self, url: str) -> PageContent:
        """Navigate, scroll job sidebar, extract search results HTML."""
        page = await self._ensure_browser()
        await self.navigate(url)

        await self._detect_rate_limit(page)
        await self._handle_modal_close(page)
        await self._wait_for_main(page)
        await self._scroll_job_sidebar(page)

        html = await page.evaluate("""
            () => {
                const main = document.querySelector('main');
                return main ? main.innerHTML : document.body.innerHTML;
            }
        """)

        return PageContent(url=page.url, html=html or "")

    async def extract_job_ids(self) -> list[str]:
        """Extract job IDs from the currently loaded job search page."""
        page = await self._ensure_browser()

        try:
            return await page.evaluate("""
                () => {
                    const cards = document.querySelectorAll(
                        '[data-job-id], [data-occludable-job-id]'
                    );
                    const ids = new Set();
                    for (const card of cards) {
                        const jid = card.getAttribute('data-job-id')
                            || card.getAttribute('data-occludable-job-id')
                            || '';
                        const cleaned = jid.trim();
                        if (cleaned && /^\\d+$/.test(cleaned)) {
                            ids.add(cleaned);
                        }
                    }
                    return [...ids];
                }
            """)
        except Exception as e:
            logger.warning("Failed to extract job IDs: %s", e)
            return []

    async def get_total_search_pages(self) -> int | None:
        """Read total page count from LinkedIn pagination."""
        page = await self._ensure_browser()

        try:
            return await page.evaluate("""
                () => {
                    const pageState = document.querySelector(
                        '[data-test-pagination-page-btn]:last-of-type'
                    );
                    if (pageState) {
                        const text = pageState.textContent.trim();
                        const num = parseInt(text, 10);
                        return isNaN(num) ? null : num;
                    }
                    return null;
                }
            """)
        except Exception:
            return None

    async def get_current_url(self) -> str:
        page = await self._ensure_browser()
        return page.url

    async def get_cookies(self, urls: list[str] | None = None) -> list[dict[str, Any]]:
        """Return cookies from the browser context."""
        if not self._context:
            await self._ensure_browser()
        if not self._context:
            logger.warning("Browser context unavailable; returning no cookies.")
            return []
        try:
            if urls:
                return await self._context.cookies(urls)
            return await self._context.cookies()
        except Exception as e:
            logger.warning("Failed to read cookies: %s", e)
            return []

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Add cookies to the browser context."""
        if not self._context:
            # Force browser init so context is available
            await self._ensure_browser()
        if self._context:
            await self._context.add_cookies(cookies)

    def is_alive(self) -> bool:
        """Check if the browser instance is running and usable."""
        return self._page is not None and self._context is not None

    async def close(self) -> None:
        """Close browser and release resources. Browser can be re-initialized later."""
        if self._context:
            try:
                await self._context.close()
            except Exception as e:
                logger.warning("Error closing browser context: %s", e)
            self._context = None
            self._page = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("Error stopping playwright: %s", e)
            self._playwright = None

        logger.info("Browser closed")

    async def create_post(self, content: str, image_path: str | None = None) -> None:
        """Create a new post on the user's feed, optionally with an image."""
        page = await self._ensure_browser()
        await self.navigate("https://www.linkedin.com/feed/")
        await self._detect_rate_limit(page)
        await self._handle_modal_close(page)

        try:
            # Click "Start a post"
            trigger = page.locator('.share-box-feed-entry__trigger')
            await trigger.click(timeout=self._config.default_timeout)

            # Wait for modal
            await page.wait_for_selector('div[role="dialog"]', timeout=self._config.default_timeout)

            # The editor is usually contenteditable
            editor = page.locator('.ql-editor')
            await editor.click(timeout=5000)
            await page.keyboard.insert_text(content)

            # Upload image if provided
            if image_path:
                await self._upload_post_image(page, image_path)

            # Click Post
            post_btn = page.locator('button.share-actions__primary-action')
            await post_btn.click(timeout=5000)

            # Wait for dialog to disappear or success toast
            await page.wait_for_selector('div[role="dialog"]', state='hidden', timeout=15000)
            logger.info("Successfully created LinkedIn post.")
        except Exception as e:
            logger.error("Failed to create post: %s", e)
            raise ScrapingError(f"Failed to create post. UI might have changed: {e}") from e

    async def _upload_post_image(self, page: Page, image_path: str) -> None:
        """Upload an image to the LinkedIn post dialog.

        Uses Playwright's FileChooser API to intercept the native file dialog
        and set the file programmatically.
        """
        # LinkedIn's post dialog has a media toolbar with an image/photo button
        media_btn = page.locator(
            'button[aria-label="Add a photo"], '
            'button[aria-label="Add media"], '
            'button[aria-label="Add a photo or video"]'
        )

        # Use expect_file_chooser to intercept the native file dialog
        async with page.expect_file_chooser(timeout=self._config.default_timeout) as fc_info:
            await media_btn.click(timeout=5000)

        file_chooser = await fc_info.value
        await file_chooser.set_files(image_path)

        # Wait for the image preview/thumbnail to appear in the dialog
        await page.wait_for_selector(
            'div[role="dialog"] img[class*="share-media"], '
            'div[role="dialog"] .share-box-image-preview, '
            'div[role="dialog"] .media-preview, '
            'div[role="dialog"] img[src*="blob:"], '
            'div[role="dialog"] .share-promoted-detour-feed-update-v2__image-container',
            timeout=15000,
        )
        logger.info("Image uploaded successfully: %s", image_path)
        # Small delay for upload processing
        await asyncio.sleep(1)

    async def apply_for_job(self, job_id: str) -> bool:
        """Attempt to Easy Apply for a job. Returns True if successful, False if blocked by manual input."""
        page = await self._ensure_browser()
        await self.navigate(f"https://www.linkedin.com/jobs/view/{job_id}/")
        await self._detect_rate_limit(page)
        
        try:
            # Check for Easy Apply button
            apply_btn = page.locator('button.jobs-apply-button')
            
            if not await apply_btn.is_visible(timeout=5000):
                logger.warning("No Easy Apply button found for job %s", job_id)
                return False
                
            btn_text = await apply_btn.inner_text()
            if "Easy Apply" not in btn_text:
                logger.warning("Job %s uses external apply: %s", job_id, btn_text)
                return False
                
            await apply_btn.click()
            await page.wait_for_selector('div[role="dialog"]', timeout=10000)
            
            # Iterate through the Easy Apply steps
            for _ in range(10):
                # Try to find the primary button in the footer
                next_btn = page.locator('div[role="dialog"] footer button.artdeco-button--primary')
                if not await next_btn.is_visible(timeout=5000):
                    break
                    
                text = await next_btn.inner_text()
                text = text.strip()
                
                if text == "Submit application":
                    await next_btn.click()
                    await page.wait_for_selector('div[role="dialog"]', state='hidden', timeout=10000)
                    logger.info("Successfully submitted application for job %s", job_id)
                    return True
                elif text in ("Next", "Review"):
                    # We might need to click Next but often there are required fields
                    # If the page doesn't change after clicking Next, it means there's an error (required field)
                    # Let's track the dialog content
                    content_before = await page.locator('div[role="dialog"]').inner_html()
                    await next_btn.click()
                    await asyncio.sleep(2)  # Wait for transition
                    content_after = await page.locator('div[role="dialog"]').inner_html()
                    
                    if content_before == content_after:
                        # Error messages likely appeared
                        if await page.locator('.artdeco-inline-feedback--error').count() > 0:
                            logger.warning("Manual input required for job %s application.", job_id)
                            # Close the modal safely
                            dismiss = page.locator('button[data-test-modal-close-btn]')
                            if await dismiss.is_visible():
                                await dismiss.click()
                                discard = page.locator('button[data-control-name="discard_application_confirm_btn"]')
                                if await discard.is_visible(timeout=2000):
                                    await discard.click()
                            return False
                else:
                    logger.warning("Unknown primary button text: %s", text)
                    break
                    
            return False
        except Exception as e:
            logger.error("Error during Easy Apply for job %s: %s", job_id, e)
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _check_auth_redirect(current_url: str, requested_url: str) -> None:
        """Detect if LinkedIn redirected us to a login page mid-operation.

        This catches session expiry during navigation — e.g., the user
        was authenticated when the server started but the cookie expired
        while scraping.
        """
        # Don't flag when we intentionally navigate to login pages
        if any(pattern in requested_url for pattern in _AUTH_REDIRECT_PATTERNS):
            return

        if any(pattern in current_url for pattern in _AUTH_REDIRECT_PATTERNS):
            logger.warning(
                "Auth redirect detected: requested %s, landed on %s",
                requested_url,
                current_url,
            )
            raise SessionExpiredError(
                "LinkedIn session expired during navigation. Please re-authenticate with --login."
            )

    async def _detect_rate_limit(self, page: Page) -> None:
        """Check if LinkedIn is rate-limiting and raise if so."""
        try:
            body_text = await page.evaluate("() => document.body.innerText.toLowerCase()")
            for marker in _RATE_LIMIT_MARKERS:
                if marker in body_text:
                    raise RateLimitError(
                        f"Rate limit detected on {page.url}",
                        suggested_wait_time=300,
                    )
        except RateLimitError:
            raise
        except Exception:
            pass  # Don't fail hard on detection errors

    async def _handle_modal_close(self, page: Page) -> None:
        """Dismiss any modal overlays (cookie consent, etc.)."""
        try:
            dismiss_btn = page.locator(
                'button:has-text("Dismiss"), '
                'button[aria-label="Dismiss"], '
                'button:has-text("Got it"), '
                'button:has-text("Accept")'
            ).first
            if await dismiss_btn.is_visible(timeout=1000):
                await dismiss_btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass  # Modal dismissal is best-effort

    async def _wait_for_main(self, page: Page) -> None:
        """Wait for the <main> element to appear."""
        try:
            await page.wait_for_selector("main", timeout=self._config.default_timeout)
        except Exception:
            logger.warning("Main element not found on %s", page.url)

    async def _scroll_to_bottom(self, page: Page) -> None:
        """Scroll page to load lazy content."""
        try:
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const height = () => document.body.scrollHeight;
                    let prev = 0;
                    while (height() !== prev) {
                        prev = height();
                        window.scrollTo(0, prev);
                        await delay(800);
                    }
                }
            """)
        except Exception as e:
            logger.debug("Scroll error (non-fatal): %s", e)

    async def _scroll_job_sidebar(self, page: Page) -> None:
        """Scroll the job sidebar to load all job cards."""
        try:
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const sidebar = document.querySelector(
                        '.jobs-search-results-list, [class*="jobs-search"]'
                    );
                    if (!sidebar) return;
                    let prev = 0;
                    for (let i = 0; i < 20; i++) {
                        sidebar.scrollTop = sidebar.scrollHeight;
                        await delay(600);
                        if (sidebar.scrollTop === prev) break;
                        prev = sidebar.scrollTop;
                    }
                }
            """)
        except Exception as e:
            logger.debug("Job sidebar scroll error (non-fatal): %s", e)
