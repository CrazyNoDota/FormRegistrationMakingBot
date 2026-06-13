import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--no-first-run",
    "--disable-infobars",
    "--js-flags=--max-old-space-size=256",
]

_SUBMIT_LABELS = [
    "Submit",
    "Register",
    "Sign Up",
    "Sign up",
    "Send",
    "Enviar",
    "Submit Form",
    "Отправить",
    "Зарегистрироваться",
    "Подать заявку",
]
_NEXT_LABELS = [
    "Next",
    "Continue",
    "Further",
    "Forward",
    "Proceed",
    "Next Page",
    "Next Section",
    "Далее",
    "Дальше",
    "Продолжить",
    "Әрі қарай",
    "Келесі",
]

_NEXT_VALUE_RE = re.compile(
    r"\b(next|continue|further|forward|proceed|"
    r"далее|дальше|продолжить|следующ|"
    r"әрі\s*қарай|жалғастыру|келес)\b",
    re.I,
)
_SUBMIT_VALUE_RE = re.compile(
    r"\b(submit|send|register|sign\s*up|enroll|apply|finish|complete|"
    r"отправить|зарегистрир|регистр|подать|готово|"
    r"тіркеу|жіберу|тіркел|аяқтау)\b",
    re.I,
)

# Google Forms can append builder/validation UI text to labels in the accessibility tree.
_LABEL_NOISE = re.compile(
    r"\s*(\u0421\u0434\u0435\u043b\u0430\u0442\u044c \u044d\u0442\u043e\u0442 "
    r"\u0432\u043e\u043f\u0440\u043e\u0441 \u043e\u0431\u044f\u0437\u0430"
    r"\u0442\u0435\u043b\u044c\u043d\u044b\u043c|Make this a required question"
    r"|Required question|\u041e\u0431\u044f\u0437\u0430\u0442\u0435\u043b"
    r"\u044c\u043d\u044b\u0439 \u0432\u043e\u043f\u0440\u043e\u0441).*$",
    re.IGNORECASE | re.DOTALL,
)


def _clean(name: str) -> str:
    name = _LABEL_NOISE.sub("", name or "")
    return name.strip(" *\t\n")


def _collect_radios(children: list[dict], recursive: bool) -> list[dict]:
    radios = [c for c in children if c.get("role") == "radio"]
    if radios or not recursive:
        return radios

    found: list[dict] = []
    for child in children:
        found.extend(_collect_radios(child.get("children", []), recursive=True))
    return found


async def get_form_fields(page: Page) -> list[dict]:
    snapshot = await page.accessibility.snapshot(interesting_only=False)
    fields: list[dict] = []
    seen_names: set[str] = set()

    def walk(node: dict, parent_label: str = "") -> None:
        if not node:
            return

        role = node.get("role", "")
        raw_name = node.get("name") or parent_label or ""
        name = _clean(raw_name)
        children = node.get("children", [])

        recursive_radio_search = role in {"group", "radiogroup"}
        radio_children = _collect_radios(children, recursive=recursive_radio_search)
        if radio_children and role in {"group", "radiogroup", "list"}:
            opts: list[str] = []
            for child in radio_children:
                opt = _clean(child.get("name", ""))
                if opt and opt not in opts:
                    opts.append(opt)

            if name and opts and name not in seen_names:
                seen_names.add(name)
                current = next(
                    (_clean(c.get("name", "")) for c in radio_children if c.get("checked")),
                    "",
                )
                fields.append(
                    {
                        "role": "radiogroup",
                        "name": name,
                        "required": node.get("required", False),
                        "value": current,
                        "checked": None,
                        "options": opts,
                    }
                )
            return

        if role in {"textbox", "combobox", "checkbox", "spinbutton", "listbox", "searchbox"}:
            if name and name not in seen_names:
                seen_names.add(name)
                opts = [
                    _clean(c.get("name", ""))
                    for c in children
                    if c.get("role") in {"option", "menuitem"} and c.get("name")
                ]
                fields.append(
                    {
                        "role": role,
                        "name": name,
                        "required": node.get("required", False),
                        "value": node.get("value", ""),
                        "checked": node.get("checked"),
                        "options": opts,
                    }
                )

        group_label = name if role in {"group", "radiogroup"} else parent_label
        for child in children:
            walk(child, group_label)

    walk(snapshot)
    return [f for f in fields if f["name"]]


