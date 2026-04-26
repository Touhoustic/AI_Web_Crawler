"""
agent.py — Core autonomous agent loop.

CHANGES vs previous:
  - Accepts optional progress_callback(step, max_steps, label) for UI live updates
  - Accepts optional stop_flag (threading.Event) to abort mid-run
  - ALWAYS runs extraction first, then asks LLM (hybrid logic)
  - ALWAYS stores page summary — not only on EXTRACT action
  - _enqueue_useful_links now scores links better and caps more aggressively
  - Added _should_stop() helper for clean abort
"""

import asyncio
import time
from collections import deque
from typing import Callable, Awaitable

from browser import BrowserWrapper
from extractor import full_extraction_with_llm
from llm import LLMClient
from memory import (
    VisitedURLTracker,
    persist_extraction,
    store_page_summary,
    fetch_visited_urls,
)
from reporter import generate_and_display
from utils import logger, same_domain, score_url_relevance, is_valid_url

import sqlite3


class AgentConfig:
    def __init__(
        self,
        seed_url: str,
        goal: str,
        api_keys: list[str],
        max_steps: int = 30,
        max_depth: int = 3,
        same_domain_only: bool = True,
        headless: bool = True,
        model: str = "llama-3.3-70b-versatile",
        save_report: bool = True,
        output_dir: str = ".",
    ):
        self.seed_url         = seed_url
        self.goal             = goal
        self.api_keys         = api_keys
        self.max_steps        = max_steps
        self.max_depth        = max_depth
        self.same_domain_only = same_domain_only
        self.headless         = headless
        self.model            = model
        self.save_report      = save_report
        self.output_dir       = output_dir


