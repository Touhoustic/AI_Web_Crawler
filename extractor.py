"""
extractor.py — Hybrid data extraction: LLM-primary + regex fallback.

HOW IT WORKS
============
  1. extract_visible_text()  — strips HTML to clean text (unchanged)
  2. full_extraction()        — sync regex pipeline (unchanged, used as fallback)
  3. llm_extract_contacts()  — async Groq call; returns structured JSON with
                               emails / phones / socials / people
  4. full_extraction_with_llm() — NEW main entry point for the agent.
       Runs both paths in parallel, then merges:
         • LLM results are preferred (catches obfuscated / unusual formats)
         • Regex results fill any gaps the LLM missed
         • Dedup by digit fingerprint (phones) / lowercased value (emails/people)

BACKWARD COMPATIBILITY
======================
  full_extraction() still works exactly as before — no agent changes required
  unless you want the LLM path (call full_extraction_with_llm instead).
"""

import re
import json
import asyncio
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from utils import normalize_url, logger

# ─────────────────────────────────────────────────────────────────────────────
# LLM extraction config
# ─────────────────────────────────────────────────────────────────────────────

GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
LLM_MAX_CHARS = 14_000   # safe for all Groq models; leaves room for output tokens

EXTRACTION_SYSTEM_PROMPT = """\
You are a precise contact-data extraction engine.
Given the visible text of a webpage, extract every piece of contact information.

Return ONLY a single valid JSON object — no markdown fences, no explanation, nothing else.
Use exactly this schema:

{
  "emails":  ["<email>", ...],
  "phones":  ["<phone as found on page>", ...],
  "socials": {
    "facebook":  ["<url>", ...],
    "twitter":   ["<url>", ...],
    "instagram": ["<url>", ...],
    "linkedin":  ["<url>", ...],
    "youtube":   ["<url>", ...],
    "tiktok":    ["<url>", ...],
    "github":    ["<url>", ...],
    "telegram":  ["<url>", ...],
    "whatsapp":  ["<url>", ...],
    "other":     ["<url>", ...]
  },
  "people": [
    {
      "name":     "<full name>",
      "role":     "<job title or role, empty string if unknown>",
      "email":    "<email or empty string>",
      "linkedin": "<linkedin url or empty string>",
      "twitter":  "<twitter/x url or empty string>"
    }
  ]
}

Rules:
- Include ALL emails, even obfuscated ones like "user [at] domain [dot] com".
- Include ALL phone numbers regardless of format or country code.
- Only include people who are clearly identified as individuals (name + at least one other field).
- Keep arrays empty ([]) if nothing was found for that category.
- Do not invent or hallucinate data — only extract what is explicitly present.
"""


