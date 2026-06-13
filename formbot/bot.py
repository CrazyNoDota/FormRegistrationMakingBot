import logging
import re

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

import formbot.agent as agent
import formbot.config as config
import formbot.memory as mem
import formbot.queue_worker as qw

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "*Бот регистрации на формы*\n\n"
    "Отправьте мне Google-форму, ссылку на регистрацию или просто сайт мероприятия — "
    "я найду форму регистрации и заполню её автоматически, используя ваш сохранённый профиль.\n\n"
    "*Команды:*\n"
    "/profile — показать сохранённые данные\n"
    "/forget `<поле>` — удалить поле из памяти\n"
    "/set `<поле> <значение>` — сохранить значение профиля\n"
    "/skip — пропустить текущий вопрос\n"
    "/cancel — отменить текущую сессию\n"
    "/help — показать это сообщение\n\n"
    "*Примеры полей:* `full_name`, `email`, `phone`, `company`, `job_title`\n"
    "Я также запоминаю повторно используемые ответы из полей форм."
)

PROFILE_KEY_ALIASES = {
    "name": "full_name",
    "full name": "full_name",
    "first name": "first_name",
    "last name": "last_name",
    "email": "email",
    "phone": "phone",
    "mobile": "phone",
    "age": "age",
    "gender": "gender",
    "company": "company",
    "job": "job_title",
    "job title": "job_title",
    "position": "job_title",
    "city": "city",
    "country": "country",
    # Russian aliases
    "имя": "first_name",
    "фамилия": "last_name",
    "полное имя": "full_name",
    "фио": "full_name",
    "почта": "email",
    "эл. почта": "email",
    "электронная почта": "email",
    "имейл": "email",
    "телефон": "phone",
    "моб": "phone",
    "мобильный": "phone",
    "возраст": "age",
    "пол": "gender",
    "компания": "company",
    "организация": "company",
    "работа": "job_title",
    "должность": "job_title",
    "город": "city",
    "страна": "country",
}
PROFILE_STATEMENT_RE = re.compile(
    r"^(?:remember\s+|запомни[,:]?\s+)?(?:my\s+|мо[йяёе]\s+|моя\s+)?"
    r"(full name|first name|last name|job title|name|email|phone|mobile|age|gender|company|job|position|city|country|"
    r"полное имя|фио|имя|фамилия|электронная почта|эл\. почта|почта|имейл|телефон|мобильный|моб|возраст|пол|"
    r"компания|организация|должность|работа|город|страна)"
    r"\s*(?:is|as|=|:|—|это)\s*(.+)$",
    re.I,
)
AGE_STATEMENT_RE = re.compile(
    r"^(?:i am|i'm|мне)\s+(\d{1,3})(?:\s*years?\s*old|\s*лет|\s*год[а]?)?\.?$",
    re.I,
)


def _is_allowed(update: Update) -> bool:
    allowed_user_id = config.get().allowed_user_id
    return allowed_user_id is None or update.effective_user.id == allowed_user_id


