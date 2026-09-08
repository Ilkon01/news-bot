"""
Персональный новостной бот.

Что делает:
1. Читает Telegram-каналы (шлёт как есть) и мировые источники из config.yaml
2. Мировые новости прогоняет через Claude: тот решает, подходит ли новость
   под темы с их приоритетами, и если да — пишет краткий пересказ по-русски
3. Отправляет в личный Telegram-бот, каждая новость отдельным сообщением,
   внизу — ссылка на оригинал
4. Отправленное запоминает в state.json, чтобы не дублировать

Настройки — только в config.yaml.
"""

import os
import time
import json
import urllib.parse

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup
import anthropic

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
MAX_SEEN_ITEMS = 5000

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
            return False
        return True
    except Exception as e:
        print("Исключение при отправке в Telegram:", e)
        return False


def fetch_telegram_channel(channel):
    """Читает последние публичные посты канала через t.me/s/<channel>."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"[КАНАЛ {channel}] не удалось прочитать: {e}")
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
                "title": text[:120],
                "text": text,
                "link": link,
            }
        )
    print(f"[КАНАЛ {channel}] получено постов: {len(items)}")
    return items


def fetch_rss(name, url):
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[{name}] ошибка чтения RSS: {e}")
        return []

    items = []
    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        items.append(
            {
                "id": link,
                "source": name,
                "title": entry.get("title", ""),
                "text": entry.get("summary", ""),
                "link": link,
            }
        )
    if not items:
        print(f"[{name}] ВНИМАНИЕ: не получено ни одной записи (проверьте URL)")
    else:
        print(f"[{name}] получено записей: {len(items)}")
    return items


def google_news_url(domain):
    q = urllib.parse.quote(f"when:2h allinurl:{domain}")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def build_filter_prompt(item, config):
    topics_lines = "\n".join(
        f"- {topic} (приоритет {weight})"
        for topic, weight in config["topics"].items()
        if weight > 0
    )
    never = "\n".join(f"- {x}" for x in config.get("never_send", []))
    breaking = (
        "Также пропускай крупные срочные мировые новости (breaking news), "
        "даже если они не попадают ни под одну тему."
        if config.get("breaking_news")
        else ""
    )

    return f"""Ты — персональный новостной фильтр и редактор.

НОВОСТЬ:
Источник: {item['source']}
Заголовок: {item['title']}
Описание: {item['text'][:1500]}

ТЕМЫ ЧИТАТЕЛЯ (приоритет 3 = присылать обязательно, 2 = присылать если новость
заметная, 1 = только если это крупное громкое событие):
{topics_lines}

НИКОГДА не пропускай:
{never}

{breaking}

ЗАДАЧА:
1. Реши, подходит ли новость читателю по правилам выше.
2. Если подходит — напиши краткий пересказ НА РУССКОМ ЯЗЫКЕ: 2-3 предложения
   своими словами, передающие суть. Не переводи дословно, не копируй фразы
   из оригинала. Без вводных вроде «В статье говорится».

Ответь СТРОГО в формате JSON, без markdown-разметки и без пояснений:
{{"send": true/false, "topic": "название темы или BREAKING", "summary": "пересказ на русском"}}

Если send=false, поля topic и summary оставь пустыми строками."""


def evaluate(item, config):
    """Возвращает (нужно_ли_слать, тема, пересказ_по-русски)."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": build_filter_prompt(item, config)}],
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return bool(data.get("send")), data.get("topic", ""), data.get("summary", "")
    except Exception as e:
        print(f"[ФИЛЬТР] ошибка на новости «{item['title'][:60]}»: {e}")
        return False, "", ""


def format_world_message(item, topic, summary):
    header = f"<b>{item['source']}</b>"
    if topic:
        header += f" · {topic}"
    return f"{header}\n\n{summary}\n\n🔗 {item['link']}"


def format_channel_message(item):
    text = item["text"]
    if len(text) > 700:
        text = text[:700] + "…"
    return f"<b>{item['source']}</b>\n\n{text}\n\n🔗 {item['link']}"


def main():
    config = load_config()
    state = load_state()
    seen = set(state["seen"])
    new_ids = []
    sent = 0
    limit = config.get("max_per_run", 0) or 10**9

    # --- Telegram-каналы: без фильтра ---
    for channel in config.get("telegram_channels", []):
        for item in fetch_telegram_channel(channel):
            if item["id"] in seen or sent >= limit:
                continue
            if send_telegram(format_channel_message(item)):
                sent += 1
            new_ids.append(item["id"])
            time.sleep(1)

    # --- Мировые источники: фильтр + пересказ по-русски ---
    world_items = []
    for src in config.get("world_news_rss", []):
        world_items += fetch_rss(src["name"], src["url"])
    for src in config.get("google_news_sites", []):
        world_items += fetch_rss(src["name"], google_news_url(src["domain"]))

    print(f"Всего мировых новостей получено: {len(world_items)}")

    checked = 0
    for item in world_items:
        if item["id"] in seen:
            continue
        if sent >= limit:
            break
        checked += 1
        ok, topic, summary = evaluate(item, config)
        new_ids.append(item["id"])
        if not ok or not summary:
            continue
        if send_telegram(format_world_message(item, topic, summary)):
            sent += 1
        time.sleep(1)

    state["seen"] = state["seen"] + new_ids
    save_state(state)
    print(f"Проверено новых мировых новостей: {checked}. Отправлено всего: {sent}")


if __name__ == "__main__":
    main()