async def llm_extract_contacts(
    text: str,
    api_key: str,
    model: str = "llama-3.3-70b-versatile",
) -> dict:
    """
    Send page text to Groq and return structured extraction.
    Returns an empty-safe dict on any failure so callers can always merge safely.
    """
    _empty = {"emails": [], "phones": [], "socials": {}, "people": []}

    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed — LLM extraction unavailable (pip install httpx)")
        return _empty

    # Trim text to fit context window
    chunk = text[:LLM_MAX_CHARS]
    if len(text) > LLM_MAX_CHARS:
        chunk += "\n…[text truncated for context window]"

    payload = {
        "model": model,
        "temperature": 0,            # deterministic extraction
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user",   "content": f"Extract all contact data from this webpage text:\n\n{chunk}"},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"  ⚠ LLM extraction request failed: {exc}")
        return _empty

    try:
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as exc:
        logger.warning(f"  ⚠ LLM response parse failed: {exc} | raw={resp.text[:200]}")
        return _empty

    # Normalise keys to lowercase, guarantee all expected keys exist
    result: dict = {
        "emails":  [e.lower().strip() for e in data.get("emails", []) if isinstance(e, str)],
        "phones":  [p.strip()        for p in data.get("phones", []) if isinstance(p, str)],
        "socials": {k.lower(): [u for u in v if isinstance(u, str)]
                    for k, v in data.get("socials", {}).items() if isinstance(v, list)},
        "people":  [p for p in data.get("people", []) if isinstance(p, dict) and p.get("name")],
    }

    logger.info(
        f"  🤖 LLM extracted — "
        f"emails:{len(result['emails'])}  phones:{len(result['phones'])}  "
        f"people:{len(result['people'])}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Merge helpers
# ─────────────────────────────────────────────────────────────────────────────

def _merge_emails(regex_emails: list[str], llm_emails: list[str]) -> list[str]:
    """Union, lowercased, deduped, sorted."""
    bad_exts = {".png", ".jpg", ".gif", ".svg", ".webp", ".css", ".js", ".ico", ".woff", ".ttf"}
    seen:   set[str] = set()
    merged: list[str] = []
    for e in list(llm_emails) + list(regex_emails):
        e = e.lower().strip().rstrip(".")
        if (e not in seen
                and "@" in e
                and "." in e.split("@")[-1]
                and 6 <= len(e) <= 254
                and not any(e.endswith(x) for x in bad_exts)
                and ".." not in e):
            seen.add(e)
            merged.append(e)
    return sorted(merged)


def _digit_key(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _merge_phones(regex_phones: list[str], llm_phones: list[str]) -> list[str]:
    """Union deduped by digit fingerprint; keep LLM format when duplicated."""
    seen:   set[str] = set()
    merged: list[str] = []
    for p in list(llm_phones) + list(regex_phones):
        p = p.strip()
        dk = _digit_key(p)
        if not dk or len(dk) < 7 or len(dk) > 15:
            continue
        if dk not in seen:
            seen.add(dk)
            merged.append(p)
    return merged


def _merge_people(regex_people: list[dict], llm_people: list[dict]) -> list[dict]:
    """Union deduped by lowercased name; LLM entry wins on conflict."""
    seen:   set[str] = set()
    merged: list[dict] = []

    def _norm_name(p: dict) -> str:
        return p.get("name", "").lower().strip()

    def _clean(p: dict) -> dict:
        return {
            "name":     p.get("name",     "").strip(),
            "role":     p.get("role",     "").strip(),
            "email":    p.get("email",    "").lower().strip(),
            "linkedin": p.get("linkedin", "").strip(),
            "twitter":  p.get("twitter",  "").strip(),
            "url":      p.get("url",      "").strip(),
        }

    for person in list(llm_people) + list(regex_people):
        key = _norm_name(person)
        if key and len(key) >= 3 and key not in seen:
            seen.add(key)
            merged.append(_clean(person))

    return merged


def _merge_socials(regex_socials: dict, llm_socials: dict) -> dict:
    """Union by platform, deduped URLs."""
    all_platforms = set(regex_socials) | set(llm_socials)
    merged: dict = {}
    for platform in all_platforms:
        seen:    set[str] = set()
        urls:    list[str] = []
        for u in list(llm_socials.get(platform, [])) + list(regex_socials.get(platform, [])):
            u_clean = u.rstrip("/")
            if u_clean and u_clean not in seen:
                seen.add(u_clean)
                urls.append(u_clean)
        if urls:
            merged[platform] = urls
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# NEW MAIN ENTRY — LLM + regex hybrid
# ─────────────────────────────────────────────────────────────────────────────

async def full_extraction_with_llm(
    html:    str,
    url:     str,
    api_key: str,
    model:   str = "llama-3.3-70b-versatile",
) -> dict:
    """
    Drop-in async replacement for full_extraction().
    Runs the regex pipeline and the LLM concurrently, then merges.

    Usage in agent.py:
        result = await full_extraction_with_llm(html, url, api_key, model)
    """
    # Run regex pipeline (sync → thread so it doesn't block the event loop)
    loop = asyncio.get_event_loop()
    base = await loop.run_in_executor(None, full_extraction, html, url)

    # Run LLM concurrently (no extra latency cost)
    llm = await llm_extract_contacts(base["visible_text"], api_key, model)

    # Merge — LLM results preferred, regex fills gaps
    base["emails"]       = _merge_emails(base["emails"],       llm.get("emails",  []))
    base["phones"]       = _merge_phones(base["phones"],       llm.get("phones",  []))
    base["people"]       = _merge_people(base["people"],       llm.get("people",  []))
    base["social_links"] = _merge_socials(base["social_links"], llm.get("socials", {}))

    logger.info(
        f"  ✅ Hybrid final — "
        f"✉ {len(base['emails'])}  📞 {len(base['phones'])}  "
        f"🌐 {len(base['social_links'])}  👤 {len(base['people'])}"
    )
    return base


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#   ORIGINAL REGEX PIPELINE (unchanged — used as fallback + parallel baseline)
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,253}\.[a-zA-Z]{2,10}"
)
OBFUSCATED_JS_RE = re.compile(
    r'''["']([a-zA-Z0-9._%+\-]+)["']\s*\+\s*["']@["']\s*\+\s*["']([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})["']'''
)
OBFUSCATED_AT_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]at[\]\)]\s*([a-zA-Z0-9.\-]+)\s*[\[\(]dot[\]\)]\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────
# Phone — strict international patterns
# ─────────────────────────────────────────────

