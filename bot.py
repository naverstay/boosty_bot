import os
import json
import time
import asyncio
import requests
import zoneinfo
import difflib
import redis.asyncio as redis
from bs4 import BeautifulSoup
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SUB_FILE = "subscribers.json"
STATE_FILE = "last_sent.json"
URL = "https://boosty.to/"

redis_client = None

# ---------------- HELPERS ----------------

def human_date_from_ts(ts: int):
    dt = datetime.fromtimestamp(ts, tz=zoneinfo.ZoneInfo("Europe/Berlin"))
    return dt.strftime("%d.%m.%Y %H:%M")

def human_date(iso_date: str) -> str:
    dt = datetime.fromisoformat(iso_date)
    return dt.strftime("%d.%m.%Y %H:%M")

async def fetch_requests(url, timeout=3):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: requests.get(url, timeout=timeout)
    )


async def redis_load(key: str):
    raw = await redis_client.get(key)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except:
        return {}

async def scheduler_loop(app):
    while True:
        subs = await redis_load("subscribers")
        state = await redis_load("last_sent")

        now = time.time()
        next_times = []

        # Собираем время следующей проверки для каждого канала
        for user_id, channels in subs.items():
            for channel, cfg in channels.items():
                interval_hours = cfg.get("interval", 6)
                interval_sec = interval_hours * 3600

                last = state.get(user_id, {}).get(channel)
                if last is None:
                    # Никогда не проверяли — проверяем сейчас
                    next_times.append(now)
                else:
                    next_times.append(last + interval_sec)

        # Если нет подписок — спим 10 минут
        if not next_times:
            await asyncio.sleep(600)
            continue

        # Находим ближайшее время проверки
        next_check = min(next_times)

        # Сколько спать
        sleep_time = max(0, next_check - now)

        # Спим ровно до нужного момента
        await asyncio.sleep(sleep_time)

        # После пробуждения — проверяем только те каналы, у которых наступило время
        await run_due_checks(app)

async def run_due_checks(app):
    subs = await redis_load("subscribers")
    state = await redis_load("last_sent")

    now = time.time()

    for user_id, channels in subs.items():
        for channel, cfg in channels.items():
            interval_hours = cfg.get("interval", 6)
            interval_sec = interval_hours * 3600

            last = state.get(user_id, {}).get(channel)

            # Проверяем, пора ли
            if last is None or now - last >= interval_sec:
                await check_channel_and_notify(app, user_id, channel, state, last is None)

    # Сохраняем обновлённый last_sent
    await redis_save("last_sent", state)

async def get_last_post_info(channel: str):
    url = f"{URL}{channel}"

    try:
        r = await fetch_requests(url)
        r.raise_for_status()
    except Exception as e:
        print(f"[{channel}] Ошибка запроса: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    script_tag = soup.find("script", {"id": "initial-state"})
    if not script_tag:
        print(f"[{channel}] initial-state не найден")
        return None

    try:
        data = json.loads(script_tag.text)
    except Exception as e:
        print(f"[{channel}] Ошибка JSON: {e}")
        return None

    try:
        posts = data["posts"]["postsList"]["data"]["posts"]
        if not posts:
            print(f"[{channel}] Постов нет")
            return None
    except KeyError:
        print(f"[{channel}] postsList не найден")
        return None

    post = posts[0]

    publish_ts = post.get("publishTime")
    if not publish_ts:
        return None

    timestamp = int(publish_ts)
    title = post.get("title") or "(без заголовка)"
    post_id = post.get("id")
    blog_url = post["user"]["blogUrl"]

    link = f"{URL}{blog_url}/posts/{post_id}"

    return {
        "title": title,
        "link": link,
        "timestamp": timestamp
    }

def get_last_post_info_(channel: str):
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
    timestamp = int(publish_ts)
    title = post.get("title") or "(без заголовка)"
    post_id = post.get("id")
    blog_url = post["user"]["blogUrl"]

    # 5. Формируем ссылку
    link = f"{URL}{blog_url}/posts/{post_id}"

    return {"title": title, "link": link, "timestamp": timestamp}

async def check_channel_and_notify(app, user_id, channel, state, skip = False):
    data = await get_last_post_info(channel)
    if not data:
        return

    last_sent = state.get(user_id, {}).get(channel)

    if last_sent is None or data["timestamp"] > last_sent:
        post_date = human_date_from_ts(data["timestamp"])

        if not skip:
            # отправляем сообщение
            await app.bot.send_message(
                chat_id=user_id,
                text=f"Новый пост {post_date} на канале <b>{channel}</b>:\n<a href='{data['link']}'>{data['title']}</a>",
                parse_mode="HTML"
            )

        # обновляем last_sent
        state.setdefault(user_id, {})[channel] = data["timestamp"]

async def redis_save(key: str, data):
    await redis_client.set(key, json.dumps(data, ensure_ascii=False))

async def suggest_channels(user_id: str, wrong_channel: str):
    subs = await redis_load("subscribers")
    user_channels = subs.get(user_id, {}).keys()

    suggestions = difflib.get_close_matches(
        wrong_channel,
        user_channels,
        n=3,
        cutoff=0.5
    )

    return suggestions

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
    return f"" + (
        str1 if (n % 10 == 1 and n % 100 != 11)
        else str2 if (2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20))
        else str5
    )