def _best_option(value: str, options: list[str]) -> str:
    if not options:
        return value

    wanted = value.strip().casefold()
    for option in options:
        if option.casefold() == wanted:
            return option
    for option in options:
        option_folded = option.casefold()
        if wanted in option_folded or option_folded in wanted:
            return option
    return value


async def _click_radio(page: Page, group_name: str, value: str) -> bool:
    """Click a radio option, preferring the matching question/group scope."""
    for group_role in ("radiogroup", "group"):
        try:
            group = page.get_by_role(group_role, name=group_name, exact=False).first
            if await group.is_visible(timeout=1000):
                radio = group.get_by_role("radio", name=value, exact=False).first
                if await radio.is_visible(timeout=1000):
                    await radio.click(timeout=5000)
                    return True
        except Exception:
            pass

    try:
        clicked = await page.evaluate(
            """({ groupName, value }) => {
                const norm = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wantedGroup = norm(groupName);
                const wantedValue = norm(value);
                const candidates = Array.from(
                    document.querySelectorAll('[role="radio"], input[type="radio"]')
                );

                const labelFor = (el) => {
                    const aria = el.getAttribute('aria-label');
                    if (aria) return aria;
                    if (el.id) {
                        const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (label) return label.innerText || label.textContent || '';
                    }
                    const parentLabel = el.closest('label');
                    if (parentLabel) return parentLabel.innerText || parentLabel.textContent || '';
                    return el.innerText || el.textContent || '';
                };

                const labelMatches = (el) => {
                    const label = norm(labelFor(el));
                    return label === wantedValue || label.includes(wantedValue);
                };

                const inMatchingGroup = (el) => {
                    let current = el;
                    for (let depth = 0; current && depth < 8; depth += 1) {
                        const text = norm(current.innerText || current.textContent || '');
                        if (text.includes(wantedGroup) && text.includes(wantedValue)) {
                            return true;
                        }
                        current = current.parentElement;
                    }
                    return false;
                };

                const scoped = candidates.find((el) => labelMatches(el) && inMatchingGroup(el));
                const fallback = candidates.find(labelMatches);
                const target = scoped || fallback;
                if (!target) return false;

                target.scrollIntoView({ block: 'center', inline: 'center' });
                target.click();
                return true;
            }""",
            {"groupName": group_name, "value": value},
        )
        if clicked:
            return True
    except Exception:
        pass

    try:
        loc = page.get_by_role("radio", name=value, exact=False).first
        if await loc.is_visible(timeout=1500):
            await loc.click(timeout=5000)
            return True
    except Exception:
        pass

    try:
        loc = page.get_by_label(value, exact=False).first
        if await loc.is_visible(timeout=1500):
            await loc.click(timeout=5000)
            return True
    except Exception:
        pass

    logger.debug("Unable to click radio '%s' in group '%s'", value, group_name)
    return False


async def fill_field(page: Page, field: dict, value: str) -> bool:
    role = field["role"]
    name = field["name"]
    try:
        match role:
            case "textbox" | "searchbox" | "spinbutton":
                loc = page.get_by_role(role, name=name, exact=False).first
                await loc.fill(value, timeout=5000)

            case "combobox":
                loc = page.get_by_role("combobox", name=name, exact=False).first
                opts = field.get("options", [])
                best = _best_option(value, opts)
                if opts:
                    await loc.select_option(label=best, timeout=5000)
                else:
                    await loc.fill(best, timeout=5000)

            case "checkbox":
                loc = page.get_by_role("checkbox", name=name, exact=False).first
                if value.lower() in {"yes", "true", "1", "check"}:
                    await loc.check(timeout=5000)
                else:
                    await loc.uncheck(timeout=5000)

            case "radio" | "radiogroup":
                option = _best_option(value, field.get("options", []))
                if not await _click_radio(page, name, option):
                    raise RuntimeError(f"Could not select radio option '{option}'")

        await asyncio.sleep(0.12)
        return True
    except Exception as e:
        logger.debug("fill_field failed for '%s': %s", name, e)
        return False


async def _is_clickable(loc) -> bool:
    try:
        if not await loc.is_visible(timeout=800):
            return False
        if not await loc.is_enabled(timeout=400):
            return False
        return True
    except Exception:
        return False


async def _button_text(loc) -> str:
    try:
        val = await loc.get_attribute("value")
        if val:
            return val.strip()
    except Exception:
        pass
    try:
        text = await loc.inner_text(timeout=400)
        return (text or "").strip()
    except Exception:
        return ""


