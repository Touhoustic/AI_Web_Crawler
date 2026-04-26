"""
memory.py — Persistent storage via SQLite and in-memory visited-URL tracking.

CHANGES vs previous:
  - Added session management: create_session, update_session, list_sessions,
    delete_session, get_session, open_session_db, snapshot_stats
  - Each run gets its own .db file under reports/sessions/<id>.db
  - sessions.json tracks metadata (url, goal, model, stats, status, created_at)
  - fetch_contacts, fetch_emails, etc. unchanged (backwards compatible)
"""

import json
import sqlite3
import uuid
from pathlib import Path
from utils import logger, timestamp

DB_PATH     = Path("agent_data.db")
SESSIONS_DIR  = Path("reports") / "sessions"
SESSIONS_FILE = Path("reports") / "sessions.json"

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# Database initialisation
# ─────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    logger.info(f"💾 Database ready: {db_path.resolve()}")
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS visited_urls (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT    UNIQUE NOT NULL,
            title      TEXT,
            visited_at TEXT,
            depth      INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'ok'
        )""",
        """CREATE TABLE IF NOT EXISTS emails (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    UNIQUE NOT NULL,
            source_url TEXT,
            found_at   TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT    NOT NULL,
            link_text  TEXT,
            source_url TEXT,
            found_at   TEXT,
            UNIQUE(url, source_url)
        )""",
        """CREATE TABLE IF NOT EXISTS social_links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            platform   TEXT    NOT NULL,
            url        TEXT    UNIQUE NOT NULL,
            source_url TEXT,
            found_at   TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS contacts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT    NOT NULL,
            value      TEXT    NOT NULL,
            source_url TEXT,
            found_at   TEXT,
            UNIQUE(type, value)
        )""",
        """CREATE TABLE IF NOT EXISTS people (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            role       TEXT,
            email      TEXT,
            linkedin   TEXT,
            twitter    TEXT,
            url        TEXT,
            source_url TEXT,
            found_at   TEXT,
            UNIQUE(name, source_url)
        )""",
        """CREATE TABLE IF NOT EXISTS page_summaries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT    UNIQUE NOT NULL,
            summary    TEXT,
            created_at TEXT
        )""",
    ]
    with conn:
        for s in stmts:
            conn.execute(s)
    logger.debug("DB tables verified.")


# ─────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────

def _load_sessions() -> list[dict]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_sessions(sessions: list[dict]) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")


def create_session(url: str, goal: str, model: str = "") -> dict:
    """Create a new session entry and its dedicated DB. Returns session dict."""
    sid = str(uuid.uuid4())[:12]
    from urllib.parse import urlparse
    domain = urlparse(url).netloc or url

    db_path = SESSIONS_DIR / f"{sid}.db"
    session = {
        "id":         sid,
        "url":        url,
        "domain":     domain,
        "goal":       goal,
        "model":      model,
        "db_path":    str(db_path),
        "created_at": timestamp(),
        "status":     "running",
        "stats":      {},
    }
    sessions = _load_sessions()
    sessions.insert(0, session)   # Newest first
    _save_sessions(sessions)
    logger.info(f"📁 Session created: {sid}  db={db_path}")
    return session


def update_session(sid: str, stats: dict | None = None, status: str | None = None) -> None:
    """Update stats and/or status for a session."""
    sessions = _load_sessions()
    for s in sessions:
        if s["id"] == sid:
            if stats is not None:
                s["stats"] = stats
            if status is not None:
                s["status"] = status
            break
    _save_sessions(sessions)


def list_sessions() -> list[dict]:
    """Return all sessions, newest first."""
    return _load_sessions()


def get_session(sid: str) -> dict | None:
    for s in _load_sessions():
        if s["id"] == sid:
            return s
    return None


def delete_session(sid: str) -> bool:
    """Remove session metadata and its DB file."""
    sessions = _load_sessions()
    remaining = [s for s in sessions if s["id"] != sid]
    if len(remaining) == len(sessions):
        return False
    _save_sessions(remaining)
    db = SESSIONS_DIR / f"{sid}.db"
    if db.exists():
        db.unlink()
    return True


def open_session_db(sid: str) -> sqlite3.Connection | None:
    """Open (read-only) the DB for a stored session. Returns None if not found."""
    db = SESSIONS_DIR / f"{sid}.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_stats(conn: sqlite3.Connection) -> dict:
    """Return a quick stats summary from a live DB connection."""
    try:
        emails   = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        phones   = conn.execute("SELECT COUNT(*) FROM contacts WHERE type='phone'").fetchone()[0]
        socials  = conn.execute("SELECT COUNT(*) FROM social_links").fetchone()[0]
        people   = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        pages    = conn.execute("SELECT COUNT(*) FROM visited_urls").fetchone()[0]
        return {"emails": emails, "phones": phones, "socials": socials,
                "people": people, "pages": pages}
    except Exception:
        return {}


# ─────────────────────────────────────────────
# In-memory URL tracking
# ─────────────────────────────────────────────

class VisitedURLTracker:
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

def store_visited_url(conn, url: str, title: str = "",
                      depth: int = 0, status: str = "ok") -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO visited_urls (url,title,visited_at,depth,status) VALUES(?,?,?,?,?)",
            (url, title, timestamp(), depth, status),
        )


def store_emails(conn, emails: list[str], source_url: str) -> int:
    count = 0
    with conn:
        for e in emails:
            cur = conn.execute(
                "INSERT OR IGNORE INTO emails (email,source_url,found_at) VALUES(?,?,?)",
                (e, source_url, timestamp()),
            )
            count += cur.rowcount
    return count


def store_links(conn, links: list[dict], source_url: str) -> None:
    with conn:
        for link in links:
            conn.execute(
                "INSERT OR IGNORE INTO links (url,link_text,source_url,found_at) VALUES(?,?,?,?)",
                (link["url"], link.get("text", ""), source_url, timestamp()),
            )


def store_social_links(conn, socials: dict[str, list[str]], source_url: str) -> None:
    with conn:
        for platform, urls in socials.items():
            for url in urls:
                conn.execute(
                    "INSERT OR IGNORE INTO social_links (platform,url,source_url,found_at) VALUES(?,?,?,?)",
                    (platform, url, source_url, timestamp()),
                )


def store_contacts(conn, phones: list[str], is_contact_page: bool, source_url: str) -> None:
    with conn:
        for phone in phones:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (type,value,source_url,found_at) VALUES(?,?,?,?)",
                ("phone", phone, source_url, timestamp()),
            )
        if is_contact_page:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (type,value,source_url,found_at) VALUES(?,?,?,?)",
                ("contact_page", source_url, source_url, timestamp()),
            )


def store_page_summary(conn, url: str, summary: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO page_summaries (url,summary,created_at) VALUES(?,?,?)",
            (url, summary, timestamp()),
        )


def store_people(conn, people: list[dict], source_url: str) -> None:
    with conn:
        for p in people:
            conn.execute(
                "INSERT OR IGNORE INTO people "
                "(name,role,email,linkedin,twitter,url,source_url,found_at) VALUES(?,?,?,?,?,?,?,?)",
                (p.get("name",""), p.get("role",""), p.get("email",""),
                 p.get("linkedin",""), p.get("twitter",""), p.get("url",""),
                 source_url, timestamp()),
            )


def persist_extraction(conn, data: dict, depth: int = 0) -> None:
    url = data["url"]
    store_visited_url(conn, url, data.get("title",""), depth)
    store_emails(conn, data.get("emails",[]), url)
    store_links(conn, data.get("links",[]), url)
    store_social_links(conn, data.get("social_links",{}), url)
    store_contacts(conn, data.get("phones",[]), data.get("is_contact_page",False), url)
    store_people(conn, data.get("people",[]), url)


# ─────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────

def fetch_emails(conn) -> list[str]:
    return [r["email"] for r in conn.execute("SELECT email FROM emails").fetchall()]


def fetch_social_links(conn) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for r in conn.execute("SELECT platform,url FROM social_links").fetchall():
        result.setdefault(r["platform"], []).append(r["url"])
    return result


def fetch_contacts(conn) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for r in conn.execute("SELECT type,value FROM contacts").fetchall():
        result.setdefault(r["type"], []).append(r["value"])
    return result


def fetch_page_summaries(conn) -> list[dict]:
    return [{"url": r["url"], "summary": r["summary"]}
            for r in conn.execute("SELECT url,summary FROM page_summaries").fetchall()]


def fetch_links(conn) -> list[dict]:
    return [{"url": r["url"], "text": r["link_text"], "source": r["source_url"]}
            for r in conn.execute("SELECT url,link_text,source_url FROM links").fetchall()]


def fetch_visited_urls(conn) -> list[dict]:
    return [{"url": r["url"], "title": r["title"], "depth": r["depth"], "status": r["status"]}
            for r in conn.execute("SELECT url,title,depth,status FROM visited_urls").fetchall()]


def fetch_people(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT name,role,email,linkedin,twitter,url,source_url FROM people"
    ).fetchall()]


def fetch_all(conn, table: str):
    return conn.execute(f"SELECT * FROM {table}").fetchall()
