"""
utils.py — Shared helper utilities for the Web Browsing Agent.
"""

import re
import time
import random
import logging
from urllib.parse import urlparse, urljoin
from datetime import datetime

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────

def setup_logger(name: str = "WebAgent", level: int = logging.INFO) -> logging.Logger:
    """Configure and return a named logger with pretty formatting."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured

    logger.setLevel(level)
    handler = logging.StreamHandler()
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    return logger


logger = setup_logger()


# ─────────────────────────────────────────────
# URL helpers
# ─────────────────────────────────────────────

def normalize_url(base: str, href: str) -> str | None:
    """
    Resolve a possibly-relative href against a base URL.
    Returns an absolute URL or None if the result is not http/https.
    """
    try:
        full = urljoin(base, href.strip())
        parsed = urlparse(full)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            # Strip fragment
            return full.split("#")[0].rstrip("/")
        return None
    except Exception:
        return None


def get_domain(url: str) -> str:
    """Return the netloc (domain) of a URL."""
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def same_domain(url1: str, url2: str) -> bool:
    """Check whether two URLs share the same domain."""
    return get_domain(url1) == get_domain(url2)


def is_valid_url(url: str) -> bool:
    """Basic URL validity check."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# ─────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────

def truncate_text(text: str, max_chars: int = 3000) -> str:
    """Truncate text to max_chars, adding an ellipsis if cut."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [content truncated]"


def clean_whitespace(text: str) -> str:
    """Collapse multiple blank lines and strip leading/trailing space."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────
# Timing helpers
# ─────────────────────────────────────────────

def human_delay(min_s: float = 1.0, max_s: float = 3.5) -> None:
    """Sleep for a random duration to simulate human browsing speed."""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def timestamp() -> str:
    """Return the current datetime as a readable string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────

def mask_key(key: str) -> str:
    """Return a masked version of an API key for safe logging."""
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "..." + key[-4:]


def score_url_relevance(url: str) -> int:
    """
    Heuristic relevance score for a URL.
    Higher score → more likely to contain useful contact/about info.
    """
    url_lower = url.lower()
    score = 0
    high_value = ["contact", "about", "team", "support", "help", "reach", "connect", "info"]
    medium_value = ["company", "people", "directory", "profile", "services"]
    low_value = ["blog", "news", "press", "media", "privacy", "terms", "cookie"]
    skip_value = ["login", "signup", "register", "cart", "checkout", "advertisement"]

    for kw in high_value:
        if kw in url_lower:
            score += 10
    for kw in medium_value:
        if kw in url_lower:
            score += 5
    for kw in low_value:
        if kw in url_lower:
            score -= 3
    for kw in skip_value:
        if kw in url_lower:
            score -= 20
    return score
