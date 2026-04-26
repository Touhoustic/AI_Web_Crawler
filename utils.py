"""
utils.py — Shared helper utilities.

CHANGES: score_url_relevance expanded with French/Arabic marketplace keywords.
"""

import re
import time
import random
import logging
from urllib.parse import urlparse, urljoin
from datetime import datetime


def setup_logger(name: str = "WebAgent", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    return logger


logger = setup_logger()


def normalize_url(base: str, href: str) -> str | None:
    try:
        full   = urljoin(base, href.strip())
        parsed = urlparse(full)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return full.split("#")[0].rstrip("/")
        return None
    except Exception:
        return None


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def same_domain(url1: str, url2: str) -> bool:
    return get_domain(url1) == get_domain(url2)


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def truncate_text(text: str, max_chars: int = 3000) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n… [content truncated]"


def clean_whitespace(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def human_delay(min_s: float = 1.0, max_s: float = 3.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "..." + key[-4:]


def score_url_relevance(url: str) -> int:
    """
    Relevance score for URL prioritization.
    Higher = more likely to contain contact/team data.
    Expanded with FR/MA/AR marketplace keywords.
    """
    u = url.lower()
    score = 0

    high = [
        "contact", "about", "team", "support", "help", "reach", "connect",
        # French
        "contacter", "equipe", "à-propos", "a-propos", "nous", "coordonnees",
        # Arabic slugs
        "annonce", "vendeur", "fiche", "profil", "seller", "profile",
        # Common
        "info", "directory", "people",
    ]
    medium = [
        "company", "services", "staff", "members", "leadership",
        "notre", "qui-sommes", "societe",
    ]
    low = ["blog", "news", "press", "media", "privacy", "terms", "cookie", "faq"]
    skip = [
        "login", "signin", "signup", "register", "cart", "checkout",
        "advertisement", "ads", "cdn", "static", "asset",
    ]

    for kw in high:
        if kw in u:
            score += 10
    for kw in medium:
        if kw in u:
            score += 5
    for kw in low:
        if kw in u:
            score -= 3
    for kw in skip:
        if kw in u:
            score -= 20
    return score
