# Architecture

## Tech Stack
| Component | Technology | Notes |
|-----------|-----------|-------|
| Runtime | Python 3.12 | Via Playwright Docker base image |
| Telegram | python-telegram-bot v21 | Async, long-polling |
| Browser | Playwright async API | Headless Chromium |
| Memory | SQLite via aiosqlite | WAL mode, per user_id |
| LLM | NVIDIA NIM API (OpenAI-compatible) | Qwen3-coder-480b |
| Container | Docker + Docker Compose | Separate from grants_* project |

## LLM API
- Provider: NVIDIA NIM
- Base URL: https://integrate.api.nvidia.com/v1
- Model: qwen/qwen3-coder-480b-a35b-instruct
- API Key: <YOUR_NVIDIA_API_KEY>
- Used for: mapping form field labels to canonical profile keys only
- Client: openai Python package (OpenAI-compatible interface)

## Data Flow
```
User sends URL
    │
    ▼
bot.py:handle_text()
    │ creates FormJob(action="analyze")
    ▼
queue_worker.py:worker()  ← asyncio.Queue (max 1 browser at a time)
    │
    ▼
agent.py:analyze_and_run()
    ├─ browser.py: open page, get_form_fields() via accessibility tree
    ├─ llm.py: map_fields() → {label: canonical_key}
    ├─ memory.py: get_all() → check what's already saved
    │
    ├─ All in memory? → _do_submit() → screenshot → send photo
    │
    └─ Missing fields? → save_session() → ask_next_field()
            │
            ▼ (user replies to questions)
        bot.py:handle_text() → agent.py:handle_answer()
            │ saves to memory.py:set_value()
            │ asks next pending field
            ▼
        All collected → _show_confirm() → inline keyboard
            │
            ▼ (user clicks Submit)
        bot.py:handle_callback() → queue FormJob(action="submit")
            │
            ▼
        agent.py:submit_from_session() → _do_submit()
            │
            ▼
        browser.py:run_browser_session() → fill all fields → click Submit
            │
            ▼
        screenshot → bot.send_photo() → done
```

## Session State Machine
Sessions are stored in SQLite `sessions` table and survive bot restarts.

States:
- `collecting` — asking user for missing fields
- `confirming` — waiting for Yes/No on the summary screen

## SQLite Schema
```sql
-- Persistent user memory (key-value per user)
profile (user_id, field_key, value, updated_at)

-- Active session (one per user, replaced on new form)
sessions (user_id, form_url, state_json, updated_at)

-- Submission history
form_submissions (id, user_id, form_url, status, submitted_at)
```

## Canonical Field Keys
The LLM maps any form label to one of these:
```
full_name, first_name, last_name, email, phone,
street_address, city, state_province, postal_code, country,
company, job_title, dietary_restrictions, t_shirt_size,
emergency_contact_name, emergency_contact_phone,
date_of_birth, website_url, message, other
```

## Playwright Browser Config
- Base image: mcr.microsoft.com/playwright/python:v1.52.0-noble
- Launch flags: --no-sandbox, --disable-dev-shm-usage, --disable-gpu, --js-flags=--max-old-space-size=256
- Single browser instance per form job, closed after submission
- Docker mem_limit: 850 MB, memswap_limit: 1700 MB
