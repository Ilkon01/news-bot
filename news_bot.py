"""
Персональный новостной бот (бесплатная версия на Google Gemini).

Как работает:
  ШАГ 1 — берём только свежие новости (по времени публикации)
  ШАГ 2 — отсев по ключевым словам (бесплатно, без ИИ)
  ШАГ 3 — дедупликация: одинаковые новости из разных источников
          склеиваются, остаётся версия от источника с высшим доверием
  ШАГ 4 — Gemini: проверка по темам + заголовок и пересказ на русском
  ШАГ 5 — отправка в Telegram

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

# Модели пробуются по порядку: если первая перегружена, сразу берётся вторая.
GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]


def gemini_url(model):
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "send": {"type": "BOOLEAN"},
        "topic": {"type": "STRING"},
        "country": {"type": "STRING"},
        "title_ru": {"type": "STRING"},
        "summary": {"type": "STRING"},
    },
    "required": ["send", "topic", "country", "title_ru", "summary"],
}

# Служебные слова, которые не участвуют в сравнении заголовков
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been",
    "will", "would", "can", "could", "has", "have", "had", "it", "its", "his",
    "her", "their", "this", "that", "these", "those", "after", "over", "into",
    "says", "said", "new", "amid", "up", "out", "no", "not", "who", "what",
    "и", "в", "во", "на", "с", "со", "по", "за", "из", "от", "до", "к", "у",
    "о", "об", "для", "что", "как", "это", "не", "но", "а", "же", "бы", "ли",
    "он", "она", "они", "его", "её", "их", "был", "была", "были", "будет",
}


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen", [])
    state.setdefault("recent", [])  # [{"tokens": [...], "ts": epoch}]
    return state


def save_state(state, memory_hours):
    state["seen"] = state["seen"][-MAX_SEEN_ITEMS:]
    cutoff = time.time() - memory_hours * 3600
    state["recent"] = [r for r in state["recent"] if r.get("ts", 0) > cutoff]
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
                "disable_web_page_preview": True,
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


def entry_age_hours(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    published = datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600


def clean_html(raw):
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text(" ").strip()


def fetch_rss(src):
    name, url = src["name"], src["url"]
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
                "country": src.get("country", ""),
                "trust": src.get("trust", 5),
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


# ---------- Дедупликация ----------

def title_tokens(title):
    """Значимые слова заголовка для сравнения."""
    # убираем хвост вроде " - Reuters", который добавляет Google News
    title = re.sub(r"\s+[-–|]\s+[^-–|]{2,30}$", "", title)
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", title.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def similarity(tokens_a, tokens_b):
    """Доля общих слов (мера Жаккара)."""
    if not tokens_a or not tokens_b:
        return 0.0
    common = len(tokens_a & tokens_b)
    return common / min(len(tokens_a), len(tokens_b))


def deduplicate(items, threshold, recent):
    """Оставляет по одной новости на сюжет — от источника с высшим доверием.
    Также убирает то, что уже отправлялось в прошлых запусках."""
    for item in items:
        item["tokens"] = title_tokens(item["title"])

    # 1) убрать совпадающее с уже отправленным ранее
    recent_sets = [set(r["tokens"]) for r in recent]
    not_repeated, repeated = [], []
    for item in items:
        if any(similarity(item["tokens"], rs) >= threshold for rs in recent_sets):
            repeated.append(item)
        else:
            not_repeated.append(item)

    # 2) сгруппировать похожие между собой, оставить сильнейший источник
    groups = []  # список списков
    for item in sorted(not_repeated, key=lambda i: -i["trust"]):
        placed = False
        for group in groups:
            if similarity(item["tokens"], group[0]["tokens"]) >= threshold:
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])

    winners = [g[0] for g in groups]  # первый в группе — с высшим trust
    losers = [i for g in groups for i in g[1:]]
    return winners, losers + repeated


# ---------- Gemini ----------

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

    strict = ""
    if item["source"] in config.get("strict_sources", []):
        strict = (
            "\nОСОБОЕ УКАЗАНИЕ ПО ЭТОМУ ИСТОЧНИКУ: он склонен к тенденциозной "
            "подаче и продвижению определённой позиции. Будь к нему намного "
            "строже обычного. Пропускай ТОЛЬКО сообщения о конкретных "
            "свершившихся фактах и событиях. Отклоняй (send: false) аналитику, "
            "колонки, мнения, прогнозы, а также материалы с оценочной или "
            "агитационной риторикой.\n"
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
{strict}
Будь строгим: если новость проходная, местечковая или интересна только
жителям одной страны и не входит в темы — send: false.

ЗАДАЧА:
1. Реши, подходит ли новость.
2. topic — ровно одно название темы из списка выше, либо BREAKING.
3. country — страна ИЛИ регион, ГДЕ ПРОИСХОДИТ СОБЫТИЕ, по-русски.
   Это НЕ страна издания. Примеры: «Израиль», «США», «Казахстан»,
   «Газа», «Китай». Если событие охватывает несколько стран — укажите
   главную или регион: «Ближний Восток», «ЕС», «Мир».
4. title_ru — заголовок на русском, до 10 слов, без точки в конце.
5. summary — пересказ на русском, 2-3 предложения своими словами.
   Не переводи дословно, не копируй фразы из оригинала, без вводных
   вроде «В статье говорится» и без упоминания названия издания.

Если новость не подходит — send: false, остальные поля пустые строки."""


