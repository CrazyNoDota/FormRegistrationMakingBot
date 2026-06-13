# Code Guide — What Each File Does

## Project Structure
```
FormRegistrationMakingBot/
├── formbot/
│   ├── __init__.py          empty
│   ├── config.py            env var loading (TELEGRAM_BOT_TOKEN, NVIDIA_API_KEY, etc.)
│   ├── memory.py            SQLite CRUD: profile, sessions, form_submissions
│   ├── llm.py               NVIDIA NIM API calls: map_fields(), pick_option()
│   ├── browser.py           Playwright: get_form_fields(), fill_field(), run_browser_session()
│   ├── crawler.py           Site exploration: find_form() (heuristic BFS + LLM nav fallback)
│   ├── agent.py             State machine: analyze_and_run(), handle_answer(), _do_submit()
│   ├── bot.py               Telegram handlers: commands, URL messages, inline callbacks
│   ├── queue_worker.py      asyncio.Queue with single worker
│   └── main.py              Entrypoint: init DB + LLM + bot + worker, graceful shutdown
├── schema.sql               CREATE TABLE statements (loaded at startup)
├── requirements.txt         playwright, python-telegram-bot, openai, aiosqlite
├── Dockerfile               FROM playwright/python:noble, pip install, COPY
├── docker-compose.yml       formbot service, formbot_data volume, mem_limit=850m
├── .env                     secrets (on VPS only, not in git)
├── .env.example             template showing required vars
├── .dockerignore
└── memory/                  this folder
```

## Key Functions

### browser.py
- `get_form_fields(page)` — walks the Playwright accessibility tree, returns list of fields
  - Handles `radiogroup`: groups all radio children into ONE field with options list
  - Cleans Google Forms label noise ("Сделать этот вопрос обязательным")
  - Deduplicates fields by name
- `_click_radio(page, value)` — 3-strategy radio click (role+click → label → JS)
  - Strategy 1: get_by_role("radio", name=value).click() — standard
  - Strategy 2: get_by_label(value).click() — alternative
  - Strategy 3: JS querySelectorAll('[role="radio"]') walk — Google Forms fallback
- `fill_field(page, field, value)` — fills one field by role
- `run_browser_session(url, answers_by_name)` — opens browser, fills form page by page, submits

### agent.py
- `analyze_and_run(job)` — opens browser, maps fields, checks memory, starts Q&A or auto-submits
- `handle_answer(user_id, chat_id, text, bot)` — saves answer to memory, advances Q&A
  - For radiogroups: resolves numeric answers ("3") to option text ("17.05.2026")
- `ask_next_field(bot, chat_id, user_id)` — sends next question or shows confirm screen
  - radiogroup fields: shows numbered choice list
  - text fields: shows free-text prompt with options if applicable
- `_do_submit(...)` — runs browser session, sends screenshot

### llm.py
- `map_fields(field_labels)` — sends labels to Qwen3, returns [{field_name, canonical_key}]
- `pick_option(...)` — picks dropdown option closest to user value
- `pick_registration_link(labels, page_url)` — returns the 0-based index of the
  clickable label most likely to lead to a registration form (used by crawler
  LLM-nav fallback). Returns None if no good match.
- Strips `<think>...</think>` tokens from Qwen3 chain-of-thought output
- Falls back to "other" for all fields if LLM call fails

### crawler.py
- `find_form(start_url)` → `(form_url, fields) | None`
- Two-phase exploration in a single browser session:
  1. **Heuristic BFS** up to depth 4: follows anchors/buttons whose visible text
     or href matches multi-language registration keywords
     (register/sign up/apply/регистрация/тіркелу/etc).
  2. **LLM navigator fallback** up to 5 steps: when heuristics fail, sends the
     list of all visible clickable labels to Qwen3 via `pick_registration_link`
     and follows its choice.
- A page is considered a form when `get_form_fields()` returns ≥2 fields.
- Pure login forms (email + password only) are skipped.
- Known limitation: modal forms that don't change the URL are returned but the
  bot will revisit the bare URL on submission — re-clicking the opener is not
  yet replayed.

### memory.py
- All functions are async (aiosqlite)
- `get_all(user_id)` — returns full profile dict
- `save_session / load_session / clear_session` — session persistence
- WAL mode enabled on startup

## Environment Variables (.env)
```
TELEGRAM_BOT_TOKEN=<YOUR_TELEGRAM_BOT_TOKEN>
NVIDIA_API_KEY=<YOUR_NVIDIA_API_KEY>
DATABASE_PATH=/data/memory.db
LOG_LEVEL=INFO
```
