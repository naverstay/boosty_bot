import os
import json
import subprocess
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
SUB_FILE = "subscribers.json"


def run_crawler():
    try:
        subprocess.Popen(["python3", "crawler.py"])
    except Exception as e:
        print("Ошибка запуска crawler.py:", e)


def load_subs():
    try:
        with open(SUB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_subs(data):
    with open(SUB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = load_subs()

    if user_id not in subs:
        subs[user_id] = {}
        save_subs(subs)

    text = (
        "Привет! Я бот уведомлений Boosty.\n\n"
        "Команды:\n"
        "/subscribe <канал> — подписаться\n"
        "/unsubscribe <канал> — отписаться\n"
        "/setinterval <канал> <часы> — изменить интервал\n"
        "/list — показать твои подписки\n"
        "/help — помощь"
    )
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.effective_message.reply_text("Используй: /subscribe historipi")

    channel = context.args[0].strip()
    user_id = str(update.effective_user.id)

    subs = load_subs()
    subs.setdefault(user_id, {})

    if channel in subs[user_id]:
        return await update.effective_message.reply_text(f"Ты уже подписан на {channel}")

    subs[user_id][channel] = {"interval": 6}
    save_subs(subs)
    await update.effective_message.reply_text(f"Подписал тебя на {channel}\nИнтервал обновления: 6 часов")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.effective_message.reply_text("Используй: /unsubscribe historipi")

    channel = context.args[0].strip()
    user_id = str(update.effective_user.id)

    subs = load_subs()
    if user_id not in subs or channel not in subs[user_id]:
        return await update.effective_message.reply_text(f"Ты не подписан на {channel}")

    del subs[user_id][channel]
    save_subs(subs)
    await update.effective_message.reply_text(f"Отписал тебя от {channel}")


async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.effective_message.reply_text("Используй: /setinterval historipi 3")

    channel = context.args[0].strip()
    try:
        hours = int(context.args[1])
    except ValueError:
        return await update.effective_message.reply_text("Интервал должен быть числом")

    if hours < 1:
        return await update.effective_message.reply_text("Интервал должен быть минимум 1 час")

    user_id = str(update.effective_user.id)
    subs = load_subs()

    if user_id not in subs or channel not in subs[user_id]:
        return await update.effective_message.reply_text("Ты не подписан на этот канал")

    subs[user_id][channel]["interval"] = hours
    save_subs(subs)

    # ← Запускаем crawler сразу после обновления интервала
    run_crawler()

    await update.effective_message.reply_text(f"Интервал для {channel} обновлён: {hours} ч.")


async def setup_commands(app):
    await app.bot.set_my_commands([
        ("start", "Начать работу с ботом"),
        ("subscribe", "Подписаться на канал Boosty"),
        ("unsubscribe", "Отписаться от канала"),
        ("setinterval", "Установить интервал обновления"),
        ("list", "Показать список подписок"),
        ("help", "Помощь"),
    ])


async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = load_subs()
    user_channels = subs.get(user_id, {})

    if not user_channels:
        return await update.effective_message.reply_text("Ты ни на что не подписан")

    text = "Твои подписки:\n"
    for ch, cfg in user_channels.items():
        text += f"- {ch} (интервал: {cfg['interval']} ч.)\n"

    await update.effective_message.reply_text(text)


def main():
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN не задан в .env")

    app = (
        ApplicationBuilder()
        .token(TG_TOKEN)
        .post_init(setup_commands)   # ← ВАЖНО!
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("setinterval", setinterval))
    app.add_handler(CommandHandler("list", list_subs))

    app.run_polling()

if __name__ == "__main__":
    main()
