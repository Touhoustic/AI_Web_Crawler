"""
browser.py — Playwright-based browser automation layer.
Handles page navigation, content retrieval, and human-like interaction simulation.
"""

import asyncio
import random
from contextlib import asynccontextmanager
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)
from utils import logger, human_delay

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

DEFAULT_TIMEOUT = 20_000          # ms — page load timeout
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


# ─────────────────────────────────────────────
# BrowserWrapper
# ─────────────────────────────────────────────

class BrowserWrapper:
    """
    Thin async wrapper around Playwright providing:
      - Headless / headed mode toggle
      - Single-context session reuse
      - Retry logic on navigation failures
      - Human-like scrolling and delays
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        """Launch the browser and create a persistent context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )
        # Mask navigator.webdriver
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info(f"🌐 Browser started (headless={self.headless})")

    async def stop(self) -> None:
        """Close browser and playwright instance cleanly."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("🛑 Browser stopped.")

    async def fetch_page(self, url: str, retries: int = 2) -> dict | None:
        """
        Navigate to `url`, wait for the DOM, optionally scroll,
        and return a dict with {url, html, status}.

        Returns None on unrecoverable failure.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")

        page: Page = await self._context.new_page()
        try:
            for attempt in range(1, retries + 2):
                try:
                    logger.info(f"🔍 Fetching: {url}  (attempt {attempt})")
                    response = await page.goto(
                        url,
                        timeout=DEFAULT_TIMEOUT,
                        wait_until="domcontentloaded",
                    )
                    status = response.status if response else 0

                    if status >= 400:
                        logger.warning(f"  HTTP {status} for {url}")
                        return {"url": url, "html": "", "status": status}

                    # Give JS a moment to settle
                    await asyncio.sleep(random.uniform(0.8, 1.8))

                    # Simulate human scrolling
                    await self._scroll_page(page)

                    html = await page.content()
                    final_url = page.url          # May differ after redirects

                    logger.info(f"  ✅ Loaded ({len(html):,} bytes, HTTP {status})")
                    return {"url": final_url, "html": html, "status": status}

                except PlaywrightTimeout:
                    logger.warning(f"  ⏱ Timeout on attempt {attempt} for {url}")
                    if attempt <= retries:
                        await asyncio.sleep(2)
                    else:
                        return None

                except PlaywrightError as e:
                    logger.warning(f"  ⚠️  Playwright error: {e}")
                    return None

        finally:
            await page.close()

    async def _scroll_page(self, page: Page, steps: int = 3) -> None:
        """Scroll down the page in stages to trigger lazy-loaded content."""
        try:
            scroll_height = await page.evaluate("document.body.scrollHeight")
            step_px = max(scroll_height // steps, 300)
            for i in range(1, steps + 1):
                await page.evaluate(f"window.scrollTo(0, {step_px * i})")
                await asyncio.sleep(random.uniform(0.3, 0.7))
            # Scroll back to top
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass   # Scrolling is best-effort


# ─────────────────────────────────────────────
# Convenience async context manager
# ─────────────────────────────────────────────

@asynccontextmanager
async def managed_browser(headless: bool = True):
    """
    Async context manager that yields a ready BrowserWrapper
    and ensures clean shutdown.

    Usage:
        async with managed_browser() as browser:
            result = await browser.fetch_page("https://example.com")
    """
    wrapper = BrowserWrapper(headless=headless)
    await wrapper.start()
    try:
        yield wrapper
    finally:
        await wrapper.stop()
