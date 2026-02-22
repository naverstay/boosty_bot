import os
import json
import time
import asyncio
import requests
import difflib
import redis.asyncio as redis
from bs4 import BeautifulSoup
from datetime import datetime, timezone
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

# ---------------- CONFIG ----------------
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
# Ожидается URL без токена в конце, например https://myapp.com
WEBHOOK_URL = os.getenv("WEBHOOK_URL") + TG_TOKEN
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
BOOSTY_BASE_URL = "https://boosty.to/"

# Глобальные клиенты
redis_client = None
telegram_app = None


# ---------------- HELPERS ----------------

def get_ngrok_url():
    """Автоматически получает URL запущенного локально ngrok"""
    try:
        r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        tunnels = r.json()["tunnels"]
        for t in tunnels:
            if t["proto"] == "https":
                return t["public_url"]
    except Exception as e:
        return None


def human_date_from_ts(ts: int):
    if not ts: return "никогда"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%d.%m.%Y %H:%M")


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


async def fetch_boosty_page(channel: str, timeout=10):
    url = f"{BOOSTY_BASE_URL}{channel}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(None, lambda: requests.get(url, headers=headers, timeout=timeout))
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"Ошибка запроса к {channel}: {e}")
        return None


async def get_last_post_info(channel: str):
    html = await fetch_boosty_page(channel)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", {"id": "initial-state"})
    if not script_tag:
        return None

    try:
        data = json.loads(script_tag.text)
        posts = data["posts"]["postsList"]["data"]["posts"]
        if not posts:
            return None

        post = posts[0]
        return {
            "title": post.get("title") or "(без заголовка)",
            "link": f"{BOOSTY_BASE_URL}{post['user']['blogUrl']}/posts/{post.get('id')}",
            "timestamp": int(post.get("publishTime")),
            "channel": channel
        }
    except (KeyError, json.JSONDecodeError, IndexError):
        return None

    # ---------------- REDIS LOGIC (HSET/HGET) ----------------


async def db_get_user_subs(user_id: str) -> dict:
    """Получает все подписки пользователя из Redis Hash"""
    data = await redis_client.hget("subscribers", str(user_id))
    return json.loads(data) if data else {}


async def db_save_user_subs(user_id: str, subs: dict):
    """Сохраняет подписки конкретного пользователя"""
    await redis_client.hset("subscribers", str(user_id), json.dumps(subs))


async def db_get_all_users():
    """Возвращает все ключи (user_id) из хэша"""
    return await redis_client.hkeys("subscribers")


# ---------------- CORE LOGIC ----------------

async def check_and_notify(user_id: str, channel: str, user_subs: dict, skip_msg=False):
    """Логика проверки одного канала для одного юзера"""
    post = await get_last_post_info(channel)
    if not post: return False

    last_sent = user_subs.get(channel, {}).get("last_sent")
    is_new = last_sent is None or post["timestamp"] > last_sent

    if is_new:
        if not skip_msg:
            text = (f"🔔 <b>Новый пост на {channel}!</b>\n"
                    f"📅 {human_date_from_ts(post['timestamp'])}\n\n"
                    f"🔗 <a href='{post['link']}'>{post['title']}</a>")
            try:
                await telegram_app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка отправки {user_id}: {e}")

        # Обновляем данные
        user_subs[channel]["last_sent"] = post["timestamp"]
        user_subs[channel]["last_check"] = int(time.time())
        return True
    return False


# ---------------- SCHEDULER ----------------

async def scheduler_loop(stop_event: asyncio.Event):
    print("Планировщик запущен")
    while not stop_event.is_set():
        try:
            user_ids = await db_get_all_users()
            now = time.time()

            for uid in user_ids:
                subs = await db_get_user_subs(uid)
                changed = False
                for channel, cfg in subs.items():
                    interval_sec = cfg.get("interval", 6) * 3600
                    last_sent = cfg.get("last_sent") or 0

                    if now - last_sent >= interval_sec:
                        if await check_and_notify(uid, channel, subs):
                            changed = True

                if changed:
                    await db_save_user_subs(uid, subs)

            await asyncio.wait_for(stop_event.wait(), timeout=300)  # Проверка каждые 5 мин
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            print(f"Ошибка планировщика: {e}")
            await asyncio.sleep(10)


# ---------------- ADDITIONAL HELPERS ----------------

def plural(n, str1, str2, str5):
    """Склонение: 1 час, 2 часа, 5 часов"""
    if n % 10 == 1 and n % 100 != 11:
        return str1
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return str2
    else:
        return str5