async def _find_button(page: Page, kind: str) -> Optional[object]:
    """Return a visible+enabled button matching ``kind`` ('submit' or 'next')."""
    labels = _SUBMIT_LABELS if kind == "submit" else _NEXT_LABELS
    for label in labels:
        try:
            loc = page.get_by_role("button", name=label, exact=False).first
            if await _is_clickable(loc):
                return loc
        except Exception:
            continue

    try:
        elements = page.locator("input[type=submit], button[type=submit]")
        count = await elements.count()
        for i in range(count):
            cand = elements.nth(i)
            if not await _is_clickable(cand):
                continue
            text = await _button_text(cand)
            is_next = bool(_NEXT_VALUE_RE.search(text))
            is_submit = bool(_SUBMIT_VALUE_RE.search(text))
            if kind == "next" and is_next:
                return cand
            if kind == "submit" and is_submit and not is_next:
                return cand
            if kind == "submit" and not text and not is_next:
                return cand
    except Exception:
        pass
    return None


async def _disabled_action_button_label(page: Page) -> Optional[str]:
    """If a submit-like button exists but is disabled, return its label for diagnostics."""
    try:
        elements = page.locator("input[type=submit], button[type=submit]")
        count = await elements.count()
        for i in range(count):
            cand = elements.nth(i)
            try:
                if not await cand.is_visible(timeout=300):
                    continue
                if await cand.is_enabled(timeout=300):
                    continue
                text = await _button_text(cand)
                return text or "(unlabeled)"
            except Exception:
                continue
    except Exception:
        pass
    return None


async def take_screenshot(page: Page) -> bytes:
    return await page.screenshot(full_page=True, type="png")


async def detect_success(page: Page, original_url: str) -> bool:
    if page.url != original_url:
        return True
    content = (await page.content()).lower()
    return any(
        w in content
        for w in [
            "thank you",
            "thanks",
            "submitted",
            "received",
            "registered",
            "success",
            "confirmation",
            "response has been recorded",
            "your response",
            "form submitted",
        ]
    )


async def run_browser_session(
    form_url: str,
    answers_by_name: dict[str, str],
) -> tuple[bytes, bool, str]:
    """
    answers_by_name: {field_label: value}
    Returns (screenshot_bytes, success, diagnostic_message)
    diagnostic_message is empty unless something blocked submission.
    """
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
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
            await page.goto(form_url, wait_until="networkidle", timeout=30000)
            original_url = page.url

            for page_num in range(10):
                await asyncio.sleep(1.0)

                visible_fields = await get_form_fields(page)
                failed_required: list[str] = []
                for vf in visible_fields:
                    value = answers_by_name.get(vf["name"])
                    if value:
                        filled = await fill_field(page, vf, value)
                        if not filled:
                            logger.warning(
                                "Failed to fill field '%s' with role '%s'",
                                vf.get("name"),
                                vf.get("role"),
                            )
                            if vf.get("required"):
                                failed_required.append(vf["name"])

                if failed_required:
                    logger.warning(
                        "Stopping before submit because required fields failed: %s",
                        failed_required,
                    )
                    screenshot = await take_screenshot(page)
                    return screenshot, False, (
                        "Не удалось заполнить обязательное(ые) поле(я): "
                        + ", ".join(failed_required)
                    )

                next_btn = await _find_button(page, "next")
                if next_btn:
                    await next_btn.click(timeout=5000)
                    logger.debug("Clicked Next (page %d)", page_num + 1)
                    continue

                submit_btn = await _find_button(page, "submit")
                if submit_btn:
                    await submit_btn.click(timeout=5000)
                    await asyncio.sleep(3)
                    success = await detect_success(page, original_url)
                    screenshot_post = await take_screenshot(page)
                    return screenshot_post, success, ""

                disabled_label = await _disabled_action_button_label(page)
                screenshot = await take_screenshot(page)
                if disabled_label:
                    logger.warning(
                        "Action button '%s' is disabled — required fields likely missing",
                        disabled_label,
                    )
                    return screenshot, False, (
                        f"Кнопка отправки «{disabled_label}» неактивна — "
                        "в форме всё ещё не заполнены обязательные поля (часто это "
                        "галочка согласия или подтверждения возраста). Используйте /cancel "
                        "и попробуйте снова, отвечая «да» на все обязательные галочки."
                    )
                return screenshot, False, "Кнопка отправки или «Далее» не найдена."

            screenshot = await take_screenshot(page)
            return screenshot, False, "Форма не завершилась в пределах 10 страниц."

        finally:
            await br.close()
