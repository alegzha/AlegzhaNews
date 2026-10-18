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
from datetime import datetime, timedelta

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from alerts import Alerts
from steam import SteamLinker, app_url
import freebies
import releases
import spotify
import ukrainian
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
    state["titles_uk"] = [
        t for t in state.get("titles_uk", []) if now - t[0] < TITLE_MEMORY_HOURS * 3600
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


UK_STOP = {
    "про", "для", "після", "через", "його", "їхній", "свій", "своє", "своїх", "який", "яка", "які",
    "що", "як", "вже", "ще", "але", "або", "це", "цей", "ця", "ці", "від", "при", "над", "під",
    "без", "буде", "може", "стала", "став", "нова", "новий", "нові", "нового", "нову",
}


def any_tokens(text):
    """Ключові слова будь-якою мовою: кириличні слова обрізаємо до 5 літер (грубий корінь),
    латиницю й числа лишаємо як є — назви в перекладах зазвичай не змінюються."""
    out = set()
    for w in re.findall(r"[a-zа-яіїєґ0-9']+", (text or "").lower()):
        if w in STOPWORDS or w in UK_STOP or (len(w) < 3 and not w.isdigit()):
            continue
        out.add(w[:5] if re.search(r"[а-яіїєґ]", w) else w)
    return out


def is_duplicate_published(title_uk, recent):
    """Та сама новина вже була в каналі (порівнюємо українські заголовки, тож ловимо
    повтори між мовами й між українськими сайтами)."""
    tokens = any_tokens(title_uk)
    if len(tokens) < 3:
        return False
    for _, other in recent:
        other_tokens = any_tokens(other)
        if len(other_tokens) < 3:
            continue
        shared = len(tokens & other_tokens)
        if shared >= 3 and shared / min(len(tokens), len(other_tokens)) >= 0.6:
            return True
    return False


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


_page_cache = {}


def fetch_page(url):
    """HTML сторінки статті (один раз за запуск: потрібен і для картинки, і для тексту)."""
    if url not in _page_cache:
        try:
            r = HTTP.get(url, timeout=15)
            r.raise_for_status()
            _page_cache[url] = r.text
        except Exception as e:  # noqa: BLE001
            log.info("Сторінка статті недоступна %s: %s", url, e)
            _page_cache[url] = ""
    return _page_cache[url]


def article_text(url, limit=3000):
    """Основний текст статті: абзаци з <article>/<main>, без меню, підписів і реклами."""
    raw = fetch_page(url)
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure", "form", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts, total = [], 0
    for p in root.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) < 60 or re.search(r"(?i)cookie|subscribe|newsletter|sign up|affiliate|©", t):
            continue
        parts.append(t)
        total += len(t)
        if total >= limit:
            break
    return " ".join(parts)[:limit]


def image_from_page(url):
    try:
        raw = fetch_page(url)
        if not raw:
            return None
        soup = BeautifulSoup(raw, "html.parser")
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


