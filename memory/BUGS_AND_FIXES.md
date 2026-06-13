# Bugs Encountered and How They Were Fixed

---

## Bug 1: playwright install --with-deps fails on Ubuntu 24.04
**Symptom:** Docker build fails:
```
E: Package 'ttf-unifont' has no installation candidate
E: Package 'ttf-ubuntu-font-family' has no installation candidate
Failed to install browsers
```
**Root cause:** `ttf-unifont` and `ttf-ubuntu-font-family` packages were renamed/removed in Ubuntu 24.04 Noble. Playwright's `--with-deps` script tried to install them.

**Fix:** Changed base Docker image from `python:3.12-slim` to `mcr.microsoft.com/playwright/python:v1.52.0-noble`. This image is pre-built for Ubuntu 24.04 with Chromium and all deps included.

---

## Bug 2: `No module named 'telegram'` after switching base image
**Symptom:** Container crashes on startup with `ModuleNotFoundError: No module named 'telegram'`.

**Root cause:** During base image switch, the `RUN pip install` line was accidentally dropped from the Dockerfile via an imprecise Edit.

**Fix:** Added back `RUN pip install --no-cache-dir -r requirements.txt` to Dockerfile.

---

## Bug 3: `No module named 'playwright'` after pip install step restored
**Symptom:** Container crashes with `ModuleNotFoundError: No module named 'playwright'`.

**Root cause:** The Playwright base image installs `playwright` in the system Python, but the `pip install` step ran in an isolated context and didn't include playwright. The requirements.txt had `playwright` removed during the base image change.

**Fix:** Added `playwright==1.52.0` back to `requirements.txt` so our pip layer explicitly installs it into the same Python that runs the app.

---

## Bug 4: Radio buttons asked 3 separate times (one per option)
**Symptom:** For a radio group "Когда вы готовы принять участие в мероприятии" with 3 options (12.05.2026, 13.05.2026, 17.05.2026), the bot asked "What is your 12.05.2026?", "What is your 13.05.2026?", "What is your 17.05.2026?" — three separate questions.

**Root cause:** In `browser.py`'s `get_form_fields()`, each `radio` role node was treated as an independent field. The `walk()` function included `"radio"` in the list of fillable field roles, so each option became its own field.

**Fix:**
1. Removed `"radio"` from the individual field type list
2. Added radio group detection: when a `group`/`radiogroup`/`list` node has radio children, create ONE `radiogroup` field with `options` list
3. Updated `ask_next_field()` to show a numbered choice list for radiogroups
4. Added numeric shorthand resolution in `handle_answer()` ("3" → "17.05.2026")

---

## Bug 5: Radio button not selected during form submission
**Symptom:** All text fields filled correctly, but radio button shows "Это обязательный вопрос" validation error — none selected.

**Root cause:** `fill_field()` used `.check()` to select radio buttons. `.check()` only works on native `<input type="radio">` HTML elements. Google Forms uses `<div role="radio">` custom elements, for which `.check()` silently fails or throws.

**Fix:** Replaced `.check()` with a `_click_radio()` helper that tries 3 strategies:
1. `get_by_role("radio", name=value).click()` — works for standard radio inputs
2. `get_by_label(value).click()` — alternative accessibility path
3. JavaScript `querySelectorAll('[role="radio"]')` walk + `.click()` — guaranteed fallback for Google Forms div-based radios

---

## Bug 6: Field labels contain Google Forms builder text
**Symptom:** Bot asks "What is your Ваше имя? Сделать этот вопрос обязательным?" — the label includes Russian text meaning "Make this a required question".

**Root cause:** Google Forms injects its form builder UI elements into the accessibility tree. The label strings in the tree include "Сделать этот вопрос обязательным" which is the tooltip/button for making a field required in the form editor.

**Fix:** Added `_clean()` function in `browser.py` using a regex that strips this and similar Google Forms noise from all field names before processing.

---

## Lesson: Docker compose builds its own image name
When running `docker compose up`, Docker Compose builds and tags the image as `{project_name}-{service_name}:latest` (e.g. `formbot-formbot`), NOT the name from a manual `docker build -t formbot:latest .` command. They are separate cached images. Always use `docker compose build` or `docker compose down && docker build -t formbot-formbot:latest . && docker compose up -d` to be consistent.
