"""
reporter.py — Generate a comprehensive human-readable (and JSON) final report.
"""

import json
import time
from pathlib import Path
from memory import (
    fetch_people,
    fetch_emails,
    fetch_social_links,
    fetch_contacts,
    fetch_page_summaries,
    fetch_links,
    fetch_visited_urls,
)
from utils import logger, timestamp

SEPARATOR = "═" * 70
THIN_SEP  = "─" * 70


def _section(title: str) -> str:
    return f"\n{SEPARATOR}\n  {title}\n{SEPARATOR}"


def build_report(
    conn,
    start_time: float,
    steps_taken: int,
    errors: int,
    seed_url: str,
    goal: str,
) -> dict:
    """
    Query the database and build a full report dict.
    """
    elapsed = time.time() - start_time

    visited      = fetch_visited_urls(conn)
    emails       = fetch_emails(conn)
    links        = fetch_links(conn)
    socials      = fetch_social_links(conn)
    contacts     = fetch_contacts(conn)
    summaries    = fetch_page_summaries(conn)
    people       = fetch_people(conn)

    return {
        "meta": {
            "seed_url": seed_url,
            "goal": goal,
            "generated_at": timestamp(),
            "elapsed_seconds": round(elapsed, 1),
            "steps_taken": steps_taken,
            "errors": errors,
        },
        "stats": {
            "pages_visited": len(visited),
            "emails_found": len(emails),
            "links_collected": len(links),
            "social_profiles": sum(len(v) for v in socials.values()),
            "phones_found": len(contacts.get("phone", [])),
            "contact_pages": len(contacts.get("contact_page", [])),
            "summaries_generated": len(summaries),
            "people_found": len(people),
        },
        "visited_urls": visited,
        "emails": emails,
        "social_links": socials,
        "contacts": contacts,
        "links": links,
        "page_summaries": summaries,
        "people": people,
    }


def print_report(report: dict) -> None:
    """Print the report to stdout in a clean, readable format."""
    meta  = report["meta"]
    stats = report["stats"]

    lines = [
        "",
        SEPARATOR,
        "  🕷️   WEB AGENT — FINAL CRAWL REPORT",
        SEPARATOR,
        f"  Seed URL   : {meta['seed_url']}",
        f"  Goal       : {meta['goal']}",
        f"  Generated  : {meta['generated_at']}",
        f"  Elapsed    : {meta['elapsed_seconds']}s",
        "",
        _section("📊 STATISTICS"),
        f"  Pages visited      : {stats['pages_visited']}",
        f"  Steps taken        : {meta['steps_taken']}",
        f"  Errors             : {meta['errors']}",
        f"  Emails found       : {stats['emails_found']}",
        f"  Links collected    : {stats['links_collected']}",
        f"  Social profiles    : {stats['social_profiles']}",
        f"  Phone numbers      : {stats['phones_found']}",
        f"  Contact pages      : {stats['contact_pages']}",
    ]

    # ── Visited URLs ──────────────────────────
    lines.append(_section("🌐 VISITED PAGES"))
    for v in report["visited_urls"]:
        status_icon = "✅" if v["status"] == "ok" else "❌"
        title = v["title"] or "(no title)"
        lines.append(f"  {status_icon} [{v['depth']}] {v['url']}")
        lines.append(f"       {title}")

    # ── Emails ────────────────────────────────
    lines.append(_section("✉️  EMAILS FOUND"))
    if report["emails"]:
        for e in report["emails"]:
            lines.append(f"  • {e}")
    else:
        lines.append("  (none found)")

    # ── Social Media ──────────────────────────
    lines.append(_section("🌍 SOCIAL MEDIA PROFILES"))
    if report["social_links"]:
        for platform, urls in report["social_links"].items():
            lines.append(f"  {platform.upper()}:")
            for url in urls:
                lines.append(f"    ↳ {url}")
    else:
        lines.append("  (none found)")

    # ── Contacts ──────────────────────────────
    lines.append(_section("📞 CONTACTS"))
    phones = report["contacts"].get("phone", [])
    contact_pages = report["contacts"].get("contact_page", [])
    if phones:
        lines.append("  Phone numbers:")
        for p in phones:
            lines.append(f"    • {p}")
    if contact_pages:
        lines.append("  Contact pages:")
        for cp in contact_pages:
            lines.append(f"    • {cp}")
    if not phones and not contact_pages:
        lines.append("  (none found)")

    # ── Summaries ─────────────────────────────
    lines.append(_section("📝 PAGE SUMMARIES"))
    if report["page_summaries"]:
        for s in report["page_summaries"]:
            lines.append(f"\n  URL: {s['url']}")
            lines.append(f"  {s['summary']}")
            lines.append(f"  {THIN_SEP}")
    else:
        lines.append("  (no summaries generated)")

    # ── People
    lines.append(_section("👤 PEOPLE FOUND"))
    people_data = report.get("people", [])
    if people_data:
        for p in people_data:
            name_line = f"  • {p['name']}"
            if p.get("role"):
                name_line += f"  — {p['role']}"
            lines.append(name_line)
            extras = [x for x in [p.get("email"), p.get("linkedin"), p.get("twitter")] if x]
            if extras:
                lines.append(f"    {' | '.join(extras)}")
    else:
        lines.append("  (none found)")

    # ── Top Links ─────────────────────────────
    lines.append(_section("🔗 COLLECTED LINKS (first 30)"))
    for link in report["links"][:30]:
        text = link.get("text", "")
        text_fmt = f" [{text}]" if text else ""
        lines.append(f"  • {link['url']}{text_fmt}")
        if link.get("source"):
            lines.append(f"    from: {link['source']}")

    lines.append(f"\n{SEPARATOR}")
    lines.append("  🏁 END OF REPORT")
    lines.append(SEPARATOR)
    lines.append("")

    print("\n".join(lines))


def save_report(report: dict, output_dir: str = ".") -> tuple[Path, Path]:
    """
    Save the report as both a human-readable .txt file and a .json file.
    Returns (txt_path, json_path).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = timestamp().replace(":", "-").replace(" ", "_")
    json_path = out / f"report_{ts}.json"
    txt_path  = out / f"report_{ts}.txt"

    # JSON export
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Text export — capture stdout via string building
    import io, sys
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    print_report(report)
    sys.stdout = old_stdout
    txt_content = buf.getvalue()

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt_content)

    logger.info(f"📄 Report saved → {txt_path}")
    logger.info(f"📦 JSON saved  → {json_path}")
    return txt_path, json_path


def generate_and_display(conn, start_time: float, steps: int, errors: int,
                         seed_url: str, goal: str, save: bool = True) -> dict:
    """
    Top-level helper: build, print, and optionally save the report.
    Returns the report dict.
    """
    report = build_report(conn, start_time, steps, errors, seed_url, goal)
    print_report(report)
    if save:
        save_report(report)
    return report