# ---------- Gemini (основний перекладач, якщо задано секрет GEMINI_API_KEY) ----------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Порядок моделей: спершу якісніша Flash; коли її безкоштовний ліміт закінчиться — Flash-Lite.
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
GEMINI_PROMPT = """Ти — досвідчений редактор українського новинного Telegram-каналу про відеоігри, \
важку й альтернативну музику, кіно, серіали, софт і штучний інтелект.

Отримуєш JSON-масив рядків англійською: перший — заголовок новини, другий (якщо є) — її короткий опис.
Перепиши кожен рядок природною сучасною українською, як це зробило б українське медіа (DOU, Mezha, \
PlayUA, ITC). Передавай зміст, а не слова: не калькуй англійський порядок слів і звороти.

Правила:
- Заголовок — стислий (до 100 символів), живий, у теперішньому або минулому часі, без клікбейту \
й без крапки в кінці. Другорядні деталі — в опис, не в заголовок.
- Назви ігор, гуртів, альбомів, пісень, фільмів, серіалів, компаній, продуктів, а також імена \
персонажів залишай латиницею точно як в оригіналі. Імена й прізвища людей пиши українською \
транслітерацією (Hideo Kojima → Хідео Кодзіма, Dan Houser → Ден Гаузер).
- Не вигадуй фактів і не додавай нічого, чого немає в оригіналі. Суми й числа не змінюй ($150 → $150).
- Жодних русизмів і канцеляриту: «брати участь» (не «приймати»), «протягом» (не «на протязі»), \
«наступний» (не «слідуючий»), «є» замість «являється», «вражаючий» лише доречно.
- Терміни: trailer — трейлер, teaser — тизер, gameplay — геймплей, early access — ранній доступ, \
patch — патч, update — оновлення, remaster — ремастер, remake — ремейк, reboot — перезапуск, \
spin-off — спінофф, publisher — видавець, developer — розробник, layoffs — звільнення, \
review — рецензія, release date — дата виходу, single — сингл, album — альбом, EP — EP, \
music video — кліп, tour — тур, lineup — склад учасників, headliner — хедлайнер, \
cover — кавер, box office — касові збори, showrunner — шоуранер, cast — акторський склад, \
season — сезон, AI — ШІ.

Відповідь — лише JSON-масив рядків тієї ж довжини й у тому ж порядку, без пояснень."""
_gemini_model = None
_gemini_exhausted = set()  # моделі, чий денний ліміт закінчився в цьому запуску


def gemini_json(system, payload):
    """Запит до Gemini з JSON-відповіддю. Перебирає моделі (Flash → Flash-Lite) при вичерпаному ліміті."""
    global _gemini_model
    last = None
    for model in [m for m in GEMINI_MODELS if m not in _gemini_exhausted]:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
                "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
            },
            timeout=90,
        )
        if r.status_code == 404:
            last = f"модель {model} недоступна"
            _gemini_exhausted.add(model)
            continue
        if r.status_code in (500, 502, 503, 504):
            log.info("Gemini %s тимчасово недоступний (%s), пробую наступну модель", model, r.status_code)
            last = f"{r.status_code}: Gemini тимчасово недоступний"
            continue
        if r.status_code == 429:
            log.info("Gemini %s: ліміт вичерпано, переходжу на наступну модель", model)
            last = "429: ліміт безкоштовних запитів Gemini вичерпано"
            _gemini_exhausted.add(model)
            continue
        if r.status_code in (400, 401, 403) and "API key" in r.text:
            raise RuntimeError("403: ключ Gemini недійсний")
        r.raise_for_status()
        data = r.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        # моделі, що «думають», можуть повернути окрему частину з міркуваннями — беремо лише відповідь
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        out = json.loads(text)
        _gemini_model = model
        TR_STATS.setdefault("models", {})[model] = TR_STATS.setdefault("models", {}).get(model, 0) + 1
        return out
    raise RuntimeError(last or "Gemini недоступний")


def gemini_translate(texts):
    """Перекладає список текстів одним запитом. Повертає список тієї ж довжини."""
    out = gemini_json(GEMINI_PROMPT, texts)
    if not isinstance(out, list) or len(out) != len(texts) or not all(isinstance(x, str) and x.strip() for x in out):
        raise RuntimeError("Gemini повернув відповідь не того формату")
    mark = re.compile(r"Q\s*Q\s*\d+\s*Q\s*Q", re.I)
    for src, dst in zip(texts, out):
        if len(mark.findall(src)) != len(mark.findall(dst)):
            raise RuntimeError("Gemini зіпсував назви (QQ-мітки) — беру запасний перекладач")
    return [x.strip() for x in out]


GEMINI_REWRITE = GEMINI_PROMPT.split("Отримуєш JSON-масив")[0] + """Отримуєш JSON-об'єкт: title — заголовок англійською, text — текст статті \
(або її короткий анонс). Напиши українською:
- title — заголовок новини за правилами нижче;
- summary — суть новини у 2–3 реченнях (до 350 символів): що сталося, головні деталі й чому це \
цікаво. Тільки факти з тексту, без вступів на кшталт «У статті йдеться» і без оцінок від себе.

Правила:""" + GEMINI_PROMPT.split("Правила:")[1].split("Відповідь —")[0] + \
    "Відповідь — лише JSON-об'єкт {\"title\": \"…\", \"summary\": \"…\"}, без пояснень."


