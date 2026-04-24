"""
memory.py — Persistent storage via SQLite and in-memory visited-URL tracking.
"""

import sqlite3
import json
from pathlib import Path
from utils import logger, timestamp

DB_PATH = Path("agent_data.db")


# ─────────────────────────────────────────────
# Database initialisation
# ─────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Create (or open) the SQLite database and ensure all tables exist.
    Returns an open connection with row_factory set for dict-like access.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Better concurrency
    _create_tables(conn)
    logger.info(f"💾 Database ready: {db_path.resolve()}")
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    sql_statements = [
        # Pages the agent actually visited
        """
        CREATE TABLE IF NOT EXISTS visited_urls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    UNIQUE NOT NULL,
            title       TEXT,
            visited_at  TEXT,
            depth       INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'ok'
        )
        """,
        # All emails discovered
        """
        CREATE TABLE IF NOT EXISTS emails (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT    UNIQUE NOT NULL,
            source_url  TEXT,
            found_at    TEXT
        )
        """,
        # All hyperlinks discovered (source → target)
        """
        CREATE TABLE IF NOT EXISTS links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    NOT NULL,
            link_text   TEXT,
            source_url  TEXT,
            found_at    TEXT,
            UNIQUE(url, source_url)
        )
        """,
        # Social media profile URLs
        """
        CREATE TABLE IF NOT EXISTS social_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT    NOT NULL,
            url         TEXT    UNIQUE NOT NULL,
            source_url  TEXT,
            found_at    TEXT
        )
        """,
        # Phone numbers and contact-page references
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT    NOT NULL,   -- 'phone' | 'contact_page'
            value       TEXT    NOT NULL,
            source_url  TEXT,
            found_at    TEXT,
            UNIQUE(type, value)
        )
        """,
        # Named people extracted from pages
        """
        CREATE TABLE IF NOT EXISTS people (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            role        TEXT,
            email       TEXT,
            linkedin    TEXT,
            twitter     TEXT,
            url         TEXT,
            source_url  TEXT,
            found_at    TEXT,
            UNIQUE(name, source_url)
        )
        """,
        # LLM-generated summaries per page
        """
        CREATE TABLE IF NOT EXISTS page_summaries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    UNIQUE NOT NULL,
            summary     TEXT,
            created_at  TEXT
        )
        """,
    ]
    with conn:
        for stmt in sql_statements:
            conn.execute(stmt)
    logger.debug("DB tables verified.")


# ─────────────────────────────────────────────
# In-memory URL tracking
# ─────────────────────────────────────────────

class VisitedURLTracker:
    """Fast in-memory set for visited URL de-duplication."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def mark(self, url: str) -> None:
        self._seen.add(url)

    def has_seen(self, url: str) -> bool:
        return url in self._seen

    def count(self) -> int:
        return len(self._seen)

    def all_urls(self) -> list[str]:
        return list(self._seen)


# ─────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────

def store_visited_url(conn: sqlite3.Connection, url: str, title: str = "",
                      depth: int = 0, status: str = "ok") -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO visited_urls (url, title, visited_at, depth, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (url, title, timestamp(), depth, status),
        )


def store_emails(conn: sqlite3.Connection, emails: list[str], source_url: str) -> int:
    """Insert emails. Returns count of newly inserted rows."""
    count = 0
    with conn:
        for email in emails:
            cur = conn.execute(
                "INSERT OR IGNORE INTO emails (email, source_url, found_at) VALUES (?, ?, ?)",
                (email, source_url, timestamp()),
            )
            count += cur.rowcount
    return count


def store_links(conn: sqlite3.Connection, links: list[dict], source_url: str) -> None:
    with conn:
        for link in links:
            conn.execute(
                "INSERT OR IGNORE INTO links (url, link_text, source_url, found_at) "
                "VALUES (?, ?, ?, ?)",
                (link["url"], link.get("text", ""), source_url, timestamp()),
            )


def store_social_links(conn: sqlite3.Connection, socials: dict[str, list[str]],
                       source_url: str) -> None:
    with conn:
        for platform, urls in socials.items():
            for url in urls:
                conn.execute(
                    "INSERT OR IGNORE INTO social_links (platform, url, source_url, found_at) "
                    "VALUES (?, ?, ?, ?)",
                    (platform, url, source_url, timestamp()),
                )


def store_contacts(conn: sqlite3.Connection, phones: list[str],
                   is_contact_page: bool, source_url: str) -> None:
    with conn:
        for phone in phones:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (type, value, source_url, found_at) "
                "VALUES (?, ?, ?, ?)",
                ("phone", phone, source_url, timestamp()),
            )
        if is_contact_page:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (type, value, source_url, found_at) "
                "VALUES (?, ?, ?, ?)",
                ("contact_page", source_url, source_url, timestamp()),
            )


def store_page_summary(conn: sqlite3.Connection, url: str, summary: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO page_summaries (url, summary, created_at) VALUES (?, ?, ?)",
            (url, summary, timestamp()),
        )


def persist_extraction(conn: sqlite3.Connection, data: dict, depth: int = 0) -> None:
    """
    Convenience function: store all extracted data from a single page at once.
    `data` is the dict returned by extractor.full_extraction().
    """
    url = data["url"]
    store_visited_url(conn, url, data.get("title", ""), depth)
    store_emails(conn, data.get("emails", []), url)
    store_links(conn, data.get("links", []), url)
    store_social_links(conn, data.get("social_links", {}), url)
    store_contacts(conn, data.get("phones", []), data.get("is_contact_page", False), url)
    store_people(conn, data.get("people", []), url)


# ─────────────────────────────────────────────
# Query helpers (used by reporter)
# ─────────────────────────────────────────────

def fetch_all(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def fetch_emails(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT email FROM emails").fetchall()
    return [r["email"] for r in rows]


def fetch_social_links(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT platform, url FROM social_links").fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r["platform"], []).append(r["url"])
    return result


def fetch_contacts(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT type, value FROM contacts").fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r["type"], []).append(r["value"])
    return result


def fetch_page_summaries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT url, summary FROM page_summaries").fetchall()
    return [{"url": r["url"], "summary": r["summary"]} for r in rows]


def fetch_links(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT url, link_text, source_url FROM links").fetchall()
    return [{"url": r["url"], "text": r["link_text"], "source": r["source_url"]} for r in rows]


def fetch_visited_urls(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT url, title, depth, status FROM visited_urls").fetchall()
    return [{"url": r["url"], "title": r["title"], "depth": r["depth"],
             "status": r["status"]} for r in rows]


def store_people(conn: sqlite3.Connection, people: list[dict], source_url: str) -> None:
    """Insert named people found on a page."""
    with conn:
        for p in people:
            conn.execute(
                "INSERT OR IGNORE INTO people (name, role, email, linkedin, twitter, url, source_url, found_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (p.get("name",""), p.get("role",""), p.get("email",""),
                 p.get("linkedin",""), p.get("twitter",""), p.get("url",""),
                 source_url, timestamp()),
            )


def fetch_people(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT name, role, email, linkedin, twitter, url, source_url FROM people").fetchall()
    return [dict(r) for r in rows]