def save_page(channel: str, txt: str):
    with open(channel + ".html", "w", encoding="utf-8") as f:
        f.write(txt)

    print("HTML сохранён в " + channel + ".html")

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
    while True:
        subs = await redis_load("subscribers")
        state = await redis_load("last_sent")

        now = time.time()
        next_times = []

        for user_id, channels in subs.items():
            for channel, cfg in channels.items():
                interval = cfg.get("interval", 6) * 3600
                last_raw = state.get(user_id, {}).get(channel)
                last = int(last_raw) if last_raw is not None else None

                if last is None:
                    next_times.append(now)
                else:
                    next_times.append(last + interval)

        if not next_times:
            await asyncio.sleep(600)
            continue

        next_check = min(next_times)
        sleep_time = max(0, next_check - now)

        await asyncio.sleep(sleep_time)

        await run_due_checks(app)

# ---------------- TELEGRAM BOT ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await redis_load("subscribers")

    if user_id not in subs:
        subs[user_id] = {}
        await redis_save("subscribers", subs)

    await update.message.reply_text(
        "Привет! Я бот уведомлений Boosty.\n\n"
        "Команды:\n"
        "/subscribe <канал>\n"
        "/unsubscribe <канал>\n"
        "/list — список каналов\n"
        "/setinterval <канал> <интервал>\n"
        "/check <канал> — проверить сейчас\n"
        "/checkall — проверить все подписки сейчас\n"
        "/reset <канал> — сбросить last_sent для канала\n"
        "/resetall — сбросить last_sent для всех каналов\n"
        "/debug — режим отладки\n"
        "/help — помощь"
    )

# ---------------- DEBUG COMMAND ----------------

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    subs = await redis_load("subscribers")
    state = await redis_load("last_sent")

    user_channels = subs.get(user_id, {})

    now = datetime.utcnow().isoformat()

    text = f"<b>DEBUG INFO</b>\n\n"
    text += f"Server time (UTC): {now}\n"
    text += f"Subscriptions: {len(user_channels)}\n\n"

    if not user_channels:
        text += "Нет подписок."
        return await update.message.reply_text(text, parse_mode="HTML")

    for ch, cfg in user_channels.items():
        last_check = cfg.get("last_check")
        last_sent = state.get(user_id, {}).get(ch)

        text += f"<b>{ch}</b>\n"
        text += f"  interval: {cfg.get('interval')}h\n"
        text += f"  last_check: {last_check}\n"
        text += f"  last_sent: {last_sent}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def check_channel(user_id: str, channel: str):
    subs = await redis_load("subscribers")
    state = await redis_load("last_sent")

    if channel not in subs.get(user_id, {}):
        return "not_subscribed", None

    data = await get_last_post_info(channel)
    if not data:
        return "error", None

    last_sent = state.get(user_id, {}).get(channel)

    if last_sent != data["link"]:
        post_date = human_date_from_ts(data["timestamp"])

        # отправляем новый пост
        send_message(
            user_id,
            f"Новый пост {post_date} на канале <b>{channel}</b>:\n<a href='{data['link']}'>{data['title']}</a>"
        )

        # обновляем состояние
        state.setdefault(user_id, {})
        state[user_id][channel] = data["link"]
        subs[user_id][channel]["last_check"] = int(datetime.utcnow().timestamp())

        await redis_save("subscribers", subs)
        await redis_save("last_sent", state)

        return "new_post", data["link"]

    else:
        # обновляем только last_check
        subs[user_id][channel]["last_check"] = int(datetime.utcnow().timestamp())
        await redis_save("subscribers", subs)

        return "no_new", None


# ---------------- FORCE CHECK COMMAND ----------------

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /check <канал>")

    channel = context.args[0].strip().lower()
    user_id = str(update.effective_user.id)

    await update.message.reply_text(f"Проверяю <b>{channel}</b>…", parse_mode="HTML")

    status, link = await check_channel(user_id, channel)

    if status == "invalid_channel":
        return await update.message.reply_text("Канал " + channel + " не найден на Boosty.")

    if status == "not_subscribed":
        return await update.message.reply_text("Ты не подписан на этот канал.")

    if status == "error":
        return await update.message.reply_text("Ошибка: не удалось получить данные с Boosty.")

    if status == "new_post":
        return await update.message.reply_text("Готово! Новый пост отправлен.")

    if status == "no_new":
        return await update.message.reply_text("Новых постов нет.")