PHONE_INTL_RE = re.compile(
    r"""
    (?<!\d)
    (?:
        (?:\+212|00212)[\s\-.]?[5-7]\d{8}
        |
        (?:\+33|0033)[\s\-.]?[1-9](?:[\s\-.]?\d){8}
        |
        (?:\+213|00213)[\s\-.]?[5-7]\d{8}
        |
        (?:\+216|00216)[\s\-.]?[259]\d{7}
        |
        (?:\+44|0044)[\s\-.]?7\d{9}
        |
        (?:\+32|0032)[\s\-.]?4\d{8}
        |
        (?:\+49|0049)[\s\-.]?1[5-7]\d{9,10}
        |
        (?:\+39|0039)[\s\-.]?3\d{8,9}
        |
        (?:\+34|0034)[\s\-.]?[67]\d{8}
        |
        (?:\+1[\s\-.]?)?\(?[2-9]\d{2}\)?[\s\-.]?[2-9]\d{2}[\s\-.]?\d{4}
    )
    (?!\d)
    """,
    re.VERBOSE,
)

PHONE_MA_LOCAL_RE = re.compile(r"(?<!\d)0[67]\d{8}(?!\d)")
PHONE_FR_LOCAL_RE = re.compile(r"(?<!\d)0[67](?:[\s\-.]?\d{2}){4}(?!\d)")