def gemini_rewrite(title, text):
    """Заголовок і короткий переказ статті українською одним запитом."""
    out = gemini_json(GEMINI_REWRITE, {"title": title, "text": text[:3000]})
    if not isinstance(out, dict) or not str(out.get("title", "")).strip():
        raise RuntimeError("Gemini повернув відповідь не того формату")
    t, s_ = str(out["title"]).strip(), str(out.get("summary", "")).strip()
    if re.search(r"Q\s*Q\s*\d+\s*Q\s*Q", t + s_, re.I):
        raise RuntimeError("Gemini зіпсував назви — беру запасний перекладач")
    return t, s_


TRANSLATORS = ([("Gemini", lambda t: gemini_translate([t])[0])] if GEMINI_API_KEY else []) + (
    [("DeepL", _deepl)] if DEEPL_API_KEY else []
) + [
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
                    TR_STATS.setdefault("count", {})[name] = TR_STATS.setdefault("count", {}).get(name, 0) + 1
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


def _msg_id(data):
    return ((data or {}).get("result") or {}).get("message_id")


def publish(caption, image_url, link, chat_id=None):
    """Публікує пост. Повертає message_id (або None у тестовому режимі)."""
    if DRY_RUN:
        print("\n" + "=" * 60 + f"\n[IMG] {image_url}\n{caption}\n")
        return None
    common = {"chat_id": chat_id or CHANNEL_ID, "parse_mode": "HTML"}
    if image_url:
        try:
            return _msg_id(tg("sendPhoto", photo=image_url, caption=caption, **common))
        except Exception as e:  # noqa: BLE001
            log.info("Telegram не взяв картинку за посиланням (%s), завантажую сам", e)
        try:
            img = HTTP.get(image_url, timeout=20)
            img.raise_for_status()
            return _msg_id(tg(
                "sendPhoto",
                caption=caption,
                files={"photo": ("image.jpg", img.content)},
                **common,
            ))
        except Exception as e:  # noqa: BLE001
            log.info("Картинку не вдалося відправити (%s), публікую текстом", e)
    return _msg_id(tg(
        "sendMessage",
        text=caption,
        link_preview_options=json.dumps(
            {"url": link, "prefer_large_media": True} if link else {"is_disabled": True}
        ),
        **common,
    ))


CHANNEL_USERNAME = ""


def post_link(message_id, chat_id=None):
    """Посилання на пост у каналі."""
    if not message_id:
        return ""
    chat = str(chat_id or CHANNEL_ID)
    if CHANNEL_USERNAME and (chat_id is None or chat == str(CHANNEL_ID)):
        return f"https://t.me/{CHANNEL_USERNAME}/{message_id}"
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}/{message_id}"
    return ""


PLACEHOLDER = "QQ{}QQ"


def placeholder_re(n):
    return re.compile(r"Q\s*Q\s*" + str(n) + r"\s*Q\s*Q", re.I)


def mark_title(title, games):
    """Заголовок, де назви ігор замінено мітками QQ0QQ, QQ1QQ… (щоб перекладач їх не чіпав)."""
    marked = title
    for n, (s, e, _, _) in reversed(list(enumerate(games))):
        marked = marked[:s] + PLACEHOLDER.format(n) + marked[e:]
    return marked


def linked_title(title, games, tr=None, use_marks=True):
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

    translated = tr(mark_title(title, games)) if use_marks else ""

    if use_marks and all(placeholder_re(n).search(translated) for n in range(len(games))):
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
            if label == "канал":
                global CHANNEL_USERNAME
                CHANNEL_USERNAME = info.get("username") or ""
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
    if "Gemini" in _disabled:
        alerts.problem(
            "gemini", "Gemini більше не перекладає",
            f"{_disabled['Gemini']}\nПоки що перекладаю через DeepL і Google. "
            "Безкоштовний ліміт Gemini оновлюється щодоби.",
            repeat_hours=24,
        )
    elif "Gemini" in TR_STATS["ok"]:
        alerts.ok("gemini")

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