# ---------------- EXTENDED HANDLERS ----------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Справка по командам:</b>\n\n"
        "/subscribe <code>name</code> — подписаться на канал\n"
        "/unsubscribe <code>name</code> — удалить из уведомлений\n"
        "/list — список каналов\n"
        "/setinterval <code>name</code> <code>time</code> — изменить частоту проверок канала\n"
        "/check <code>name</code> — проверить канал сейчас\n"
        "/checkall — проверить все каналы сейчас\n"
        "/reset <code>name</code> — сбросить последнее уведомление для канала\n"
        "/resetall — сбросить последнее уведомление для всех каналов\n"
        "/debug — техническая информация\n"
        "/help — помощь"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    now_ts = int(time.time())
    text = f"⚙️ <b>Debug Info</b>\n"
    text += f"Server Time: {human_date_from_ts(now_ts)}\n"
    text += f"User ID: <code>{user_id}</code>\n"
    text += f"Total Subs: {len(subs)}\n\n"

    for ch, cfg in subs.items():
        text += f"<b>{ch}</b>:\n"
        text += f"  Interval: {cfg.get('interval')}h\n"
        text += f"  Last Sent: {human_date_from_ts(cfg.get('last_sent'))}\n"
        text += f"  Last Check: {human_date_from_ts(cfg.get('last_check'))}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def reset_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс last_sent для всех подписок пользователя"""
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя нет подписок для сброса.")
        return

    for channel in subs:
        subs[channel]["last_sent"] = None

    await db_save_user_subs(user_id, subs)
    await update.message.reply_text(
        "♻️ <b>Все счетчики сброшены!</b>\nПри следующей проверке планировщик пришлет уведомления о последних постах по всем каналам.",
        parse_mode="HTML"
    )


async def check_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя нет подписок.")
        return

    msg = await update.message.reply_text("🔄 Начинаю полную проверку всех каналов...")

    changed = False
    results = []
    for channel in subs.keys():
        is_new = await check_and_notify(user_id, channel, subs)
        status = "✅ Есть новый пост!" if is_new else "😴 Изменений нет"
        results.append(f"• {channel}: {status}")
        if is_new: changed = True

    if changed:
        await db_save_user_subs(user_id, subs)

    await msg.edit_text("<b>Результаты проверки:</b>\n\n" + "\n".join(results), parse_mode="HTML")

    return


async def check_func(update_text, user_id, subs, channel=""):
    await update_text(f"⏳ Проверяю <b>{channel}</b>...", parse_mode="HTML")
    is_new = await check_and_notify(user_id, channel, subs)
    await db_save_user_subs(user_id, subs)
    if not is_new:
        await update_text(f"😴 На канале <b>{channel}</b> новых постов нет.", parse_mode="HTML")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя нет подписок.")
        return

        # Если аргумент есть: /check name
    if context.args:
        channel = context.args[0].strip().lower()
        if channel not in subs:
            await update.message.reply_text(f"Ты не подписан на {channel}.")
            return

        await check_func(update.message.reply_text, user_id, subs, channel)
    else:
        # Если аргумента нет — показываем кнопки
        keyboard = [[InlineKeyboardButton(ch, callback_data=f"check_pick:{ch}")] for ch in subs.keys()]
        await update.message.reply_text("Выбери канал для проверки:", reply_markup=InlineKeyboardMarkup(keyboard))

    return


async def reset_func(update_text, user_id, subs, channel=""):
    subs[channel]["last_sent"] = None
    await db_save_user_subs(user_id, subs)
    await update_text(f"♻️ Память для <b>{channel}</b> сброшена.", parse_mode="HTML")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя нет подписок.")
        return

    if context.args:
        channel = context.args[0].strip().lower()
        if channel not in subs:
            await update.message.reply_text(f"Ты не подписан на {channel}.")
            return

        await reset_func(update.message.reply_text, user_id, subs, channel)

    else:
        keyboard = [[InlineKeyboardButton(ch, callback_data=f"reset_pick:{ch}")] for ch in subs.keys()]
        await update.message.reply_text("Выбери канал для сброса:", reply_markup=InlineKeyboardMarkup(keyboard))

    return


