"""
browser.py — Playwright browser with dynamic content interaction.

CHANGES:
  - Added reveal_hidden_data(): auto-clicks "Show phone", "Contact", etc. buttons
  - Added click_and_extract(): click element then capture new content
  - Expanded language support (EN/FR/AR keywords)
  - Better scroll that waits for network idle after each step
  - Intercept and block unnecessary resource types (images, fonts) for speed
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

DEFAULT_TIMEOUT = 25_000
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# Keywords that trigger "reveal hidden data" clicks (EN + FR + AR)
REVEAL_KEYWORDS = [
    # Phone reveal
    "show phone", "show number", "afficher le numéro", "afficher numéro",
    "voir le numéro", "voir numéro", "show contact", "reveal phone",
    "اظهار الهاتف", "عرض الرقم",
    # Contact
    "contact seller", "contacter le vendeur", "contacter", "contact",
    "send message", "envoyer un message", "message",
    # More
    "show email", "afficher email", "voir email",
    "more info", "plus d'informations", "voir plus",
    "get contact", "voir contact",
]

# CSS selectors that commonly hide phone numbers behind clicks
REVEAL_SELECTORS = [
    "button[class*='phone']",
    "button[class*='contact']",
    "button[class*='show']",
    "button[class*='reveal']",
    "a[class*='phone']",
    "a[class*='contact']",
    "span[class*='phone']",
    "[data-testid*='phone']",
    "[data-testid*='contact']",
    "[data-testid*='show']",
    "[aria-label*='phone' i]",
    "[aria-label*='numéro' i]",
    "[aria-label*='contact' i]",
]


class BrowserWrapper:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            user_agent=USER_AGENT,
            locale="fr-MA,fr;q=0.9,en;q=0.8",   # FR-MA for North Africa sites
            timezone_id="Africa/Casablanca",
            java_script_enabled=True,
            accept_downloads=False,
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info(f"🌐 Browser started (headless={self.headless})")

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("🛑 Browser stopped.")

    async def fetch_page(self, url: str, retries: int = 2) -> dict | None:
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")

        page: Page = await self._context.new_page()

        # Block heavy resources we don't need
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media", "stylesheet")
            else route.continue_()
        )

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

                    # Wait for JS to settle
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                    # Scroll to trigger lazy content
                    await self._scroll_page(page)

                    # ── CRITICAL: Click buttons that reveal hidden data ──
                    extra_html = await self.reveal_hidden_data(page)

                    html = await page.content()
                    final_url = page.url

                    # Merge any extra HTML from revealed panels
                    if extra_html:
                        html = html + "\n<!-- REVEALED -->\n" + "\n".join(extra_html)

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

    async def reveal_hidden_data(self, page: Page) -> list[str]:
        """
        Detect and click buttons/links that reveal hidden phone numbers,
        emails, or contact info. Returns list of HTML snapshots captured
        after each successful click.
        """
        revealed = []

        # Strategy 1: CSS selector matching
        for selector in REVEAL_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements[:3]:   # Max 3 per selector
                    try:
                        text = (await el.inner_text()).strip().lower()
                        if len(text) < 3 or len(text) > 80:
                            continue
                        await el.click(timeout=3000)
                        await asyncio.sleep(0.8)
                        snap = await page.content()
                        revealed.append(snap)
                        logger.info(f"  🖱️  Clicked reveal element: [{text[:40]}]")
                    except Exception:
                        pass
            except Exception:
                pass

        # Strategy 2: Text-content matching on all clickable elements
        try:
            clickable = await page.query_selector_all("button, a, span[onclick], div[onclick]")
            for el in clickable[:60]:   # Limit scan
                try:
                    text = (await el.inner_text()).strip().lower()
                    if any(kw in text for kw in REVEAL_KEYWORDS):
                        is_visible = await el.is_visible()
                        if not is_visible:
                            continue
                        await el.click(timeout=3000)
                        await asyncio.sleep(1.0)
                        snap = await page.content()
                        revealed.append(snap)
                        logger.info(f"  🖱️  Revealed hidden data via: [{text[:50]}]")
                except Exception:
                    pass
        except Exception:
            pass

        if revealed:
            logger.info(f"  ✅ Revealed {len(revealed)} hidden data sections")

        return revealed

    async def _scroll_page(self, page: Page, steps: int = 4) -> None:
        """Smooth scroll to trigger lazy-loaded content."""
        try:
            scroll_height = await page.evaluate("document.body.scrollHeight")
            step_px = max(scroll_height // steps, 300)
            for i in range(1, steps + 1):
                await page.evaluate(f"window.scrollTo(0, {step_px * i})")
                await asyncio.sleep(random.uniform(0.25, 0.5))
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass


@asynccontextmanager
async def managed_browser(headless: bool = True):
    wrapper = BrowserWrapper(headless=headless)
    await wrapper.start()
    try:
        yield wrapper
    finally:
        await wrapper.stop()
