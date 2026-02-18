import os
import json
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
        subs[user_id] = []
        save_subs(subs)

    text = (
        "Привет! Я бот уведомлений Boosty.\n\n"
        "Команды:\n"
        "/subscribe <канал> — подписаться\n"
        "/unsubscribe <канал> — отписаться\n"
        "/list — показать твои подписки\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /subscribe historipi")

    channel = context.args[0].strip()
    user_id = str(update.effective_user.id)

    subs = load_subs()
    subs.setdefault(user_id, [])

    if channel in subs[user_id]:
        return await update.message.reply_text(f"Ты уже подписан на {channel}")

    subs[user_id].append(channel)
    save_subs(subs)
    await update.message.reply_text(f"Подписал тебя на {channel}")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Используй: /unsubscribe historipi")

    channel = context.args[0].strip()
    user_id = str(update.effective_user.id)

    subs = load_subs()
    if user_id not in subs or channel not in subs[user_id]:
        return await update.message.reply_text(f"Ты не подписан на {channel}")

    subs[user_id].remove(channel)
    save_subs(subs)
    await update.message.reply_text(f"Отписал тебя от {channel}")


async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subs = load_subs()
    user_channels = subs.get(user_id, [])

    if not user_channels:
        return await update.message.reply_text("Ты ни на что не подписан")

    text = "Твои подписки:\n" + "\n".join(f"- {ch}" for ch in user_channels)
    await update.message.reply_text(text)


def main():
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN не задан в .env")

    app = ApplicationBuilder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("list", list_subs))

    app.run_polling()


if __name__ == "__main__":
    main()
