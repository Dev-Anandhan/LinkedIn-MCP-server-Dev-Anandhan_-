"""Patchright browser adapter — BrowserPort implementation.

Handles browser lifecycle, page navigation, scrolling, modal dismissal,
rate limit detection, and HTML extraction.
"""

import asyncio
import logging
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

        # Use configured user agent or pick a consistent realistic one
        user_agent = self._config.user_agent or _USER_AGENT_POOL[0]
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
        self._page.set_default_timeout(30000) # Increased to 30s for better reliability in headless mode

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
        # Navigate directly to the active share modal URL to bypass brittle UI selectors
        await self.navigate("https://www.linkedin.com/feed/?shareActive=true")
        await self._detect_rate_limit(page)
        await self._handle_modal_close(page)

        # Give the feed and the modal ample time to fully render, especially on cold starts
        await asyncio.sleep(10)

        trigger_selectors = [
            'div[role="button"]:has-text("Start a post")',
            'button:has-text("Start a post")',
            '.share-box-feed-entry__trigger',
            '.share-box-feed-entry-v2__trigger',
            'button[data-control-name="sharebox-start-post"]',
            'div.share-box-feed-entry__wrapper button'
        ]

        try:
            # Check if the modal opened automatically from ?shareActive=true
            modal_opened = False
            try:
                # Wait for the specific artdeco-modal — intensified wait for slow loads
                await page.wait_for_selector(
                    'div.artdeco-modal, div.share-box-v2__modal, div[role="dialog"]',
                    state='visible',
                    timeout=15000
                )
                modal_opened = True
                logger.info("Post modal opened automatically via URL parameter.")
            except Exception:
                logger.debug("Modal didn't open automatically, checking for trigger...")
                pass

            if not modal_opened:
                # Attempt to find and click the trigger with retries
                for attempt in range(1, 4):
                    try:
                        logger.debug("Attempt %d/3: Clicking 'Start a post' trigger...", attempt)
                        trigger = page.locator(", ".join(trigger_selectors)).first
                        await trigger.wait_for(state="visible", timeout=15000)
                        await trigger.click(timeout=10000, force=True)

                        # Verify modal opened after click
                        await page.wait_for_selector(
                            'div.artdeco-modal, div.share-box-v2__modal, div[role="dialog"]',
                            state='visible',
                            timeout=10000
                        )
                        modal_opened = True
                        break
                    except Exception as e:
                        logger.warning("Trigger click attempt %d failed: %s", attempt, e)
                        await asyncio.sleep(3)
                        # Refresh or check if we are still on the feed
                        if attempt < 3 and "feed" not in page.url:
                            await page.goto("https://www.linkedin.com/feed/?shareActive=true", wait_until="domcontentloaded")

            if not modal_opened:
                raise ScrapingError("Could not open post creation modal after multiple attempts.")
            # Final modal stabilization — handle the loading spinner if present
            logger.debug("Performing final modal stabilization...")
            spinner = page.locator('.artdeco-loader, .share-box-v2__loading-box').first
            if await spinner.count() > 0:
                logger.debug("Detected loading spinner in modal, waiting for stabilization...")
                try:
                    await spinner.wait_for(state='hidden', timeout=15000)
                except Exception:
                    logger.warning("Modal spinner didn't disappear within timeout, proceeding anyway")

            await asyncio.sleep(2)

            # Upload image FIRST if provided
            # This prevents LinkedIn's modal transitions from clearing the editor state
            if image_path:
                await self._upload_post_image(page, image_path)
                await asyncio.sleep(1)

            # Re-focus into the editor and type content — multiple fallback selectors
            editor = page.locator(
                '.ql-editor:visible, '
                'div[role="textbox"]:visible, '
                'div[contenteditable="true"]:visible, '
                'div[role="textbox"][aria-multiline="true"]:visible, '
                'div[contenteditable="true"][role="textbox"]:visible, '
                'div[contenteditable="true"][data-placeholder]:visible'
            ).first

            await editor.click(timeout=10000)
            # Use insert_text to handle technical characters and preserve formatting
            await page.keyboard.insert_text(content)
            await asyncio.sleep(1)

            # Click Post button — multiple fallback selectors with exact match preference
            post_btn = page.locator(
                'button.share-actions__primary-action:visible, '
                'button:text("Post"):visible, '
                'button[data-control-name="sharebox-post"]:visible, '
                'div[role="dialog"] footer button:has-text("Post"):visible, '
                '.artdeco-button--primary:has-text("Post"):visible'
            ).first

            # Wait for button to be enabled (image processing can disable it briefly)
            # Increased timeout to 20s for slow media processing on LinkedIn's end
            await post_btn.wait_for(state="visible", timeout=20000)
            for _ in range(20): # Up to 10s wait
                if not await post_btn.is_disabled():
                    break
                logger.info("Waiting for 'Post' button to be enabled after media processing...")
                await asyncio.sleep(0.5)

            await post_btn.click(timeout=15000)

            # Wait for dialog to disappear or success toast
            # Increased timeout as image posts take longer to process/submit
            await page.wait_for_selector(
                'div.artdeco-modal, div.share-box-v2__modal, div[role="dialog"]',
                state='hidden',
                timeout=30000
            )
            logger.info("Successfully created LinkedIn post.")
        except (SessionExpiredError, RateLimitError):
            raise
        except Exception as e:
            logger.error("Failed to create post: %s", e)
            # PROACTIVE DIAGNOSTIC: Capture screenshot and HTML on failure ONLY if debug is enabled
            # This prevents leaking PII for standard users.
            if getattr(self._config, "debug", False):
                try:
                    debug_path = "debug_post_failure.png"
                    await page.screenshot(path=debug_path)
                    logger.info("Failure screenshot captured to: %s", debug_path)
                    with open("debug_post_failure.html", "w", encoding="utf-8") as f:
                        f.write(await page.content())
                except Exception as capture_e:
                    logger.warning("Failed to capture diagnostic info: %s", capture_e)

            raise ScrapingError(
                f"Failed to create post. UI might have changed: {e}"
            ) from e

    async def _upload_post_image(self, page: Page, image_path: str) -> None:
        """Upload an image to the LinkedIn post dialog.

        Uses Playwright's FileChooser API to intercept the native file dialog
        and set the file programmatically.
        """
        # LinkedIn's post dialog has a media toolbar — multiple fallback selectors
        media_btn = page.locator(
            'button[aria-label="Add a photo"], '
            'button[aria-label="Add media"], '
            'button[aria-label="Add a photo or video"], '
            'button[aria-label="Add media to your post"], '
            '.share-media-button, '
            'button:has-text("Photo"), '
            'div[role="dialog"] button:has-text("Photo")'
        ).first

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
            'div[role="dialog"] .share-promoted-detour-feed-update-v2__image-container, '
            'div[role="dialog"] img[alt]',
            timeout=15000,
        )

        # After selecting the file, LinkedIn often shows an editor/cropper modal
        # We need to click "Next" or "Done" to finalize the selection
        finish_btn = page.locator(
            'button.share-box-footer__primary-btn:visible, '
            'div[role="dialog"] button:has-text("Next"):visible, '
            'div[role="dialog"] button:has-text("Done"):visible, '
            'button.share-media-editor__next-button:visible, '
            'button.share-media-editor__done-button:visible'
        ).first

        if await finish_btn.is_visible(timeout=5000):
            logger.info("Clicking Next/Done in media editor...")
            await finish_btn.click()
            await asyncio.sleep(1)

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

    async def check_url_accessibility(self, url: str) -> dict[str, Any]:
        """Check if a URL is accessible by the LinkedInBot user agent."""
        # Ensure playwright is started
        if not self._playwright:
            self._playwright = await async_playwright().start()

        # LinkedInBot sample user agent
        ua = "LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)"

        request_context = await self._playwright.request.new_context(user_agent=ua)
        try:
            # Check for localhost/internal URLs
            if "localhost" in url or "127.0.0.1" in url:
                return {
                    "ok": False,
                    "status": 0,
                    "error": "Localhost/Private URLs are not accessible by LinkedIn's crawler.",
                    "is_local": True
                }

            # LinkedIn crawler uses its own backend, so we simulate it via a headless request
            response = await request_context.get(url, timeout=10000)
            status = response.status
            body = await response.text()

            # Simple metadata detection
            lowered_body = body.lower()
            og_tags = {
                "title": 'property="og:title"' in lowered_body or "property='og:title'" in lowered_body,
                "description": 'property="og:description"' in lowered_body or "property='og:description'" in lowered_body,
                "image": 'property="og:image"' in lowered_body or "property='og:image'" in lowered_body,
                "url": 'property="og:url"' in lowered_body or "property='og:url'" in lowered_body,
            }

            # Bot blocking detection (Cloudflare, etc often return 403/401)
            is_blocked = status in (401, 403, 429) or "cloudflare" in lowered_body

            return {
                "ok": response.ok and not is_blocked,
                "status": status,
                "og_tags": og_tags,
                "is_blocked": is_blocked,
                "content_preview": body[:1000] if body else "",
                "headers": dict(response.headers)
            }
        except Exception as e:
            logger.error("Failed to check URL accessibility: %s", e)
            return {
                "ok": False,
                "status": 0,
                "error": str(e)
            }
        finally:
            await request_context.dispose()

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
