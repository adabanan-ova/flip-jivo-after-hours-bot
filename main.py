import os
import sqlite3
import time as time_module
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "CHANGE_ME")
JIVO_API_URL = os.getenv("JIVO_API_URL", "TEMP")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
FORCE_AFTER_HOURS = os.getenv("FORCE_AFTER_HOURS", "false").lower() == "true"

WORK_START = time(8, 0)

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
    if FORCE_AFTER_HOURS:
        return False
    return now_almaty().time() >= WORK_START


def current_period_key() -> str:
    return f"night_{now_almaty().date().isoformat()}"


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


def is_valid_client_message(payload: dict) -> bool:
    if payload.get("event") != "CLIENT_MESSAGE":
        return False

    message = payload.get("message", {})
    if message.get("type") != "TEXT":
        return False

    return bool(message.get("text"))


def build_bot_message(payload: dict) -> dict:
    return {
        "id": payload.get("id"),
        "client_id": payload.get("client_id"),
        "chat_id": payload.get("chat_id"),
        "message": {
            "type": "TEXT",
            "text": AUTO_REPLY_TEXT,
            "timestamp": int(time_module.time())
        },
        "event": "BOT_MESSAGE"
    }


def send_bot_message(payload: dict):
    body = build_bot_message(payload)
    print("Sending BOT_MESSAGE:", body)

    response = requests.post(
        JIVO_API_URL,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=3
    )

    print("Jivo response:", response.status_code, response.text)
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

    if not is_valid_client_message(payload):
        return jsonify({"status": "ignored_event"})

    chat_id = str(payload.get("chat_id"))
    period_key = current_period_key()

    if already_replied(chat_id, period_key):
        return jsonify({"status": "already_replied_this_night"})

    try:
        send_bot_message(payload)
        mark_replied(chat_id, period_key)
        return jsonify({"status": "auto_reply_sent"})
    except Exception as e:
        print("Send error:", str(e))
        return jsonify({"status": "send_error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