def ask_gemini(item, config):
    """Спрашивает Gemini. Если модель перегружена — пробует следующую.
    Возвращает словарь, {} при сбое, None если исчерпан дневной лимит."""
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

    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                resp = requests.post(
                    gemini_url(model), headers=headers, json=payload, timeout=30
                )
            except Exception as e:
                print(f"[GEMINI/{model}] сеть: {type(e).__name__}")
                break  # к следующей модели

            if resp.status_code == 503:
                if attempt == 0:
                    time.sleep(5)
                    continue
                print(f"[GEMINI/{model}] перегружена, пробую следующую модель")
                break

            if resp.status_code == 429:
                print(f"[GEMINI/{model}] лимит модели исчерпан, пробую следующую")
                break

            if not resp.ok:
                print(f"[GEMINI/{model}] ошибка {resp.status_code}: {resp.text[:150]}")
                break

            try:
                raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(raw)
            except Exception as e:
                print(f"[GEMINI/{model}] не разобрал ответ: {e}")
                return {}

    print(f"[GEMINI] все модели недоступны, пропускаю «{item['title'][:45]}»")
    return {}


# ---------- Оформление ----------

def esc(text):
    return html.escape(str(text or ""), quote=False)


def format_message(item, data, emoji_map):
    topic = data.get("topic", "")
    emoji = emoji_map.get(topic, emoji_map.get("_default", "📰"))
    title = esc(data.get("title_ru", "")).strip()
    summary = esc(data.get("summary", "")).strip()

    signature = f"<i>{esc(item['source'])}</i>"
    event_country = (data.get("country") or "").strip()
    if event_country:
        signature += f"  ·  {esc(event_country)}"
    signature += f"  ·  <a href=\"{esc(item['link'])}\">Читать оригинал</a>"

    head = f"{emoji} <b>{title}</b>" if title else f"{emoji} <b>{esc(topic)}</b>"
    return f"{head}\n\n{summary}\n\n{signature}"


# ---------- Основной цикл ----------

def main():
    config = load_config()
    memory_hours = config.get("duplicate_memory_hours", 48)
    state = load_state()
    seen = set(state["seen"])
    new_ids = []
    sent = 0

    send_limit = config.get("max_per_run", 0) or 10**9
    ai_limit = config.get("max_ai_calls_per_run", 30)
    max_age = config.get("max_age_hours", 4)
    pause = config.get("seconds_between_ai_calls", 5)
    threshold = config.get("duplicate_threshold", 0.5)
    emoji_map = config.get("topic_emoji", {})
    patterns = build_keyword_patterns(config.get("keywords", []))

    # Сбор
    items = []
    for src in config.get("world_news_rss", []):
        items += fetch_rss(src)

    unseen = [i for i in items if i["id"] not in seen]
    print(f"Всего получено: {len(items)}, новых: {len(unseen)}")

    # ШАГ 1 — свежесть
    fresh = []
    for i in unseen:
        if is_fresh(i, max_age):
            fresh.append(i)
        else:
            new_ids.append(i["id"])
    print(f"Свежих (моложе {max_age} ч): {len(fresh)}")

    # ШАГ 2 — ключевые слова
    candidates = []
    for i in fresh:
        if passes_keywords(i, patterns):
            candidates.append(i)
        else:
            new_ids.append(i["id"])
    print(f"Прошло отсев по словам: {len(candidates)}")

    # ШАГ 3 — дедупликация
    unique, duplicates = deduplicate(candidates, threshold, state["recent"])
    for i in duplicates:
        new_ids.append(i["id"])
    print(f"После склейки дублей осталось: {len(unique)} (убрано {len(duplicates)})")

    # порядок: сначала самые авторитетные источники
    unique.sort(key=lambda i: -i["trust"])
    print(f"Пойдёт в Gemini: {min(len(unique), ai_limit)}")

    # ШАГ 4-5 — ИИ и отправка
    ai_used = 0
    for item in unique:
        if ai_used >= ai_limit or sent >= send_limit:
            break

        data = ask_gemini(item, config)
        ai_used += 1

        if data is None:
            break

        new_ids.append(item["id"])
        if data.get("send") and data.get("summary"):
            if send_telegram(format_message(item, data, emoji_map)):
                sent += 1
                state["recent"].append(
                    {"tokens": sorted(item["tokens"]), "ts": time.time()}
                )

        time.sleep(pause)

    state["seen"] = state["seen"] + new_ids
    save_state(state, memory_hours)
    print(f"Обращений к Gemini: {ai_used}. Отправлено сообщений: {sent}")


if __name__ == "__main__":
    main()