async def set_interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя нет подписок.")
        return

    # Логика /setinterval name hours
    if len(context.args) == 2:
        channel = context.args[0].lower()
        try:
            hours = int(context.args[1])
            if channel in subs:
                subs[channel]["interval"] = hours
                await db_save_user_subs(user_id, subs)
                await update.message.reply_text(f"⏱ Интервал для {channel}: {hours} ч.")
                return
        except ValueError:
            pass

    # Если аргументов нет или они неверны — кнопки
    keyboard = [[InlineKeyboardButton(ch, callback_data=f"setint_pick:{ch}")] for ch in subs.keys()]
    await update.message.reply_text("⏱ Выбери канал для настройки интервала:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    return


# ---------------- UPDATED BUTTON HANDLER ----------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    data = query.data
    await query.answer()

    # Парсим данные: действие и канал
    if ":" not in data:
        return

    action, channel = data.split(":", 1)
    subs = await db_get_user_subs(user_id)

    if action == "unsub_pick":
        if channel in subs:
            del subs[channel]
            await db_save_user_subs(user_id, subs)
            await query.edit_message_text(f"✅ Подписка на <b>{channel}</b> удалена.", parse_mode="HTML")
        else:
            await query.edit_message_text("Ошибка: подписка уже была удалена ранее.")

    elif action == "check_pick":
        await check_func(query.edit_message_text, user_id, subs, channel)

    elif action == "reset_pick":
        if channel in subs:
            await reset_func(query.edit_message_text, user_id, subs, channel)

    elif action == "setint_pick":
        # Устанавливаем состояние ожидания ввода числа
        context.user_data["awaiting_interval_for"] = channel
        await query.edit_message_text(f"Введите новый интервал в часах для <b>{channel}</b>:", parse_mode="HTML")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит ввод числа после нажатия кнопки интервала"""
    user_id = str(update.effective_user.id)
    channel = context.user_data.get("awaiting_interval_for")

    if not channel:
        return  # Если пользователь просто что-то пишет боту

    try:
        hours = int(update.message.text.strip())
        if hours < 1: hours = 1

        subs = await db_get_user_subs(user_id)
        if channel in subs:
            subs[channel]["interval"] = hours
            await db_save_user_subs(user_id, subs)
            del context.user_data["awaiting_interval_for"]

            h_text = plural(hours, "час", "часа", "часов")
            await update.message.reply_text(f"✅ Интервал для <b>{channel}</b> изменен на {hours} {h_text}.",
                                            parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ Пожалуйста, введи целое число (количество часов).")


# ---------------- BOT HANDLERS ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Используй: /subscribe [канал]")
        return

    channel = context.args[0].strip().lower()
    user_id = str(update.effective_user.id)

    await update.message.reply_text(f"🔍 Проверяю канал {channel}...")
    post = await get_last_post_info(channel)

    if not post:
        await update.message.reply_text("❌ Канал не найден или нет постов.")
        return

    subs = await db_get_user_subs(user_id)
    if channel in subs:
        await update.message.reply_text("✅ Ты уже подписан.")
        return

    subs[channel] = {
        "interval": 6,
        "last_sent": post["timestamp"],
        "last_check": int(time.time())
    }
    await db_save_user_subs(user_id, subs)
    await update.message.reply_text(f"🎉 Успешно! Последний пост был {human_date_from_ts(post['timestamp'])}.")


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)

    if not subs:
        await update.message.reply_text("У тебя пока нет активных подписок.")
        return

    # Если канал указан текстом: /unsubscribe kuji
    if context.args:
        channel = context.args[0].strip().lower()
        if channel in subs:
            del subs[channel]
            await db_save_user_subs(user_id, subs)
            await update.message.reply_text(f"✅ Ты успешно отписался от <b>{channel}</b>.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Ты не подписан на канал {channel}.")
        return

    # Если аргумента нет — выводим список кнопок
    keyboard = [[InlineKeyboardButton(f"❌ {ch}", callback_data=f"unsub_pick:{ch}")] for ch in subs.keys()]

    await update.message.reply_text(
        "Выбери канал, от которого хочешь отписаться:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = await db_get_user_subs(user_id)
    if not subs:
        await update.message.reply_text("У тебя нет подписок.")
        return

    text = "📋 <b>Твои подписки:</b>\n\n"
    keyboard = []
    for ch, cfg in subs.items():
        t = cfg['interval']
        text += (f"• <b>{ch}</b> ({plural(t, 'каждый', 'каждые', 'каждые')} "
                 f"{'' if t == 1 else (str(t) + ' ')}"
                 f"{plural(t, 'час', 'часа', 'часов')})\n"
                 )
        keyboard.append([InlineKeyboardButton(f"❌ Отписаться от {ch}", callback_data=f"unsub_pick:{ch}")])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    return


# ---------------- WEBHOOK & LIFESPAN ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    telegram_app = ApplicationBuilder().token(TG_TOKEN).build()

    # Регистрация
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    telegram_app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    telegram_app.add_handler(CommandHandler("list", list_cmd))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("debug", debug_cmd))
    telegram_app.add_handler(CommandHandler("check", check_cmd))
    telegram_app.add_handler(CommandHandler("checkall", check_all_cmd))
    telegram_app.add_handler(CommandHandler("reset", reset_cmd))
    telegram_app.add_handler(CommandHandler("resetall", reset_all_cmd))
    telegram_app.add_handler(CommandHandler("setinterval", set_interval_cmd))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    await telegram_app.initialize()
    await setup_commands(telegram_app)
    await telegram_app.start()

    # Webhook Logic (Local + Prod)
    webhook_url = WEBHOOK_URL
    if not webhook_url:
        ngrok_url = get_ngrok_url()
        if ngrok_url:
            webhook_url = f"{ngrok_url}/webhook/{TG_TOKEN}"
            print(f"🚀 Локальный Webhook через ngrok: {webhook_url}")

    if webhook_url:
        await telegram_app.bot.set_webhook(url=webhook_url)

    stop_event = asyncio.Event()
    st_task = asyncio.create_task(scheduler_loop(stop_event))

    yield

    stop_event.set()
    await st_task
    await telegram_app.stop()
    await telegram_app.shutdown()
    await redis_client.close()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """Эндпоинт для проверки работоспособности сервера (Health Check)"""
    return {"status": "ok"}


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token == TG_TOKEN:
        update = Update.de_json(await request.json(), telegram_app.bot)
        await telegram_app.process_update(update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