# ---------- тижнева статистика ----------

def _week(ts=None):
    return datetime.fromtimestamp(ts or time.time(), wishlist.KYIV).strftime("%G-W%V")


def stats_add(state, kind, key, n=1):
    if not n:
        return
    st = state.setdefault("stats", {"week": _week()})
    if st.get("week") != _week():
        return  # звіт за минулий тиждень ще не надіслано — weekly_report обнулить на початку запуску
    bucket = st.setdefault(kind, {})
    bucket[key] = bucket.get(key, 0) + n


TRANSLATOR_NAMES = {
    "gemini-flash-latest": "Gemini Flash",
    "gemini-flash-lite-latest": "Gemini Flash-Lite",
}


def weekly_report(config, state):
    """Раз на тиждень (у перший запуск нового тижня) — звіт власнику в особисті."""
    st = state.get("stats")
    if not st:
        state["stats"] = {"week": _week()}
        return
    if st.get("week") == _week():
        return
    sources = st.get("sources", {})
    extra = st.get("extra", {})
    total = sum(sources.values()) + sum(extra.values())
    try:
        y, w = st["week"].split("-W")
        monday = datetime.fromisocalendar(int(y), int(w), 1).date()
        title = f"📊 <b>Звіт за тиждень {releases.date_range_label(monday, monday + timedelta(days=6))}</b>"
    except Exception:  # noqa: BLE001
        title = "📊 <b>Звіт за тиждень</b>"
    lines = [title, "", f"Опубліковано постів: <b>{total}</b>"]
    if sources:
        top = sorted(sources.items(), key=lambda kv: -kv[1])
        lines += ["", "<b>Джерела:</b>"] + [f"• {html.escape(k)} — {v}" for k, v in top]
    if extra:
        lines += ["", "<b>Окремо:</b>"] + [f"• {html.escape(k)} — {v}" for k, v in extra.items()]
    configured = []
    for cat in config["categories"].values():
        for f in cat["feeds"]:
            if f["name"] not in configured:
                configured.append(f["name"])
    silent = [n for n in configured if n not in sources]
    if silent:
        lines += ["", "<b>Мовчали весь тиждень:</b> " + html.escape(", ".join(silent))]
    tr = st.get("translators", {})
    if tr:
        parts = [f"{TRANSLATOR_NAMES.get(k, k)} — {v}" for k, v in sorted(tr.items(), key=lambda kv: -kv[1])]
        lines += ["", "<b>Переклад:</b> " + html.escape(", ".join(parts))]
    if ADMIN_ID or DRY_RUN:
        try:
            notify_admin("\n".join(lines))
            log.info("📊 Тижневий звіт надіслано")
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося надіслати тижневий звіт: %s", e)
            return  # спробуємо наступного запуску
    state["stats"] = {"week": _week()}


DIGEST_PROMPT = """Ти — редактор українського Telegram-каналу про ігри, важку й альтернативну музику, \
кіно, серіали, софт і ШІ. Отримуєш JSON-масив сьогоднішніх заголовків каналу з номерами. \
Вибери до {n} найважливіших і найцікавіших для читачів новин дня: великі анонси, релізи, \
скандали, гучні оголошення. Уникай дрібниць, оглядів дрібних інді й повторів однієї теми. \
Відповідь — лише JSON-масив номерів у порядку важливості, наприклад [4, 17, 2]."""


