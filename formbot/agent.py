import asyncio
import hashlib
import io
import logging
import re
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.helpers import escape_markdown

import formbot.browser as browser
import formbot.crawler as crawler
import formbot.llm as llm
import formbot.memory as mem

logger = logging.getLogger(__name__)
MAX_BUTTON_TEXT_LEN = 64
MAX_MESSAGE_LEN = 3900
FIELD_MEMORY_PREFIX = "field:"
MAX_FIELD_KEY_LABEL_LEN = 80

_DO_NOT_REMEMBER_LABEL_RE = re.compile(
    r"\b("
    r"password|passcode|otp|one[- ]?time|verification|verify|captcha|security\s*code|"
    r"cvv|cvc|card\s*number|credit\s*card|debit\s*card|pin|ssn|social\s*security|"
    r"passport|national\s*id|iin|tax\s*id|signature|upload|file|resume|cv|"
    r"terms|conditions|privacy|consent|agree|agreement|subscribe"
    r")\b",
    re.I,
)
_DO_NOT_REMEMBER_ROLE_SET = {"checkbox"}


@dataclass
class FormJob:
    user_id: int
    chat_id: int
    form_url: str
    bot: Bot
    action: str = "analyze"  # "analyze" | "submit"


async def analyze_and_run(job: FormJob) -> None:
    await job.bot.send_message(job.chat_id, "Открываю страницу и ищу форму...")
    try:
        result = await crawler.find_form(job.form_url)
        if not result:
            await job.bot.send_message(
                job.chat_id,
                "Форма регистрации не найдена. Возможно, сайт требует входа, "
                "загружается динамически или форма находится в неподдерживаемом окне.",
            )
            return

        found_url, form_fields = result
        if found_url != job.form_url:
            await job.bot.send_message(
                job.chat_id,
                f"Найдена форма регистрации по адресу:\n{found_url}",
            )
            job.form_url = found_url

        await job.bot.send_message(
            job.chat_id,
            f"Найдено полей: {len(form_fields)}. Сопоставляю с вашим профилем...",
        )

        labels = [f["name"] for f in form_fields]
        mapping = await llm.map_fields(labels)
        key_map = {m["field_name"]: m["canonical_key"] for m in mapping}
        enriched = [
            {**f, "canonical_key": key_map.get(f["name"], "other")}
            for f in form_fields
        ]

        profile = await mem.get_all(job.user_id)
        answers: dict[str, str] = {}
        answers_by_name: dict[str, str] = {}
        pending_fields: list[dict] = []

        for ef in enriched:
            ckey = ef["canonical_key"]
            value = _profile_value_for_field(profile, ef)
            if value is not None:
                answers[ckey] = value
                answers_by_name[ef["name"]] = value
            else:
                pending_fields.append(ef)

        if not pending_fields:
            await job.bot.send_message(
                job.chat_id,
                "Все данные найдены в памяти. Отправляю...",
            )
            await _do_submit(
                job.bot,
                job.chat_id,
                job.user_id,
                job.form_url,
                enriched,
                answers,
                answers_by_name,
            )
        else:
            state = {
                "status": "collecting",
                "enriched_fields": enriched,
                "answers": answers,
                "answers_by_name": answers_by_name,
                "pending_fields": pending_fields,
            }
            await mem.save_session(job.user_id, job.form_url, state)
            await ask_next_field(job.bot, job.chat_id, job.user_id)

    except Exception as e:
        logger.exception("analyze_and_run failed")
        await job.bot.send_message(job.chat_id, f"Ошибка при анализе формы: {e}")


async def submit_from_session(job: FormJob) -> None:
    session = await mem.load_session(job.user_id)
    if not session:
        await job.bot.send_message(job.chat_id, "Активная сессия не найдена.")
        return

    await _do_submit(
        job.bot,
        job.chat_id,
        job.user_id,
        session["form_url"],
        session.get("enriched_fields", []),
        session.get("answers", {}),
        session.get("answers_by_name", {}),
    )


async def handle_answer(user_id: int, chat_id: int, text: str, bot: Bot) -> None:
    session = await mem.load_session(user_id)
    if not session or session.get("status") != "collecting":
        return

    pending = session.get("pending_fields", [])
    if not pending:
        return

    current = pending[0]
    resolved = _resolve_radio_answer(current, text)
    if resolved is None:
        await bot.send_message(chat_id, "Пожалуйста, выберите один из предложенных вариантов.")
        await ask_next_field(bot, chat_id, user_id)
        return

    await _record_current_answer(user_id, chat_id, session, resolved, bot)


