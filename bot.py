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

from alerts import Alerts
from steam import SteamLinker, app_url
import releases
import wishlist

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "feeds.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")



def normalize_chat_id(raw):
    """Виправляє типові помилки: пробіли, t.me-посилання, назва без @."""
    value = (raw or "").strip().strip('"').strip("'").strip()
    m = re.search(r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([A-Za-z0-9_]{4,})", value)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-?\d+", value):
        return value
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", value):
        return "@" + value
    return value


def describe_chat_id(value):
    """Опис формату без розкриття самого значення."""
    if not value:
        return "порожньо"
    if value.startswith("@"):
        return f"публічна назва (@ + {len(value) - 1} символів)"
    if value.startswith("-100") and value[1:].isdigit():
        return f"id каналу (-100…, {len(value)} символів)"
    if value.lstrip("-").isdigit():
        return f"числовий id без -100 ({len(value)} символів) — для каналу потрібен формат -100…"
    return f"незвичний формат ({len(value)} символів)"


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = normalize_chat_id(os.environ.get("TELEGRAM_CHANNEL_ID", ""))
# куди слати пости про вішліст (якщо не задано — у той самий канал)
WISHLIST_CHAT_ID = normalize_chat_id(os.environ.get("TELEGRAM_WISHLIST_CHAT_ID", "")) or CHANNEL_ID
ADMIN_ID = normalize_chat_id(os.environ.get("TELEGRAM_ADMIN_ID", ""))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_RELEASES = os.environ.get("FORCE_RELEASES", "").lower() in ("1", "true", "yes")

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
            url = m.get("url") or ""
            kind = (m.get("type") or m.get("medium") or "").lower()
            if not url or "video" in kind or "audio" in kind:
                continue
            if re.search(r"youtube\.com|youtu\.be|vimeo\.com|\.(mp4|mp3|webm)(\?|$)", url, re.I):
                continue
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
# Перекладачі по черзі. DeepL (якщо задано секрет DEEPL_API_KEY) — основний,
# далі Google і MyMemory як запасні. Перекладач, який відмовив через ліміт
# або блокування, пропускається до кінця запуску, щоб не гаяти час.

from deep_translator import MyMemoryTranslator

DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
_google_page = GoogleTranslator(source="auto", target="uk")
_mymemory = MyMemoryTranslator(source="english", target="ukrainian")
_last_call = 0.0
_disabled = {}  # назва перекладача -> причина
TR_STATS = {"ok": set(), "failed": 0}


def _deepl(text):
    host = "api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "api.deepl.com"
    r = requests.post(
        f"https://{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
        data={"text": text, "target_lang": "UK"},
        timeout=30,
    )
    if r.status_code == 456:
        raise RuntimeError("quota exceeded (456): місячний ліміт DeepL вичерпано")
    if r.status_code == 403:
        raise RuntimeError("403: ключ DeepL недійсний")
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


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


TRANSLATORS = ([("DeepL", _deepl)] if DEEPL_API_KEY else []) + [
    ("Google API", _google_api),
    ("Google", lambda t: _google_page.translate(t)),
    ("MyMemory", lambda t: _mymemory.translate(t[:480])),
]

UK_ONLY_LETTERS = set("іїєґІЇЄҐ")