def daily_digest(state, cfg):
    """Ввечері — пост «Головне за день» з посиланнями на найважливіші пости дня."""
    now = datetime.now(wishlist.KYIV)
    if now.hour < int(cfg.get("hour", 21)) or state.get("daily_digest_date") == now.strftime("%Y-%m-%d"):
        return 0
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    posts = [p for p in state.get("post_log", []) if p[0] >= day_start and p[1]]
    state["daily_digest_date"] = now.strftime("%Y-%m-%d")
    n = int(cfg.get("count", 7))
    if len(posts) < 5:
        log.info("Головне за день: замало постів (%d)", len(posts))
        return 0
    order = []
    if GEMINI_API_KEY and "Gemini" not in _disabled:
        try:
            picked = gemini_json(DIGEST_PROMPT.replace("{n}", str(n)),
                                 [{"n": i, "title": f"{p[2]} {p[3]}"} for i, p in enumerate(posts)])
            order = [int(i) for i in picked if str(i).isdigit() and int(i) < len(posts)]
        except Exception as e:  # noqa: BLE001
            log.info("Gemini не вибрав головне за день: %s", e)
    if not order:
        return 0
    lines = [f"🗞 <b>Головне за {now.day} {releases.MONTHS[now.month - 1]}</b>", ""]
    for k, i in enumerate(dict.fromkeys(order[:n]), 1):
        ts, msg_id, emoji, title, source = posts[i]
        url = post_link(msg_id)
        t = html.escape(title)
        lines.append(f"{k}. {emoji} " + (f'<a href="{url}">{t}</a>' if url else t))
    lines += ["", "#головне"]
    send_text("\n".join(lines))
    log.info("🗞 Головне за день опубліковано")
    return 1


def log_post(state, msg_id, emoji, title, source):
    """Журнал опублікованих постів: для «Головного за день» і реакцій у тижневому звіті."""
    now = time.time()
    plog = [p for p in state.get("post_log", []) if now - p[0] < 8 * 86400]
    plog.append([now, msg_id, emoji, title, source])
    state["post_log"] = plog


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
            err = RuntimeError(f"{last.status_code} напряму; через воркер: {str(e)[:120]}")
            _source_cache[url] = err
            raise err
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


def split_post(text, limit=140):
    """Текст поста → (заголовок, решта): перше речення або перший рядок, не довше limit."""
    text = (text or "").strip()
    m = re.match(r"(.{15,%d}?[.!?…])(\s|$)" % limit, text)
    if m:
        return m.group(1).strip(), text[m.end():].strip()
    if len(text) <= limit:
        return text, ""
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…", text


def _kw_regex(words):
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)


_kw_cache = {}


