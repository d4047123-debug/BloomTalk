import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "answers.db")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Railway Variables.")
if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("Set WEBAPP_URL to your public HTTPS Railway domain.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                answers_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        con.commit()


@dp.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(
        f"Твой числовой Telegram ID: `{message.from_user.id}`\n\n"
        "Скопируй число и вставь его в Railway → Variables → OWNER_ID. "
        "После этого перезапусти сервис.",
        parse_mode="Markdown"
    )


@dp.message(CommandStart())
async def start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="Открыть анкету ✨",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )]],
        resize_keyboard=True,
        is_persistent=True
    )
    await message.answer(
        "Привет! Это небольшая анкета от меня 💗\n\n"
        "Сначала — «Как спалось?», а затем ещё несколько лёгких вопросов. "
        "Отвечай так, как тебе удобно. Ответы будут отправлены создателю анкеты. "
        "Можно пропустить любой вопрос.",
        reply_markup=keyboard
    )


def verify_init_data(init_data: str) -> dict:
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing Telegram signature")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("Invalid Telegram signature")
    auth_date = int(parsed.get("auth_date", "0"))
    if auth_date <= 0 or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
        raise ValueError("Telegram session expired")
    user = json.loads(parsed.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("Telegram user not found")
    return user


async def save_answers(request: web.Request):
    try:
        body = await request.json()
        user = verify_init_data(body.get("initData", ""))
        answers = body.get("answers")

        # Never report success when the owner destination has not been configured.
        if OWNER_ID <= 0:
            return web.json_response(
                {"ok": False, "error": "Создатель ещё не настроил получение ответов. Попробуй позже."},
                status=503
            )

        if not isinstance(answers, list) or len(answers) != 10:
            return web.json_response({"ok": False, "error": "Проверь ответы: нужно 10 вопросов."}, status=400)

        cleaned = []
        for item in answers:
            if not isinstance(item, dict):
                return web.json_response({"ok": False, "error": "Некорректный ответ."}, status=400)
            q = str(item.get("question", ""))[:300]
            a = str(item.get("answer", "")).strip()[:1000]
            if not q or not a:
                return web.json_response({"ok": False, "error": "Ответь на каждый вопрос или выбери «Пропустить»."}, status=400)
            cleaned.append({"question": q, "answer": a})

        # Deliver to one fixed numeric Telegram account only.
        name = str(user.get("first_name", "Без имени"))[:80]
        username = str(user.get("username", ""))[:80]
        uname = f" (@{username})" if username else ""
        message_text = f"💌 Новые ответы от {name}{uname}\nTelegram ID: {user['id']}\n\n"
        message_text += "\n\n".join(
            f"{i+1}. {x['question']}\nОтвет: {x['answer']}" for i, x in enumerate(cleaned)
        )

        # Send first; only save after Telegram confirms delivery.
        chunks = [message_text[i:i+3800] for i in range(0, len(message_text), 3800)]
        for chunk in chunks:
            await bot.send_message(OWNER_ID, chunk)

        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO submissions (telegram_id, username, first_name, answers_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(user["id"]), username, name, json.dumps(cleaned, ensure_ascii=False), int(time.time()))
            )
            con.commit()

        return web.json_response({"ok": True})
    except (ValueError, KeyError, json.JSONDecodeError):
        return web.json_response(
            {"ok": False, "error": "Не удалось проверить Telegram. Открой анкету через кнопку в боте."},
            status=401
        )
    except Exception:
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить ответы. Попробуй ещё раз позже."},
            status=500
        )


async def health(_request):
    return web.json_response({"status": "ok"})


async def on_startup(app):
    init_db()
    app["polling_task"] = asyncio.create_task(dp.start_polling(bot))


async def on_cleanup(app):
    task = app.get("polling_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await bot.session.close()


app = web.Application(client_max_size=64 * 1024)
app.router.add_get("/", lambda request: web.FileResponse("web/index.html"))
app.router.add_get("/health", health)
app.router.add_post("/api/answers", save_answers)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
