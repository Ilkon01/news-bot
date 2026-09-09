"""
Персональный новостной бот (бесплатная версия на Google Gemini).

Как работает:
1. Telegram-каналы — присылаются как есть, без ИИ
2. Мировые новости проходят три этапа отсева:
   ШАГ 1 — только свежие (по времени публикации), бесплатно
   ШАГ 2 — отсев по ключевым словам, бесплатно
   ШАГ 3 — то, что прошло, идёт в Gemini: проверка по темам + пересказ на русском
3. Отправка в Telegram, каждая новость отдельным сообщением, внизу ссылка
4. Отправленное запоминается в state.json

Настройки — только в config.yaml.
"""

import os
import re
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

# Flash-Lite: самый щедрый бесплатный лимит (15 запросов в минуту)
GEMINI_MODEL = "gemini-flash-lite-latest"
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
                "age_hours": 0.0,  # каналы не фильтруем по возрасту
            }
        )
    print(f"[КАНАЛ {channel}] постов: {len(items)}")
    return items


def entry_age_hours(entry):
    """Сколько часов назад опубликовано. None, если дата не указана."""
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    published = datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600


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
                "age_hours": entry_age_hours(entry),
            }
        )
    if not items:
        print(f"[{name}] ВНИМАНИЕ: ноль записей (проверьте источник)")
    else:
        print(f"[{name}] записей: {len(items)}")
    return items


def is_fresh(item, max_age_hours):
    """ШАГ 1 — отсев по свежести. Без даты считаем свежей."""
    if item["age_hours"] is None:
        return True
    return item["age_hours"] <= max_age_hours


def build_keyword_patterns(keywords):
    """Готовит регулярки с границами слов, чтобы 'war' не ловил 'warning'."""
    patterns = []
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw.endswith("*"):  # 'санкц*' — совпадение по началу слова
            patterns.append(re.compile(r"\b" + re.escape(kw[:-1]), re.IGNORECASE))
        else:
            patterns.append(
                re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
            )
    return patterns


def passes_keywords(item, patterns):
    """ШАГ 2 — бесплатный отсев по словам."""
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
жителям одной страны и не входит в темы — отвечай send: false.

ЗАДАЧА:
1. Реши, подходит ли новость читателю.
2. Если подходит — напиши краткий пересказ НА РУССКОМ: 2-3 предложения
   своими словами. Не переводи дословно и не копируй фразы из оригинала.

Ответь СТРОГО в формате JSON, без markdown и без пояснений:
{{"send": true, "topic": "тема или BREAKING", "summary": "пересказ"}}
Если не подходит: {{"send": false, "topic": "", "summary": ""}}"""


def ask_gemini(item, config):
    """ШАГ 3. Возвращает (слать, тема, пересказ) либо None — лимит исчерпан."""
    payload = {
        "contents": [{"parts": [{"text": build_prompt(item, config)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    for attempt in range(3):
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=45)
        except Exception as e:
            print(f"[GEMINI] сеть: {e}")
            time.sleep(5)
            continue

        # Перегрузка на стороне Google — ждём и пробуем снова
        if resp.status_code == 503:
            wait = 10 * (attempt + 1)
            print(f"[GEMINI] модель перегружена, жду {wait}с (попытка {attempt + 1}/3)")
            time.sleep(wait)
            continue

        # Слишком часто — подождать и повторить; на третий раз сдаёмся
        if resp.status_code == 429:
            if attempt < 2:
                print("[GEMINI] слишком часто, жду 30с")
                time.sleep(30)
                continue
            print("[GEMINI] дневной лимит исчерпан, останавливаюсь")
            return None

        if not resp.ok:
            print(f"[GEMINI] ошибка {resp.status_code}: {resp.text[:200]}")
            return False, "", ""

        try:
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            return (
                bool(parsed.get("send")),
                parsed.get("topic", ""),
                parsed.get("summary", ""),
            )
        except Exception as e:
            print(f"[GEMINI] не разобрал ответ на «{item['title'][:50]}»: {e}")
            return False, "", ""

    print("[GEMINI] три попытки не удались, пропускаю новость")
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
    ai_limit = config.get("max_ai_calls_per_run", 30)
    max_age = config.get("max_age_hours", 4)
    pause = config.get("seconds_between_ai_calls", 5)
    patterns = build_keyword_patterns(config.get("keywords", []))

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

    fresh_new = [
        i for i in world_items if i["id"] not in seen and is_fresh(i, max_age)
    ]
    stale_or_old = [i for i in world_items if i["id"] not in seen and i not in fresh_new]
    for i in stale_or_old:
        new_ids.append(i["id"])

    print(f"Всего получено: {len(world_items)}")
    print(f"Новых и свежих (моложе {max_age} ч): {len(fresh_new)}")

    # ШАГ 2 — ключевые слова
    candidates = [i for i in fresh_new if passes_keywords(i, patterns)]
    for i in fresh_new:
        if i not in candidates:
            new_ids.append(i["id"])
    print(f"Прошло отсев по словам: {len(candidates)} (отсеяно {len(fresh_new) - len(candidates)})")
    print(f"Пойдёт в Gemini: {min(len(candidates), ai_limit)}")

    # ШАГ 3 — Gemini
    ai_used = 0
    for item in candidates:
        if ai_used >= ai_limit or sent >= send_limit:
            break

        result = ask_gemini(item, config)
        ai_used += 1

        if result is None:  # лимит исчерпан — прекращаем, остальное на след. раз
            break

        new_ids.append(item["id"])
        ok, topic, summary = result
        if ok and summary:
            if send_telegram(format_world_message(item, topic, summary)):
                sent += 1

        time.sleep(pause)  # пауза выполняется ВСЕГДА, даже после ошибки

    state["seen"] = state["seen"] + new_ids
    save_state(state)
    print(f"Обращений к Gemini: {ai_used}. Отправлено сообщений: {sent}")


if __name__ == "__main__":
    main()
