"""
extractor.py — HTML parsing, text extraction, and smart data mining.

Improvements over v1:
  - Smart name extraction: JSON-LD, schema.org, hCard, Open Graph, vCard, data attrs
  - Expanded social media: 20+ platforms including Threads, Discord, Telegram, etc.
  - Better email detection: mailto links, JS obfuscation patterns
  - Phone deduplication by digit normalization
"""

import re
import json
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from utils import normalize_url, logger

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?[\d]{1,3}[\s\-.]?)?(?:\(?[\d]{1,4}\)?[\s\-.]?)?[\d]{3,4}[\s\-.]?[\d]{3,4}[\s\-.]?[\d]{0,4}")
OBFUSCATED_EMAIL_RE = re.compile(r'''["']([a-zA-Z0-9._%+\-]+)["']\s*\+?\s*["']@["']\s*\+?\s*["']([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})["']''')

SOCIAL_DOMAINS = {
    "facebook":   ["facebook.com", "fb.com", "fb.me"],
    "twitter":    ["twitter.com", "x.com", "t.co"],
    "instagram":  ["instagram.com", "instagr.am"],
    "linkedin":   ["linkedin.com", "lnkd.in"],
    "youtube":    ["youtube.com", "youtu.be"],
    "tiktok":     ["tiktok.com", "vm.tiktok.com"],
    "github":     ["github.com", "gist.github.com"],
    "snapchat":   ["snapchat.com"],
    "pinterest":  ["pinterest.com", "pin.it"],
    "threads":    ["threads.net"],
    "mastodon":   ["mastodon.social", "mstdn.social"],
    "discord":    ["discord.com", "discord.gg", "discordapp.com"],
    "telegram":   ["t.me", "telegram.me", "telegram.org"],
    "whatsapp":   ["wa.me", "whatsapp.com"],
    "reddit":     ["reddit.com", "redd.it"],
    "twitch":     ["twitch.tv"],
    "medium":     ["medium.com"],
    "substack":   ["substack.com"],
    "behance":    ["behance.net"],
    "dribbble":   ["dribbble.com"],
}

CONTACT_KEYWORDS = re.compile(
    r"\b(contact|about us|get in touch|reach us|our team|support|helpdesk|"
    r"send us|write to us|talk to us|customer service|meet the team|our people)\b",
    re.IGNORECASE,
)


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def extract_visible_text(html: str, max_chars: int = 4000) -> str:
    soup = parse_html(html)
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "svg"]):
        tag.decompose()

    text_parts = []
    for element in soup.find_all(["p", "h1", "h2", "h3", "h4", "li", "td", "span", "div", "a"]):
        t = element.get_text(separator=" ", strip=True)
        if len(t) > 20:
            text_parts.append(t)

    full_text = "\n".join(text_parts)
    lines = full_text.splitlines()
    seen, deduped = set(), []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            deduped.append(stripped)

    result = "\n".join(deduped)
    return result[:max_chars] + ("\n... [truncated]" if len(result) > max_chars else "")


def extract_links(html: str, base_url: str, max_links: int = 40) -> list[dict]:
    from utils import score_url_relevance
    soup = parse_html(html)
    links = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        abs_url = normalize_url(base_url, href)
        if not abs_url or abs_url in seen:
            continue
        seen.add(abs_url)
        text = tag.get_text(strip=True)[:80]
        score = score_url_relevance(abs_url)
        links.append({"url": abs_url, "text": text, "score": score})

    links.sort(key=lambda x: x["score"], reverse=True)
    return links[:max_links]


def extract_emails(text: str, html: str = "") -> list[str]:
    """Extract emails from text, mailto: links, and JS obfuscation patterns."""
    found = set()
    found.update(e.lower() for e in EMAIL_RE.findall(text))

    if html:
        mailto_re = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.I)
        found.update(m.lower() for m in mailto_re.findall(html))
        for user, domain in OBFUSCATED_EMAIL_RE.findall(html):
            found.add(f"{user.lower()}@{domain.lower()}")

    bad_exts = {".png", ".jpg", ".gif", ".svg", ".webp", ".css", ".js", ".ico"}
    cleaned = [
        e for e in found
        if not any(e.endswith(ext) for ext in bad_exts)
        and "@" in e and "." in e.split("@")[1]
    ]
    return list(dict.fromkeys(sorted(cleaned)))