async def checkall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    subs = await redis_load("subscribers")
    user_channels = subs.get(user_id, {})

    if not user_channels:
        return await update.message.reply_text("У тебя нет подписок.")

    await update.message.reply_text("Проверяю все каналы…")

    results = []

    for channel in user_channels.keys():
        status, link = await check_channel(user_id, channel)

        if status == "error":
            results.append(f"{channel}: ошибка получения данных")
        elif status == "invalid_channel":
            results.append(f"{channel}: канал не найден")
        elif status == "new_post":
            results.append(f"{channel}: отправлен новый пост")
        elif status == "no_new":
            results.append(f"{channel}: новых постов нет")
        elif status == "not_subscribed":
            results.append(f"{channel}: не подписан (ошибка логики)")

    text = "<b>Результат проверки:</b>\n\n" + "\n".join(results)
    await update.message.reply_text(text, parse_mode="HTML")

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    state = await redis_load("last_sent")

    if user_id not in state or not state[user_id]:
        return await update.message.reply_text("Нет данных для сброса.")

    count = len(state[user_id])

    # Удаляем все last_sent для пользователя
    del state[user_id]

    await redis_save("last_sent", state)

    await update.message.reply_text(
        f"Сброшены last_sent для всех каналов ({count} шт.).\n"
        f"Бот снова отправит новые посты при следующей проверке."
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /reset <канал>")

    channel = context.args[0].strip().lower()
    user_id = str(update.effective_user.id)

    state = await redis_load("last_sent")

    if user_id not in state or channel not in state[user_id]:
        return await update.message.reply_text("Для этого канала нет last_sent.")

    del state[user_id][channel]

    if not state[user_id]:
        del state[user_id]

    await redis_save("last_sent", state)

    await update.message.reply_text(f"last_sent для <b>{channel}</b> сброшен.", parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /subscribe <канал>")

    channel = context.args[0].strip().lower()
    user_id = str(update.effective_user.id)

    await update.message.reply_text(
        f"Проверяю канал <b>{channel}</b>…",
        parse_mode="HTML"
    )

    # Проверяем, что канал существует
    data = await get_last_post_info(channel)
    if not data:
        # Автодополнение
        # suggestions = suggest_channels(user_id, channel)
        #
        # if suggestions:
        #     text = (
        #         f"Канал <b>{channel}</b> не найден.\n"
        #         f"Возможно, ты имел в виду:\n"
        #         + "\n".join(f"• {s}" for s in suggestions)
        #     )
        # else:
        text = (
            f"Канал <b>{channel}</b> не найден на Boosty.\n"
            f"Проверь правильность написания."
        )

        return await update.message.reply_text(text, parse_mode="HTML")

    subs = await redis_load("subscribers")
    subs.setdefault(user_id, {})

    if channel in subs[user_id]:
        return await update.message.reply_text("Ты уже подписан")

    subs[user_id][channel] = {
        "interval": 6,
        "last_check": None
    }
    await redis_save("subscribers", subs)

    post_date = human_date_from_ts(data["timestamp"])

    await update.message.reply_text(
        f"Подписал на <b>{channel}</b>.\n"
        f"Последний пост {post_date}\n<a href='{data['link']}'>{data['title']}</a>",
        parse_mode="HTML"
    )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /unsubscribe <канал>")

    channel = context.args[0].strip().lower()
    user_id = str(update.effective_user.id)

    subs = await redis_load("subscribers")

    if channel not in subs.get(user_id, {}):
        return await update.message.reply_text("Ты не подписан")

    del subs[user_id][channel]
    await redis_save("subscribers", subs)

    await update.message.reply_text(f"Отписал от {channel}")

async def setinterval_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if "setinterval_channel" not in context.user_data:
        return  # это не ответ на выбор канала

    channel = context.user_data["setinterval_channel"]
    subs = await redis_load("subscribers")

    try:
        hours = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("Интервал должен быть числом.")

    await update_interval_and_check(context.application, user_id, channel, hours)

    del context.user_data["setinterval_channel"]

    await update.message.reply_text(
        f"Интервал для <b>{channel}</b> обновлён: {hours} ч.",
        parse_mode="HTML"
    )

async def setinterval_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    channel = data[1]

    context.user_data["setinterval_channel"] = channel

    await query.edit_message_text(
        f"Ты выбрал канал <b>{channel}</b>.\n"
        f"Теперь отправь интервал в часах.\n\n"
        f"Пример: 3",
        parse_mode="HTML"
    )
async def update_interval_and_check(app, user_id, channel, hours):
    subs = await redis_load("subscribers")
    subs[user_id][channel]["interval"] = hours
    await redis_save("subscribers", subs)

    state = await redis_load("last_sent")
    await check_channel_and_notify(app, user_id, channel, state)
    await redis_save("last_sent", state)

async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await redis_load("subscribers")
    user_channels = subs.get(user_id, {})

    # Если нет подписок
    if not user_channels:
        return await update.message.reply_text("У тебя нет подписок.")

    # Если пользователь указал канал и интервал — обычная логика
    if len(context.args) == 2:
        channel = context.args[0].strip().lower()
        hours = context.args[1]

        if channel not in user_channels:
            return await update.message.reply_text("Ты не подписан на этот канал.")

        try:
            hours = int(hours)
        except ValueError:
            return await update.message.reply_text("Интервал должен быть числом.")

        await update_interval_and_check(context.application, user_id, channel, hours)

        return await update.message.reply_text(
            f"Интервал для <b>{channel}</b> обновлён: {hours} ч.",
            parse_mode="HTML"
        )

    # Если пользователь НЕ указал канал → показываем кнопки
    keyboard = [
        [InlineKeyboardButton(ch, callback_data=f"setinterval:{ch}")]
        for ch in user_channels.keys()
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Выбери канал, для которого хочешь изменить интервал:",
        reply_markup=reply_markup
    )

async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await redis_load("subscribers")

    user_channels = subs.get(user_id, {})
    if not user_channels:
        return await update.message.reply_text("Ты ни на что не подписан")

    text = "Твои подписки:\n"
    for ch, cfg in user_channels.items():
        t = cfg['interval']
        text += (
            f"- {ch} (проверка "
            f"{plural(t, 'каждый', 'каждые', 'каждые')} "
            f"{'' if t == 1 else (str(t) + ' ')}"
            f"{plural(t, 'час', 'часа', 'часов')})\n"
        )

    await update.message.reply_text(text)

async def setup_commands(app):
    commands = [
        BotCommand("start", "Начать работу с ботом"),
        BotCommand("help", "Справка по командам"),
        BotCommand("subscribe", "Подписаться на канал"),
        BotCommand("unsubscribe", "Отписаться от канала"),
        BotCommand("list", "Список подписок"),
        BotCommand("setinterval", "Изменить интервал проверки"),
        BotCommand("check", "Проверить канал вручную"),
        BotCommand("checkall", "Проверить все каналы"),
        BotCommand("reset", "Сбросить last_sent"),
        BotCommand("resetall", "Сбросить все last_sent"),
        BotCommand("debug", "Отладочная информация"),
    ]

    await app.bot.set_my_commands(commands)

# ---------------- FASTAPI + WEBHOOK ----------------

telegram_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, redis_client

    # --- Redis ---
    redis_client = redis.from_url(
        os.getenv("REDIS_URL"),
        decode_responses=True
    )

    # 1. Создаём Telegram Application
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
    telegram_app.add_handler(CommandHandler("debug", debug_cmd))
    telegram_app.add_handler(CommandHandler("check", check))
    telegram_app.add_handler(CommandHandler("checkall", checkall))
    telegram_app.add_handler(CommandHandler("reset", reset))
    telegram_app.add_handler(CommandHandler("resetall", resetall))
    telegram_app.add_handler(CallbackQueryHandler(setinterval_button, pattern="^setinterval:"))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setinterval_value))

    # 2. ИНИЦИАЛИЗАЦИЯ (обязательно!)
    await telegram_app.initialize()

    # Устанавливаем команды
    await setup_commands(telegram_app)

    # 3. Определяем webhook URL
    webhook_url = WEBHOOK_URL
    if not webhook_url:
        ngrok_url = get_ngrok_url()
        if ngrok_url:
            webhook_url = f"{ngrok_url}/webhook/{TG_TOKEN}"
            print("Использую локальный webhook:", webhook_url)
        else:
            print("⚠️ WEBHOOK_URL не задан и ngrok не найден. Webhook не установлен.")
            webhook_url = None

    # 4. Устанавливаем webhook
    if webhook_url:
        set_url = f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook"
        r = requests.get(set_url, params={"url": webhook_url})
        print("Webhook set:", r.text)

    # 5. Запускаем scheduler
    asyncio.create_task(scheduler_loop(telegram_app))

    # 6. Передаём управление FastAPI
    yield

    # 7. Корректное завершение
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != TG_TOKEN:
        return {"status": "forbidden"}

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)

    return {"status": "ok"}

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok"}

# ---------------- MAIN ----------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
