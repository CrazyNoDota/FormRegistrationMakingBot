# Event Registration Bot — Refinement & Redeployment Plan

> **Status:** Planning complete, ready to execute.
> **Created:** 2026-06-09
> **Base codebase:** `C:\Users\Jalil\myApps\FormRegistrationMakingBot` (refine in place)
> **Target VPS:** `31.210.174.74` (shared with the grants stack)
> **Execution model:** Claude subagent builds each phase → Codex CLI reviews & fixes → orchestrator scores ≥7/10 to advance.

This document is **self-contained** so it can be executed from a fresh chat with no prior context.

---

## 1. Context

The user has a Telegram bot that auto-registers users for events from a link and reminds them before the event. There are **two existing codebases** doing variants of this:

| | `FormRegistrationMakingBot` ← **OUR BASE** | `AnsarAIAgentSearchGrants\event-bot\` (reference only) |
|---|---|---|
| Telegram lib | python-telegram-bot v21 (long-polling) | aiogram 3 |
| Field detection | **Accessibility tree** (handles Google Forms `div[role=radio]`) | DOM/JS — **misses Google Forms** |
| Form discovery | ✅ heuristic BFS + LLM-nav crawler | ❌ opens URL directly only |
| Multi-page "Next" | ✅ up to 10 pages | ❌ single page |
| Profile onboarding | ❌ learns fields ad-hoc | ✅ guided FSM (reference for UX) |
| Captcha | ❌ none | ⚠️ detect-only |
| Reminders | ❌ none | ❌ none |

**Decision:** Refine `FormRegistrationMakingBot` **in place**. It has the stronger engine (accessibility-tree detection is correct for the Russian/Kazakh Google-Forms-heavy event scene, proven by its bug log). Borrow the **profile-onboarding UX** from `event-bot` (port it, don't switch frameworks).

### Reference code (read these before building)
- Engine: `formbot/browser.py` (accessibility-tree detection, radio handling, multi-page submit)
- Discovery: `formbot/crawler.py` (heuristic BFS + LLM-nav)
- State machine: `formbot/agent.py` (analyze → Q&A → confirm → submit)
- Telegram layer: `formbot/bot.py`, `formbot/main.py`, `formbot/queue_worker.py`
- Memory: `formbot/memory.py` (SQLite: `profile`, `sessions`, `form_submissions`)
- LLM: `formbot/llm.py` (NVIDIA NIM, Qwen3-coder-480b — field-label mapping)
- Profile UX to copy: `AnsarAIAgentSearchGrants\event-bot\bot\handlers\profile.py`, `bot\states.py`
- Stealth reference: `AnsarAIAgentSearchGrants\backend\scraping\stealth_browser.py` (camoufox pattern)

---

## 2. Core architectural principle

**LLM is used ONLY for the two genuinely hard sub-tasks; everything else is deterministic code.**

| Concern | Implementation |
|---|---|
| Telegram commands, FSM, callbacks | Deterministic (python-telegram-bot) |
| Profile storage & editing | Deterministic (SQLite) |
| Crawling/orchestration, queue, single-browser lock | Deterministic |
| Reminders, scheduling, date math | Deterministic (scheduler + SQLite) |
| Screenshots, success detection | Deterministic |
| **Finding the registration form on a non-obvious site** | **LLM / Webwright** |
| **Reading a non-standard form and entering the right data** | **LLM / Webwright** |
| Captcha | **No paid solver.** Stealth avoidance + notify-user fallback |

### Engine flow (deterministic shell, LLM core)
```
Profile onboarding (deterministic FSM)  → collect once: first/last name, email, phone, company?, title?
        │  user sends a link
        ▼
[deterministic] open page in stealth browser
        │
        ├─ Fast-path: accessibility-tree fill+submit (cheap, covers Google Forms + standard forms)
        │       └─ if it cleanly succeeds → done (NO LLM cost)
        │
        └─ Hard-path (LLM): Webwright agent finds the form / fills nonstandard fields
                            when the fast-path can't detect or complete the form
        │
        ▼
