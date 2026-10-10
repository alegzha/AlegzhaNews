"""
Telegram-агрегатор новин: ігри, важка/альтернативна музика, кіно й серіали.
Збирає RSS, перекладає українською, публікує окремими постами з картинкою.
Запускається за розкладом (GitHub Actions, кожні 30 хв).
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from calendar import timegm

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from steam import SteamLinker, app_url
import wishlist

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "feeds.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
# куди слати пости про вішліст (якщо не задано — у той самий канал)
WISHLIST_CHAT_ID = os.environ.get("TELEGRAM_WISHLIST_CHAT_ID", "") or CHANNEL_ID
DRY_RUN = os.environ.get("DRY_RUN") == "1"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA})

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("news-bot")

SEEN_TTL_DAYS = 14
TITLE_MEMORY_HOURS = 48
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at",
    "by", "from", "is", "are", "his", "her", "their", "new", "after", "as",
    "watch", "listen", "hear", "video", "announces", "reveals", "releases",
}


# ---------- стан ----------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"seen": {}, "titles": []}, True
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f), False


def save_state(state):
    now = time.time()
    state["seen"] = {
        k: v for k, v in state["seen"].items() if now - v < SEEN_TTL_DAYS * 86400
    }
    state["titles"] = [
        t for t in state["titles"] if now - t[0] < TITLE_MEMORY_HOURS * 3600
    ]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def item_key(entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:16]


# ---------- схожі заголовки (одна новина з різних сайтів) ----------

def title_tokens(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def is_duplicate_story(title, recent_titles):
    tokens = title_tokens(title)
    if len(tokens) < 3:
        return False
    for _, other in recent_titles:
        other_tokens = title_tokens(other)
        if not other_tokens:
            continue
        overlap = len(tokens & other_tokens) / min(len(tokens), len(other_tokens))
        if overlap >= 0.7:
            return True
    return False


# ---------- текст і картинки ----------

BOILERPLATE = [
    re.compile(r"The post .*? appeared first on .*?\.?$", re.S),
    re.compile(r"\(as transcribed by BLABBERMOUTH\.NET\)", re.I),
]


def clean_text(raw_html):
    soup = BeautifulSoup(raw_html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    for rx in BOILERPLATE:
        text = rx.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # обрізаємо по кінцю речення, якщо можливо
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def image_from_entry(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url and not url.lower().endswith((".mp4", ".mp3")):
                return url
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            return enc["href"]
    for field in ("summary", "content"):
        value = entry.get(field)
        if isinstance(value, list):
            value = " ".join(v.get("value", "") for v in value)
        if value:
            img = BeautifulSoup(value, "html.parser").find("img")
            if img and img.get("src", "").startswith("http"):
                return img["src"]
    return None


def image_from_page(url):
    try:
        r = HTTP.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for prop in ("og:image", "twitter:image"):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop}
            )
            if tag and tag.get("content", "").startswith("http"):
                return tag["content"]
    except Exception as e:  # noqa: BLE001
        log.info("og:image не знайдено для %s: %s", url, e)
    return None


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return timegm(t)
    return None


# ---------- переклад ----------
# Кілька перекладачів по черзі: якщо один заблокований або перевантажений,
# пробуємо наступний. Google через translate.googleapis.com (client=gtx) зазвичай
# працює і з серверів GitHub, на відміну від сторінкової версії.

from deep_translator import MyMemoryTranslator

_google_page = GoogleTranslator(source="auto", target="uk")
_mymemory = MyMemoryTranslator(source="english", target="ukrainian")
_last_call = 0.0


def _google_api(text):
    r = HTTP.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "uk", "dt": "t", "q": text},
        timeout=20,
    )
    r.raise_for_status()
    parts = r.json()[0] or []
    out = "".join(p[0] for p in parts if p and p[0])
    if not out:
        raise RuntimeError("empty translation")
    return out


TRANSLATORS = [
    ("Google API", _google_api),
    ("Google", lambda t: _google_page.translate(t)),
    ("MyMemory", lambda t: _mymemory.translate(t[:480])),
]


def translate(text):
    global _last_call
    if not text:
        return ""
    text = text[:4500]
    for attempt in range(2):
        for name, fn in TRANSLATORS:
            # не частіше ~3 запитів на секунду
            wait = 0.35 - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
            try:
                result = fn(text)
                if result and result.strip():
                    return result
            except Exception as e:  # noqa: BLE001
                log.info("Переклад через %s не вдався: %s", name, str(e)[:120])
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("translation failed")


# ---------- Telegram ----------

def tg(method, **payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    files = payload.pop("files", None)
    for attempt in range(3):
        r = requests.post(url, data=payload, files=files, timeout=60)
        data = r.json()
        if data.get("ok"):
            return data
        retry = data.get("parameters", {}).get("retry_after")
        if retry:
            log.info("Telegram просить зачекати %s с", retry)
            time.sleep(retry + 1)
            continue
        raise RuntimeError(data.get("description", "unknown Telegram error"))
    raise RuntimeError("Telegram rate limit")


def publish(caption, image_url, link, chat_id=None):
    if DRY_RUN:
        print("\n" + "=" * 60 + f"\n[IMG] {image_url}\n{caption}\n")
        return
    common = {"chat_id": chat_id or CHANNEL_ID, "parse_mode": "HTML"}
    if image_url:
        try:
            tg("sendPhoto", photo=image_url, caption=caption, **common)
            return
        except Exception as e:  # noqa: BLE001
            log.info("Telegram не взяв картинку за посиланням (%s), завантажую сам", e)
        try:
            img = HTTP.get(image_url, timeout=20)
            img.raise_for_status()
            tg(
                "sendPhoto",
                caption=caption,
                files={"photo": ("image.jpg", img.content)},
                **common,
            )
            return
        except Exception as e:  # noqa: BLE001
            log.info("Картинку не вдалося відправити (%s), публікую текстом", e)
    tg(
        "sendMessage",
        text=caption,
        link_preview_options=json.dumps({"url": link, "prefer_large_media": True}),
        **common,
    )


PLACEHOLDER = "QQ{}QQ"


def placeholder_re(n):
    return re.compile(r"Q\s*Q\s*" + str(n) + r"\s*Q\s*Q", re.I)


def linked_title(title, games):
    """
    Перекладає заголовок і робить назви ігор посиланнями на Steam.
    Назви замінюються мітками перед перекладом, щоб перекладач їх не чіпав.
    Повертає (html_заголовка, [ігри, які не вдалося вставити в заголовок]).
    """
    esc = html.escape
    if not games:
        return esc(translate(title)), []

    def link(text, app_id):
        return f'<a href="{app_url(app_id)}">{esc(text)}</a>'

    marked = title
    for n, (s, e, _, _) in reversed(list(enumerate(games))):
        marked = marked[:s] + PLACEHOLDER.format(n) + marked[e:]
    translated = translate(marked)

    if all(placeholder_re(n).search(translated) for n in range(len(games))):
        out = esc(translated)
        for n, (s, e, app_id, _) in enumerate(games):
            out = placeholder_re(n).sub(lambda _m: link(title[s:e], app_id), out, count=1)
        return out, []

    # запасний варіант: звичайний переклад і пошук назви в ньому
    out = esc(translate(title))
    leftovers = []
    for s, e, app_id, name in games:
        text = title[s:e]
        rx = re.compile(r"(?<![\w>])" + re.escape(esc(text)) + r"(?![\w<])", re.I)
        if rx.search(out):
            out = rx.sub(lambda m, a=app_id: f'<a href="{app_url(a)}">{m.group(0)}</a>', out, count=1)
        else:
            leftovers.append((text, app_id))
    return out, leftovers


def build_caption(cat, title_html, summary_uk, source, link, extra_games=()):
    esc = html.escape
    parts = [f"{cat['emoji']} <b>{title_html}</b>"]
    if extra_games:
        links = ", ".join(f'<a href="{app_url(a)}">{esc(t)}</a>' for t, a in extra_games)
        parts[0] += f"\n🕹 Steam: {links}"
    if summary_uk:
        parts.append(esc(summary_uk))
    parts.append(f'🔗 <a href="{esc(link, quote=True)}">{esc(source)}</a>')
    parts.append(cat["hashtag"])
    caption = "\n\n".join(parts)
    # ліміт підпису до фото в Telegram — 1024 символи (рахуються лише видимі символи,
    # але для простоти рахуємо з розміткою — так гарантовано влізе)
    if len(caption) > 1024 and summary_uk:
        overflow = len(caption) - 1024 + 2
        return build_caption(cat, title_html, summary_uk[:-overflow] + "…", source, link, extra_games)
    return caption


# ---------- головний цикл ----------

def collect(config, state):
    limits = config["limits"]
    max_age = limits["max_age_hours"] * 3600
    skip = [k.lower() for k in config.get("filters", {}).get("skip_keywords", [])]
    now = time.time()
    candidates = []

    for cat_id, cat in config["categories"].items():
        for feed in cat["feeds"]:
            try:
                resp = HTTP.get(feed["url"], timeout=20)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as e:  # noqa: BLE001
                log.warning("❌ %s: не вдалося завантажити стрічку (%s)", feed["name"], e)
                continue
            if not parsed.entries:
                log.warning("❌ %s: стрічка порожня або зламана", feed["name"])
                continue
            fresh = []
            for entry in parsed.entries:
                key = item_key(entry)
                if key in state["seen"]:
                    continue
                ts = entry_time(entry) or now
                title = clean_text(entry.get("title", ""))
                if not title or now - ts > max_age:
                    continue
                if any(k in title.lower() for k in skip):
                    state["seen"][key] = now
                    continue
                fresh.append(
                    {
                        "key": key,
                        "ts": ts,
                        "title": title,
                        "entry": entry,
                        "cat": cat,
                        "cat_id": cat_id,
                        "source": feed["name"],
                    }
                )
            fresh.sort(key=lambda c: c["ts"], reverse=True)
            candidates.extend(fresh[: limits["max_per_source_per_run"]])
            log.info("✅ %s: нових %d", feed["name"], len(fresh))

    candidates.sort(key=lambda c: c["ts"], reverse=True)
    return candidates


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHANNEL_ID):
        sys.exit("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHANNEL_ID")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    limits = config["limits"]
    state, first_run = load_state()
    candidates = collect(config, state)
    steam = SteamLinker(HTTP, state, config.get("filters", {}).get("steam_ignore", []))

    budget = limits["max_posts_per_run"]
    if first_run:
        # Перший запуск: публікуємо лише кілька найсвіжіших, решту позначаємо як бачені,
        # щоб не завалити канал старими новинами.
        budget = min(budget, 5)
        log.info("Перший запуск — опублікую до %d новин", budget)

    selected, posted = [], 0
    for c in candidates:
        if len(selected) >= budget:
            break
        if is_duplicate_story(c["title"], state["titles"] + [(0, s["title"]) for s in selected]):
            log.info("↪ дубль іншої новини, пропускаю: %s", c["title"])
            state["seen"][c["key"]] = time.time()
            continue
        selected.append(c)

    if first_run:
        for c in candidates:
            if c not in selected:
                state["seen"][c["key"]] = time.time()

    # публікуємо від старішої до новішої, щоб у каналі був хронологічний порядок
    for c in reversed(selected):
        entry = c["entry"]
        link = entry.get("link", "")
        summary = clean_text(entry.get("summary", ""))
        if summary.lower().startswith(c["title"].lower()[:40]):
            summary = ""
        summary = shorten(summary, limits["summary_chars"])
        games = []
        if c["cat"].get("steam_links"):
            article = entry.get("summary", "") + " ".join(
                part.get("value", "") for part in entry.get("content", []) or []
            )
            try:
                games = steam.find_games(c["title"], article)
            except Exception as e:  # noqa: BLE001
                log.warning("Пошук у Steam не вдався: %s", e)
            if games:
                log.info("🕹 Steam: %s", ", ".join(g[3] for g in games))
        try:
            title_html, extra_games = linked_title(c["title"], games)
            summary_uk = translate(summary) if summary else ""
        except RuntimeError:
            log.warning("Пропускаю до наступного запуску (переклад): %s", c["title"])
            continue

        image = image_from_entry(entry) or image_from_page(link)
        caption = build_caption(c["cat"], title_html, summary_uk, c["source"], link, extra_games)
        try:
            publish(caption, image, link)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося опублікувати «%s»: %s", c["title"], e)
            continue

        state["seen"][c["key"]] = time.time()
        state["titles"].append([time.time(), c["title"]])
        posted += 1
        log.info("📤 %s — %s", c["source"], c["title"])
        time.sleep(3)  # не частіше ~20 повідомлень на хвилину в канал

    wl_cfg = config.get("wishlist") or {}
    if wl_cfg.get("enabled") and wl_cfg.get("profile"):
        try:
            posted += wishlist.check(
                HTTP,
                state,
                wl_cfg,
                lambda text, img, url: publish(text, img, url, chat_id=WISHLIST_CHAT_ID),
                html.escape,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Перевірка вішліста впала: %s", e)

    save_state(state)
    log.info("Готово: опубліковано %d", posted)


if __name__ == "__main__":
    main()
