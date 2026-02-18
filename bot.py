import os
import json
import asyncio
import requests
import zoneinfo
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SUB_FILE = "subscribers.json"
STATE_FILE = "last_sent.json"
URL = "https://boosty.to/"

# ---------------- JSON HELPERS ----------------

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_ngrok_url():
    try:
        r = requests.get("http://127.0.0.1:4040/api/tunnels")
        tunnels = r.json()["tunnels"]
        for t in tunnels:
            if t["proto"] == "https":
                return t["public_url"]
    except Exception:
        return None

def plural(n, str1, str2, str5):
    return f"{n} " + (
        str1 if (n % 10 == 1 and n % 100 != 11)
        else str2 if (2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20))
        else str5
    )

# ---------------- BOOSTY CRAWLER ----------------

def save_page(channel: str, txt: str):
    with open(channel + ".html", "w", encoding="utf-8") as f:
        f.write(txt)

    print("HTML сохранён в " + channel + ".html")

def get_last_post_info(channel: str):
    url = f"{URL}{channel}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()

#     save_page(channel, r.text)

    soup = BeautifulSoup(r.text, "html.parser")

    # 1. Находим JSON
    script_tag = soup.find("script", {"id": "initial-state"})
    if not script_tag:
        print(f"[{channel}] initial-state не найден")
        return None

    try:
        data = json.loads(script_tag.text)
    except Exception as e:
        print(f"[{channel}] Ошибка JSON: {e}")
        return None

    # 2. Достаём список постов
    try:
        posts = data["posts"]["postsList"]["data"]["posts"]
        if not posts:
            print(f"[{channel}] Постов нет")
            return None
    except KeyError:
        print(f"[{channel}] postsList не найден")
        return None

    # 3. Берём самый свежий пост
    post = posts[0]

    # 4. Достаём данные
    publish_ts = post.get("publishTime")  # UNIX timestamp
    title = post.get("title") or "(без заголовка)"
    post_id = post.get("id")
    blog_url = post["user"]["blogUrl"]

    # 5. Формируем ссылку
    link = f"{URL}/{blog_url}/posts/{post_id}"

    # 6. Конвертируем дату
    dt = datetime.fromtimestamp(publish_ts, tz=zoneinfo.ZoneInfo("UTC"))
    dt_local = dt.astimezone(zoneinfo.ZoneInfo("Europe/Berlin"))
    iso_date = dt_local.isoformat()

    return {"title": title, "link": link, "iso_date": iso_date}

# ---------------- TELEGRAM SEND ----------------

def send_message(user_id, text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": user_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Ошибка отправки:", e)

# ---------------- SCHEDULER ----------------

async def scheduler_loop(app):
    print("Scheduler started")

    while True:
        subs = load_json(SUB_FILE)
        state = load_json(STATE_FILE)
        now = datetime.utcnow()

        for user_id, channels in subs.items():
            for channel, cfg in channels.items():

                interval = cfg.get("interval", 6)
                last_check_str = cfg.get("last_check")

                if last_check_str:
                    last_check = datetime.fromisoformat(last_check_str)
                else:
                    last_check = now - timedelta(hours=interval)

                if now - last_check < timedelta(hours=interval):
                    continue

                data = get_last_post_info(channel)
                if not data:
                    continue

                last_sent = state.get(user_id, {}).get(channel)

                if last_sent != data["link"]:
                    send_message(
                        user_id,
                        f"Новый пост на канале <b>{channel}</b>:\n\n"
                        f"<b>{data['title']}</b>\n{data['link']}"
                    )

                    state.setdefault(user_id, {})
                    state[user_id][channel] = data["link"]
                else:
                    send_message(
                        user_id,
                        f"Новых постов на <b>{channel}</b> нет."
                    )

                subs[user_id][channel]["last_check"] = now.isoformat()

        save_json(SUB_FILE, subs)
        save_json(STATE_FILE, state)

        await asyncio.sleep(60)