[deterministic] captcha? → screenshot + "solve it yourself, then /continue"
        │
        ▼
[deterministic] screenshot confirmation → send to user
        │
        ▼
[deterministic] extract event date (page parse first; LLM only if parse fails) → schedule reminders
```

> **Why a fast-path before Webwright:** Webwright runs an LLM agent loop **per form** (tokens + 10–40s latency). The deterministic accessibility-tree path already handles the easy ~80% (Google Forms, standard HTML forms) for free. Webwright is the escalation for the hard ~20%. This is "LLM only for actual work," applied literally.

---

## 3. Tooling decisions

| Tool | Decision | Notes |
|---|---|---|
| **Webwright** (`github.com/microsoft/Webwright`) | **Adopt** as the LLM form-finding/filling engine (hard-path only) | LLM-writes-Playwright agent. Integrates with Codex/Claude. Drives Chromium. |
| **camoufox** (anti-detect Firefox) | **Conditional** — see RAM contingency below | 0.5–1.5 GB/session. Use only if VPS has RAM headroom. |
| **playwright-stealth** (stealthed Chromium) | **Fallback for low-RAM** | One browser engine shared with Webwright; much lighter than camoufox. |
| **solvecaptcha-python** (paid) | **DROPPED** | User wants free only. No reliable free captcha *solver* exists → use avoidance + manual fallback. |
| NVIDIA NIM / Qwen3 | **Keep** | Already wired in `llm.py`. Reuse for field mapping + date extraction + as a Webwright backend if desired. |
| Codex CLI (v0.137.0, installed) | **Keep** | Per-phase reviewer in the build loop. |

### ⚠️ RAM contingency (resolve in Phase 0)
Grants stack already used **~1.3 GB on a 2 GB box** (≈679 MB free, on swap). Adding camoufox (0.5–1.5 GB) + Webwright Chromium would OOM a 2 GB box.
- **If new VPS ≥ 4–8 GB:** camoufox + Webwright as planned.
- **If new VPS ≈ 2 GB:** **drop camoufox**, run Webwright on **stealthed Chromium** (`playwright-stealth`) — one engine, free, light. Keep hard `mem_limit` in compose.
- camoufox is Firefox; Webwright drives Chromium — **do not run both engines** unless RAM is abundant. Default to one Chromium-based stealth path.

---

## 4. Security (do this in Phase 0, before anything else)

- **Live secrets are committed in the repo:** `memory/PROJECT_OVERVIEW.md`, `memory/ARCHITECTURE.md`, `memory/DEPLOYMENT.md`, `memory/CODE_GUIDE.md` contain the **real Telegram bot token and NVIDIA API key**. Move them to `.env` only; scrub from the markdown; **rotate both**.
- **VPS root password** was shared in chat in plaintext → **rotate it**, switch to SSH keys, never commit it. Credentials live in the operator's local secret store / `.env`, never in git.
- Add a `.gitignore` entry for `.env` (verify it's already ignored).

---

## 5. VPS / deployment facts

- Target: `31.210.174.74`, **shared with the grants `grants_*` containers**.
- Old grants box (`2.134.15.37`) ran: `grants_backend`, `grants_bot`, `grants_db` (pgvector), `grants_redis`, `grants_nginx`, `grants_n8n` on ~2 GB RAM + 512 MB swap.
- **Open question for Phase 0:** confirm whether the new box has more RAM, and whether grants is already running there or being migrated. Capture `free -h`, `nproc`, `docker ps`, free disk before committing the stealth strategy.
- Deploy as an **independent docker-compose project** (own image/container/volume names, e.g. `eventbot_*`) so it never collides with `grants_*`. No exposed ports (long-polling). `restart: unless-stopped`. Hard `mem_limit`.

---

## 6. Phased build plan

Each phase has: **Goal · Deliverables · Acceptance criteria · Files**. A phase is "done" only when the orchestrator scores it **≥7/10** (see §7).

### Phase 0 — Foundation, specs & secret hygiene
- **Goal:** Safe starting point + the RAM decision that gates the engine choice.
- **Deliverables:**
  - SSH to `31.210.174.74`; record `free -h`, `nproc`, `docker ps`, `df -h` into `memory/VPS_SERVER.md`.
  - Decide camoufox vs stealthed-Chromium per §3 contingency; record the decision.
  - Move all secrets to `.env`; scrub markdown; rotate Telegram token + NVIDIA key; verify `.gitignore`.
  - Confirm Codex CLI works: `codex --version`.
- **Acceptance:** No secrets in tracked files; VPS specs recorded; engine decision written down; bot still starts locally.

### Phase 1 — Profile onboarding (deterministic FSM)
- **Goal:** Match the spec's "fill profile once" flow.
- **Deliverables:**
  - `/start` → guided collection: first name, last name, email, phone, company (optional), job title (optional).
  - `/profile` → view + edit any field (inline keyboard).
  - Persist to existing `profile` table (`formbot/memory.py`). Reuse canonical keys (`first_name`, `last_name`, `email`, `phone`, `company`, `job_title`).
  - Port UX patterns from `event-bot\bot\handlers\profile.py` + `states.py` into python-telegram-bot conversation handlers.
- **Acceptance:** New user completes profile in one pass; values reused on next form without re-asking; `/profile` edits persist.
- **Files:** `formbot/bot.py`, `formbot/agent.py`, `formbot/memory.py`.

### Phase 2 — Webwright as the hard-path engine
- **Goal:** LLM-driven form finding/filling for forms the deterministic path can't handle.
- **Deliverables:**
  - Spike Webwright in an isolated module; confirm install, Python compat, model backend (Qwen3/NVIDIA or Anthropic), Chromium under Playwright.
  - Wrap Webwright behind a function with the same contract as the deterministic filler (input: url + profile + ask-callback; output: screenshot + success + filled/skipped).
  - In `agent.py`: try deterministic fast-path first; on detection failure / required-field failure / nonstandard form → escalate to Webwright.
  - Enforce guardrails: per-run timeout, step cap, single-browser lock (reuse `queue_worker.py`), token budget.
- **Acceptance:** A form that the accessibility-tree path fails on is completed by Webwright with a confirmation screenshot; the easy Google-Form path still completes WITHOUT invoking Webwright.
- **Files:** new `formbot/webwright_engine.py`, `formbot/agent.py`, `requirements.txt`.

### Phase 3 — Free captcha handling
- **Goal:** Reduce captcha frequency; degrade gracefully when one appears.
- **Deliverables:**
  - Stealth browser context per Phase-0 decision (camoufox **or** playwright-stealth).
  - Detect reCAPTCHA/hCaptcha (reuse event-bot's content check) → screenshot + clear message: "Solve it yourself, then /continue."
  - `/continue` command resumes the paused session.
- **Acceptance:** Captcha page yields a helpful message + screenshot, not a crash; `/continue` resumes; no paid service used.
- **Files:** `formbot/browser.py`, `formbot/webwright_engine.py`, `formbot/bot.py`, `formbot/agent.py`.

### Phase 4 — Event date extraction
- **Goal:** Know when the event is, for reminders.
- **Deliverables:**
  - Deterministic first pass: scan page text for date patterns / keywords (date, дата, when, когда, уақыты).
  - LLM fallback (Qwen3) only if the parse fails.
  - If still unknown → ask the user ("Когда мероприятие?") and store it.
- **Acceptance:** Date captured for common event pages without LLM; LLM/user fallback works when parsing fails; stored against the submission.
- **Files:** new `formbot/dates.py`, `formbot/agent.py`, `formbot/llm.py`, `formbot/memory.py` (+`event_date` storage).

### Phase 5 — Reminders (deterministic scheduler)
- **Goal:** Deliver the spec's MVP reminders.
- **Deliverables:**
  - After successful registration with a known date → offer reminders (inline yes/no).
  - Schedule: **1 day before**, **3 hours before**, **morning of**. Persist schedule in SQLite so it survives restarts.
  - Scheduler: APScheduler (AsyncIOScheduler) **or** an asyncio loop that wakes from a persisted `reminders` table. Timezone-aware.
  - `/reminders` → list / toggle / cancel.
- **Acceptance:** Reminders fire at the right offsets (verify with a near-future test date); survive a bot restart; `/reminders` manages them.
- **Files:** new `formbot/reminders.py`, `schema.sql` (+`reminders` table), `formbot/bot.py`, `formbot/main.py`.

### Phase 6 — Deploy to shared VPS
- **Goal:** Run alongside grants without collisions.
- **Deliverables:**
  - `docker-compose.yml` as an independent project: `eventbot_*` names, own volume, hard `mem_limit` (sized per Phase 0), `restart: unless-stopped`, no exposed ports.
  - Update `Dockerfile` for new deps (Webwright, stealth). Document deploy/update steps in `memory/DEPLOYMENT.md`.
  - End-to-end smoke test on the VPS: profile → link → fill → screenshot → date → reminder.
- **Acceptance:** Container healthy alongside `grants_*`; RAM within limit (no OOM); full happy-path works over real Telegram.
- **Files:** `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `memory/DEPLOYMENT.md`.

