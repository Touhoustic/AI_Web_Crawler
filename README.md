# 🕷️ WebAgent — AI-Powered Web Crawler

An autonomous web crawling agent that extracts contact information (emails, phone numbers, social profiles, people) from any website. Powered by Groq LLMs, Playwright for browser automation, and a Flask web UI.

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square)
![Flask](https://img.shields.io/badge/Flask-3.0+-green?style=flat-square)
![Playwright](https://img.shields.io/badge/Playwright-1.40+-orange?style=flat-square)
![Groq](https://img.shields.io/badge/Groq-LLaMA_3.3-red?style=flat-square)

---

## ✨ Features

- **Hybrid extraction** — LLM + regex pipeline run in parallel and merge results, catching both obvious and obfuscated contact data
- **Hidden data reveal** — Automatically clicks "Show phone", "Contact seller" buttons before extracting
- **Multi-key rotation** — Rotates Groq API keys automatically on rate limits with cooldown-and-retry logic
- **Per-session storage** — Every crawl gets its own SQLite database; past sessions are resumable from the UI
- **Live UI** — Real-time terminal log, animated stats counters, progress bar, and stop button via SSE
- **Smart navigation** — LLM decides where to crawl next based on link relevance scoring
- **Multilingual** — Handles English, French, Arabic page content and obfuscation patterns
- **Report export** — Generates structured JSON + plaintext reports after every run

---

## 🖥️ UI Preview

The web interface features a **red neon sci-fi aesthetic** with:
- Animated grid background and CRT scanlines
- Live terminal log with color-coded message types
- Stats panel (pages, emails, phones, socials, people) with animated counters
- Session sidebar for browsing and reloading past crawls
- Tabs: Terminal · Emails · Phones · Social · People · Visited · Report

---

## 🚀 Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/Touhoustic/AI_Web_Crawler.git
cd AI_Web_Crawler
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
pip install httpx              # required for LLM extraction
playwright install chromium
```

### 4. Add your Groq API key

Copy the example file and never commit the real one:

```bash
cp keys.example.json keys.json
nano keys.json
```

Format — one or more keys for automatic rotation:

```json
["gsk_your_key_here", "gsk_second_key_optional"]
```

Get a free key at [console.groq.com](https://console.groq.com).

### 5. Launch the UI

```bash
python ui_server.py
```

Then open **http://localhost:5000** in your browser.

---

## ⚙️ Configuration

All options are available in the UI sidebar. Here's what each does:

| Option | Default | Description |
|---|---|---|
| Target URL | — | The seed URL to start crawling from |
| Crawl Goal | Contact info | Natural language description of what to find |
| Max Steps | 25 | Maximum pages to visit per run |
| Max Depth | 3 | Maximum link depth from seed URL |
| LLM Model | llama-3.3-70b | Groq model for navigation + extraction |
| Same Domain Only | ✅ | Restrict crawl to the seed domain |
| Headless Browser | ✅ | Run Playwright without a visible window |
| Save Report | ✅ | Write JSON + TXT report files after run |

---

## 🧠 How It Works

```
Seed URL
   │
   ▼
Browser (Playwright)
   ├─ Fetch page HTML
   ├─ Scroll to trigger lazy content
   └─ Click reveal buttons (show phone, contact seller…)
   │
   ▼
Extractor (Hybrid)
   ├─ Regex pipeline  ──┐
   └─ LLM (Groq)     ──┴──► Merge & dedup
   │
   ▼
Memory (SQLite)
   └─ Store emails, phones, socials, people, visited URLs
   │
   ▼
LLM Navigation Decision
   └─ CLICK <url> / EXTRACT / STOP
   │
   ▼
Repeat until max_steps or queue empty
   │
   ▼
Report (JSON + TXT)
```

### Extraction pipeline

Each page goes through two extraction paths **in parallel**:

1. **Regex pipeline** — strict patterns for emails, international phone numbers (MA, FR, DZ, TN, UK, US…), social links via domain matching, people via JSON-LD / hCard / CSS class patterns
2. **LLM extraction** — the cleaned visible text is sent to Groq with a structured JSON prompt; the model catches obfuscated formats like `user [at] domain [dot] com` that regex misses

Results are merged with LLM results taking priority, regex filling gaps, and deduplication by digit fingerprint (phones) or lowercase value (emails/people).

---

## 📁 Project Structure

```
AI_Web_Crawler/
├── agent.py          # Core autonomous crawl loop
├── browser.py        # Playwright wrapper + hidden data reveal
├── extractor.py      # Hybrid LLM + regex extraction engine
├── llm.py            # Groq API client with key rotation
├── memory.py         # SQLite storage + session management
├── reporter.py       # JSON + plaintext report generation
├── ui_server.py      # Flask web server + SSE live updates
├── utils.py          # Shared helpers (URL scoring, logging…)
├── main.py           # CLI entry point
├── requirements.txt
├── keys.example.json # Safe to commit — no real keys
├── keys.json         # ⛔ Never committed (gitignored)
├── templates/
│   └── index.html    # Main UI (red neon sci-fi theme)
├── static/
│   └── style.css     # All styling
└── reports/          # ⛔ Gitignored
    ├── sessions/     # Per-session SQLite databases
    ├── sessions.json # Session metadata index
    └── report_*.json # Generated reports
```

---

## 🖥️ CLI Usage

You can also run the agent directly from the terminal without the UI:

```bash
python main.py \
  --url "https://example.com" \
  --goal "Find all emails and phone numbers" \
  --steps 30 \
  --depth 3 \
  --model llama-3.3-70b-versatile
```

---

## 🔑 API Keys

- Keys are stored in `keys.json` (gitignored automatically on first save via the UI)
- Multiple keys rotate seamlessly on rate limits — add as many as you have
- After all keys are exhausted, the agent waits 60 seconds then resets
- Supported provider: **Groq** (free tier available, fast inference)

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `groq` | LLM API client (navigation + summarization) |
| `httpx` | Async HTTP for LLM extraction calls |
| `playwright` | Headless browser automation |
| `beautifulsoup4` | HTML parsing |
| `flask` | Web UI server + SSE streaming |
| `lxml` | Fast HTML parser backend |

---

## 🔒 Security Notes

- `keys.json` is gitignored and never committed
- Session databases in `reports/` are gitignored (may contain scraped PII)
- Generated reports (`report_*.json/txt`) are gitignored
- The `.gitignore` is written automatically by the UI when you save a key

---

## 📄 License

MIT — do whatever you want, just don't use it to scrape sites that prohibit it in their ToS.