async def handle_radio_choice(
    user_id: int,
    chat_id: int,
    callback_data: str,
    bot: Bot,
) -> bool:
    session = await mem.load_session(user_id)
    if not session or session.get("status") != "collecting":
        await bot.send_message(chat_id, "Сейчас нет поля, ожидающего выбора.")
        return False

    pending = session.get("pending_fields", [])
    if not pending:
        await _show_confirm(bot, chat_id, user_id, session)
        return False

    current = pending[0]
    parts = callback_data.split(":")
    if len(parts) != 3 or current.get("role") != "radiogroup":
        await bot.send_message(chat_id, "Этот выбор больше не активен.")
        return False

    token, raw_index = parts[1], parts[2]
    if token != _field_token(current):
        await bot.send_message(
            chat_id,
            "Этот выбор относится к более старому вопросу. Используйте актуальные варианты.",
        )
        return False

    opts = current.get("options", [])
    try:
        idx = int(raw_index)
    except ValueError:
        idx = -1

    if not (0 <= idx < len(opts)):
        await bot.send_message(chat_id, "Этот вариант больше недоступен.")
        return False

    await _record_current_answer(user_id, chat_id, session, opts[idx], bot)
    return True


def _resolve_radio_answer(current: dict, text: str) -> str | None:
    if current.get("role") != "radiogroup":
        return text

    opts = current.get("options", [])
    if not opts:
        return text

    value = text.strip()
    if value.isdigit():
        idx = int(value) - 1
        return opts[idx] if 0 <= idx < len(opts) else None

    lowered = value.casefold()
    for opt in opts:
        if opt.casefold() == lowered:
            return opt
    return None


async def _record_current_answer(
    user_id: int,
    chat_id: int,
    session: dict,
    text: str,
    bot: Bot,
) -> None:
    pending = session.get("pending_fields", [])
    if not pending:
        return

    current = pending[0]
    ckey = current["canonical_key"]

    if text and _should_remember_field(current):
        if ckey != "other":
            await mem.set_value(user_id, ckey, text)
        field_key = _field_memory_key(current["name"])
        if field_key != ckey:
            await mem.set_value(user_id, field_key, text)

    answers = session.get("answers", {})
    answers_by_name = session.get("answers_by_name", {})
    if text:
        answers_by_name[current["name"]] = text
        if ckey != "other":
            answers[ckey] = text

    session["answers"] = answers
    session["answers_by_name"] = answers_by_name
    session["pending_fields"] = pending[1:]

    await mem.save_session(user_id, session["form_url"], session)
    await bot.send_message(chat_id, "Принято.")
    await ask_next_field(bot, chat_id, user_id)


async def skip_current_field(user_id: int, chat_id: int, bot: Bot) -> None:
    session = await mem.load_session(user_id)
    if not session or session.get("status") != "collecting":
        await bot.send_message(chat_id, "Нет активной сессии.")
        return

    pending = session.get("pending_fields", [])
    if pending and pending[0].get("required"):
        await bot.send_message(chat_id, "Это поле обязательное, его нельзя пропустить.")
        await ask_next_field(bot, chat_id, user_id)
        return

    if pending:
        session["pending_fields"] = pending[1:]
        await mem.save_session(user_id, session["form_url"], session)

    await bot.send_message(chat_id, "Пропущено.")
    await ask_next_field(bot, chat_id, user_id)


async def ask_next_field(bot: Bot, chat_id: int, user_id: int) -> None:
    session = await mem.load_session(user_id)
    if not session:
        return

    pending = session.get("pending_fields", [])
    if not pending:
        await _show_confirm(bot, chat_id, user_id, session)
        return

    current = pending[0]
    label = current["name"]
    label_md = _md(label)
    required = current.get("required", False)
    role = current.get("role", "")
    opts = current.get("options", [])

    req_tag = " _(обязательно)_" if required else ""

    if role == "radiogroup" and opts:
        option_lines = "\n".join(
            f"  {i + 1}. {_md(o)}" for i, o in enumerate(opts)
        )
        msg = (
            f"*{label_md}*{req_tag}\n\n"
            f"Выберите один вариант:\n{option_lines}\n\n"
            f"{_skip_hint(required, 'Нажмите вариант ниже или отправьте точный текст либо номер.')}"
        )
        token = _field_token(current)
        rows = [
            [
                InlineKeyboardButton(
                    _button_text(option),
                    callback_data=f"radio:{token}:{idx}",
                )
            ]
            for idx, option in enumerate(opts)
        ]
        await _send_markdown(
            bot,
            chat_id,
            _trim_message(msg),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    parts = [f"Укажите *{label_md}*{req_tag}"]
    if opts:
        parts.append("Варианты: " + ", ".join(_md(o) for o in opts[:8]))
    parts.append(_skip_hint(required, "Отправьте ваш ответ."))
    await _send_markdown(bot, chat_id, _trim_message("\n".join(parts)))


def _field_token(field: dict) -> str:
    raw = f"{field.get('name', '')}\0{field.get('role', '')}\0{field.get('options', [])}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _profile_value_for_key(profile: dict[str, str], key: str) -> str | None:
    if key == "other":
        return None
    if key in profile:
        return profile[key]
    if key == "age":
        legacy_value = profile.get("date_of_birth")
        if legacy_value and legacy_value.strip().isdigit():
            return legacy_value
    return None


def _profile_value_for_field(profile: dict[str, str], field: dict) -> str | None:
    canonical_value = _profile_value_for_key(profile, field.get("canonical_key", "other"))
    if canonical_value is not None:
        return canonical_value

    if not _should_remember_field(field):
        return None

    return profile.get(_field_memory_key(field.get("name", "")))


def _field_memory_key(label: str) -> str:
    normalized = re.sub(r"\s+", " ", str(label).strip().casefold())
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE).strip("_")
    if not normalized:
        normalized = hashlib.sha1(str(label).encode("utf-8")).hexdigest()[:12]
    if len(normalized) > MAX_FIELD_KEY_LABEL_LEN:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:MAX_FIELD_KEY_LABEL_LEN - 9].rstrip('_')}_{digest}"
    return f"{FIELD_MEMORY_PREFIX}{normalized}"