def keywords_ok(feed, title):
    """include_keywords — у заголовку має бути хоч одне слово; exclude_keywords — жодного."""
    inc, exc = feed.get("include_keywords"), feed.get("exclude_keywords")
    if inc:
        rx = _kw_cache.setdefault(("i", tuple(inc)), _kw_regex(inc))
        if not rx.search(title):
            return False
    if exc:
        rx = _kw_cache.setdefault(("e", tuple(exc)), _kw_regex(exc))
        if rx.search(title):
            return False
    return True


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
            entries, error, errors = [], None, []
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
                    errors.append(f"{u} → {str(e)[:120]}")
                    continue
                if entries:
                    if u != feed["url"] or feed.get("alt_urls"):
                        working_urls[feed["url"]] = u
                    break
            if error and not entries:
                e = error
                log.warning("❌ %s: не вдалося завантажити стрічку (%s)", feed["name"], e)
                alerts.problem(alert_key, down, "\n".join(errors) or f"{feed['url']}\n{e}", after_hours=12)
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
                social = False
                if not title and entry.get("summary"):
                    # пост із соцмережі (Bluesky, Mastodon…): заголовка немає, лише текст
                    text = re.sub(r"\s*https?://\S+", "", clean_text(entry["summary"])).strip()
                    title, rest = split_post(text)
                    entry["summary"] = rest
                    social = True
                if not title or now - ts > max_age:
                    continue
                if any(k in title.lower() for k in skip):
                    state["seen"][key] = now
                    continue
                if not keywords_ok(feed, title):
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
                        "social": social,
                        "summarize": feed.get("summarize", True),
                    }
                )
            fresh.sort(key=lambda c: c["ts"], reverse=True)
            take = int(feed.get("max_per_run", limits["max_per_source_per_run"]))
            if feed.get("max_per_day"):
                # для дуже плідних джерел (Deadline, Product Hunt…) — не більше N постів на добу
                day_log = [t for t in state.setdefault("posted_by_source", {}).get(source_id, []) if now - t < 86400]
                take = min(take, max(0, int(feed["max_per_day"]) - len(day_log)))
            for f in fresh:
                f["source_id"] = source_id
            candidates.extend(fresh[:take])
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

    favorites = [str(x) for x in (config.get("favorites") or []) if str(x).strip()]
    fav_rx = _kw_regex(favorites) if favorites else None

    def post(*args, **kwargs):
        try:
            msg_id = publish(*args, **kwargs)
            pub["ok"] += 1
            return msg_id
        except Exception as e:
            pub["failed"] += 1
            pub["error"] = str(e)
            raise

    weekly_report(config, state)
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
        if not c.get("social") and summary.lower().startswith(c["title"].lower()[:40]):
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
        use_marks = True
        summary_ready = None
        summarize = limits.get("summarize", True) and c.get("summarize", True)
        if not already_uk and GEMINI_API_KEY and "Gemini" not in _disabled and summarize:
            # короткий переказ самої статті замість обрізаного анонсу з RSS (той самий 1 запит)
            text = article_text(link) or summary
            if len(text) >= 120:
                try:
                    t_uk, summary_ready = gemini_rewrite(c["title"], text)
                    TR_STATS["ok"].add("Gemini")
                    tr = lambda t, done={c["title"]: t_uk}: done.get(t) or translate(t)  # noqa: E731
                    use_marks = False
                except Exception as e:  # noqa: BLE001
                    summary_ready = None
                    msg = str(e)
                    if any(k in msg.lower() for k in BLOCKING_ERRORS):
                        _disabled["Gemini"] = msg[:200]
                    TR_STATS["gemini_error"] = msg[:300]
                    log.info("Gemini не переписав статтю (%s) — перекладаю анонс", msg[:120])
        if summary_ready is None and not already_uk and GEMINI_API_KEY and "Gemini" not in _disabled:
            # заголовок і опис — одним запитом, щоб берегти безкоштовний ліміт Gemini
            # без QQ-міток: Gemini сам лишає назви латиницею, посилання на Steam ставимо після
            batch = [c["title"]] + ([summary] if summary else [])
            try:
                done = dict(zip(batch, gemini_translate(batch)))
                TR_STATS["ok"].add("Gemini")
                tr = lambda t, done=done: done.get(t) or translate(t)  # noqa: E731
                use_marks = False
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if any(k in msg.lower() for k in BLOCKING_ERRORS):
                    _disabled["Gemini"] = msg[:200]
                TR_STATS["gemini_error"] = msg[:300]
                log.info("Gemini не переклав (%s) — беру запасний перекладач", msg[:120])
        try:
            title_html, extra_games = linked_title(c["title"], games, tr, use_marks)
            if summary_ready is not None:
                summary_uk = shorten(summary_ready, limits["summary_chars"])
            else:
                summary_uk = tr(summary) if summary else ""
        except RuntimeError:
            log.warning("Пропускаю до наступного запуску (переклад): %s", c["title"])
            continue
        if re.search(r"Q\s*Q\s*\d+\s*Q\s*Q", f"{title_html} {summary_uk}", re.I):
            log.warning("Переклад зіпсував назви (QQ-мітки), спробую наступного разу: %s", c["title"])
            continue

        title_plain = html.unescape(re.sub(r"<[^>]+>", "", title_html))
        if is_duplicate_published(title_plain, state.setdefault("titles_uk", [])):
            log.info("↪ така новина вже була в каналі, пропускаю: %s", title_plain)
            state["seen"][c["key"]] = time.time()
            continue

        image = image_from_entry(entry) or image_from_page(link)
        caption = build_caption(c["cat"], title_html, summary_uk, c["source"], link, extra_games)
        favorite = fav_rx and (fav_rx.search(c["title"]) or fav_rx.search(title_plain))
        if favorite:
            caption = "⭐ " + caption
        try:
            msg_id = post(caption, image, link)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося опублікувати «%s»: %s", c["title"], e)
            continue
        log_post(state, msg_id, c["cat"].get("emoji", ""), title_plain, c["source"])
        if favorite and (ADMIN_ID or DRY_RUN):
            try:
                url = post_link(msg_id)
                notify_admin(f"⭐ <b>Про твоє улюблене — {html.escape(favorite.group(0))}</b>"
                             + (f"\n{url}" if url else "") + f"\n\n{caption}")
            except Exception as e:  # noqa: BLE001
                log.info("Не вдалося надіслати ⭐ в особисті: %s", e)

        state["seen"][c["key"]] = time.time()
        state["titles"].append([time.time(), c["title"]])
        state["titles_uk"].append([time.time(), title_plain])
        stats_add(state, "sources", c["source"])
        if c.get("source_id"):
            log_ = state.setdefault("posted_by_source", {}).setdefault(c["source_id"], [])
            log_[:] = [t for t in log_ if time.time() - t < 86400] + [time.time()]
        posted += 1
        log.info("📤 %s — %s", c["source"], c["title"])
        time.sleep(1.5)  # Telegram дозволяє ~20 повідомлень на хвилину в канал

    wl_cfg = config.get("wishlist") or {}
    if wl_cfg.get("enabled") and wl_cfg.get("profile"):
        before = state.get("wishlist", {}).get("last_check", 0)
        try:
            wl_posted = wishlist.check(
                HTTP,
                state,
                wl_cfg,
                lambda text, img, url: post(text, img, url, chat_id=WISHLIST_CHAT_ID),
                html.escape,
                force=FORCE_RELEASES,
            )
            posted += wl_posted
            stats_add(state, "extra", "Вішліст Steam", wl_posted)
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

    fr_cfg = config.get("freebies") or {}
    if fr_cfg.get("enabled"):
        try:
            n = freebies.check(HTTP, state, fr_cfg, post, html.escape)
            posted += n
            stats_add(state, "extra", "Халява", n)
            alerts.ok("freebies")
        except Exception as e:  # noqa: BLE001
            log.error("Халява: %s", e)
            alerts.problem("freebies", "Халява (GamerPower) недоступна", str(e), after_hours=24)

    ua_cfg = config.get("ukrainian") or {}
    if ua_cfg.get("enabled"):
        try:
            n = ukrainian.check(HTTP, state, ua_cfg, post, html.escape, wl_cfg.get("country") or "UA")
            posted += n
            stats_add(state, "extra", "Українська локалізація", n)
            alerts.ok("ukrainian")
        except Exception as e:  # noqa: BLE001
            log.error("Українська локалізація: %s", e)
            alerts.problem("ukrainian", "Перевірка української локалізації не працює", str(e), after_hours=24)

    dd_cfg = config.get("daily_digest") or {}
    if dd_cfg.get("enabled"):
        try:
            posted += daily_digest(state, dd_cfg)
        except Exception as e:  # noqa: BLE001
            log.error("Головне за день не вдалося: %s", e)

    sp_cfg = config.get("spotify") or {}
    if sp_cfg.get("enabled") and spotify.configured():
        try:
            sp_posted = spotify.check(HTTP, state, sp_cfg, post, html.escape)
            posted += sp_posted
            stats_add(state, "extra", "Spotify", sp_posted)
            alerts.ok("spotify")
        except Exception as e:  # noqa: BLE001
            log.error("Spotify: %s", e)
            alerts.problem("spotify", "Spotify недоступний", str(e), after_hours=3)

    for model, n in TR_STATS.get("models", {}).items():
        stats_add(state, "translators", model, n)
    for name, n in TR_STATS.get("count", {}).items():
        if name != "Gemini":  # Gemini вже пораховано по моделях
            stats_add(state, "translators", name, n)
    report_health(alerts, pub)
    if TR_STATS["ok"] or TR_STATS.get("gemini_error"):
        # для діагностики: якими перекладачами користувався останній запуск
        state["last_translation"] = {
            "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "used": sorted(TR_STATS["ok"]),
            "gemini_model": _gemini_model,
            "gemini_models_used": TR_STATS.get("models", {}),
            "gemini_error": TR_STATS.get("gemini_error", ""),
        }
    save_state(state)
    log.info("Готово: опубліковано %d", posted)


if __name__ == "__main__":
    main()
