import datetime
import html
import json
import os
import re
import sqlite3
import threading

import telebot
from telebot import types
import google.generativeai as genai

CONFIG_PATH = "/app/config.json" if os.path.exists("/app/config.json") else "config.json"
DB_PATH = "/app/bot.db" if os.path.exists("/app") else "bot.db"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

SYSTEM_PROMPT = config.get("SYSTEM_PROMPT", "")
MODELS = config["MODELS"]  # {key: {provider, api_name, api_key, display_name, daily_limit}}
DEFAULT_MODEL = config.get("DEFAULT_MODEL", next(iter(MODELS)))
ALLOWED_USERS = config["ALLOWED_USERS"]  # {uid: {"name": ...}}
ADMIN_IDS = set(str(x) for x in config.get("ADMIN_IDS", []))

bot = telebot.TeleBot(config["TELEGRAM_BOT_TOKEN"])

# ---------------------------------------------------------------------------
# База данных (SQLite вместо ручной работы с json + save_config())
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                current_model TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                user_id TEXT,
                model_key TEXT,
                date TEXT,
                requests INTEGER DEFAULT 0,
                tokens INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, model_key, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS totals (
                user_id TEXT PRIMARY KEY,
                total_tokens INTEGER DEFAULT 0
            )
            """
        )


init_db()


def today_str():
    return datetime.date.today().isoformat()


def ensure_user(uid):
    name = ALLOWED_USERS[uid].get("name", uid)
    with _db_lock, get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, name, current_model) VALUES (?, ?, ?)",
                (uid, name, DEFAULT_MODEL),
            )
            conn.execute(
                "INSERT OR IGNORE INTO totals (user_id, total_tokens) VALUES (?, 0)", (uid,)
            )
        else:
            conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, uid))


def get_current_model(uid):
    with _db_lock, get_db() as conn:
        row = conn.execute(
            "SELECT current_model FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        if row and row["current_model"] in MODELS:
            return row["current_model"]
        return DEFAULT_MODEL


def set_current_model(uid, model_key):
    with _db_lock, get_db() as conn:
        conn.execute("UPDATE users SET current_model=? WHERE user_id=?", (model_key, uid))


def get_usage(uid, model_key):
    with _db_lock, get_db() as conn:
        row = conn.execute(
            "SELECT requests, tokens FROM usage WHERE user_id=? AND model_key=? AND date=?",
            (uid, model_key, today_str()),
        ).fetchone()
        return (row["requests"], row["tokens"]) if row else (0, 0)


def add_usage(uid, model_key, tokens):
    with _db_lock, get_db() as conn:
        conn.execute(
            """
            INSERT INTO usage (user_id, model_key, date, requests, tokens)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(user_id, model_key, date)
            DO UPDATE SET requests = requests + 1, tokens = tokens + excluded.tokens
            """,
            (uid, model_key, today_str(), tokens),
        )
        conn.execute(
            """
            INSERT INTO totals (user_id, total_tokens) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_tokens = total_tokens + ?
            """,
            (uid, tokens, tokens),
        )


def get_total_tokens(uid):
    with _db_lock, get_db() as conn:
        row = conn.execute("SELECT total_tokens FROM totals WHERE user_id=?", (uid,)).fetchone()
        return row["total_tokens"] if row else 0


# ---------------------------------------------------------------------------
# Модели (Gemini сейчас, легко добавить ещё провайдеров позже)
# ---------------------------------------------------------------------------

def get_gemini_model(model_key):
    cfg = MODELS[model_key]
    genai.configure(api_key=cfg["api_key"])
    return genai.GenerativeModel(cfg["api_name"], system_instruction=SYSTEM_PROMPT or None)


chat_sessions = {}  # (uid, model_key) -> chat session


def get_chat_session(uid, model_key):
    key = (uid, model_key)
    if key not in chat_sessions:
        cfg = MODELS[model_key]
        provider = cfg.get("provider", "gemini")
        if provider != "gemini":
            # Место для будущих провайдеров (openai-совместимые и т.д.)
            raise NotImplementedError(f"Провайдер '{provider}' пока не подключен")
        model = get_gemini_model(model_key)
        chat_sessions[key] = model.start_chat(history=[])
    return chat_sessions[key]


def clear_chat_session(uid, model_key):
    chat_sessions.pop((uid, model_key), None)


# ---------------------------------------------------------------------------
# Markdown (в стиле Gemini) -> Telegram HTML, чтобы шрифты/жирный/код работали
# ---------------------------------------------------------------------------

def gemini_to_telegram_html(text: str) -> str:
    text = html.escape(text, quote=False)
    # ```код``` -> <pre>
    text = re.sub(r"```(?:\w*\n)?(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", text, flags=re.DOTALL)
    # `код` -> <code>
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    # **жирный** -> <b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # *курсив* / _курсив_ -> <i>
    text = re.sub(r"(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)([^_\n]+?)(?<!_)_(?!_)", r"<i>\1</i>", text)
    return text


TELEGRAM_LIMIT = 4096


def send_long_message(chat_id, text, edit_message_id=None):
    chunks = [text[i:i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)] or [""]
    if edit_message_id:
        bot.edit_message_text(chunks[0], chat_id, edit_message_id, parse_mode="HTML")
        chunks = chunks[1:]
    for chunk in chunks:
        bot.send_message(chat_id, chunk, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------

def is_allowed(uid):
    return uid in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start", "stats"])
def send_stats(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    model_key = get_current_model(uid)
    requests_today, tokens_today = get_usage(uid, model_key)
    limit = MODELS[model_key]["daily_limit"]
    total_tokens = get_total_tokens(uid)

    text = (
        f"📊 <b>Статистика {html.escape(ALLOWED_USERS[uid].get('name', uid))}</b>\n"
        f"🧠 Модель: {html.escape(MODELS[model_key]['display_name'])}\n"
        f"⚡ Запросов сегодня: {requests_today}/{limit}\n"
        f"🪙 Токенов сегодня: {tokens_today}\n"
        f"🪙 Всего токенов: {total_tokens}\n\n"
        f"Сменить модель: /model"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=["clear"])
def clear_history(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    model_key = get_current_model(uid)
    clear_chat_session(uid, model_key)
    bot.reply_to(message, "🧹 История нашего диалога очищена! Начнем с чистого листа.")


@bot.message_handler(commands=["model"])
def choose_model(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    current = get_current_model(uid)
    kb = types.InlineKeyboardMarkup()
    for key, cfg in MODELS.items():
        mark = "✅ " if key == current else ""
        kb.add(
            types.InlineKeyboardButton(
                f"{mark}{cfg['display_name']} ({cfg['daily_limit']}/день)",
                callback_data=f"setmodel:{key}",
            )
        )
    bot.reply_to(message, "Выбери модель:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("setmodel:"))
def on_model_selected(call):
    uid = str(call.from_user.id)
    if not is_allowed(uid):
        return
    model_key = call.data.split(":", 1)[1]
    if model_key not in MODELS:
        bot.answer_callback_query(call.id, "Такой модели нет 🤔")
        return
    set_current_model(uid, model_key)
    bot.answer_callback_query(call.id, f"Модель переключена: {MODELS[model_key]['display_name']}")
    bot.edit_message_text(
        f"🧠 Теперь ты общаешься с: <b>{html.escape(MODELS[model_key]['display_name'])}</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_chat(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return

    ensure_user(uid)
    model_key = get_current_model(uid)
    requests_today, _ = get_usage(uid, model_key)
    limit = MODELS[model_key]["daily_limit"]

    if requests_today >= limit:
        bot.reply_to(
            message,
            "⚠️ Лимит запросов для этой модели на сегодня исчерпан..\n"
            "Можешь попробовать другую модель: /model",
        )
        return

    msg = bot.reply_to(message, "🤖 Думаю...")

    try:
        session = get_chat_session(uid, model_key)
        response = session.send_message(message.text)

        tokens_count = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            tokens_count = response.usage_metadata.total_token_count

        add_usage(uid, model_key, tokens_count)
        new_requests_today, _ = get_usage(uid, model_key)

        footer = (
            f"\n\n<i>[Запрос #{new_requests_today} | Токенов: {tokens_count} | "
            f"{html.escape(MODELS[model_key]['display_name'])}]</i>"
        )
        final_text = gemini_to_telegram_html(response.text) + footer

        send_long_message(message.chat.id, final_text, edit_message_id=msg.message_id)

    except Exception as e:
        error_str = str(e)
        print(f"!!! КРИТИЧЕСКАЯ ОШИБКА В ЛОГАХ: {error_str}")
        clear_chat_session(uid, model_key)
        bot.edit_message_text(
            f"❌ Ошибка: {html.escape(error_str[:200])}... Попробуй /clear и так же отправь мне код ошибки",
            message.chat.id,
            msg.message_id,
            parse_mode="HTML",
        )


if __name__ == "__main__":
    bot.infinity_polling()
