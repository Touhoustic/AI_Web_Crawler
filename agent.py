"""
agent.py — Core autonomous agent loop.

Orchestrates: browser → extractor → LLM → memory → repeat
"""

import asyncio
import time
from collections import deque
from urllib.parse import urlparse

from browser import BrowserWrapper
from extractor import full_extraction
from llm import LLMClient
from memory import (
    VisitedURLTracker,
    persist_extraction,
    store_page_summary,
    fetch_visited_urls,
)
from reporter import generate_and_display
from utils import logger, human_delay, same_domain, score_url_relevance, is_valid_url

import sqlite3


# ─────────────────────────────────────────────
# Agent configuration dataclass
# ─────────────────────────────────────────────

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
        self.seed_url        = seed_url
        self.goal            = goal
        self.api_keys        = api_keys
        self.max_steps       = max_steps
        self.max_depth       = max_depth
        self.same_domain_only = same_domain_only
        self.headless        = headless
        self.model           = model
        self.save_report     = save_report
        self.output_dir      = output_dir


# ─────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────

class WebAgent:
    """
    Autonomous web-browsing agent.

    Execution model:
      1. Start from seed URL
      2. Fetch & extract page
      3. Ask LLM for next action
      4. Execute action (CLICK / EXTRACT / STOP)
      5. Repeat until max_steps or LLM says STOP
      6. Generate final report
    """

    def __init__(self, config: AgentConfig, db_conn: sqlite3.Connection) -> None:
        self.config     = config
        self.conn       = db_conn
        self.llm        = LLMClient(config.api_keys, model=config.model)
        self.tracker    = VisitedURLTracker()
        self.errors     = 0
        self.steps      = 0
        self.start_time = time.time()

        # BFS-style queue: (url, depth)
        self._queue: deque[tuple[str, int]] = deque()
        self._queue.append((config.seed_url, 0))

        logger.info("=" * 60)
        logger.info(f"🕷️  WebAgent initialised")
        logger.info(f"   Seed URL : {config.seed_url}")
        logger.info(f"   Goal     : {config.goal}")
        logger.info(f"   Max steps: {config.max_steps}")
        logger.info(f"   Max depth: {config.max_depth}")
        logger.info("=" * 60)

    async def run(self, browser: BrowserWrapper) -> dict:
        """
        Main execution loop. Runs until:
          - Queue is empty
          - Max steps reached
          - LLM returns STOP
        Returns the final report dict.
        """
        while self._queue and self.steps < self.config.max_steps:
            url, depth = self._queue.popleft()

            # ── Pre-flight checks ──────────────────────────────────────
            if self.tracker.has_seen(url):
                logger.debug(f"⏭  Already visited, skipping: {url}")
                continue

            if not is_valid_url(url):
                logger.warning(f"⚠️  Invalid URL, skipping: {url}")
                continue

            if self.config.same_domain_only and not same_domain(url, self.config.seed_url):
                logger.info(f"🚫 Off-domain URL skipped: {url}")
                continue

            if depth > self.config.max_depth:
                logger.info(f"📏 Max depth ({self.config.max_depth}) reached, skipping: {url}")
                continue

            # ── Step header ───────────────────────────────────────────
            self.steps += 1
            self.tracker.mark(url)
            logger.info("")
            logger.info(f"━━━ Step {self.steps}/{self.config.max_steps}  Depth {depth} ━━━")
            logger.info(f"🌐 URL: {url}")

            # ── Fetch page ────────────────────────────────────────────
            result = await browser.fetch_page(url)
            if not result or not result.get("html"):
                logger.warning(f"❌ Failed to fetch: {url}")
                self.errors += 1
                continue

            actual_url = result["url"]   # Post-redirect URL
            html       = result["html"]

            # ── Extract data ──────────────────────────────────────────
            extraction = full_extraction(html, actual_url)

            # Persist everything found on this page
            persist_extraction(self.conn, extraction, depth=depth)

            # ── Ask LLM for next action ───────────────────────────────
            visited_set = set(self.tracker.all_urls())
            try:
                decision = self.llm.decide_action(
                    goal=self.config.goal,
                    page_url=actual_url,
                    page_text=extraction["visible_text"],
                    available_links=extraction["links"],
                    visited_urls=visited_set,
                    step=self.steps,
                    max_steps=self.config.max_steps,
                )
            except RuntimeError as e:
                logger.error(f"LLM fatal error: {e}")
                break

            action = decision["action"]
            logger.info(f"⚡ Action: {action}  →  {decision.get('url') or ''}")

            # ── Execute action ────────────────────────────────────────
            if action == "STOP":
                logger.info("🛑 Agent decided to STOP.")
                break

            elif action == "EXTRACT":
                # Generate and store an LLM summary of this page
                summary = self.llm.summarize_page(
                    actual_url, extraction["visible_text"], self.config.goal
                )
                store_page_summary(self.conn, actual_url, summary)
                logger.info(f"📝 Summary stored for {actual_url}")
                # Continue to next queued URL

            elif action == "CLICK":
                next_url = decision["url"]
                if next_url and not self.tracker.has_seen(next_url):
                    # Prioritise the clicked URL (push to front of queue)
                    self._queue.appendleft((next_url, depth + 1))
                    logger.info(f"➡️  Queued (priority): {next_url}")

                # Also queue other high-value links found on this page
                self._enqueue_useful_links(extraction["links"], depth)

                # Human-like delay before next navigation
                await asyncio.sleep(1.5)

        # ── Finished ──────────────────────────────────────────────────
        logger.info("")
        logger.info("═" * 60)
        logger.info(f"✅ Agent finished after {self.steps} steps.")
        logger.info("Generating report…")

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

    def _enqueue_useful_links(self, links: list[dict], current_depth: int) -> None:
        """
        Add high-relevance unvisited links to the back of the queue.
        Limits additions per page to avoid queue explosion.
        """
        added = 0
        for link in links:
            url = link["url"]
            if self.tracker.has_seen(url):
                continue
            if self.config.same_domain_only and not same_domain(url, self.config.seed_url):
                continue
            score = link.get("score", 0)
            if score >= 0:           # Filter out negatively-scored (irrelevant) links
                self._queue.append((url, current_depth + 1))
                added += 1
                if added >= 8:       # Cap per-page additions
                    break
        if added:
            logger.debug(f"  ↳ Queued {added} additional links")
