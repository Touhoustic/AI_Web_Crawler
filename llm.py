"""
llm.py — Groq API interaction with automatic multi-key rotation and retry logic.
"""

import time
import json
import re
from groq import Groq, RateLimitError, APIStatusError, APIConnectionError
from utils import logger, mask_key, truncate_text

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

DEFAULT_MODEL = "llama-3.3-70b-versatile"  # Groq's most capable model; swap to llama3-70b-8192 for lower latency
MAX_RETRIES_PER_KEY = 2
RETRY_DELAY = 3.0                    # Seconds between retries on the same key
KEY_SWITCH_DELAY = 1.5               # Seconds before switching to the next key

# Strictly structured action keywords
ACTION_RE = re.compile(
    r"^\s*(CLICK|EXTRACT|STOP)(?:\s+(https?://\S+))?\s*$",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────
# Key Rotation Manager
# ─────────────────────────────────────────────

class APIKeyManager:
    """
    Manages a pool of Groq API keys.
    Rotates automatically when a key hits rate limits or quota errors.
    """

    def __init__(self, api_keys: list[str]) -> None:
        if not api_keys:
            raise ValueError("At least one Groq API key is required.")
        self._keys = [k.strip() for k in api_keys if k.strip()]
        self._index = 0
        self._exhausted: set[int] = set()
        logger.info(f"🔑 Loaded {len(self._keys)} API key(s).")

    @property
    def current_key(self) -> str:
        return self._keys[self._index]

    @property
    def current_masked(self) -> str:
        return mask_key(self.current_key)

    def rotate(self) -> bool:
        """
        Advance to the next available key.
        Returns False if all keys have been exhausted.
        """
        self._exhausted.add(self._index)
        for i in range(len(self._keys)):
            candidate = (self._index + 1 + i) % len(self._keys)
            if candidate not in self._exhausted:
                self._index = candidate
                logger.warning(
                    f"🔄 Switching to API key #{self._index + 1} ({self.current_masked})"
                )
                return True
        logger.error("❌ All API keys exhausted. Cannot continue.")
        return False

    def reset_exhausted(self) -> None:
        """Reset exhausted set (e.g., after a cool-down period)."""
        self._exhausted.clear()


# ─────────────────────────────────────────────
# LLM Client
# ─────────────────────────────────────────────

class LLMClient:
    """
    Wraps the Groq client with key rotation, retry logic, and
    structured prompt formatting.
    """

    def __init__(self, api_keys: list[str], model: str = DEFAULT_MODEL) -> None:
        self.key_manager = APIKeyManager(api_keys)
        self.model = model
        self._build_client()

    def _build_client(self) -> None:
        self._client = Groq(api_key=self.key_manager.current_key)

    def _call_api(self, messages: list[dict], max_tokens: int = 256) -> str:
        """
        Make a single API call. Handles rate limits and rotates keys automatically.
        Returns the response text.
        """
        attempts = 0
        while True:
            try:
                logger.debug(
                    f"📡 Calling Groq [{self.model}] with key {self.key_manager.current_masked}"
                )
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.1,   # Near-deterministic for decision making
                )
                return response.choices[0].message.content.strip()

            except RateLimitError as e:
                logger.warning(f"⚠️  Rate limit hit on key {self.key_manager.current_masked}: {e}")
                attempts += 1
                if attempts < MAX_RETRIES_PER_KEY:
                    logger.info(f"   Waiting {RETRY_DELAY}s before retry…")
                    time.sleep(RETRY_DELAY)
                else:
                    time.sleep(KEY_SWITCH_DELAY)
                    if not self.key_manager.rotate():
                        raise RuntimeError("All API keys exhausted due to rate limits.")
                    self._build_client()
                    attempts = 0

            except APIStatusError as e:
                if e.status_code in (429, 402):
                    logger.warning(f"⚠️  Quota error ({e.status_code}) on key {self.key_manager.current_masked}")
                    time.sleep(KEY_SWITCH_DELAY)
                    if not self.key_manager.rotate():
                        raise RuntimeError("All API keys exhausted.")
                    self._build_client()
                    attempts = 0
                else:
                    logger.error(f"❌ API error {e.status_code}: {e.message}")
                    raise

            except APIConnectionError as e:
                logger.error(f"❌ Connection error: {e}")
                time.sleep(RETRY_DELAY)
                attempts += 1
                if attempts >= MAX_RETRIES_PER_KEY:
                    raise

    # ─────────────────────────────────────────
    # Decision prompt
    # ─────────────────────────────────────────

    def decide_action(
        self,
        goal: str,
        page_url: str,
        page_text: str,
        available_links: list[dict],
        visited_urls: set[str],
        step: int,
        max_steps: int,
    ) -> dict:
        """
        Ask the LLM to decide the next action given the current page state.
        Returns a parsed dict: {"action": "CLICK"|"EXTRACT"|"STOP", "url": str|None}
        """
        # Build a tidy link list for the prompt
        link_lines = []
        for i, link in enumerate(available_links[:15], 1):
            visited_marker = " [visited]" if link["url"] in visited_urls else ""
            link_lines.append(f"  {i}. {link['url']}{visited_marker}  ({link.get('text','')[:50]})")
        links_block = "\n".join(link_lines) if link_lines else "  (no links found)"

        prompt = f"""You are a precise web browsing AI agent.

GOAL: {goal}

CURRENT PAGE: {page_url}
STEP: {step} of {max_steps}

PAGE CONTENT (truncated):
---
{truncate_text(page_text, 2500)}
---

AVAILABLE LINKS (sorted by relevance):
{links_block}

INSTRUCTIONS:
- Decide ONE action to take next.
- Prefer pages likely to contain: emails, phone numbers, team info, contact or about info.
- Avoid already-visited links, login pages, privacy/terms pages.
- If sufficient data has been collected or no useful links remain, choose STOP.
- If this page contains useful data, choose EXTRACT.
- If a promising unvisited link exists, choose CLICK followed by the exact URL.

STRICT OUTPUT FORMAT (one line only, no explanation):
CLICK <full_url>
EXTRACT
STOP"""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a deterministic web browsing agent. "
                    "Respond with exactly one line matching: CLICK <url> | EXTRACT | STOP. "
                    "No explanation, no markdown, no punctuation."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        raw = self._call_api(messages, max_tokens=60)
        logger.info(f"🤖 LLM decision: {raw!r}")
        return self._parse_action(raw, available_links)

    # ─────────────────────────────────────────
    # Summary prompt
    # ─────────────────────────────────────────

    def summarize_page(self, page_url: str, page_text: str, goal: str) -> str:
        """
        Ask the LLM to produce a short summary of key findings on a page.
        """
        prompt = f"""You are an information extraction assistant.

GOAL: {goal}
URL: {page_url}

PAGE TEXT:
---
{truncate_text(page_text, 2000)}
---

Write a concise 2-4 sentence summary of the most useful information on this page
relevant to the goal. Focus on: contacts, emails, company info, team members,
services. If nothing relevant is found, respond with "No relevant information found."
"""
        messages = [{"role": "user", "content": prompt}]
        try:
            return self._call_api(messages, max_tokens=200)
        except Exception as e:
            logger.warning(f"Summary generation failed: {e}")
            return "Summary unavailable."

    # ─────────────────────────────────────────
    # Action parser
    # ─────────────────────────────────────────

    def _parse_action(self, raw: str, available_links: list[dict]) -> dict:
        """
        Parse the LLM's one-line action response into a structured dict.
        Falls back gracefully on malformed output.
        """
        raw = raw.strip().split("\n")[0]   # Take first line only
        match = ACTION_RE.match(raw)

        if not match:
            logger.warning(f"⚠️  Unparseable LLM response: {raw!r}. Defaulting to STOP.")
            return {"action": "STOP", "url": None}

        action = match.group(1).upper()
        url = match.group(2)

        if action == "CLICK":
            if url:
                # Validate the URL is actually in the available link list
                valid_urls = {l["url"] for l in available_links}
                if url not in valid_urls:
                    # Try fuzzy match
                    for link in available_links:
                        if url in link["url"] or link["url"].endswith(url):
                            url = link["url"]
                            break
                    else:
                        logger.warning(f"⚠️  LLM chose URL not in link list: {url!r}. Defaulting to EXTRACT.")
                        return {"action": "EXTRACT", "url": None}
            else:
                return {"action": "EXTRACT", "url": None}

        return {"action": action, "url": url}