# ---------------- TELEGRAM BOT ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = load_json(SUB_FILE)

    if user_id not in subs:
        subs[user_id] = {}
        save_json(SUB_FILE, subs)

    await update.message.reply_text(
        "Привет! Я бот уведомлений Boosty.\n\n"
        "Команды:\n"
        "/subscribe <канал>\n"
        "/unsubscribe <канал>\n"
        "/setinterval <канал> <интервал>\n"
        "/list — показать твои подписки\n"
        "/help — помощь"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /subscribe historipi")

    channel = context.args[0]
    user_id = str(update.effective_user.id)

    subs = load_json(SUB_FILE)
    subs.setdefault(user_id, {})

    if channel in subs[user_id]:
        return await update.message.reply_text("Ты уже подписан")

    subs[user_id][channel] = {
        "interval": 6,
        "last_check": None
    }
    save_json(SUB_FILE, subs)

    await update.message.reply_text(f"Подписал на {channel}")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /unsubscribe historipi")

    channel = context.args[0]
    user_id = str(update.effective_user.id)

    subs = load_json(SUB_FILE)

    if channel not in subs.get(user_id, {}):
        return await update.message.reply_text("Ты не подписан")

    del subs[user_id][channel]
    save_json(SUB_FILE, subs)

    await update.message.reply_text(f"Отписал от {channel}")

async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Используй: /setinterval historipi 3")

    channel = context.args[0]
    hours = int(context.args[1])
    user_id = str(update.effective_user.id)

    subs = load_json(SUB_FILE)

    if channel not in subs.get(user_id, {}):
        return await update.message.reply_text("Ты не подписан")

    subs[user_id][channel]["interval"] = hours
    save_json(SUB_FILE, subs)

    await update.message.reply_text(f"Интервал обновлён: {hours} ч.")

async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = load_json(SUB_FILE)

    user_channels = subs.get(user_id, {})
    if not user_channels:
        return await update.message.reply_text("Ты ни на что не подписан")

    text = "Твои подписки:\n"
    for ch, cfg in user_channels.items():
        t = cfg['interval']
        if t == 1:
            text += f"- {ch} (проверка каждый час)\n"
        else:
            text += f"- {ch} (проверка каждые {t} {plural(t, 'час', 'часа', 'часов')})\n"

    await update.message.reply_text(text)

# ---------------- FASTAPI + WEBHOOK ----------------

telegram_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    telegram_app = (
        ApplicationBuilder()
        .token(TG_TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("subscribe", subscribe))
    telegram_app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    telegram_app.add_handler(CommandHandler("setinterval", setinterval))
    telegram_app.add_handler(CommandHandler("list", list_subs))
    telegram_app.add_handler(CommandHandler("help", help_cmd))

    # --- Автоматический выбор webhook URL ---
    webhook_url = WEBHOOK_URL

    if not webhook_url:
        # локальный режим → пробуем взять URL из ngrok
        ngrok_url = get_ngrok_url()
        if ngrok_url:
            webhook_url = f"{ngrok_url}/webhook/{TG_TOKEN}"
            print("Использую локальный webhook:", webhook_url)
        else:
            print("⚠️ WEBHOOK_URL не задан и ngrok не найден. Webhook не установлен.")
            webhook_url = None

    # --- Устанавливаем webhook ---
    if webhook_url:
        set_url = f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook"
        r = requests.get(set_url, params={"url": webhook_url})
        print("Webhook set:", r.text)

    # --- Запускаем планировщик ---
    asyncio.create_task(scheduler_loop(telegram_app))

    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != TG_TOKEN:
        return {"status": "forbidden"}

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)

    return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "ok"}

# ---------------- MAIN ----------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)

# def start_bot():
#     # создаём event loop для этого потока
#     loop = asyncio.new_event_loop()
#     asyncio.set_event_loop(loop)
#
#     tg_app = (
#         ApplicationBuilder()
#         .token(TG_TOKEN)
#         .build()
#     )
#
#     tg_app.add_handler(CommandHandler("start", start))
#     tg_app.add_handler(CommandHandler("subscribe", subscribe))
#     tg_app.add_handler(CommandHandler("unsubscribe", unsubscribe))
#     tg_app.add_handler(CommandHandler("setinterval", setinterval))
#     tg_app.add_handler(CommandHandler("list", list_subs))
#     tg_app.add_handler(CommandHandler("help", help_cmd))
#
#     loop.run_until_complete(tg_app.run_polling())
#
#
# def start_scheduler():
#     loop = asyncio.new_event_loop()
#     asyncio.set_event_loop(loop)
#     loop.run_until_complete(scheduler_loop(None))
#
#
# if __name__ == "__main__":
#     # 1) Запускаем Telegram‑бота в отдельном потоке
#     threading.Thread(target=start_bot, daemon=True).start()
#
#     # 2) Запускаем планировщик в отдельном потоке
#     threading.Thread(target=start_scheduler, daemon=True).start()
#
#     # 3) Запускаем FastAPI сервер (главный поток)
#     port = int(os.getenv("PORT", 10000))
#     uvicorn.run(app, host="0.0.0.0", port=port)
