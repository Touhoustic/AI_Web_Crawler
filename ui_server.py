"""
ui_server.py — Web UI for the Web Browsing Agent.

Run:  python ui_server.py
Then: http://localhost:5000

CHANGES vs original:
  - Full visual redesign: dark amber/orange aesthetic, animated stats, smooth transitions
  - Per-site session management: every crawl stored separately, resumable from UI
  - Session sidebar: view past scrapes instantly (no re-crawl needed)
  - Phones tab added to results panel
  - Fixed progress tracking: real step counter during agent run
  - Fixed SSE handler leak: handler removed cleanly after each run
  - Fixed DB path: each run gets its own session DB via memory.create_session()
  - Added /api/sessions GET/DELETE endpoints
  - Added /api/sessions/<id>/data for viewing stored session results
  - Animated number counters, live step display
  - Stop button to abort a running agent
"""

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)
KEYS_FILE = Path("keys.json")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.urandom(24)

_run_state: dict = {
    "running":   False,
    "log_queue": queue.Queue(),
    "result":    None,
    "session_id": None,
    "stop_flag": threading.Event(),
}


# ── Key helpers ───────────────────────────────────────────────────────────────

def load_keys() -> list[str]:
    if KEYS_FILE.exists():
        try:
            return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_keys(keys: list[str]) -> None:
    KEYS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    gi = Path(".gitignore")
    content = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if "keys.json" not in content:
        with gi.open("a", encoding="utf-8") as f:
            f.write("\nkeys.json\n")



# ── Routes ───────────────────────────────────────────────────────────────────
# HTML → templates/index.html   CSS → static/style.css


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# Keys
@app.route("/api/keys", methods=["GET"])
def get_keys():
    return jsonify(load_keys())

@app.route("/api/keys", methods=["POST"])
def set_keys():
    save_keys(request.get_json().get("keys", []))
    return jsonify({"ok": True})

@app.route("/api/keys/<int:idx>", methods=["DELETE"])
def del_key(idx):
    keys = load_keys()
    if 0 <= idx < len(keys):
        keys.pop(idx)
        save_keys(keys)
    return jsonify({"ok": True})

@app.route("/api/keys", methods=["DELETE"])
def clear_keys_route():
    save_keys([])
    return jsonify({"ok": True})


# Sessions
@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    from memory import list_sessions
    return jsonify(list_sessions())

@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    from memory import delete_session
    ok = delete_session(sid)
    return jsonify({"ok": ok})

@app.route("/api/sessions/<sid>/data", methods=["GET"])
def api_session_data(sid):
    """Return the full scraped data for a stored session."""
    from memory import (
        get_session, open_session_db,
        fetch_emails, fetch_contacts, fetch_social_links,
        fetch_people, fetch_visited_urls
    )
    session = get_session(sid)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    conn = open_session_db(sid)
    if not conn:
        return jsonify({"error": "Session database not available"}), 404

    try:
        contacts = fetch_contacts(conn)
        data = {
            "url":     session["url"],
            "goal":    session["goal"],
            "emails":  fetch_emails(conn),
            "phones":  contacts.get("phone", []),
            "socials": fetch_social_links(conn),
            "people":  fetch_people(conn),
            "visited": fetch_visited_urls(conn),
            "report":  None,
        }
        return jsonify(data)
    finally:
        conn.close()