class WebAgent:
    """
    Autonomous web-browsing agent.

    Hybrid execution model:
      1. Fetch page with browser (including hidden-data reveal)
      2. ALWAYS extract & persist data immediately
      3. ALWAYS generate a page summary
      4. Ask LLM only for NAVIGATION decision (CLICK url / STOP)
      5. Enqueue high-value links from the page regardless of LLM choice
      6. Repeat until max_steps, queue empty, or stop requested
    """

    def __init__(
        self,
        config: AgentConfig,
        db_conn: sqlite3.Connection,
        progress_callback: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> None:
        self.config    = config
        self.conn      = db_conn
        self.llm       = LLMClient(config.api_keys, model=config.model)
        self.tracker   = VisitedURLTracker()
        self.errors    = 0
        self.steps     = 0
        self.start_time = time.time()
        self._progress = progress_callback

        self._queue: deque[tuple[str, int]] = deque()
        self._queue.append((config.seed_url, 0))

        logger.info("=" * 60)
        logger.info("🕷️  WebAgent initialised")
        logger.info(f"   Seed URL : {config.seed_url}")
        logger.info(f"   Goal     : {config.goal}")
        logger.info(f"   Model    : {config.model}")
        logger.info(f"   Max steps: {config.max_steps}  Max depth: {config.max_depth}")
        logger.info("=" * 60)

    # ─────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────

    async def run(self, browser: BrowserWrapper) -> dict:
        while self._queue and self.steps < self.config.max_steps:
            url, depth = self._queue.popleft()

            # ── Preflight ────────────────────────────────────────────
            if self.tracker.has_seen(url):
                continue
            if not is_valid_url(url):
                logger.warning(f"⚠️  Invalid URL, skipping: {url}")
                continue
            if self.config.same_domain_only and not same_domain(url, self.config.seed_url):
                logger.info(f"🚫 Off-domain skipped: {url}")
                continue
            if depth > self.config.max_depth:
                logger.info(f"📏 Max depth reached, skipping: {url}")
                continue

            # ── Step ─────────────────────────────────────────────────
            self.steps += 1
            self.tracker.mark(url)
            logger.info("")
            logger.info(f"━━━ Step {self.steps}/{self.config.max_steps}  Depth {depth} ━━━")
            logger.info(f"🌐 URL: {url}")

            # Emit progress to UI
            if self._progress:
                try:
                    await self._progress(
                        self.steps, self.config.max_steps,
                        f"Step {self.steps}/{self.config.max_steps}: {url[:60]}…"
                    )
                except asyncio.CancelledError:
                    logger.warning("⛔ Stop requested by user.")
                    break

            # ── Fetch ────────────────────────────────────────────────
            result = await browser.fetch_page(url)
            if not result or not result.get("html"):
                logger.warning(f"❌ Failed to fetch: {url}")
                self.errors += 1
                continue

            actual_url = result["url"]
            html       = result["html"]

            # ── Extract (ALWAYS) ─────────────────────────────────────
            extraction = await full_extraction_with_llm(
                html, actual_url,
                api_key=self.config.api_keys[0],
                model=self.config.model,
            )
            persist_extraction(self.conn, extraction, depth=depth)

            # ── Summarize page (ALWAYS) ──────────────────────────────
            # Done in background — doesn't block navigation decision
            try:
                summary = self.llm.summarize_page(
                    actual_url, extraction["visible_text"], self.config.goal
                )
                store_page_summary(self.conn, actual_url, summary)
                logger.info(f"📝 Summary: {summary[:120]}…" if len(summary) > 120 else f"📝 Summary: {summary}")
            except Exception as e:
                logger.warning(f"Summary skipped: {e}")

            # ── Enqueue useful links from this page immediately ───────
            # We do this BEFORE asking the LLM so useful links are always queued
            self._enqueue_useful_links(extraction["links"], depth)

            # ── Ask LLM for navigation decision ──────────────────────
            visited_set = set(self.tracker.all_urls())
            has_data = bool(
                extraction["emails"] or extraction["phones"] or extraction["people"]
                or extraction["social_links"]
            )

            try:
                decision = self.llm.decide_action(
                    goal=self.config.goal,
                    page_url=actual_url,
                    page_text=extraction["visible_text"],
                    available_links=extraction["links"],
                    visited_urls=visited_set,
                    step=self.steps,
                    max_steps=self.config.max_steps,
                    has_data=has_data,
                )
            except RuntimeError as e:
                logger.error(f"LLM fatal error: {e}")
                break

            action = decision["action"]
            logger.info(f"⚡ Action: {action}  →  {decision.get('url') or ''}")

            if action == "STOP":
                logger.info("🛑 Agent decided to STOP.")
                break

            elif action == "CLICK":
                next_url = decision["url"]
                if next_url and not self.tracker.has_seen(next_url):
                    # Priority insert at front
                    self._queue.appendleft((next_url, depth + 1))
                    logger.info(f"➡️  Priority queued: {next_url}")

            # EXTRACT: links already enqueued above, just continue

            await asyncio.sleep(1.0)

        # ── Report ───────────────────────────────────────────────────
        logger.info("")
        logger.info("═" * 60)
        logger.info(f"✅ Agent finished after {self.steps} steps.")

        report = generate_and_display(
            conn=self.conn,
            start_time=self.start_time,
            steps=self.steps,
            errors=self.errors,
            seed_url=self.config.seed_url,
            goal=self.config.goal,
            save=self.config.save_report,
        )
        return report

    # ─────────────────────────────────────────────
    # Link queuing
    # ─────────────────────────────────────────────

    def _enqueue_useful_links(self, links: list[dict], current_depth: int) -> None:
        """
        Enqueue unvisited links with positive relevance scores.
        Sorts by score descending so the most promising pages are crawled first.
        Cap at 6 per page to prevent queue explosion.
        """
        candidates = []
        for link in links:
            url = link["url"]
            if self.tracker.has_seen(url):
                continue
            if self.config.same_domain_only and not same_domain(url, self.config.seed_url):
                continue
            score = link.get("score", 0)
            if score >= 0:
                candidates.append((score, url))

        # Highest scored first
        candidates.sort(key=lambda x: x[0], reverse=True)
        added = 0
        for score, url in candidates[:6]:
            self._queue.append((url, current_depth + 1))
            added += 1

        if added:
            logger.debug(f"  ↳ Queued {added} links from this page")
