# Project Overview — NSART Event Registration Bot

## What It Is
A Telegram bot that automatically fills web forms (Google Forms, event registration pages) on behalf of users. Users send a form URL, the bot analyzes it, asks for any missing information, then fills and submits the form automatically.

## Telegram Bot
- Username: @NSARTEventRegistrationBot
- Token: <YOUR_TELEGRAM_BOT_TOKEN>
- Mode: Long-polling (no webhook, no port needed)
- Access: Open to all users (no allowlist in current version)

## Core User Flow
1. User sends a Google Form URL to the bot
2. Bot opens the form in headless Chromium, reads all fields via accessibility tree
3. Bot uses Qwen3 LLM to map field labels to canonical profile keys (email, name, company, etc.)
4. Bot checks SQLite memory for each field
5. If ALL fields are in memory → auto-submits immediately (no confirmation)
6. If ANY fields are missing → asks user one by one via Telegram
7. User answers are saved to memory for future forms
8. After all answers collected → shows summary → user confirms → submits
9. Bot sends a screenshot of the submitted form

## Bot Commands
- Send any URL → triggers form analysis and fill
- `/start` or `/help` → welcome message with command list
- `/profile` → shows all saved field values
- `/forget <field>` → deletes a field from memory (e.g. `/forget email`)
- `/skip` → skips the current question during Q&A
- `/cancel` → cancels current session

## Key Design Decisions
- Accessibility tree navigation (not CSS selectors) — survives Google Forms DOM changes
- Claude/LLM used only for field label → canonical key mapping (not control flow)
- Single asyncio.Queue worker → max 1 browser session at a time (RAM constraint)
- SQLite per user_id → each Telegram user has isolated memory
- Auto-submit when 100% memory hit; confirm when questions were asked
- Multi-page form support: bot clicks "Next" buttons during submission