def _should_remember_field(field: dict) -> bool:
    label = str(field.get("name", ""))
    if not label.strip():
        return False
    if field.get("role") in _DO_NOT_REMEMBER_ROLE_SET:
        return False
    if _DO_NOT_REMEMBER_LABEL_RE.search(label):
        return False
    return True


def _md(value: object) -> str:
    return escape_markdown(str(value))


def _button_text(value: object) -> str:
    text = str(value).strip()
    if len(text) <= MAX_BUTTON_TEXT_LEN:
        return text
    return text[: MAX_BUTTON_TEXT_LEN - 3].rstrip() + "..."


def _trim_message(value: str) -> str:
    if len(value) <= MAX_MESSAGE_LEN:
        return value
    return value[: MAX_MESSAGE_LEN - 4].rstrip() + "\n..."


def _skip_hint(required: bool, text: str) -> str:
    if required:
        return f"_{text} Это поле обязательное._"
    return f"_{text} /skip — чтобы пропустить._"


async def _send_markdown(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning("Markdown send failed, retrying as plain text: %s", e)
        await bot.send_message(
            chat_id,
            text.replace("\\", ""),
            reply_markup=reply_markup,
        )


async def _show_confirm(bot: Bot, chat_id: int, user_id: int, session: dict) -> None:
    enriched = session.get("enriched_fields", [])
    answers = session.get("answers", {})
    answers_by_name = session.get("answers_by_name", {})

    lines = []
    for ef in enriched:
        value = answers_by_name.get(ef["name"])
        if not value:
            value = answers.get(ef["canonical_key"])
        if value:
            lines.append(f"- *{_md(ef['name'])}*: {_md(value)}")

    if lines:
        summary = "Вот что я отправлю:\n\n" + "\n".join(lines)
    else:
        summary = "Ни одно поле не заполнено. Всё равно отправить?"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Отправить", callback_data="confirm_yes"),
            InlineKeyboardButton("Отмена", callback_data="confirm_no"),
        ]]
    )

    session["status"] = "confirming"
    await mem.save_session(user_id, session["form_url"], session)
    await _send_markdown(
        bot,
        chat_id,
        _trim_message(summary),
        reply_markup=kb,
    )


async def _do_submit(
    bot: Bot,
    chat_id: int,
    user_id: int,
    form_url: str,
    enriched: list[dict],
    answers: dict[str, str],
    answers_by_name: dict[str, str] | None = None,
) -> None:
    try:
        await bot.send_message(chat_id, "Заполняю и отправляю форму...")

        field_answers = dict(answers_by_name or {})
        for ef in enriched:
            ckey = ef["canonical_key"]
            if ef["name"] not in field_answers and ckey in answers:
                field_answers[ef["name"]] = answers[ckey]

        screenshot, success, diagnostic = await browser.run_browser_session(
            form_url, field_answers
        )

        if success:
            caption = "Форма успешно отправлена."
        elif diagnostic:
            caption = diagnostic
        else:
            caption = (
                "Форма заполнена, но подтвердить успешную отправку не удалось. "
                "Пожалуйста, проверьте скриншот."
            )
        await bot.send_photo(
            chat_id, photo=io.BytesIO(screenshot), caption=caption[:1024]
        )
        await mem.log_submission(user_id, form_url, "submitted" if success else "uncertain")

    except Exception as e:
        logger.exception("_do_submit failed")
        await bot.send_message(chat_id, f"Ошибка при отправке: {e}")
        await mem.log_submission(user_id, form_url, "failed")
    finally:
        await mem.clear_session(user_id)
