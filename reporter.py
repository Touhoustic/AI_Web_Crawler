"""
reporter.py — Generate final report (print + JSON + txt).

CHANGES: no functional changes — stays compatible with memory.py.
page_summaries section now prints the full summary text clearly.
"""

import json
import time
from pathlib import Path
from memory import (
    fetch_people, fetch_emails, fetch_social_links, fetch_contacts,
    fetch_page_summaries, fetch_links, fetch_visited_urls,
)
from utils import logger, timestamp

SEP  = "═" * 70
THIN = "─" * 70


def _section(title: str) -> str:
    return f"\n{SEP}\n  {title}\n{SEP}"


def build_report(conn, start_time: float, steps_taken: int, errors: int,
                 seed_url: str, goal: str) -> dict:
    elapsed   = time.time() - start_time
    visited   = fetch_visited_urls(conn)
    emails    = fetch_emails(conn)
    links     = fetch_links(conn)
    socials   = fetch_social_links(conn)
    contacts  = fetch_contacts(conn)
    summaries = fetch_page_summaries(conn)
    people    = fetch_people(conn)

    return {
        "meta": {
            "seed_url": seed_url, "goal": goal,
            "generated_at": timestamp(),
            "elapsed_seconds": round(elapsed, 1),
            "steps_taken": steps_taken, "errors": errors,
        },
        "stats": {
            "pages_visited":    len(visited),
            "emails_found":     len(emails),
            "links_collected":  len(links),
            "social_profiles":  sum(len(v) for v in socials.values()),
            "phones_found":     len(contacts.get("phone", [])),
            "contact_pages":    len(contacts.get("contact_page", [])),
            "summaries_generated": len(summaries),
            "people_found":     len(people),
        },
        "visited_urls":   visited,
        "emails":         emails,
        "social_links":   socials,
        "contacts":       contacts,
        "links":          links,
        "page_summaries": summaries,
        "people":         people,
    }


def print_report(report: dict) -> None:
    meta  = report["meta"]
    stats = report["stats"]

    lines = [
        "", SEP, "  🕷️   WEB AGENT — FINAL CRAWL REPORT", SEP,
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
        f"  Phone numbers      : {stats['phones_found']}",
        f"  Social profiles    : {stats['social_profiles']}",
        f"  People found       : {stats['people_found']}",
        f"  Links collected    : {stats['links_collected']}",
        f"  Page summaries     : {stats['summaries_generated']}",
    ]

    lines.append(_section("🌐 VISITED PAGES"))
    for v in report["visited_urls"]:
        icon  = "✅" if v["status"] == "ok" else "❌"
        title = v["title"] or "(no title)"
        lines.append(f"  {icon} [depth {v['depth']}] {v['url']}")
        lines.append(f"        {title}")

    lines.append(_section("✉️  EMAILS FOUND"))
    lines.extend([f"  • {e}" for e in report["emails"]] or ["  (none found)"])

    lines.append(_section("📞 PHONE NUMBERS"))
    phones = report["contacts"].get("phone", [])
    lines.extend([f"  • {p}" for p in phones] or ["  (none found)"])

    lines.append(_section("🌍 SOCIAL MEDIA"))
    if report["social_links"]:
        for platform, urls in report["social_links"].items():
            lines.append(f"  {platform.upper()}:")
            lines.extend([f"    ↳ {u}" for u in urls])
    else:
        lines.append("  (none found)")

    lines.append(_section("👤 PEOPLE"))
    for p in report.get("people", []):
        line = f"  • {p['name']}"
        if p.get("role"):
            line += f"  — {p['role']}"
        lines.append(line)
        extras = [x for x in [p.get("email"), p.get("linkedin"), p.get("twitter")] if x]
        if extras:
            lines.append(f"    {' | '.join(extras)}")
    if not report.get("people"):
        lines.append("  (none found)")

    lines.append(_section("📝 PAGE SUMMARIES"))
    for s in report["page_summaries"]:
        lines.append(f"\n  🔗 {s['url']}")
        lines.append(f"  {s['summary']}")
        lines.append(f"  {THIN}")
    if not report["page_summaries"]:
        lines.append("  (none generated)")

    lines.append(_section("🔗 COLLECTED LINKS (first 30)"))
    for link in report["links"][:30]:
        text = f" [{link.get('text','')}]" if link.get("text") else ""
        lines.append(f"  • {link['url']}{text}")

    lines += [f"\n{SEP}", "  🏁 END OF REPORT", SEP, ""]
    print("\n".join(lines))


def save_report(report: dict, output_dir: str = ".") -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = timestamp().replace(":", "-").replace(" ", "_")
    json_path = out / f"report_{ts}.json"
    txt_path  = out / f"report_{ts}.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    import io, sys
    buf = io.StringIO()
    sys.stdout, old = buf, sys.stdout
    print_report(report)
    sys.stdout = old
    txt_path.write_text(buf.getvalue(), encoding="utf-8")

    logger.info(f"📄 Report → {txt_path}")
    logger.info(f"📦 JSON   → {json_path}")
    return txt_path, json_path


def generate_and_display(conn, start_time: float, steps: int, errors: int,
                         seed_url: str, goal: str, save: bool = True) -> dict:
    report = build_report(conn, start_time, steps, errors, seed_url, goal)
    print_report(report)
    if save:
        save_report(report)
    return report
