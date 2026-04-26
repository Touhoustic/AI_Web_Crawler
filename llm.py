"""
llm.py — Groq API interaction with automatic multi-key rotation and retry logic.

CHANGES vs previous:
  - decide_action() now receives has_data flag — biases toward EXTRACT when data found,
    reducing unnecessary LLM STOP decisions
  - summarize_page() has improved prompt that explicitly asks for structured output
    (emails found, phones found, key facts) so summaries are actually useful
  - Better fallback: unparseable → EXTRACT (not STOP), keeps crawler moving
  - Cooldown-and-retry: after all keys exhausted, waits 60s then resets
"""

import time
import re
from groq import Groq, RateLimitError, APIStatusError, APIConnectionError
from utils import logger, mask_key, truncate_text

DEFAULT_MODEL        = "llama-3.3-70b-versatile"
MAX_RETRIES_PER_KEY  = 2
RETRY_DELAY          = 3.0
KEY_SWITCH_DELAY     = 1.5
ALL_KEYS_COOLDOWN    = 60.0    # Wait before retrying after all keys exhausted

ACTION_RE = re.compile(
    r"^\s*(CLICK|EXTRACT|STOP)(?:\s+(https?://\S+))?\s*$",
    re.IGNORECASE,
)


class APIKeyManager:
    def __init__(self, api_keys: list[str]) -> None:
        if not api_keys:
            raise ValueError("At least one Groq API key is required.")
        self._keys     = [k.strip() for k in api_keys if k.strip()]
        self._index    = 0
        self._exhausted: set[int] = set()
        logger.info(f"🔑 Loaded {len(self._keys)} API key(s).")

    @property
    def current_key(self) -> str:
        return self._keys[self._index]

    @property
    def current_masked(self) -> str:
        return mask_key(self.current_key)

    def rotate(self) -> bool:
        self._exhausted.add(self._index)
        for i in range(len(self._keys)):
            candidate = (self._index + 1 + i) % len(self._keys)
            if candidate not in self._exhausted:
                self._index = candidate
                logger.warning(f"🔄 Switched to API key #{self._index + 1} ({self.current_masked})")
                return True
        logger.error("❌ All API keys exhausted.")
        return False

    def reset_exhausted(self) -> None:
        self._exhausted.clear()