def extract_phone_numbers(text: str) -> list[str]:
    raw = PHONE_RE.findall(text)
    seen_digits = set()
    phones = []
    for p in raw:
        digits = re.sub(r"\D", "", p)
        if 7 <= len(digits) <= 15 and digits not in seen_digits:
            seen_digits.add(digits)
            phones.append(p.strip())
    return phones


def extract_social_links(html: str, base_url: str) -> dict[str, list[str]]:
    """Detect social links via href scanning, JSON-LD sameAs, and Open Graph meta."""
    soup = parse_html(html)
    found: dict[str, list[str]] = {k: [] for k in SOCIAL_DOMAINS}

    def _add(platform: str, url: str) -> None:
        url = url.split("?")[0].rstrip("/")
        if url not in found[platform]:
            found[platform].append(url)

    # 1. <a href> links
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        abs_url = normalize_url(base_url, href)
        if not abs_url:
            continue
        netloc = urlparse(abs_url).netloc.lower().lstrip("www.")
        for platform, domains in SOCIAL_DOMAINS.items():
            if any(d in netloc for d in domains):
                _add(platform, abs_url)

    # 2. JSON-LD sameAs
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                same_as = item.get("sameAs", [])
                if isinstance(same_as, str):
                    same_as = [same_as]
                for url in same_as:
                    netloc = urlparse(url).netloc.lower().lstrip("www.")
                    for platform, domains in SOCIAL_DOMAINS.items():
                        if any(d in netloc for d in domains):
                            _add(platform, url)
        except Exception:
            pass

    # 3. Open Graph / meta content links
    for meta in soup.find_all("meta"):
        content = meta.get("content", "")
        if content.startswith("http"):
            netloc = urlparse(content).netloc.lower().lstrip("www.")
            for platform, domains in SOCIAL_DOMAINS.items():
                if any(d in netloc for d in domains):
                    _add(platform, content)

    return {k: v for k, v in found.items() if v}


def extract_people(html: str, base_url: str) -> list[dict]:
    """
    Smart multi-strategy name/person extraction:
    1. JSON-LD schema.org Person
    2. hCard / h-card microformat
    3. Team card CSS class patterns
    4. Open Graph / meta author
    5. data-name / data-author attributes
    """
    soup = parse_html(html)
    people = []
    seen_names: set[str] = set()

    def _add(name: str, role: str = "", email: str = "",
             linkedin: str = "", twitter: str = "", url: str = "") -> None:
        name = name.strip()
        if not name or len(name) < 3 or name.lower() in seen_names:
            return
        if re.search(r"[<>{}\[\]|\\@]", name):
            return
        if len(name.split()) > 6:   # reject sentence-length artifacts
            return
        seen_names.add(name.lower())
        people.append({
            "name": name,
            "role": role.strip(),
            "email": email.strip().lower() if email else "",
            "linkedin": linkedin.strip(),
            "twitter": twitter.strip(),
            "url": url.strip(),
        })

    # 1. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                _extract_schema_person(item, _add)
        except Exception:
            pass

    # 2. hCard
    for vcard in soup.find_all(class_=re.compile(r"\bvcard\b|\bh-card\b", re.I)):
        fn = vcard.find(class_=re.compile(r"\bfn\b", re.I))
        role_el = vcard.find(class_=re.compile(r"\brole\b|\btitle\b|\bposition\b", re.I))
        email_el = vcard.find("a", href=re.compile(r"^mailto:", re.I))
        li_el = vcard.find("a", href=re.compile(r"linkedin\.com", re.I))
        tw_el = vcard.find("a", href=re.compile(r"twitter\.com|x\.com", re.I))
        if fn:
            _add(fn.get_text(strip=True),
                 role=role_el.get_text(strip=True) if role_el else "",
                 email=(email_el["href"].replace("mailto:", "") if email_el else ""),
                 linkedin=(li_el["href"] if li_el else ""),
                 twitter=(tw_el["href"] if tw_el else ""))

    # 3. Team card patterns
    team_patterns = [
        {"class": re.compile(r"\bteam[-_]?member\b|\bstaff[-_]?card\b|\bperson[-_]?card\b", re.I)},
        {"class": re.compile(r"\bprofile[-_]?card\b|\bbio[-_]?card\b|\bemployee\b", re.I)},
        {"itemtype": re.compile(r"schema\.org/Person", re.I)},
    ]
    for selector in team_patterns:
        for card in soup.find_all(True, attrs=selector):
            name_el = card.find(re.compile(r"^h[1-6]$"))
            role_el = card.find(class_=re.compile(r"\brole\b|\btitle\b|\bposition\b|\bjob\b", re.I))
            email_el = card.find("a", href=re.compile(r"^mailto:", re.I))
            li_el = card.find("a", href=re.compile(r"linkedin\.com", re.I))
            tw_el = card.find("a", href=re.compile(r"twitter\.com|x\.com", re.I))
            if name_el:
                _add(name_el.get_text(strip=True),
                     role=role_el.get_text(strip=True) if role_el else "",
                     email=(email_el["href"].replace("mailto:", "") if email_el else ""),
                     linkedin=(li_el["href"] if li_el else ""),
                     twitter=(tw_el["href"] if tw_el else ""))

    # 4. Meta author
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"author", re.I)}):
        if meta.get("content"):
            _add(meta["content"])

    # 5. data attributes
    for el in soup.find_all(attrs={"data-name": True}):
        _add(el["data-name"])
    for el in soup.find_all(attrs={"data-author": True}):
        _add(el["data-author"])

    logger.info(f"  👤 People found (structured): {len(people)}")
    return people