# Run
@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_state["running"]:
        return jsonify({"error": "Agent already running"}), 409

    cfg = request.get_json()
    keys = load_keys()
    if not keys:
        return jsonify({"error": "No API keys saved. Go to the Keys tab and add your Groq key."})

    _run_state["running"] = True
    _run_state["result"] = None
    _run_state["stop_flag"].clear()

    # Drain old log queue
    while not _run_state["log_queue"].empty():
        try:
            _run_state["log_queue"].get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(target=_agent_thread, args=(cfg, keys), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _run_state["stop_flag"].set()
    return jsonify({"ok": True})


@app.route("/api/stream")
def api_stream():
    def gen():
        q = _run_state["log_queue"]
        while True:
            try:
                evt = q.get(timeout=25)
                yield evt
                if '"done"' in evt:
                    break
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── SSE helpers ────────────────────────────────────────────────────────────────

def _emit(event: str, data: dict):
    _run_state["log_queue"].put(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")


# ── Background agent thread ────────────────────────────────────────────────────

def _agent_thread(cfg: dict, keys: list[str]):
    sse_handler = None
    conn = None

    try:
        import logging
        from agent import WebAgent, AgentConfig
        from browser import BrowserWrapper
        from memory import (
            init_db, create_session, update_session, snapshot_stats,
            fetch_emails, fetch_contacts, fetch_social_links,
            fetch_people, fetch_visited_urls,
        )
        from utils import logger

        # ── Create per-site session ────────────────────────────────────
        session = create_session(cfg["url"], cfg["goal"], cfg["model"])
        sid = session["id"]
        _run_state["session_id"] = sid
        db_path = Path(session["db_path"])

        _emit("log", {"msg": f"Session {sid} created", "cls": "info"})
        _emit("log", {"msg": f"Target: {cfg['url']}", "cls": "info"})
        _emit("log", {"msg": f"Model: {cfg['model']}  Steps: {cfg['steps']}  Depth: {cfg['depth']}", "cls": "info"})
        _emit("progress", {"pct": 3, "label": "Initialising…", "step": 0, "max_steps": cfg['steps']})

        # ── Patch logger to emit SSE ───────────────────────────────────
        class SSEHandler(logging.Handler):
            def emit(self, record):
                msg = self.format(record)
                cls = ("ok"   if any(x in msg for x in ("✅","Email","Phone","People")) else
                       "warn" if "⚠" in msg else
                       "err"  if "❌" in msg else
                       "step" if "Step" in msg or "━━━" in msg else
                       "info" if any(x in msg for x in ("🌐","🤖","Action","Summary","Session")) else
                       "dim")
                _emit("log", {"msg": msg, "cls": cls})

        sse_handler = SSEHandler()
        sse_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sse_handler)

        # ── Init DB ───────────────────────────────────────────────────
        conn = init_db(db_path)

        # ── Progress callback ─────────────────────────────────────────
        def progress_cb(step, max_steps, label):
            pct = max(5, min(95, int(step / max_steps * 100)))
            _emit("progress", {"pct": pct, "label": label, "step": step, "max_steps": max_steps})

            # Emit live stats after each step
            emails   = fetch_emails(conn)
            contacts = fetch_contacts(conn)
            socials  = fetch_social_links(conn)
            people   = fetch_people(conn)
            visited  = fetch_visited_urls(conn)
            pages    = len(visited)
            _emit("stats", {
                "emails":  emails,
                "phones":  contacts.get("phone", []),
                "socials": socials,
                "people":  people,
                "visited": visited,
                "pages":   pages,
            })
            # Persist stats to session metadata
            update_session(sid, stats=snapshot_stats(conn))

        # ── Build agent config ─────────────────────────────────────────
        agent_cfg = AgentConfig(
            seed_url=cfg["url"],
            goal=cfg["goal"],
            api_keys=keys,
            max_steps=cfg.get("steps", 25),
            max_depth=cfg.get("depth", 3),
            same_domain_only=cfg.get("same_domain", True),
            headless=cfg.get("headless", True),
            model=cfg.get("model", "llama-3.3-70b-versatile"),
            save_report=cfg.get("save_report", True),
            output_dir=str(REPORTS_DIR),
        )

        # ── Run async agent ────────────────────────────────────────────
        async def _run_async():
            browser = BrowserWrapper(headless=agent_cfg.headless)
            await browser.start()
            try:
                agent = WebAgent(
                    agent_cfg, conn,
                    progress_callback=_async_progress(progress_cb),
                )
                return await agent.run(browser)
            finally:
                await browser.stop()

        report = asyncio.run(_run_async())

        # ── Finalise session ───────────────────────────────────────────
        update_session(sid, stats=snapshot_stats(conn), status="done")

        _emit("progress", {"pct": 100, "label": "Done", "step": cfg['steps'], "max_steps": cfg['steps']})
        _emit("done", {"report": report})

    except Exception as exc:
        import traceback
        _emit("log", {"msg": f"Fatal error: {exc}", "cls": "err"})
        _emit("log", {"msg": traceback.format_exc(), "cls": "err"})
        _emit("done", {"report": None})

        sid = _run_state.get("session_id")
        if sid:
            try:
                from memory import update_session
                update_session(sid, status="error")
            except Exception:
                pass

    finally:
        if sse_handler:
            try:
                from utils import logger
                logger.removeHandler(sse_handler)
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        _run_state["running"] = False
        _run_state["session_id"] = None


def _async_progress(sync_cb):
    """Wraps a sync progress callback into an async one for agent.py."""
    import asyncio
    async def wrapper(step, max_steps, label):
        await asyncio.get_event_loop().run_in_executor(None, sync_cb, step, max_steps, label)
        # Check stop flag
        if _run_state["stop_flag"].is_set():
            raise asyncio.CancelledError("User requested stop")
    return wrapper


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    print("\n" + "═" * 48)
    print("  WebAgent UI")
    print("  http://localhost:5000")
    print("═" * 48 + "\n")
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)