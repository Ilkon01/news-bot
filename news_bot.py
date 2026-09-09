"""
Персональный новостной бот (бесплатная версия на Google Gemini).

Как работает:
1. Telegram-каналы — присылаются как есть, без ИИ
2. Мировые новости проходят три этапа отсева:
   ШАГ 1 — только свежие (по времени публикации), бесплатно
   ШАГ 2 — отсев по ключевым словам, бесплатно
   ШАГ 3 — Gemini: проверка по темам + заголовок и пересказ на русском
3. Отправка в Telegram красиво оформленным сообщением со скрытой ссылкой
4. Отправленное запоминается в state.json

Настройки — только в config.yaml.
"""

import os
import re
import html
import time
import json
import calendar
from datetime import datetime, timezone

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
MAX_SEEN_ITEMS = 8000

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Строгая схема ответа — Gemini физически не сможет вернуть сломанный JSON
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "send": {"type": "BOOLEAN"},
        "topic": {"type": "STRING"},
        "title_ru": {"type": "STRING"},
        "summary": {"type": "STRING"},
    },
    "required": ["send", "topic", "title_ru", "summary"],
}


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
                "disable_web_page_preview": True,  # без громоздких превью
            },
            timeout=20,
        )
        if not resp.ok:
            print("Ошибка отправки в Telegram:", resp.text[:200])
            return False
        return True
    except Exception as e:
        print("Исключение при отправке в Telegram:", e)
        return False


def fetch_telegram_channel(channel):
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
                "age_hours": 0.0,
            }
        )
    print(f"[КАНАЛ {channel}] постов: {len(items)}")
    return items


def entry_age_hours(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    published = datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600


def clean_html(raw):
    """Убирает html-теги из описания RSS."""
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text(" ").strip()


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
                "text": clean_html(entry.get("summary", "")),
                "link": link,
                "age_hours": entry_age_hours(entry),
            }
        )
    if not items:
        print(f"[{name}] ВНИМАНИЕ: ноль записей (проверьте источник)")
    else:
        print(f"[{name}] записей: {len(items)}")
    return items


def is_fresh(item, max_age_hours):
    if item["age_hours"] is None:
        return True
    return item["age_hours"] <= max_age_hours


