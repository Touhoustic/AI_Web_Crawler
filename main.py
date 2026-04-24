"""
main.py — Entry point for the Web Browsing AI Agent.

Usage:
    python main.py --url https://example.com --goal "Find contact info" \
                   --keys gsk_key1,gsk_key2 --steps 25

    Or with a config file:
    python main.py --config config.json
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# ── Graceful import error messages ───────────────────────────────────────────
MISSING_DEPS = []
try:
    from groq import Groq
except ImportError:
    MISSING_DEPS.append("groq")
try:
    from playwright.async_api import async_playwright
except ImportError:
    MISSING_DEPS.append("playwright")
try:
    from bs4 import BeautifulSoup
except ImportError:
    MISSING_DEPS.append("beautifulsoup4")

if MISSING_DEPS:
    print(f"\n❌ Missing dependencies: {', '.join(MISSING_DEPS)}")
    print("   Run: pip install -r requirements.txt")
    print("   Then (for Playwright): playwright install chromium\n")
    sys.exit(1)

from agent import WebAgent, AgentConfig
from browser import BrowserWrapper
from memory import init_db
from utils import logger, is_valid_url


# ─────────────────────────────────────────────
# CLI argument parsing
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="🕷️  Autonomous Web Browsing AI Agent (Groq + Playwright)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --url https://example.com --keys gsk_abc123
  python main.py --url https://company.com --goal "Find team emails" \\
                 --keys gsk_k1,gsk_k2 --steps 30 --depth 3
  python main.py --config my_config.json
        """,
    )
    p.add_argument("--url",       type=str, help="Seed URL to start crawling")
    p.add_argument("--goal",      type=str, default="Find all contact information, emails, and social media profiles",
                   help="Agent goal description")
    p.add_argument("--keys",      type=str, help="Comma-separated Groq API keys (or set GROQ_API_KEYS env var)")
    p.add_argument("--model",     type=str, default="llama-3.3-70b-versatile", help="Groq model name")
    p.add_argument("--steps",     type=int, default=25, help="Maximum agent steps (default: 25)")
    p.add_argument("--depth",     type=int, default=3,  help="Maximum crawl depth (default: 3)")
    p.add_argument("--all-domains", action="store_true",
                   help="Allow crawling outside the seed domain (default: same-domain only)")
    p.add_argument("--headed",    action="store_true", help="Run browser in visible (headed) mode")
    p.add_argument("--db",        type=str, default="agent_data.db", help="SQLite database path")
    p.add_argument("--output",    type=str, default=".", help="Directory for report output files")
    p.add_argument("--no-report", action="store_true", help="Skip saving report files")
    p.add_argument("--config",    type=str, help="Path to JSON config file (overrides CLI args)")
    return p.parse_args()


def load_config_file(path: str) -> dict:
    """Load a JSON configuration file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        sys.exit(1)


def resolve_api_keys(args: argparse.Namespace, cfg: dict) -> list[str]:
    """Collect API keys from CLI → config file → environment variable."""
    raw = (
        args.keys
        or cfg.get("keys")
        or os.environ.get("GROQ_API_KEYS", "")
    )
    if not raw:
        logger.error(
            "❌ No Groq API keys provided.\n"
            "   Use --keys gsk_xxx or set the GROQ_API_KEYS environment variable."
        )
        sys.exit(1)
    return [k.strip() for k in raw.split(",") if k.strip()]


# ─────────────────────────────────────────────
# Main async runner
# ─────────────────────────────────────────────

async def run_agent(config: AgentConfig, db_path: str) -> None:
    """Initialise components and run the agent to completion."""
    # Validate seed URL
    if not is_valid_url(config.seed_url):
        logger.error(f"❌ Invalid seed URL: {config.seed_url}")
        sys.exit(1)

    # Initialise database
    conn = init_db(Path(db_path))

    # Start browser
    browser = BrowserWrapper(headless=config.headless)
    await browser.start()

    try:
        agent = WebAgent(config, conn)
        await agent.run(browser)
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Interrupted by user. Generating partial report…")
        from reporter import generate_and_display
        import time
        generate_and_display(
            conn=conn,
            start_time=time.time(),
            steps=0,
            errors=0,
            seed_url=config.seed_url,
            goal=config.goal,
            save=config.save_report,
        )
    finally:
        await browser.stop()
        conn.close()


def main() -> None:
    args = parse_args()

    # Load config file if provided
    cfg: dict = {}
    if args.config:
        cfg = load_config_file(args.config)
        logger.info(f"📋 Loaded config from {args.config}")

    # Resolve all settings (CLI > config file > defaults)
    seed_url   = args.url    or cfg.get("url")
    goal       = args.goal   or cfg.get("goal", "Find all contact information and emails")
    model      = args.model  or cfg.get("model", "llama-3.3-70b-versatile")
    max_steps  = args.steps  if args.steps != 25 else cfg.get("steps", 25)
    max_depth  = args.depth  if args.depth != 3  else cfg.get("depth", 3)
    same_domain_only = not args.all_domains and not cfg.get("all_domains", False)
    headless   = not args.headed and not cfg.get("headed", False)
    db_path    = args.db     or cfg.get("db", "agent_data.db")
    output_dir = args.output or cfg.get("output", ".")
    save_report = not args.no_report

    if not seed_url:
        logger.error("❌ No seed URL provided. Use --url or set 'url' in config file.")
        sys.exit(1)

    api_keys = resolve_api_keys(args, cfg)

    config = AgentConfig(
        seed_url=seed_url,
        goal=goal,
        api_keys=api_keys,
        max_steps=max_steps,
        max_depth=max_depth,
        same_domain_only=same_domain_only,
        headless=headless,
        model=model,
        save_report=save_report,
        output_dir=output_dir,
    )

    logger.info("🚀 Starting Web Browsing Agent…")
    asyncio.run(run_agent(config, db_path))


if __name__ == "__main__":
    main()
