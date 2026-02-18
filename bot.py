import os
import json
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from dotenv import load_dotenv

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
SUB_FILE = "subscribers.json"

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())


# -----------------------------
# JSON helpers
# -----------------------------
def load_subs():
    try:
        with open(SUB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_subs(data):
    with open(SUB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -----------------------------
# Commands
# -----------------------------
@dp.message_handler(commands=["start", "help"])
async def start(message: types.Message):
    user_id = str(message.from_user.id)
    subs = load_subs()

    if user_id not in subs:
        subs[user_id] = {}
        save_subs(subs)

    text = (
        "Привет! Я бот уведомлений Boosty.\n\n"
        "Команды:\n"
        "/subscribe <канал> — подписаться (интервал 6 часов)\n"
        "/unsubscribe <канал> — отписаться\n"
        "/setinterval <канал> <часы> — изменить интервал\n"
        "/list — показать твои подписки\n"
        "/help — помощь"
    )
    await message.answer(text)


@dp.message_handler(commands=["subscribe"])
async def subscribe(message: types.Message):
    args = message.get_args().split()
    if not args:
        return await message.answer("Используй: /subscribe historipi")

    channel = args[0].strip()
    user_id = str(message.from_user.id)

    subs = load_subs()
    subs.setdefault(user_id, {})

    if channel in subs[user_id]:
        return await message.answer(f"Ты уже подписан на {channel}")

    subs[user_id][channel] = {"interval": 6}
    save_subs(subs)

    await message.answer(f"Подписал тебя на {channel}\nИнтервал: 6 часов")


@dp.message_handler(commands=["unsubscribe"])
async def unsubscribe(message: types.Message):
    args = message.get_args().split()
    if not args:
        return await message.answer("Используй: /unsubscribe historipi")

    channel = args[0].strip()
    user_id = str(message.from_user.id)

    subs = load_subs()
    if user_id not in subs or channel not in subs[user_id]:
        return await message.answer(f"Ты не подписан на {channel}")

    del subs[user_id][channel]
    save_subs(subs)

    await message.answer(f"Отписал тебя от {channel}")


@dp.message_handler(commands=["setinterval"])
async def setinterval(message: types.Message):
    args = message.get_args().split()
    if len(args) < 2:
        return await message.answer("Используй: /setinterval historipi 3")

    channel = args[0].strip()

    try:
        hours = int(args[1])
    except ValueError:
        return await message.answer("Интервал должен быть числом")

    if hours < 1:
        return await message.answer("Интервал должен быть минимум 1 час")

    user_id = str(message.from_user.id)
    subs = load_subs()

    if user_id not in subs or channel not in subs[user_id]:
        return await message.answer("Ты не подписан на этот канал")

    subs[user_id][channel]["interval"] = hours
    save_subs(subs)

    await message.answer(f"Интервал для {channel} обновлён: {hours} ч.")


@dp.message_handler(commands=["list"])
async def list_subs(message: types.Message):
    user_id = str(message.from_user.id)
    subs = load_subs().get(user_id, {})

    if not subs:
        return await message.answer("Ты ни на что не подписан")

    text = "Твои подписки:\n"
    for ch, cfg in subs.items():
        text += f"- {ch} (интервал: {cfg['interval']} ч.)\n"

    await message.answer(text)


# -----------------------------
# HTTP server for Render
# -----------------------------
async def handle(request):
    return web.Response(text="Boosty bot is running!")


async def start_web_app():
    app = web.Application()
    app.router.add_get("/", handle)

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# -----------------------------
# Main
# -----------------------------
async def on_startup(dp):
    asyncio.create_task(start_web_app())


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
