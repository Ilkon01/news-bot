"""
Персональный новостной бот Ильяса.

Что делает:
1. Читает список Telegram-каналов и RSS-источников из config.yaml
2. Telegram-каналы присылаются как есть (без фильтра тем)
3. Мировые RSS-источники прогоняются через Claude — пропускаются только
   темы из filter_topics.include, всё остальное отбрасывается
4. Новое (ещё не отправленное) шлётся в личный Telegram-бот, каждая новость
   отдельным сообщением
5. Список уже отправленного хранится в state.json, чтобы не дублировать

Настройки меняются в config.yaml, этот файл трогать не нужно.
"""

import os
import time
import json

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup
import anthropic

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
MAX_SEEN_ITEMS = 3000  # чтобы state.json не рос бесконечно

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN_ITEMS:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if not resp.ok:
            print("Ошибка отправки в Telegram:", resp.text)
    except Exception as e:
        print("Исключение при отправке в Telegram:", e)


def fetch_telegram_channel(channel):
    """Читает последние публичные посты канала через t.me/s/<channel>.
    Работает без входа в аккаунт, только для публичных каналов."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"Не удалось прочитать канал {channel}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message"):
        msg_id = msg.get("data-post")
        if not msg_id:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n").strip() if text_el else ""
        if not text:
            continue
        link = f"https://t.me/{msg_id}"
        items.append(
            {
                "id": link,
                "source": f"Telegram: {channel}",
                "title": text[:80],
                "text": text,
                "link": link,
            }
        )
    return items


def fetch_rss(name, url):
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        items.append(
            {"id": link, "source": name, "title": title, "text": summary, "link": link}
        )
    return items


def passes_filter(item, filter_topics):
    include = "\n".join(f"- {t}" for t in filter_topics.get("include", []))
    exclude = "\n".join(f"- {t}" for t in filter_topics.get("exclude", []))
    prompt = f"""Ты — фильтр новостей для одного конкретного читателя.

Заголовок: {item['title']}
Описание: {item['text'][:800]}

Пропускай (отвечай ДА) ТОЛЬКО если новость относится к одной из этих тем:
{include}

Отфильтровывай (отвечай НЕТ), если новость про:
{exclude}

Если новость не подходит ни под одну тему из списка "пропускай" — тоже отвечай НЕТ.
Ответь строго одним словом: ДА или НЕТ."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = resp.content[0].text.strip().upper()
        return answer.startswith("ДА")
    except Exception as e:
        print("Ошибка фильтрации через Claude:", e)
        return False  # при сбое лучше пропустить новость, чем случайно заспамить


def format_message(item):
    text = item["text"]
    if len(text) > 500:
        text = text[:500] + "…"
    return f"<b>{item['source']}</b>\n{text}\n{item['link']}"


def main():
    config = load_config()
    state = load_state()
    seen = set(state["seen"])
    new_seen = list(state["seen"])
    sent_count = 0

    jobs = []  # (item, filter_topics_or_None)

    for channel in config.get("telegram_channels", []):
        for item in fetch_telegram_channel(channel):
            jobs.append((item, None))

    filter_topics = config.get("filter_topics", {})
    for src in config.get("world_news_rss", []):
        for item in fetch_rss(src["name"], src["url"]):
            jobs.append((item, filter_topics))

    for item, topics in jobs:
        if item["id"] in seen:
            continue

        if topics is not None and not passes_filter(item, topics):
            new_seen.append(item["id"])
            continue

        send_telegram(format_message(item))
        sent_count += 1
        new_seen.append(item["id"])
        time.sleep(1)  # не спамить Telegram API подряд

    state["seen"] = new_seen
    save_state(state)
    print(f"Готово. Отправлено новостей: {sent_count}")


if __name__ == "__main__":
    main()