---

## 7. Subagent → Codex review loop (the execution protocol)

Run this loop **for every phase**. The orchestrator (the chat you run this from) drives it.

```
FOR each phase P:
  1. BUILD — spawn a Claude subagent (Agent tool) with:
       • the full text of Phase P from this file
       • the "Reference code" list (§1) and the architectural principle (§2)
       • instruction to implement + run/extend tests + summarize the diff
  2. REVIEW — run Codex on the resulting diff:
       codex exec "Review the changes for Phase P of PLAN.md.
                   Check correctness, error handling, security, and adherence
                   to §2 (LLM only for form find/fill; everything else deterministic).
                   Apply fixes directly. List remaining risks."
  3. SCORE — orchestrator scores the phase 1–10 against the rubric below.
       • score ≥ 7  → mark phase DONE, append result to memory/PROGRESS.md, go to next phase
       • score < 7  → feed Codex's remaining-risks + the failing rubric items back to a
                      Claude subagent for another iteration; repeat from step 2
  4. RECORD — update memory/PROGRESS.md with score, what changed, and any follow-ups.
```

### Scoring rubric (≥7/10 to advance)
1. **Correctness** — does it do what the phase's Acceptance criteria require? (heaviest weight)
2. **Determinism boundary** — LLM used *only* for form find/fill; rest is plain code.
3. **Error handling** — failures degrade gracefully (screenshot + message, no crash).
4. **Resource safety** — single-browser lock honored; timeouts/step caps; within `mem_limit`.
5. **Security** — no secrets in code; inputs handled safely.
6. **Fits the existing codebase** — matches FormReg's style/structure; no needless rewrites.
7. **Tested** — acceptance path verified (manually or automated).

A phase scoring <7 names the exact failing items so the next iteration is targeted.

### Notes for the orchestrator
- Codex CLI is at `C:\Users\Jalil\AppData\Roaming\npm\codex` (v0.137.0). Confirm the exact non-interactive invocation (`codex exec "..."`) in Phase 0; adjust flags if needed.
- Keep one browser session at a time (existing `queue_worker.py` asyncio.Queue) — critical on the shared VPS.
- Update `memory/PROGRESS.md` after each phase so a fresh chat can resume mid-build.

---

## 8. Open questions (carry into Phase 0)
1. New VPS RAM/CPU/disk — decides camoufox vs stealthed-Chromium.
2. Is grants already on `31.210.174.74`, or being migrated there?
3. Webwright model backend preference — reuse NVIDIA/Qwen3, or use Anthropic/OpenRouter?
4. Reminder timezone — per-user, or a fixed default (e.g. Asia/Almaty)?

---

## 9. Quick-start for the executing chat
1. Read this file end to end.
2. Read the §1 "Reference code" files.
3. Start **Phase 0** (secrets + VPS specs + engine decision) — do NOT skip; it gates Phase 2/3.
4. Run the §7 loop per phase; gate on ≥7/10; log to `memory/PROGRESS.md`.
