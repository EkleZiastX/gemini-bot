import html
import json
import os
import sqlite3
import tempfile
import threading
import time
from fpdf import FPDF
import google.generativeai as genai
import telebot
import yt_dlp

# ---------------------------------------------------------------------------
# Загрузка конфигурации
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config["TELEGRAM_BOT_TOKEN"]
SYSTEM_PROMPT = config.get("SYSTEM_PROMPT", "")
DEFAULT_MODEL = config.get("DEFAULT_MODEL", "flash")
MODELS = config.get("MODELS", {})
ADMIN_IDS = [str(i) for i in config.get("ADMIN_IDS", [])]
ALLOWED_USERS = {str(k): v for k, v in config.get("ALLOWED_USERS", {}).items()}

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Настройка подключения к локальному Telegram Bot API серверу (до 2 ГБ)
local_api_url = os.getenv("TELEGRAM_API_URL")
if local_api_url:
    telebot.apihelper.API_URL = f"{local_api_url}/bot{{0}}/{{1}}"
    telebot.apihelper.FILE_URL = f"{local_api_url}/file/bot{{0}}/{{1}}"

# ---------------------------------------------------------------------------
# Работа с БД (SQLite)
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "bot.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                selected_model TEXT
            )
        """
        )
        conn.commit()


init_db()


def is_allowed(user_id: str) -> bool:
    return user_id in ALLOWED_USERS or user_id in ADMIN_IDS


def ensure_user(user_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_model FROM users WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO users (user_id, selected_model) VALUES (?, ?)",
                (user_id, DEFAULT_MODEL),
            )
            conn.commit()


def get_current_model(user_id: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_model FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0] in MODELS:
            return row[0]
        return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Генерация PDF с поддержкой кириллицы
# ---------------------------------------------------------------------------
def create_pdf_from_text(text: str, output_path: str):
    pdf = FPDF()
    pdf.add_page()

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(font_path):
        pdf.add_font("DejaVu", "", font_path)
        pdf.set_font("DejaVu", size=10)
    else:
        pdf.set_font("Helvetica", size=10)
        text = text.encode("latin-1", "replace").decode("latin-1")

    pdf.multi_cell(0, 6, text)
    pdf.output(output_path)


# ---------------------------------------------------------------------------
# Обработка тяжелых медиафайлов и ссылок
# ---------------------------------------------------------------------------
def process_media_worker(chat_id, source_type, payload, file_name, model_key):
    status_msg = bot.send_message(chat_id, "Начинаю обработку медиа...")
    cfg = MODELS[model_key]

    with tempfile.TemporaryDirectory() as temp_dir:
        input_file_path = os.path.join(temp_dir, file_name)
        audio_path = os.path.join(temp_dir, "media.mp3")
        pdf_path = os.path.join(temp_dir, "summary.pdf")

        try:
            if source_type == "telegram_file":
                bot.edit_message_text("Скачиваю файл из Telegram...", chat_id, status_msg.message_id)
                file_info = bot.get_file(payload)
                downloaded_file = bot.download_file(file_info.file_path)

                with open(input_file_path, "wb") as new_file:
                    new_file.write(downloaded_file)

                bot.edit_message_text("Извлекаю аудио через FFmpeg...", chat_id, status_msg.message_id)
                os.system(f'ffmpeg -y -i "{input_file_path}" -vn -ar 44100 -ac 2 -b:a 192k "{audio_path}"')

            elif source_type == "url":
                bot.edit_message_text("Скачиваю по ссылке через yt-dlp...", chat_id, status_msg.message_id)
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(temp_dir, "downloaded.%(ext)s"),
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                    "quiet": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([payload])

                for f in os.listdir(temp_dir):
                    if f.endswith(".mp3"):
                        audio_path = os.path.join(temp_dir, f)
                        break

            bot.edit_message_text("Загружаю в Gemini Files API...", chat_id, status_msg.message_id)
            genai.configure(api_key=cfg["api_key"])
            audio_file = genai.upload_file(path=audio_path)

            while audio_file.state.name == "PROCESSING":
                time.sleep(2)
                audio_file = genai.get_file(audio_file.name)

            bot.edit_message_text("Расшифровываю речь и делаю конспект...", chat_id, status_msg.message_id)
            model = genai.GenerativeModel(cfg["api_name"], system_instruction=SYSTEM_PROMPT or None)
            prompt = (
                "Сделай подробную расшифровку этого аудиоматериала и составь структурированный конспект. "
                "Выдели основные тезисы, ключевые мысли и добавь таймкоды."
            )
            response = model.generate_content([audio_file, prompt])

            try:
                genai.delete_file(audio_file.name)
            except Exception:
                pass

            bot.edit_message_text("Формирую PDF...", chat_id, status_msg.message_id)
            create_pdf_from_text(response.text, pdf_path)

            with open(pdf_path, "rb") as doc:
                bot.send_document(chat_id, doc, caption="Твой конспект и расшифровка готовы!")

            bot.delete_message(chat_id, status_msg.message_id)

        except Exception as e:
            bot.edit_message_text(
                f"❌ Ошибка при обработке: {html.escape(str(e)[:200])}",
                chat_id,
                status_msg.message_id,
                parse_mode="HTML",
            )


# ---------------------------------------------------------------------------
# Хэндлеры бота
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    bot.send_message(message.chat.id, "Привет! Я Юки. Присылай мне текстовые сообщения, ссылки на видео или медиафайлы")


@bot.message_handler(regexp=r"(https?://[^\s]+)")
def handle_link(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    model_key = get_current_model(uid)

    threading.Thread(
        target=process_media_worker,
        args=(message.chat.id, "url", message.text.strip(), "media.mp4", model_key),
    ).start()


@bot.message_handler(content_types=["document", "video", "audio", "voice"])
def handle_media_files(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    model_key = get_current_model(uid)

    file_id, file_name = None, "file.tmp"
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "file.tmp"
    elif message.video:
        file_id = message.video.file_id
        file_name = "video.mp4"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio.mp3"
    elif message.voice:
        file_id = message.voice.file_id
        file_name = "voice.ogg"

    if file_id:
        threading.Thread(
            target=process_media_worker,
            args=(message.chat.id, "telegram_file", file_id, file_name, model_key),
        ).start()


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message):
    uid = str(message.from_user.id)
    if not is_allowed(uid):
        return
    ensure_user(uid)
    model_key = get_current_model(uid)
    cfg = MODELS[model_key]

    try:
        genai.configure(api_key=cfg["api_key"])
        model = genai.GenerativeModel(cfg["api_name"], system_instruction=SYSTEM_PROMPT or None)
        response = model.generate_content(message.text)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)[:200]}")


if __name__ == "__main__":
    print("Бот Юки запущен...")
    bot.infinity_polling()