class LLMClient:
    def __init__(self, api_keys: list[str], model: str = DEFAULT_MODEL) -> None:
        self.key_manager = APIKeyManager(api_keys)
        self.model = model
        self._build_client()

    def _build_client(self) -> None:
        self._client = Groq(api_key=self.key_manager.current_key)

    def _call_api(self, messages: list[dict], max_tokens: int = 256) -> str:
        attempts = 0
        while True:
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.1,
                )
                return response.choices[0].message.content.strip()

            except RateLimitError as e:
                logger.warning(f"⚠️  Rate limit on key {self.key_manager.current_masked}")
                attempts += 1
                if attempts < MAX_RETRIES_PER_KEY:
                    time.sleep(RETRY_DELAY)
                else:
                    time.sleep(KEY_SWITCH_DELAY)
                    if not self.key_manager.rotate():
                        logger.warning(f"⏳ All keys exhausted — waiting {ALL_KEYS_COOLDOWN}s…")
                        time.sleep(ALL_KEYS_COOLDOWN)
                        self.key_manager.reset_exhausted()
                    self._build_client()
                    attempts = 0

            except APIStatusError as e:
                if e.status_code in (429, 402):
                    logger.warning(f"⚠️  Quota ({e.status_code}) on key {self.key_manager.current_masked}")
                    time.sleep(KEY_SWITCH_DELAY)
                    if not self.key_manager.rotate():
                        logger.warning(f"⏳ All keys exhausted — waiting {ALL_KEYS_COOLDOWN}s…")
                        time.sleep(ALL_KEYS_COOLDOWN)
                        self.key_manager.reset_exhausted()
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
    # Navigation decision
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
        has_data: bool = False,
    ) -> dict:
        """
        Ask LLM for navigation decision only.
        Extraction is ALWAYS done first by agent.py — this only decides CLICK/STOP.
        """
        # If we already found useful data on this page, bias toward continuing crawl
        data_hint = (
            "NOTE: Useful data was already extracted from this page (emails/phones/people). "
            "Continue crawling unless step limit is close.\n"
            if has_data else ""
        )

        link_lines = []
        for i, link in enumerate(available_links[:12], 1):
            marker = " [visited]" if link["url"] in visited_urls else ""
            link_lines.append(f"  {i}. {link['url']}{marker}  ({link.get('text','')[:50]})")
        links_block = "\n".join(link_lines) or "  (no links found)"

        steps_left = max_steps - step
        prompt = f"""You are a web crawling navigation AI. Your job: decide where to go NEXT.
Extraction already happened on this page. You only decide navigation.

GOAL: {goal}
{data_hint}
CURRENT PAGE: {page_url}
STEPS REMAINING: {steps_left}/{max_steps}

PAGE CONTENT (summary):
---
{truncate_text(page_text, 1800)}
---

UNVISITED LINKS (sorted by relevance):
{links_block}

RULES:
- CLICK <url>  →  Go to a new, relevant, unvisited page likely to have more data.
- EXTRACT      →  Stay here / already captured enough from this page, continue queue.
- STOP         →  Only if steps ≤ 2 remaining OR no useful links exist at all.
- NEVER click visited links. NEVER click login/register/cart/checkout/privacy/terms.
- On marketplace listings: prefer pages with seller info or contact details.
- Prefer: contact, about, team, profile, annonce, vendeur, fiche pages.

ONE LINE ONLY — no explanation:
CLICK <full_url>
EXTRACT
STOP"""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a deterministic web navigation agent. "
                    "Respond with exactly one line: CLICK <url> | EXTRACT | STOP. "
                    "Nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        raw = self._call_api(messages, max_tokens=60)
        logger.info(f"🤖 LLM: {raw!r}")
        return self._parse_action(raw, available_links)

    # ─────────────────────────────────────────
    # Page summary
    # ─────────────────────────────────────────

    def summarize_page(self, page_url: str, page_text: str, goal: str) -> str:
        """
        Produce a structured summary of key findings on this page.
        Always called — not conditional on LLM action.
        """
        prompt = f"""You are an information extraction assistant. Summarize findings relevant to the goal.

GOAL: {goal}
URL: {page_url}

PAGE TEXT:
---
{truncate_text(page_text, 2500)}
---

Write a 2-5 sentence summary covering:
1. What kind of page this is (contact, about, listing, home, etc.)
2. Any emails, phone numbers, or social media found (list them explicitly if present)
3. Any team members or company names mentioned
4. Whether this page is useful for the goal

If nothing relevant: write "No relevant contact information found on this page."
Be concise and factual. No markdown."""

        messages = [{"role": "user", "content": prompt}]
        try:
            result = self._call_api(messages, max_tokens=300)
            return result
        except Exception as e:
            logger.warning(f"Summary skipped: {e}")
            return "Summary unavailable."

    # ─────────────────────────────────────────
    # Action parser
    # ─────────────────────────────────────────

    def _parse_action(self, raw: str, available_links: list[dict]) -> dict:
        raw   = raw.strip().split("\n")[0]
        match = ACTION_RE.match(raw)

        if not match:
            logger.warning(f"⚠️  Unparseable LLM response: {raw!r} → defaulting to EXTRACT")
            # Default to EXTRACT (not STOP) so crawler keeps going
            return {"action": "EXTRACT", "url": None}

        action = match.group(1).upper()
        url    = match.group(2)

        if action == "CLICK":
            if url:
                valid_urls = {l["url"] for l in available_links}
                if url not in valid_urls:
                    for link in available_links:
                        if url in link["url"] or link["url"].endswith(url):
                            url = link["url"]
                            break
                    else:
                        logger.warning(f"⚠️  URL not in link list → EXTRACT")
                        return {"action": "EXTRACT", "url": None}
            else:
                return {"action": "EXTRACT", "url": None}

        return {"action": action, "url": url}