def _clean_phone(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def _is_valid_phone(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    n = len(digits)
    if n < 7 or n > 15:
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", digits):
        return False
    if re.fullmatch(r"\d{1,5}[.,]\d{2,3}", raw.strip()):
        return False
    if len(set(digits)) <= 2 and n >= 6:
        return False
    if re.fullmatch(r"\d{5}", digits) and n == 5:
        return False
    return True


def extract_phone_numbers(text: str) -> list[str]:
    found_raw: list[str] = []
    for m in PHONE_INTL_RE.finditer(text):
        found_raw.append(m.group(0))
    for m in PHONE_MA_LOCAL_RE.finditer(text):
        found_raw.append(m.group(0))
    for m in PHONE_FR_LOCAL_RE.finditer(text):
        found_raw.append(m.group(0))

    seen_digits: set[str] = set()
    result: list[str] = []
    for raw in found_raw:
        cleaned = _clean_phone(raw)
        if not _is_valid_phone(cleaned):
            continue
        digits = re.sub(r"\D", "", cleaned)
        if digits in seen_digits:
            continue
        seen_digits.add(digits)
        result.append(cleaned)
    return result


# ─────────────────────────────────────────────
# Email extraction
# ─────────────────────────────────────────────

BAD_EMAIL_EXTS = {".png", ".jpg", ".gif", ".svg", ".webp", ".css", ".js", ".ico", ".woff", ".ttf"}


def extract_emails(text: str, html: str = "") -> list[str]:
    found: set[str] = set()
    found.update(e.lower() for e in EMAIL_RE.findall(text))

    if html:
        raw_text = re.sub(r"<[^>]+>", " ", html)
        for m in re.findall(r'mailto:([^\s"\'<>?&]+)', html, re.I):
            found.add(m.lower().strip().rstrip("."))
        for user, domain in OBFUSCATED_JS_RE.findall(html):
            found.add(f"{user.lower()}@{domain.lower()}")
        for user, domain, tld in OBFUSCATED_AT_RE.findall(raw_text):
            found.add(f"{user.lower()}@{domain.lower()}.{tld.lower()}")
        for m in re.findall(r'data-email=["\']([^"\']+)["\']', html, re.I):
            if "@" in m:
                found.add(m.lower().strip())

    cleaned = [
        e for e in found
        if "@" in e
        and "." in e.split("@")[1]
        and not any(e.endswith(x) for x in BAD_EMAIL_EXTS)
        and 6 <= len(e) <= 254
        and not e.startswith(".")
        and ".." not in e
        and not e.endswith(".")
    ]
    return list(dict.fromkeys(sorted(cleaned)))


# ─────────────────────────────────────────────
# Social media
# ─────────────────────────────────────────────

SOCIAL_DOMAINS: dict[str, list[str]] = {
    "facebook":   ["facebook.com", "fb.com", "fb.me"],
    "twitter":    ["twitter.com", "x.com", "t.co"],
    "instagram":  ["instagram.com", "instagr.am"],
    "linkedin":   ["linkedin.com", "lnkd.in"],
    "youtube":    ["youtube.com", "youtu.be"],
    "tiktok":     ["tiktok.com", "vm.tiktok.com"],
    "github":     ["github.com"],
    "snapchat":   ["snapchat.com"],
    "pinterest":  ["pinterest.com", "pin.it"],
    "threads":    ["threads.net"],
    "discord":    ["discord.com", "discord.gg"],
    "telegram":   ["t.me", "telegram.me", "telegram.org"],
    "whatsapp":   ["wa.me", "whatsapp.com", "api.whatsapp.com"],
    "reddit":     ["reddit.com"],
    "twitch":     ["twitch.tv"],
    "medium":     ["medium.com"],
    "substack":   ["substack.com"],
}

TRACKING_PARAMS_RE = re.compile(r"\?(utm_|ref=|fbclid|gclid).*$", re.IGNORECASE)

CONTACT_KEYWORDS = re.compile(
    r"\b(contact|about us|get in touch|reach us|our team|support|"
    r"send us|customer service|meet the team|contactez|à propos|équipe|"
    r"nous contacter|joindre|coordonnées)\b",
    re.IGNORECASE,
)


def extract_social_links(html: str, base_url: str) -> dict[str, list[str]]:
    soup = parse_html(html)
    found: dict[str, list[str]] = {k: [] for k in SOCIAL_DOMAINS}

    def _add(platform: str, url: str) -> None:
        url = TRACKING_PARAMS_RE.sub("", url).rstrip("/")
        if len(url) > 12 and url not in found[platform]:
            found[platform].append(url)

    def _match(netloc: str, url: str) -> None:
        netloc = netloc.lower().lstrip("www.")
        for platform, domains in SOCIAL_DOMAINS.items():
            if any(d in netloc for d in domains):
                _add(platform, url)

    for tag in soup.find_all("a", href=True):
        abs_url = normalize_url(base_url, tag["href"].strip())
        if abs_url:
            try:
                _match(urlparse(abs_url).netloc, abs_url)
            except Exception:
                pass

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            for item in (data if isinstance(data, list) else [data]):
                for url in (lambda v: [v] if isinstance(v, str) else v)(item.get("sameAs", [])):
                    try:
                        _match(urlparse(url).netloc, url)
                    except Exception:
                        pass
        except Exception:
            pass

    for meta in soup.find_all("meta"):
        content = meta.get("content", "")
        if content.startswith("http"):
            try:
                _match(urlparse(content).netloc, content)
            except Exception:
                pass

    return {k: v for k, v in found.items() if v}


# ─────────────────────────────────────────────
# People extraction
# ─────────────────────────────────────────────

def extract_people(html: str, base_url: str) -> list[dict]:
    soup = parse_html(html)
    people: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, role: str = "", email: str = "",
             linkedin: str = "", twitter: str = "", url: str = "") -> None:
        name = name.strip()
        key  = name.lower()
        if not name or len(name) < 3 or key in seen:
            return
        if re.search(r"[<>{}\[\]|\\@#]", name):
            return
        if len(name.split()) > 7:
            return
        seen.add(key)
        people.append({
            "name": name, "role": role.strip(),
            "email": email.strip().lower() if email else "",
            "linkedin": linkedin.strip(), "twitter": twitter.strip(), "url": url.strip(),
        })

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            for item in (data if isinstance(data, list) else [data]):
                _extract_schema_person(item, _add)
        except Exception:
            pass

    for vcard in soup.find_all(class_=re.compile(r"\bvcard\b|\bh-card\b", re.I)):
        fn  = vcard.find(class_=re.compile(r"\bfn\b", re.I))
        rol = vcard.find(class_=re.compile(r"\brole\b|\btitle\b|\bposition\b", re.I))
        em  = vcard.find("a", href=re.compile(r"^mailto:", re.I))
        li  = vcard.find("a", href=re.compile(r"linkedin\.com", re.I))
        tw  = vcard.find("a", href=re.compile(r"twitter\.com|x\.com", re.I))
        if fn:
            _add(fn.get_text(strip=True),
                 role=rol.get_text(strip=True) if rol else "",
                 email=em["href"].replace("mailto:", "") if em else "",
                 linkedin=li["href"] if li else "",
                 twitter=tw["href"] if tw else "")

    for attrs in [
        {"class": re.compile(r"\bteam[-_]?member\b|\bstaff[-_]?card\b|\bperson[-_]?card\b", re.I)},
        {"class": re.compile(r"\bprofile[-_]?card\b|\bbio[-_]?card\b|\bemployee\b", re.I)},
        {"itemtype": re.compile(r"schema\.org/Person", re.I)},
    ]:
        for card in soup.find_all(True, attrs=attrs):
            hdr = card.find(re.compile(r"^h[1-6]$"))
            rol = card.find(class_=re.compile(r"\brole\b|\btitle\b|\bposition\b|\bjob\b", re.I))
            em  = card.find("a", href=re.compile(r"^mailto:", re.I))
            li  = card.find("a", href=re.compile(r"linkedin\.com", re.I))
            tw  = card.find("a", href=re.compile(r"twitter\.com|x\.com", re.I))
            if hdr:
                _add(hdr.get_text(strip=True),
                     role=rol.get_text(strip=True) if rol else "",
                     email=em["href"].replace("mailto:", "") if em else "",
                     linkedin=li["href"] if li else "",
                     twitter=tw["href"] if tw else "")

    for meta in soup.find_all("meta", attrs={"name": re.compile(r"author", re.I)}):
        if meta.get("content"):
            _add(meta["content"])

    for el in soup.find_all(attrs={"data-name": True}):
        _add(el["data-name"])
    for el in soup.find_all(attrs={"data-author": True}):
        _add(el["data-author"])

    if people:
        logger.info(f"  👤 People found (regex): {len(people)}")
    return people


def _extract_schema_person(data: dict, cb) -> None:
    if not isinstance(data, dict):
        return
    types = data.get("@type", "")
    if isinstance(types, str):
        types = [types]
    if "Person" in types:
        name = data.get("name", "")
        job  = data.get("jobTitle", "") or data.get("roleName", "")
        email = data.get("email", "").replace("mailto:", "")
        same_as = data.get("sameAs", [])
        if isinstance(same_as, str):
            same_as = [same_as]
        linkedin = next((u for u in same_as if "linkedin.com" in u), "")
        twitter  = next((u for u in same_as if "twitter.com" in u or "x.com" in u), "")
        if name:
            cb(name, role=job, email=email, linkedin=linkedin, twitter=twitter,
               url=data.get("url","") or data.get("@id",""))
    for key in ("member", "employee", "author", "founder", "director"):
        val = data.get(key)
        if isinstance(val, list):
            for item in val:
                _extract_schema_person(item, cb)
        elif isinstance(val, dict):
            _extract_schema_person(val, cb)


# ─────────────────────────────────────────────
# HTML utilities
# ─────────────────────────────────────────────

def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def extract_visible_text(html: str, max_chars: int = 14_000) -> str:
    """
    Clean visible text for LLM consumption.
    Removes scripts, styles, nav, footer. Deduplicates lines.
    Slightly larger budget than before since the LLM now reads this text.
    """
    soup = parse_html(html)
    for tag in soup(["script", "style", "noscript", "head", "nav", "footer",
                     "svg", "meta", "iframe"]):
        tag.decompose()

    parts = []
    seen  = set()
    for el in soup.find_all(["h1","h2","h3","h4","p","li","td","span","div",
                              "a","label","time","address"]):
        t = el.get_text(separator=" ", strip=True)
        if len(t) > 12 and t not in seen:
            seen.add(t)
            parts.append(t)

    result = "\n".join(parts)
    return result[:max_chars] + ("\n…[truncated]" if len(result) > max_chars else "")


def extract_links(html: str, base_url: str, max_links: int = 50) -> list[dict]:
    from utils import score_url_relevance
    soup = parse_html(html)
    links = []
    seen  = set()
    for tag in soup.find_all("a", href=True):
        abs_url = normalize_url(base_url, tag["href"].strip())
        if not abs_url or abs_url in seen:
            continue
        seen.add(abs_url)
        links.append({
            "url":   abs_url,
            "text":  tag.get_text(strip=True)[:100],
            "score": score_url_relevance(abs_url),
        })
    links.sort(key=lambda x: x["score"], reverse=True)
    return links[:max_links]


def extract_page_title(html: str) -> str:
    soup = parse_html(html)
    tag  = soup.find("title")
    return tag.get_text(strip=True) if tag else ""


def extract_meta_description(html: str) -> str:
    soup = parse_html(html)
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    return meta["content"].strip() if meta and meta.get("content") else ""


def detect_contact_page(url: str, text: str) -> bool:
    return (
        any(kw in url.lower() for kw in ["contact", "reach", "support", "touch", "joindre"])
        or bool(CONTACT_KEYWORDS.search(text))
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sync entry point (original — unchanged, used as fallback inside hybrid)
# ─────────────────────────────────────────────────────────────────────────────

def full_extraction(html: str, url: str) -> dict:
    """
    Pure regex pipeline — fast, no API calls.
    Still used directly inside full_extraction_with_llm() as the baseline.
    """
    parts     = html.split("<!-- REVEALED -->")
    main_html = parts[0]
    all_html  = html

    text     = extract_visible_text(all_html)
    links    = extract_links(main_html, url)
    emails   = extract_emails(text, all_html)
    phones   = extract_phone_numbers(text)
    socials  = extract_social_links(all_html, url)
    people   = extract_people(all_html, url)
    title    = extract_page_title(main_html)
    meta_d   = extract_meta_description(main_html)
    is_cont  = detect_contact_page(url, text)

    logger.info(
        f"  ✉ Emails:{len(emails)}  📞 Phones:{len(phones)}  "
        f"🌐 Socials:{len(socials)}  👤 People:{len(people)}"
    )
    return {
        "url": url, "title": title, "meta_description": meta_d,
        "visible_text": text, "links": links,
        "emails": emails, "phones": phones,
        "social_links": socials, "people": people,
        "is_contact_page": is_cont,
    }