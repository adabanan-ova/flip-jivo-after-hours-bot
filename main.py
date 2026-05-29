import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "CHANGE_ME")
JIVO_API_URL = os.getenv("JIVO_API_URL", "CHANGE_ME_AFTER_JIVO_REPLY")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")

# Рабочее время операторов: ежедневно с 08:00 до 00:00
WORK_START = time(8, 0)
WORK_END = time(0, 0)

AUTO_REPLY_TEXT = os.getenv(
    "AUTO_REPLY_TEXT",
    "Здравствуйте!\n\n"
    "Благодарим за обращение 💙\n\n"
    "Сейчас наши операторы отдыхают, но Ваше сообщение уже получено.\n\n"
    "Мы работаем ежедневно с 08:00 до 00:00 и обязательно ответим Вам, "
    "как только начнётся рабочее время."
)

DB_PATH = os.getenv("DB_PATH", "/tmp/jivo_bot.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_replies (
                chat_id TEXT NOT NULL,
                period_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, period_key)
            )
        """)
        conn.commit()


init_db()


def now_almaty():
    return datetime.now(ZoneInfo(TIMEZONE))


def is_working_time() -> bool:
    """
    Рабочее время: 08:00–00:00.
    Нерабочее время: 00:00–07:59.
    """
    now = now_almaty().time()
    return WORK_START <= now


def current_period_key() -> str:
    """
    Ключ нерабочего периода.
    Для ночи 00:00–07:59 используем текущую дату.
    Например: night_2026-05-29
    """
    now = now_almaty()
    return f"night_{now.date().isoformat()}"


def already_replied(chat_id: str, period_key: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_replies WHERE chat_id = ? AND period_key = ?",
            (chat_id, period_key)
        ).fetchone()
    return row is not None


def mark_replied(chat_id: str, period_key: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_replies (chat_id, period_key, created_at) VALUES (?, ?, ?)",
            (chat_id, period_key, now_almaty().isoformat())
        )
        conn.commit()


def extract_chat_id(payload: dict):
    """
    Jivo может прислать разные поля в зависимости от типа события.
    После первого тестового события при необходимости подправим под реальный payload.
    """
    return (
        payload.get("chat_id")
        or payload.get("chat", {}).get("id")
        or payload.get("dialog_id")
        or payload.get("client_id")
        or payload.get("client", {}).get("id")
        or payload.get("visitor", {}).get("id")
    )


def is_client_message(payload: dict) -> bool:
    """
    Бот должен отвечать только на текстовые сообщения клиента.
    Не отвечаем на сообщения операторов, ботов и системные события.
    """
    sender_type = str(payload.get("sender", {}).get("type", "")).lower()
    author_type = str(payload.get("author", {}).get("type", "")).lower()
    event_type = str(payload.get("event", "")).lower()

    blocked_words = ["bot", "agent", "operator", "admin", "manager"]
    if any(word in sender_type for word in blocked_words):
        return False
    if any(word in author_type for word in blocked_words):
        return False

    text = (
        payload.get("text")
        or payload.get("message", {}).get("text")
        or payload.get("body", {}).get("text")
    )

    if not text:
        return False

    # Если Jivo явно передаст, что чат назначен оператору, не отвечаем
    assigned_agent = (
        payload.get("agent")
        or payload.get("operator")
        or payload.get("assigned_agent")
        or payload.get("chat", {}).get("agent")
        or payload.get("chat", {}).get("operator")
    )
    if assigned_agent:
        return False

    return True


def send_jivo_message(chat_id: str, text: str):
    """
    ВАЖНО:
    После ответного письма Jivo может понадобиться поменять формат body
    под их точный пример API-запроса.
    """
    if not JIVO_API_URL or JIVO_API_URL == "CHANGE_ME_AFTER_JIVO_REPLY":
        print("JIVO_API_URL is not set. Message was not sent.")
        return

    body = {
        "chat_id": chat_id,
        "text": text
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {BOT_TOKEN}"
    }

    response = requests.post(JIVO_API_URL, json=body, headers=headers, timeout=10)
    response.raise_for_status()


@app.route("/", methods=["GET"])
def healthcheck():
    return "Jivo after-hours bot is running", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def jivo_webhook():
    payload = request.get_json(silent=True) or {}
    print("Incoming payload:", payload)

    if is_working_time():
        return jsonify({"status": "working_time_no_reply"})

    if not is_client_message(payload):
        return jsonify({"status": "ignored_event"})

    chat_id = extract_chat_id(payload)

    if not chat_id:
        return jsonify({"status": "no_chat_id", "payload": payload}), 400

    period_key = current_period_key()

    if already_replied(str(chat_id), period_key):
        return jsonify({"status": "already_replied_this_night"})

    try:
        send_jivo_message(str(chat_id), AUTO_REPLY_TEXT)
        mark_replied(str(chat_id), period_key)
    except Exception as exc:
        print("Send error:", exc)
        return jsonify({"status": "send_error", "error": str(exc)}), 500

    return jsonify({"status": "auto_reply_sent"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
