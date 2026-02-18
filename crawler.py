import os
import json
import requests
from bs4 import BeautifulSoup
from aiogram import Bot
from dotenv import load_dotenv

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
SUB_FILE = "subscribers.json"
STATE_FILE = "last_sent.json"

bot = Bot(token=TG_TOKEN, parse_mode="HTML")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_boosty(channel):
    url = f"https://boosty.to/{channel}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    post = soup.find("a", {"class": "post-card"})
    if not post:
        return None

    title = post.get("title") or "Новый пост"
    link = "https://boosty.to" + post.get("href")

    return {"title": title, "link": link}


async def main():
    subs = load_json(SUB_FILE)
    state = load_json(STATE_FILE)

    for user_id, channels in subs.items():
        for channel, cfg in channels.items():
            data = fetch_boosty(channel)
            if not data:
                continue

            last = state.get(user_id, {}).get(channel)

            if last == data["link"]:
                continue

            await bot.send_message(
                chat_id=user_id,
                text=f"Новый пост на <b>{channel}</b>:\n\n"
                     f"<b>{data['title']}</b>\n{data['link']}"
            )

            state.setdefault(user_id, {})
            state[user_id][channel] = data["link"]

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