def _extract_schema_person(data: dict, callback) -> None:
    """Recursively extract schema.org Person entries from JSON-LD."""
    if not isinstance(data, dict):
        return
    type_ = data.get("@type", "")
    types = [type_] if isinstance(type_, str) else (type_ if isinstance(type_, list) else [])
    if "Person" in types:
        name = data.get("name", "")
        job = data.get("jobTitle", "") or data.get("roleName", "")
        email = data.get("email", "").replace("mailto:", "")
        same_as = data.get("sameAs", [])
        if isinstance(same_as, str):
            same_as = [same_as]
        linkedin = next((u for u in same_as if "linkedin.com" in u), "")
        twitter = next((u for u in same_as if "twitter.com" in u or "x.com" in u), "")
        url = data.get("url", "") or data.get("@id", "")
        if name:
            callback(name, role=job, email=email, linkedin=linkedin, twitter=twitter, url=url)
    for key in ("member", "employee", "author", "founder", "director"):
        val = data.get(key)
        if isinstance(val, list):
            for item in val:
                _extract_schema_person(item, callback)
        elif isinstance(val, dict):
            _extract_schema_person(val, callback)


def extract_page_title(html: str) -> str:
    soup = parse_html(html)
    tag = soup.find("title")
    return tag.get_text(strip=True) if tag else ""


def extract_meta_description(html: str) -> str:
    soup = parse_html(html)
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""


def detect_contact_page(url: str, text: str) -> bool:
    url_lower = url.lower()
    contact_url = any(kw in url_lower for kw in ["contact", "reach", "support", "touch"])
    contact_text = bool(CONTACT_KEYWORDS.search(text))
    return contact_url or contact_text


def full_extraction(html: str, url: str) -> dict:
    """
    Run all extractors on a page and return a consolidated dict.
    Primary entry-point called by the agent after fetching a page.
    """
    text = extract_visible_text(html)
    links = extract_links(html, url)
    emails = extract_emails(text, html)
    phones = extract_phone_numbers(text)
    socials = extract_social_links(html, url)
    people = extract_people(html, url)
    title = extract_page_title(html)
    meta_desc = extract_meta_description(html)
    is_contact = detect_contact_page(url, text)

    logger.info(f"  ✉  Emails found: {len(emails)}")
    logger.info(f"  📞 Phones found: {len(phones)}")
    logger.info(f"  🔗 Links found:  {len(links)}")
    logger.info(f"  🌐 Socials:      {list(socials.keys())}")
    logger.info(f"  👤 People:       {len(people)}")

    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "visible_text": text,
        "links": links,
        "emails": emails,
        "phones": phones,
        "social_links": socials,
        "people": people,
        "is_contact_page": is_contact,
    }
