"""
Персональный новостной бот (бесплатная версия на Google Gemini).

Как работает:
1. Telegram-каналы — присылаются как есть, без фильтра и без ИИ (бесплатно)
2. Мировые новости проходят два этапа:
   ШАГ 1 — бесплатный отсев по ключевым словам из config.yaml
           (отсекает большую часть, не тратит лимиты)
   ШАГ 2 — то, что прошло, идёт в Gemini: он решает, подходит ли новость
           по темам, и пишет краткий пересказ на русском
3. Отправка в личный Telegram-бот, каждая новость отдельным сообщением,
   внизу ссылка на оригинал
4. Отправленное запоминается в state.json, чтобы не дублировать

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

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
MAX_SEEN_ITEMS = 5000

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


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
    print(f"[КАНАЛ {channel}] постов: {len(items)}")
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
        print(f"[{name}] ВНИМАНИЕ: ноль записей (проверьте источник)")
    else:
        print(f"[{name}] записей: {len(items)}")
    return items


def google_news_url(domain):
    q = urllib.parse.quote(f"when:2h allinurl:{domain}")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def passes_keywords(item, keywords):
    """ШАГ 1 — бесплатный отсев. Без обращения к ИИ."""
    haystack = f" {item['title']} {item['text']} ".lower()
    return any(kw.lower() in haystack for kw in keywords)


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
Описание: {item['text'][:1200]}

ТЕМЫ ЧИТАТЕЛЯ (3 = присылать обязательно, 2 = если новость заметная,
1 = только если это крупное громкое событие):
{topics_lines}

НИКОГДА не пропускай:
{never}

{breaking}

ЗАДАЧА:
1. Реши, подходит ли новость читателю.
2. Если подходит — напиши краткий пересказ НА РУССКОМ: 2-3 предложения
   своими словами, передающие суть. Не переводи дословно и не копируй
   фразы из оригинала. Без вводных вроде «В статье говорится».

Ответь СТРОГО в формате JSON, без markdown и без пояснений:
{{"send": true, "topic": "тема или BREAKING", "summary": "пересказ"}}
Если новость не подходит: {{"send": false, "topic": "", "summary": ""}}"""


def ask_gemini(item, config):
    """ШАГ 2 — точная проверка + пересказ. Возвращает (слать, тема, пересказ)."""
    payload = {
        "contents": [{"parts": [{"text": build_prompt(item, config)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
            json=payload,
            timeout=40,
        )
        if resp.status_code == 429:
            print("[GEMINI] превышен бесплатный лимит, пропускаю остальное")
            return None
        if not resp.ok:
            print(f"[GEMINI] ошибка {resp.status_code}: {resp.text[:300]}")
            return False, "", ""

        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return (
            bool(parsed.get("send")),
            parsed.get("topic", ""),
            parsed.get("summary", ""),
        )
    except Exception as e:
        print(f"[GEMINI] сбой на «{item['title'][:60]}»: {e}")
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

    send_limit = config.get("max_per_run", 0) or 10**9
    ai_limit = config.get("max_ai_calls_per_run", 40)
    keywords = config.get("keywords", [])

    # --- Telegram-каналы: без ИИ ---
    for channel in config.get("telegram_channels", []):
        for item in fetch_telegram_channel(channel):
            if item["id"] in seen or sent >= send_limit:
                continue
            if send_telegram(format_channel_message(item)):
                sent += 1
            new_ids.append(item["id"])
            time.sleep(1)

    # --- Мировые источники ---
    world_items = []
    for src in config.get("world_news_rss", []):
        world_items += fetch_rss(src["name"], src["url"])
    for src in config.get("google_news_sites", []):
        world_items += fetch_rss(src["name"], google_news_url(src["domain"]))

    fresh = [i for i in world_items if i["id"] not in seen]
    print(f"Мировых новостей всего: {len(world_items)}, из них новых: {len(fresh)}")

    # ШАГ 1 — бесплатный отсев
    candidates = [i for i in fresh if passes_keywords(i, keywords)]
    skipped = len(fresh) - len(candidates)
    for item in fresh:
        if item not in candidates:
            new_ids.append(item["id"])
    print(f"Отсеяно по ключевым словам без ИИ: {skipped}")
    print(f"Пойдёт в Gemini: {min(len(candidates), ai_limit)}")

    # ШАГ 2 — Gemini
    ai_used = 0
    for item in candidates:
        if ai_used >= ai_limit or sent >= send_limit:
            break
        result = ask_gemini(item, config)
        if result is None:  # исчерпан дневной лимит Gemini
            break
        ai_used += 1
        new_ids.append(item["id"])

        ok, topic, summary = result
        if not ok or not summary:
            continue
        if send_telegram(format_world_message(item, topic, summary)):
            sent += 1
        time.sleep(4)  # не более ~15 запросов в минуту (бесплатный лимит)

    state["seen"] = state["seen"] + new_ids
    save_state(state)
    print(f"Обращений к Gemini: {ai_used}. Отправлено сообщений: {sent}")


if __name__ == "__main__":
    main()