async def _reject(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Этот бот приватный.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Этот бот приватный.")


async def _clear_markup(query) -> None:
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest as e:
        logger.debug("Could not clear callback keyboard: %s", e)


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_profile(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    profile = await mem.get_all(update.effective_user.id)
    if not profile:
        await update.message.reply_text(
            "Ваш профиль пуст. Заполните любую форму, чтобы начать его наполнять."
        )
        return

    lines = [_format_profile_line(k, v) for k, v in sorted(profile.items())]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: `/forget <ключ_поля>`\n\nКоманда /profile покажет ключи ваших полей.",
            parse_mode="Markdown",
        )
        return

    key = ctx.args[0].lower().strip()
    deleted = await mem.delete_value(update.effective_user.id, key)
    if deleted:
        await update.message.reply_text(
            f"Удалено: `{escape_markdown(key)}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"Поле `{escape_markdown(key)}` не найдено. Команда /profile покажет ваши поля.",
            parse_mode="Markdown",
        )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Использование: `/set <поле> <значение>`\nПример: `/set age 23`",
            parse_mode="Markdown",
        )
        return

    key = PROFILE_KEY_ALIASES.get(ctx.args[0].lower().strip(), ctx.args[0].lower().strip())
    value = " ".join(ctx.args[1:]).strip()
    await mem.set_value(update.effective_user.id, key, value)
    await update.message.reply_text(
        f"Saved `{escape_markdown(key)}`: {escape_markdown(value)}",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    await mem.clear_session(update.effective_user.id)
    await update.message.reply_text("Сессия отменена.")


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    await agent.skip_current_field(
        update.effective_user.id,
        update.effective_chat.id,
        ctx.bot,
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    session = await mem.load_session(user_id)

    if session and session.get("status") == "collecting":
        await agent.handle_answer(user_id, chat_id, text, ctx.bot)
        return

    saved = await _try_save_profile_statement(user_id, text)
    if saved:
        key, value = saved
        await update.message.reply_text(
            f"Сохранено `{escape_markdown(key)}`: {escape_markdown(value)}",
            parse_mode="Markdown",
        )
        return

    if text.startswith("http://") or text.startswith("https://"):
        await mem.clear_session(user_id)
        job = agent.FormJob(
            user_id=user_id,
            chat_id=chat_id,
            form_url=text,
            bot=ctx.bot,
            action="analyze",
        )
        if qw.enqueue(job):
            await update.message.reply_text("Добавлено в очередь. Анализирую вашу форму.")
        else:
            await update.message.reply_text(
                "Очередь переполнена. Подождите немного и попробуйте снова."
            )
        return

    if session and session.get("status") == "confirming":
        await update.message.reply_text(
            "Используйте кнопки выше, чтобы подтвердить или отменить, либо /cancel для отмены."
        )
        return

    await update.message.reply_text(
        "Отправьте ссылку на форму, чтобы начать, или используйте /help для списка команд."
    )


async def _try_save_profile_statement(user_id: int, text: str) -> tuple[str, str] | None:
    age_match = AGE_STATEMENT_RE.match(text.strip())
    if age_match:
        value = age_match.group(1)
        await mem.set_value(user_id, "age", value)
        return "age", value

    match = PROFILE_STATEMENT_RE.match(text.strip())
    if not match:
        return None

    raw_key, value = match.group(1).lower(), match.group(2).strip()
    if not value:
        return None

    key = PROFILE_KEY_ALIASES[raw_key]
    await mem.set_value(user_id, key, value)
    return key, value


def _format_profile_line(key: str, value: str) -> str:
    if key.startswith(agent.FIELD_MEMORY_PREFIX):
        label = key.removeprefix(agent.FIELD_MEMORY_PREFIX).replace("_", " ")
        return (
            f"*{escape_markdown(label)}* "
            f"(`{escape_markdown(key)}`): {escape_markdown(value)}"
        )
    return f"*{escape_markdown(key)}*: {escape_markdown(value)}"


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reject(update)
        return

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data = query.data or ""

    if data.startswith("radio:"):
        accepted = await agent.handle_radio_choice(user_id, chat_id, data, ctx.bot)
        if accepted:
            await _clear_markup(query)
        return

    if data == "confirm_yes":
        await _clear_markup(query)
        job = agent.FormJob(
            user_id=user_id,
            chat_id=chat_id,
            form_url="",
            bot=ctx.bot,
            action="submit",
        )
        if qw.enqueue(job):
            await ctx.bot.send_message(chat_id, "Отправляю форму.")
        else:
            await ctx.bot.send_message(
                chat_id,
                "Очередь переполнена. Попробуйте снова через мгновение.",
            )

    elif data == "confirm_no":
        await _clear_markup(query)
        await mem.clear_session(user_id)
        await ctx.bot.send_message(chat_id, "Отменено. Сессия очищена.")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