def build_keyword_patterns(keywords):
    patterns = []
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw.endswith("*"):
            patterns.append(re.compile(r"\b" + re.escape(kw[:-1]), re.IGNORECASE))
        else:
            patterns.append(re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    return patterns


def passes_keywords(item, patterns):
    haystack = f"{item['title']} {item['text']}"
    return any(p.search(haystack) for p in patterns)


def build_prompt(item, config):
    topics_lines = "\n".join(
        f"- {t} (приоритет {w})" for t, w in config["topics"].items() if w > 0
    )
    never = "\n".join(f"- {x}" for x in config.get("never_send", []))
    breaking = (
        "Также пропускай крупные срочные мировые новости, даже если они "
        "не попадают ни под одну тему."
        if config.get("breaking_news")
        else ""
    )

    return f"""Ты — персональный новостной фильтр и редактор.

НОВОСТЬ:
Источник: {item['source']}
Заголовок: {item['title']}
Описание: {item['text'][:1000]}

ТЕМЫ ЧИТАТЕЛЯ (3 = присылать обязательно, 2 = если новость заметная,
1 = только если это крупное громкое событие):
{topics_lines}

НИКОГДА не пропускай:
{never}

{breaking}

Будь строгим: если новость проходная, местечковая или интересна только
жителям одной страны и не входит в темы — send: false.

ЗАДАЧА:
1. Реши, подходит ли новость.
2. topic — ровно одно название темы из списка выше, либо BREAKING.
3. title_ru — заголовок на русском, до 10 слов, без точки в конце.
4. summary — пересказ на русском, 2-3 предложения своими словами.
   Не переводи дословно, не копируй фразы из оригинала, без вводных
   вроде «В статье говорится» и без упоминания названия издания.

Если новость не подходит — send: false, остальные поля пустые строки."""


def ask_gemini(item, config):
    """Возвращает словарь результата, либо None если лимит исчерпан."""
    payload = {
        "contents": [{"parts": [{"text": build_prompt(item, config)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 600,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    for attempt in range(3):
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=45)
        except Exception as e:
            print(f"[GEMINI] сеть: {e}")
            time.sleep(5)
            continue

        if resp.status_code == 503:
            wait = 10 * (attempt + 1)
            print(f"[GEMINI] перегрузка, жду {wait}с (попытка {attempt + 1}/3)")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            if attempt < 2:
                print("[GEMINI] слишком часто, жду 30с")
                time.sleep(30)
                continue
            print("[GEMINI] дневной лимит исчерпан, останавливаюсь")
            return None

        if not resp.ok:
            print(f"[GEMINI] ошибка {resp.status_code}: {resp.text[:200]}")
            return {}

        try:
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw)
        except Exception as e:
            print(f"[GEMINI] не разобрал ответ на «{item['title'][:50]}»: {e}")
            return {}

    print("[GEMINI] три попытки не удались, пропускаю новость")
    return {}


def esc(text):
    """Экранирует символы, которые сломали бы HTML-разметку Telegram."""
    return html.escape(str(text or ""), quote=False)


def format_world_message(item, data, emoji_map):
    topic = data.get("topic", "")
    emoji = emoji_map.get(topic, emoji_map.get("_default", "📰"))
    title = esc(data.get("title_ru", "")).strip()
    summary = esc(data.get("summary", "")).strip()
    source = esc(item["source"])
    link = esc(item["link"])

    parts = [f"{emoji} <b>{title}</b>" if title else f"{emoji} <b>{esc(topic)}</b>"]
    parts.append("")
    parts.append(summary)
    parts.append("")
    parts.append(
        f"<i>{source}</i>  ·  <a href=\"{link}\">Читать оригинал</a>"
    )
    return "\n".join(parts)


def format_channel_message(item, emoji_map):
    text = item["text"]
    if len(text) > 700:
        text = text[:700].rstrip() + "…"
    channel = item["source"].replace("Telegram: ", "")
    emoji = emoji_map.get("_channel", "💬")
    return (
        f"{emoji} <b>{esc(channel)}</b>\n\n"
        f"{esc(text)}\n\n"
        f"<a href=\"{esc(item['link'])}\">Открыть пост</a>"
    )


def main():
    config = load_config()
    state = load_state()
    seen = set(state["seen"])
    new_ids = []
    sent = 0

    send_limit = config.get("max_per_run", 0) or 10**9
    ai_limit = config.get("max_ai_calls_per_run", 30)
    max_age = config.get("max_age_hours", 4)
    pause = config.get("seconds_between_ai_calls", 5)
    emoji_map = config.get("topic_emoji", {})
    patterns = build_keyword_patterns(config.get("keywords", []))

    # --- Telegram-каналы ---
    for channel in config.get("telegram_channels", []):
        for item in fetch_telegram_channel(channel):
            if item["id"] in seen or sent >= send_limit:
                continue
            if send_telegram(format_channel_message(item, emoji_map)):
                sent += 1
            new_ids.append(item["id"])
            time.sleep(1)

    # --- Мировые источники ---
    world_items = []
    for src in config.get("world_news_rss", []):
        world_items += fetch_rss(src["name"], src["url"])

    unseen = [i for i in world_items if i["id"] not in seen]
    fresh_new = [i for i in unseen if is_fresh(i, max_age)]
    for i in unseen:
        if not is_fresh(i, max_age):
            new_ids.append(i["id"])

    print(f"Всего получено: {len(world_items)}")
    print(f"Новых и свежих (моложе {max_age} ч): {len(fresh_new)}")

    candidates = [i for i in fresh_new if passes_keywords(i, patterns)]
    for i in fresh_new:
        if not passes_keywords(i, patterns):
            new_ids.append(i["id"])
    print(
        f"Прошло отсев по словам: {len(candidates)} "
        f"(отсеяно {len(fresh_new) - len(candidates)})"
    )
    print(f"Пойдёт в Gemini: {min(len(candidates), ai_limit)}")

    ai_used = 0
    for item in candidates:
        if ai_used >= ai_limit or sent >= send_limit:
            break

        data = ask_gemini(item, config)
        ai_used += 1

        if data is None:  # лимит — остальное разберём в следующий запуск
            break

        new_ids.append(item["id"])
        if data.get("send") and data.get("summary"):
            if send_telegram(format_world_message(item, data, emoji_map)):
                sent += 1

        time.sleep(pause)

    state["seen"] = state["seen"] + new_ids
    save_state(state)
    print(f"Обращений к Gemini: {ai_used}. Отправлено сообщений: {sent}")


if __name__ == "__main__":
    main()