def is_ukrainian(text):
    """Текст уже українською: переважно кирилиця і є суто українські літери (і, ї, є, ґ)."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    return cyrillic >= len(letters) * 0.5 and any(c in UK_ONLY_LETTERS for c in text)


BLOCKING_ERRORS = ("429", "too many requests", "quota", "403", "limit")


def translate(text):
    global _last_call
    if not text:
        return ""
    text = text[:4500]
    for attempt in range(2):
        for name, fn in TRANSLATORS:
            if name in _disabled:
                continue
            # не частіше ~3 запитів на секунду
            wait = 0.35 - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
            try:
                result = fn(text)
                if result and result.strip():
                    TR_STATS["ok"].add(name)
                    return result
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if any(k in msg.lower() for k in BLOCKING_ERRORS):
                    _disabled[name] = msg[:200]
                    log.info("Переклад через %s недоступний до кінця запуску: %s", name, msg[:120])
                else:
                    log.info("Переклад через %s не вдався: %s", name, msg[:120])
        time.sleep(5 * (attempt + 1))
    TR_STATS["failed"] += 1
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


def linked_title(title, games, tr=None):
    """
    Перекладає заголовок і робить назви ігор посиланнями на Steam.
    Назви замінюються мітками перед перекладом, щоб перекладач їх не чіпав.
    Повертає (html_заголовка, [ігри, які не вдалося вставити в заголовок]).
    """
    esc = html.escape
    tr = tr or translate
    if not games:
        return esc(tr(title)), []

    def link(text, app_id):
        return f'<a href="{app_url(app_id)}">{esc(text)}</a>'

    marked = title
    for n, (s, e, _, _) in reversed(list(enumerate(games))):
        marked = marked[:s] + PLACEHOLDER.format(n) + marked[e:]
    translated = tr(marked)

    if all(placeholder_re(n).search(translated) for n in range(len(games))):
        out = esc(translated)
        for n, (s, e, app_id, _) in enumerate(games):
            out = placeholder_re(n).sub(lambda _m: link(title[s:e], app_id), out, count=1)
        return out, []

    # запасний варіант: звичайний переклад і пошук назви в ньому
    out = esc(tr(title))
    leftovers = []
    for s, e, app_id, name in games:
        text = title[s:e]
        rx = re.compile(r"(?<![\w>])" + re.escape(esc(text)) + r"(?![\w<])", re.I)
        if rx.search(out):
            out = rx.sub(lambda m, a=app_id: f'<a href="{app_url(a)}">{m.group(0)}</a>', out, count=1)
        else:
            leftovers.append((text, app_id))
    return out, leftovers


def discover_channels():
    """Канали, які бот бачив в оновленнях (додавання адміном, нові пости)."""
    found = {}
    try:
        updates = tg(
            "getUpdates",
            allowed_updates=json.dumps(["channel_post", "my_chat_member", "edited_channel_post"]),
            limit=100,
        )["result"]
    except Exception as e:  # noqa: BLE001
        log.info("getUpdates недоступний: %s", e)
        return found
    for u in updates:
        for key in ("channel_post", "edited_channel_post", "my_chat_member"):
            chat = (u.get(key) or {}).get("chat") or {}
            if chat.get("type") == "channel":
                found[str(chat["id"])] = chat.get("title") or chat.get("username") or "?"
    return found


def check_telegram():
    """Перевіряє токен і доступ до каналу до того, як щось перекладати."""
    global CHANNEL_ID
    try:
        me = tg("getMe")["result"]
        log.info("Telegram: бот @%s підключено", me.get("username"))
    except Exception as e:  # noqa: BLE001
        log.error("Telegram: токен не працює (%s). Перевір TELEGRAM_BOT_TOKEN.", e)
        return False
    for label, chat in (("канал", CHANNEL_ID), ("вішліст", WISHLIST_CHAT_ID)):
        if label == "вішліст" and chat == CHANNEL_ID:
            continue
        try:
            try:
                info = tg("getChat", chat_id=chat)["result"]
            except Exception:
                # частая помилка: id каналу скопійовано без префікса -100
                if label != "канал" or not chat.isdigit():
                    raise
                info = tg("getChat", chat_id="-100" + chat)["result"]
                chat = "-100" + chat
                CHANNEL_ID = chat
                log.info("Telegram: до id каналу додано префікс -100")
            log.info("Telegram: %s знайдено — «%s» (%s)", label, info.get("title"), info.get("type"))
            member = tg("getChatMember", chat_id=chat, user_id=me["id"])["result"]
            if member.get("status") not in ("administrator", "creator") and info.get("type") == "channel":
                log.error("Telegram: бот не адміністратор у каналі «%s» — додай його з правом публікації", info.get("title"))
                return False
        except Exception as e:  # noqa: BLE001
            log.error(
                "Telegram: %s не знайдено (%s). Формат значення: %s. "
                "Переконайся, що бот доданий у канал адміністратором; для приватного каналу потрібен id -100…",
                label, e, describe_chat_id(chat),
            )
            channels = discover_channels()
            if channels:
                for cid, title in channels.items():
                    log.error("👉 Бот бачить канал «%s» з id %s — встав це число в секрет TELEGRAM_CHANNEL_ID", title, cid)
            else:
                log.error(
                    "👉 Бот поки не бачить жодного каналу. Додай бота адміністратором каналу "
                    "(або прибери й додай знову), опублікуй у каналі будь-який пост і запусти бота ще раз — "
                    "тут з'явиться правильний id."
                )
            return False
    return True


def send_text(text, chat_id=None):
    """Текстовий пост без картинки (для «Релізів тижня»)."""
    if DRY_RUN:
        print("\n" + "=" * 60 + f"\n{text}\n")
        return
    tg(
        "sendMessage",
        chat_id=chat_id or CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        link_preview_options=json.dumps({"is_disabled": True}),
    )


def notify_admin(text):
    """Особисте повідомлення власнику про поломку."""
    if DRY_RUN:
        print(f"\n[АДМІНУ] {text}\n")
        return
    send_text(text, chat_id=ADMIN_ID)


def report_health(alerts, pub):
    """Сповіщення про переклад і публікацію за підсумками запуску."""
    if "DeepL" in _disabled:
        alerts.problem(
            "deepl", "DeepL більше не перекладає",
            f"{_disabled['DeepL']}\nПоки що перекладаю запасними сервісами (якість може бути гіршою). "
            "Безкоштовний ліміт DeepL оновлюється щомісяця.",
        )
    elif "DeepL" in TR_STATS["ok"]:
        alerts.ok("deepl")

    if TR_STATS["failed"] and not TR_STATS["ok"]:
        alerts.problem(
            "translate", "Переклад не працює",
            "Жоден перекладач не відповів — англомовні новини чекають наступного запуску.",
            after_hours=1, repeat_hours=12,
        )
    elif TR_STATS["ok"]:
        alerts.ok("translate")

    if pub["failed"] and not pub["ok"]:
        alerts.problem("publish", "Не вдається публікувати в канал", pub["error"], repeat_hours=6)
    elif pub["ok"]:
        alerts.ok("publish")


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


# ---------- джерела без RSS (читання сторінки сайту) ----------

FEED_HEADERS = {
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}
READER_UA = "AlegzhaNewsBot/1.0 (RSS reader; +https://github.com/alegzha/AlegzhaNews)"
_source_cache = {}
FETCH_PROXY = ""  # задається в feeds.yaml (fetch_proxy) — адреса /feed воркера Cloudflare


def fetch_source(url):
    """
    Завантажує стрічку чи сторінку джерела (один раз за запуск, навіть якщо джерело
    в кількох рубриках). Якщо сайт відмовляє (403/429) — повторює з іншими заголовками:
    деякі сайти не пускають «браузер» із серверів, але пускають RSS-читалки.
    """
    if url in _source_cache:
        cached = _source_cache[url]
        if isinstance(cached, Exception):
            raise cached
        return cached
    attempts = [
        dict(FEED_HEADERS),
        {**FEED_HEADERS, "User-Agent": READER_UA},
        {"User-Agent": READER_UA, "Accept": "*/*"},
    ]
    last = None
    for i, headers in enumerate(attempts):
        if i:
            time.sleep(2)
        r = HTTP.get(url, headers=headers, timeout=20)
        if r.status_code not in (401, 403, 429, 503):
            r.raise_for_status()
            if i:
                log.info("↪ %s відкрився з %d-ї спроби", url, i + 1)
            _source_cache[url] = r
            return r
        last = r
    if FETCH_PROXY:
        try:
            r = HTTP.get(FETCH_PROXY, params={"url": url}, timeout=30)
            r.raise_for_status()
            log.info("↪ %s відкрився через воркер Cloudflare", url)
            _source_cache[url] = r
            return r
        except Exception as e:  # noqa: BLE001
            log.info("Воркер теж не відкрив %s: %s", url, e)
    try:
        last.raise_for_status()
    except Exception as e:
        _source_cache[url] = e  # не стукаємо вдруге за запуск
        raise


class HtmlEntry(dict):
    """Схоже на запис feedparser, щоб решта коду працювала однаково."""


def fetch_html_feed(feed):
    """
    Збирає новини зі сторінки: посилання, що відповідають link_pattern.
    Картинку й опис бере зі сторінки самої новини (og:image, og:description).
    """
    r = fetch_source(feed["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    pattern = re.compile(feed["link_pattern"])
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(feed["url"], a["href"])
        title = a.get_text(" ", strip=True)
        if not pattern.search(href) or len(title) < 15 or href in seen:
            continue
        seen.add(href)
        items.append(HtmlEntry(id=href, link=href, title=title, summary="", html_source=True))
        if len(items) >= 20:
            break
    return items


def enrich_html_entry(entry):
    """Опис і картинка зі сторінки новини (лише для тих, що реально публікуємо)."""
    try:
        r = HTTP.get(entry["link"], timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def meta(prop):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            return (tag.get("content") or "").strip() if tag else ""

        entry["summary"] = meta("og:description") or meta("description")
        image = meta("og:image")
        if image:
            entry["media_content"] = [{"url": requests.compat.urljoin(entry["link"], image)}]
    except Exception as e:  # noqa: BLE001
        log.info("Не вдалося прочитати сторінку новини %s: %s", entry.get("link"), e)


def entry_tags(entry):
    return {(t.get("term") or "").strip().lower() for t in entry.get("tags", []) or []}


# ---------- головний цикл ----------

def collect(config, state, alerts):
    limits = config["limits"]
    max_age = limits["max_age_hours"] * 3600
    skip = [k.lower() for k in config.get("filters", {}).get("skip_keywords", [])]
    now = time.time()
    candidates = []

    known_sources = state.setdefault("sources", [])
    working_urls = state.setdefault("source_urls", {})
    tag_hints = state.setdefault("tag_hints", {})
    active = set()

    for cat_id, cat in config["categories"].items():
        for feed in cat["feeds"]:
            source_id = f'{cat_id}|{feed["name"]}|{feed["url"]}'
            alert_key = "src:" + source_id
            active.add(alert_key)
            down = f"Джерело «{feed['name']}» не працює"
            entries, error = [], None
            # alt_urls — запасні адреси стрічки, якщо сайт її переніс
            urls = [feed["url"]] + list(feed.get("alt_urls") or [])
            known_good = working_urls.get(feed["url"])
            if known_good in urls:
                urls.remove(known_good)
                urls.insert(0, known_good)
            for u in urls:
                try:
                    if feed.get("type") == "html":
                        entries = fetch_html_feed({**feed, "url": u})
                    else:
                        entries = feedparser.parse(fetch_source(u).content).entries
                except Exception as e:  # noqa: BLE001
                    error = e
                    continue
                if entries:
                    if u != feed["url"] or feed.get("alt_urls"):
                        working_urls[feed["url"]] = u
                    break
            if error and not entries:
                e = error
                log.warning("❌ %s: не вдалося завантажити стрічку (%s)", feed["name"], e)
                alerts.problem(alert_key, down, f"{feed['url']}\n{e}", after_hours=12)
                continue
            if not entries:
                log.warning("❌ %s: стрічка порожня або зламана", feed["name"])
                alerts.problem(
                    alert_key, down,
                    f"{feed['url']}\nСтрічка порожня — можливо, сайт змінив адресу RSS або верстку.",
                    after_hours=12,
                )
                continue
            alerts.ok(alert_key)

            include = {t.lower() for t in feed.get("include_tags", [])}
            exclude = {t.lower() for t in feed.get("exclude_tags", [])}

            # Нове джерело без дат (сторінка сайту): усе, що там зараз є, вважаємо старим,
            # щоб не завалити канал архівом. Публікуватимемо лише те, що з'явиться далі.
            if feed.get("type") == "html" and source_id not in known_sources:
                for entry in entries:
                    state["seen"][item_key(entry)] = now
                known_sources.append(source_id)
                log.info("🆕 %s: нове джерело, запам'ятав %d поточних новин", feed["name"], len(entries))
                continue
            if source_id not in known_sources:
                known_sources.append(source_id)

            if include and not any(entry_tags(e) & include for e in entries):
                all_tags = sorted({t for e in entries for t in entry_tags(e)})
                log.info(
                    "ℹ️ %s: жодної статті з тегами %s. Теги в стрічці: %s",
                    feed["name"], ", ".join(feed["include_tags"]), ", ".join(all_tags[:60]) or "—",
                )
                tag_hints[feed["name"]] = all_tags[:80]

            fresh = []
            for entry in entries:
                key = item_key(entry)
                if key in state["seen"]:
                    continue
                if include or exclude:
                    tags = entry_tags(entry)
                    if (include and not tags & include) or (tags & exclude):
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
                        "lang": feed.get("lang", ""),
                    }
                )
            fresh.sort(key=lambda c: c["ts"], reverse=True)
            candidates.extend(fresh[: limits["max_per_source_per_run"]])
            log.info("✅ %s: нових %d", feed["name"], len(fresh))

    alerts.ok_prefix("src:", active)  # джерела, які прибрали з feeds.yaml
    candidates.sort(key=lambda c: c["ts"], reverse=True)
    return candidates


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHANNEL_ID):
        sys.exit("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHANNEL_ID")
    if not DRY_RUN and not check_telegram():
        sys.exit("Telegram не налаштований — дивись помилку вище")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    limits = config["limits"]
    global FETCH_PROXY
    FETCH_PROXY = (config.get("fetch_proxy") or "").strip()
    state, first_run = load_state()
    alerts = Alerts(state, notify_admin if (ADMIN_ID or DRY_RUN) else None)
    pub = {"ok": 0, "failed": 0, "error": ""}

    def post(*args, **kwargs):
        try:
            publish(*args, **kwargs)
            pub["ok"] += 1
        except Exception as e:
            pub["failed"] += 1
            pub["error"] = str(e)
            raise

    candidates = collect(config, state, alerts)
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
        if any(s["key"] == c["key"] for s in selected):
            continue
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
        if entry.get("html_source"):
            enrich_html_entry(entry)
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
        already_uk = c.get("lang") == "uk" or is_ukrainian(c["title"])
        tr = (lambda t: t) if already_uk else translate
        try:
            title_html, extra_games = linked_title(c["title"], games, tr)
            summary_uk = tr(summary) if summary else ""
        except RuntimeError:
            log.warning("Пропускаю до наступного запуску (переклад): %s", c["title"])
            continue

        image = image_from_entry(entry) or image_from_page(link)
        caption = build_caption(c["cat"], title_html, summary_uk, c["source"], link, extra_games)
        try:
            post(caption, image, link)
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
        before = state.get("wishlist", {}).get("last_check", 0)
        try:
            posted += wishlist.check(
                HTTP,
                state,
                wl_cfg,
                lambda text, img, url: post(text, img, url, chat_id=WISHLIST_CHAT_ID),
                html.escape,
                force=FORCE_RELEASES,
            )
            if state["wishlist"].get("last_check", 0) != before:
                alerts.ok("wishlist")
        except Exception as e:  # noqa: BLE001
            log.error("Перевірка вішліста не вдалася: %s", e)
            alerts.problem("wishlist", "Вішліст Steam недоступний", str(e), after_hours=6)

    rel_cfg = config.get("weekly_releases") or {}
    if rel_cfg.get("enabled") or FORCE_RELEASES:
        try:
            posted += releases.run(
                HTTP, state, rel_cfg, (wl_cfg.get("country") or "UA"),
                send_text, html.escape, force=FORCE_RELEASES,
            )
            alerts.ok("releases")
        except Exception as e:  # noqa: BLE001
            log.error("Релізи тижня не вдалися: %s", e)
            alerts.problem("releases", "Не вдалося опублікувати «Релізи тижня»", str(e), repeat_hours=6)

    report_health(alerts, pub)
    save_state(state)
    log.info("Готово: опубліковано %d", posted)


if __name__ == "__main__":
    main()
