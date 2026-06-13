"""Registration-form discovery.

When the user submits a website URL that doesn't itself contain a form, this
module navigates the site to locate one. Two phases:

1. Heuristic BFS: follow links/buttons whose text matches multi-language
   registration keywords, up to MAX_HEURISTIC_DEPTH.
2. LLM navigator fallback: if heuristics fail, ask the LLM which clickable
   element to follow next, up to MAX_LLM_STEPS.

The crawler runs the whole exploration in a single browser session, then
returns the final URL plus the already-extracted form fields so the caller
doesn't need to re-open the page.
"""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import Page, async_playwright

import formbot.browser as browser
import formbot.llm as llm

logger = logging.getLogger(__name__)

REG_KEYWORDS = re.compile(
    r"(register|registration|sign[\s-]?up|signup|apply|application|"
    r"enroll|enrolment|enrollment|join|book\s*(now|ticket)|get\s*tickets?|"
    r"\bregister\b|"
    r"регистрац|зарегистр|записаться|подать\s*заявк|заявк|"
    r"тіркел|тіркеу|өтінім|өтініш|қатысу|жазыл)",
    re.IGNORECASE,
)

MAX_HEURISTIC_DEPTH = 4
MAX_CANDIDATES_PER_PAGE = 8
MAX_LLM_STEPS = 5
MIN_FIELDS_FOR_FORM = 2
NAV_TIMEOUT_MS = 25000
CLICK_TIMEOUT_MS = 5000


async def _has_real_form(page: Page) -> tuple[bool, list[dict]]:
    try:
        fields = await browser.get_form_fields(page)
    except Exception as e:
        logger.debug("get_form_fields failed: %s", e)
        return False, []
    # Skip pure login forms (email/username + password only).
    if len(fields) == 2:
        names = " ".join(f["name"].lower() for f in fields)
        if "password" in names:
            return False, fields
    return len(fields) >= MIN_FIELDS_FOR_FORM, fields


async def _collect_clickables(page: Page) -> list[dict]:
    """Return up to ~60 visible clickable elements with text + href."""
    try:
        return await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const selectors = 'a, button, [role="button"], [role="link"], input[type="submit"], input[type="button"]';
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const style = getComputedStyle(el);
                    return style.visibility !== 'hidden' && style.display !== 'none';
                };
                document.querySelectorAll(selectors).forEach(el => {
                    if (!visible(el)) return;
                    let text = (el.innerText || el.textContent || '').trim();
                    if (!text) text = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('value') || '';
                    text = text.replace(/\\s+/g, ' ').trim();
                    if (!text || text.length > 200) return;
                    const href = el.getAttribute('href') || '';
                    const key = text.toLowerCase() + '|' + href;
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({ text, href, tag: el.tagName.toLowerCase() });
                });
                return out.slice(0, 60);
            }"""
        )
    except Exception as e:
        logger.debug("_collect_clickables failed: %s", e)
        return []


def _filter_keyword_candidates(clickables: list[dict]) -> list[dict]:
    matched: list[dict] = []
    for item in clickables:
        text = item.get("text", "")
        href = item.get("href", "")
        if REG_KEYWORDS.search(text) or REG_KEYWORDS.search(href):
            matched.append(item)
    return matched[:MAX_CANDIDATES_PER_PAGE]


def _resolve_href(base_url: str, href: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    return urljoin(base_url, href)


async def _follow(page: Page, item: dict, base_url: str) -> bool:
    """Navigate the page based on a clickable item. Returns True on action attempted."""
    target = _resolve_href(base_url, item.get("href", ""))
    if target:
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(0.8)
            return True
        except Exception as e:
            logger.debug("goto %s failed: %s", target, e)
            return False
    # No usable href — try clicking by text.
    text = item.get("text", "").strip()
    if not text:
        return False
    try:
        locator = page.get_by_text(text, exact=False).first
        await locator.click(timeout=CLICK_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        return True
    except Exception as e:
        logger.debug("click '%s' failed: %s", text[:40], e)
        return False


async def _heuristic_crawl(
    page: Page,
    visited: set[str],
    depth: int = 0,
) -> Optional[tuple[str, list[dict]]]:
    has_form, fields = await _has_real_form(page)
    if has_form:
        return page.url, fields

    if depth >= MAX_HEURISTIC_DEPTH:
        return None

    clickables = await _collect_clickables(page)
    candidates = _filter_keyword_candidates(clickables)
    if not candidates:
        return None

    anchor_url = page.url
    for item in candidates:
        target = _resolve_href(anchor_url, item.get("href", ""))
        if target and target in visited:
            continue
        if target:
            visited.add(target)

        if not await _follow(page, item, anchor_url):
            continue

        result = await _heuristic_crawl(page, visited, depth + 1)
        if result:
            return result

        # Return to anchor page for next candidate.
        try:
            await page.goto(
                anchor_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            await asyncio.sleep(0.4)
        except Exception:
            return None

    return None


async def _llm_crawl(page: Page, visited: set[str]) -> Optional[tuple[str, list[dict]]]:
    for step in range(MAX_LLM_STEPS):
        has_form, fields = await _has_real_form(page)
        if has_form:
            return page.url, fields

        clickables = await _collect_clickables(page)
        if not clickables:
            return None

        labels = [c["text"] for c in clickables]
        idx = await llm.pick_registration_link(labels, page_url=page.url)
        if idx is None or not (0 <= idx < len(clickables)):
            return None

        chosen = clickables[idx]
        target = _resolve_href(page.url, chosen.get("href", ""))
        if target and target in visited:
            logger.debug("LLM picked already-visited %s; stopping", target)
            return None
        if target:
            visited.add(target)

        if not await _follow(page, chosen, page.url):
            return None

    return None


async def find_form(start_url: str) -> Optional[tuple[str, list[dict]]]:
    """Locate a registration form starting from ``start_url``.

    Returns (form_url, fields) or None. The form_url may equal start_url if
    the form was already on the initial page.
    """
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True, args=browser.LAUNCH_ARGS)
        try:
            ctx = await br.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            try:
                await page.goto(
                    start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.warning("Initial goto failed: %s", e)
                return None

            visited: set[str] = {page.url}
            result = await _heuristic_crawl(page, visited, depth=0)
            if result:
                logger.info("Crawler (heuristic): form found at %s", result[0])
                return result

            try:
                await page.goto(
                    start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                await asyncio.sleep(0.6)
            except Exception:
                return None

            result = await _llm_crawl(page, visited)
            if result:
                logger.info("Crawler (LLM): form found at %s", result[0])
            return result
        finally:
            await br.close()
