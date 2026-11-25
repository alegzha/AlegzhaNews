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
            return other
    return None


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
            return other
    return None


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


def tidy_excerpt(text):
    """Анонс WordPress обривається посеред речення з «[…]» — прибираємо обривок."""
    t = re.sub(r"\s*(\[\s*(…|\.\.\.|&hellip;)\s*\]|Read more.*|Читати далі.*|Детальніше.*)\s*$", "", text or "", flags=re.I)
    if t != (text or "") and t and not re.search(r"[.!?…»\"”)]$", t):
        end = max(t.rfind(". "), t.rfind("! "), t.rfind("? "), t.rfind(".» "))
        t = t[: end + 1] if end > len(t) * 0.4 else t.rstrip(" ,;:—-") + "…"
    return t.strip()


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
    return " ".join(article_paragraphs(url, limit))[:limit]


def article_paragraphs(url, limit=3000):
    raw = fetch_page(url)
    if not raw:
        return []
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
    return parts


GENERIC_IMAGE_RE = re.compile(r"logo|default|placeholder|favicon|avatar|social[-_]?share|site[-_]?image", re.I)
SMALL_IMAGE_RE = re.compile(r"[-_]\d{2,3}x\d{2,3}\.(jpe?g|png|webp)|[?&](w|width|resize)=\d{2,3}\b|/thumb|thumbnail", re.I)


def best_image(entry, link, social=False):
    """Велика картинка статті (og:image) краща за мініатюру з RSS; логотип сайту — ні."""
    rss = image_from_entry(entry)
    if social:
        return rss or image_from_page(link)
    page = image_from_page(link)
    if page and not GENERIC_IMAGE_RE.search(page.rsplit("/", 1)[-1]):
        return page
    return rss or page


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
# Порядок моделей: Flash (найкраща, але малий денний ліміт) → Gemma 4 (той самий ключ,
# значно більший ліміт) → Flash-Lite (запасна).
GEMINI_MODELS = ["gemini-flash-latest", "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-flash-lite-latest"]
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
        user_text = json.dumps(payload, ensure_ascii=False)
        if model.startswith("gemma"):
            # Gemma не має окремої системної інструкції й JSON-режиму — інструкція йде в сам запит
            body = {
                "contents": [{"role": "user", "parts": [{"text": system + "\n\nВхідні дані:\n" + user_text}]}],
                "generationConfig": {"temperature": 0.3},
            }
        else:
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
            }
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=90,
        )
        if r.status_code == 404:
            last = f"модель {model} недоступна"
            _gemini_exhausted.add(model)
            log.info("Gemini: модель %s недоступна (404)", model)
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
        if r.status_code == 400 and model.startswith("gemma"):
            log.info("Gemma відхилила запит: %s", r.text[:200])
            _gemini_exhausted.add(model)
            continue
        r.raise_for_status()
        data = r.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        # моделі, що «думають», можуть повернути окрему частину з міркуваннями — беремо лише відповідь
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        # Gemma іноді загортає JSON у ```json … ``` — знімаємо обгортку
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            out = json.loads(text)
        except ValueError:
            m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
            if not m:
                raise RuntimeError(f"{model} повернув не JSON")
            out = json.loads(m.group(1))
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
цікаво. Тільки факти з тексту, без вступів на кшталт «У статті йдеться» і без оцінок від себе;
- tags — 0–2 тематичні хештеги без знаку #: назва головної гри, франшизи, гурту, фільму, серіалу \
чи компанії, про яку новина, одним словом латиницею у стилі CamelCase без пробілів і символів \
(GTA6, Nintendo, Metallica, TheWitcher, OpenAI). Лише якщо новина справді про щось конкретне.

Правила:""" + GEMINI_PROMPT.split("Правила:")[1].split("Відповідь —")[0] + \
    "Відповідь — лише JSON-об'єкт {\"title\": \"…\", \"summary\": \"…\", \"tags\": [\"…\"]}, без пояснень."


LAST_TAGS = []  # тематичні хештеги останнього переписаного поста

GEMINI_LONGREAD = """Ти — редактор українського новинного Telegram-каналу. Отримуєш JSON-об'єкт: \
title — заголовок статті, paragraphs — її абзаци англійською. Переклади статтю українською \
повністю й природно, як для українського медіа: зберігай зміст і порядок абзаців, нічого не \
вигадуй і не скорочуй, назви ігор, фільмів, серіалів, гуртів, альбомів і компаній лишай мовою \
оригіналу. Пропусти абзаци-рекламу, заклики підписатися та підписи до фото.
Відповідь — лише JSON-об'єкт {"paragraphs": ["…", "…"]}, без пояснень."""

_telegraph_token = None


def telegraph_page(title_uk, paragraphs, link, source, image):
    """Сторінка telegra.ph з повним перекладом — у Telegram відкривається миттєво (Instant View)."""
    global _telegraph_token
    out = gemini_json(GEMINI_LONGREAD, {"title": title_uk, "paragraphs": paragraphs})
    paras = [str(p).strip() for p in (out.get("paragraphs") if isinstance(out, dict) else None) or [] if str(p).strip()]
    if len(paras) < 3 or re.search(r"Q\s*Q\s*\d+\s*Q\s*Q", " ".join(paras)):
        raise RuntimeError("Gemini не переклав статтю повністю")
    if not _telegraph_token:
        r = HTTP.post("https://api.telegra.ph/createAccount", data={
            "short_name": "AlegzhaNews", "author_name": "Алєгофренський Вісник"}, timeout=20)
        _telegraph_token = r.json()["result"]["access_token"]
    nodes = []
    if image:
        nodes.append({"tag": "img", "attrs": {"src": image}})
    nodes += [{"tag": "p", "children": [p]} for p in paras]
    nodes.append({"tag": "p", "children": [{"tag": "i", "children": ["Джерело: "]},
                                           {"tag": "a", "attrs": {"href": link}, "children": [source]}]})
    channel = f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else link
    r = HTTP.post("https://api.telegra.ph/createPage", data={
        "access_token": _telegraph_token, "title": title_uk[:256], "author_name": "Алєгофренський Вісник",
        "author_url": channel, "content": json.dumps(nodes, ensure_ascii=False)}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph: {data.get('error')}")
    return data["result"]["url"]


def gemini_rewrite(title, text):
    """Заголовок і короткий переказ статті українською одним запитом."""
    out = gemini_json(GEMINI_REWRITE, {"title": title, "text": text[:3000]})
    if not isinstance(out, dict) or not str(out.get("title", "")).strip():
        raise RuntimeError("Gemini повернув відповідь не того формату")
    t, s_ = str(out["title"]).strip(), str(out.get("summary", "")).strip()
    if re.search(r"Q\s*Q\s*\d+\s*Q\s*Q", t + s_, re.I):
        raise RuntimeError("Gemini зіпсував назви — беру запасний перекладач")
    tags = []
    for tag in out.get("tags") or []:
        tag = re.sub(r"[^A-Za-z0-9_]", "", str(tag).lstrip("#"))
        if 2 <= len(tag) <= 30 and not tag.isdigit() and tag not in tags:
            tags.append(tag)
    LAST_TAGS[:] = tags[:2]
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

class TelegramUnreachable(RuntimeError):
    """Telegram тимчасово недоступний (мережа), а не помилка налаштувань."""


def tg(method, **payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    files = payload.pop("files", None)
    net_errors = 0
    for attempt in range(6):
        try:
            r = requests.post(url, data=payload, files=files, timeout=60)
            data = r.json()
        except (requests.ConnectionError, requests.Timeout, ValueError) as e:
            # короткий збій мережі між GitHub і Telegram — кілька спроб з паузою
            net_errors += 1
            if net_errors >= 4:
                raise TelegramUnreachable(str(e)) from e
            log.info("Telegram не відповів (%s), повторюю за %d с", e, 5 * net_errors)
            time.sleep(5 * net_errors)
            continue
        if data.get("ok"):
            return data
        retry = data.get("parameters", {}).get("retry_after")
        if retry:
            log.info("Telegram просить зачекати %s с", retry)
            time.sleep(retry + 1)
            continue
        raise RuntimeError(data.get("description", "unknown Telegram error"))
    raise RuntimeError("Telegram rate limit")


LAST_KIND = "text"  # тип останнього поста: photo або text (для подальшого редагування)


def _msg_id(data):
    global LAST_KIND
    result = (data or {}).get("result") or {}
    LAST_KIND = "photo" if result.get("photo") else "text"
    return result.get("message_id")


def publish(caption, image_url, link, chat_id=None, spoiler=False):
    """Публікує пост. Повертає message_id (або None у тестовому режимі)."""
    if DRY_RUN:
        print("\n" + "=" * 60 + f"\n[IMG{' spoiler' if spoiler else ''}] {image_url}\n{caption}\n")
        return None
    common = {"chat_id": chat_id or CHANNEL_ID, "parse_mode": "HTML", "disable_notification": json.dumps(quiet_now())}
    if spoiler and image_url:
        common["has_spoiler"] = "true"  # картинка теж може виказати сюжет
    if spoiler and not image_url:
        link = ""  # без прев'ю посилання — воно показало б заголовок статті
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
QUIET_HOURS = (23, 8)  # задається в feeds.yaml (quiet_hours); у цей час пости приходять без звуку


def quiet_now():
    start, end = QUIET_HOURS
    if start == end:
        return False
    h = datetime.now(wishlist.KYIV).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


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
    except TelegramUnreachable as e:
        log.warning("Telegram тимчасово недоступний (%s) — пропускаю цей запуск, спробую за 30 хв", e)
        sys.exit(0)  # не помилка: наступний запуск усе надолужить
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
        disable_notification=json.dumps(quiet_now() if not chat_id or chat_id == CHANNEL_ID else False),
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
            "gemini", "Gemini не перекладає вже добу",
            f"{_disabled['Gemini']}\nПерекладаю через DeepL і Google. Денний ліміт, що закінчився на кілька "
            "годин, — це нормально; сповіщаю лише якщо Gemini не працює цілу добу (напр., ключ недійсний).",
            after_hours=24, repeat_hours=24,
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
    "gemma-4-31b-it": "Gemma 4 31B",
    "gemma-4-26b-a4b-it": "Gemma 4 26B",
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
    filtered = st.get("filtered", {})
    if filtered:
        top_f = sorted(filtered.items(), key=lambda kv: -kv[1])[:5]
        lines += ["", f"<b>Відсіяно як шум:</b> {sum(filtered.values())} ("
                  + html.escape(", ".join(f"{k} — {v}" for k, v in top_f)) + ")"]
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
        ts, msg_id, emoji, title, source = posts[i][:5]
        url = post_link(msg_id)
        t = html.escape(title)
        lines.append(f"{k}. {emoji} " + (f'<a href="{url}">{t}</a>' if url else t))
    lines += ["", "#головне"]
    send_text("\n".join(lines))
    log.info("🗞 Головне за день опубліковано")
    return 1


def log_post(state, msg_id, emoji, title, source, orig="", caption="", kind="text", link=""):
    """Журнал опублікованих постів: для «Головного за день» і дописування «Також пишуть»."""
    now = time.time()
    plog = [p for p in state.get("post_log", []) if now - p[0] < 8 * 86400]
    for p in plog[:-60]:
        if len(p) > 5:
            p[5].pop("caption", None)  # повний текст тримаємо лише для свіжих постів
    plog.append([now, msg_id, emoji, title, source,
                 {"orig": orig, "caption": caption, "kind": kind, "link": link, "also": []}])
    state["post_log"] = plog


def find_post(state, title=None, orig=None):
    for p in reversed(state.get("post_log", [])):
        if time.time() - p[0] > 48 * 3600:
            break
        extra = p[5] if len(p) > 5 else {}
        if (title and p[3] == title) or (orig and extra.get("orig") == orig):
            return p
    return None


def merge_source(post, source, link):
    """Та сама новина з іншого сайту: дописуємо його в уже опублікований пост («Також пишуть»)."""
    if not post or not post[1] or len(post) < 6:
        return False
    extra = post[5]
    if not extra.get("caption") or source == post[4] or any(a[0] == source for a in extra["also"]):
        return False
    also = extra["also"] + [[source, link]]
    links = ", ".join(f'<a href="{html.escape(l, quote=True)}">{html.escape(s)}</a>' for s, l in also)
    caption = f"{extra['caption']}\n➕ Також пишуть: {links}"
    if len(also) >= 2 and not caption.startswith("🔥"):
        caption = "🔥 " + caption  # про це пишуть уже 3+ сайти
    if extra.get("kind") == "photo" and len(caption) > 1024:
        return False
    if DRY_RUN:
        print(f"\n[РЕДАГУЮ {post[1]}] {caption}\n")
    else:
        try:
            if extra.get("kind") == "photo":
                tg("editMessageCaption", chat_id=CHANNEL_ID, message_id=post[1], caption=caption, parse_mode="HTML")
            else:
                tg("editMessageText", chat_id=CHANNEL_ID, message_id=post[1], text=caption, parse_mode="HTML",
                   link_preview_options=json.dumps({"url": extra.get("link", ""), "prefer_large_media": True}
                                                   if extra.get("link") else {"is_disabled": True}))
        except Exception as e:  # noqa: BLE001
            log.info("Не вдалося дописати джерело в пост %s: %s", post[1], e)
            return False
    extra["also"] = also
    log.info("➕ %s дописано до поста «%s»", source, post[3])
    return True


def build_caption(cat, title_html, summary_uk, source, link, extra_lines=(), tags=(), spoiler=False):
    esc = html.escape
    if spoiler:
        head = f"{cat['emoji']} ⚠️ <b>Обережно, спойлери!</b>\n<tg-spoiler><b>{title_html}</b></tg-spoiler>"
    else:
        head = f"{cat['emoji']} <b>{title_html}</b>"
    if extra_lines:
        head += "\n" + "\n".join(extra_lines)
    parts = [head]
    if summary_uk:
        parts.append(f"<tg-spoiler>{esc(summary_uk)}</tg-spoiler>" if spoiler else esc(summary_uk))
    hashtags = " ".join([cat["hashtag"]] + [f"#{t}" for t in tags if f"#{t}".lower() != cat["hashtag"].lower()])
    parts.append(f'🔗 <a href="{esc(link, quote=True)}">{esc(source)}</a> · {hashtags}')
    caption = "\n\n".join(parts)
    # ліміт підпису до фото в Telegram — 1024 символи (рахуються лише видимі символи,
    # але для простоти рахуємо з розміткою — так гарантовано влізе)
    if len(caption) > 1024 and summary_uk:
        overflow = len(caption) - 1024 + 2
        return build_caption(cat, title_html, summary_uk[:-overflow] + "…", source, link, extra_lines, tags, spoiler)
    return caption


def steam_rating(app_id):
    """Частка позитивних відгуків у Steam (None, якщо відгуків замало)."""
    try:
        r = HTTP.get(f"https://store.steampowered.com/appreviews/{app_id}",
                     params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
                     timeout=15)
        q = r.json().get("query_summary") or {}
        total = int(q.get("total_reviews") or 0)
        if total >= 50:
            return round(100 * int(q.get("total_positive") or 0) / total)
    except Exception as e:  # noqa: BLE001
        log.info("Відгуки Steam недоступні: %s", e)
    return None


STEAM_IMAGE = {}  # app_id -> обкладинка, для новин без власної картинки


def steam_price_lines(games, extra_games):
    """🕹 Назва — ціна в ₴ (зі знижкою), для ігор, згаданих у новині."""
    ids, names = [], {}
    for _, _, app_id, name in games:
        if app_id not in ids:
            ids.append(app_id)
            names[app_id] = name
    for name, app_id in extra_games:
        if app_id not in ids:
            ids.append(app_id)
            names[app_id] = name
    if not ids:
        return []
    try:
        info = wishlist.fetch_items(HTTP, ids[:3], "UA", "english")
    except Exception as e:  # noqa: BLE001
        log.info("Ціни Steam недоступні: %s", e)
        info = {}
    esc = html.escape
    lines = []
    for app_id in ids[:3]:
        it = info.get(app_id) or {}
        if it.get("image"):
            STEAM_IMAGE.setdefault("first", it["image"])
        name = it.get("name") or names[app_id]
        link = f'<a href="{app_url(app_id)}">{esc(name)}</a>'
        if it.get("is_free"):
            price = "безкоштовно"
        elif it.get("coming_soon"):
            price = "ще не вийшла"
        elif it.get("price"):
            price = esc(it["price"])
            if it.get("discount"):
                price += f" (−{it['discount']}%)"
        else:
            price = ""
        rating = None if it.get("coming_soon") else steam_rating(app_id)
        if rating is not None:
            price = (price + " · " if price else "") + f"👍 {rating}%"
        lines.append(f"🕹 {link}" + (f" — {price}" if price else ""))
    return lines


YT_ID_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})")
TRAILER_RE = re.compile(r"\b(trailer|teaser|трейлер|тизер)", re.I)
VIDEO_RE = re.compile(r"\b(music video|video|visualizer|clip|кліп|відео)\b", re.I)


def video_line(title, entry, link, cat_id):
    """▶️ посилання на трейлер чи кліп з YouTube, якщо стаття його вбудовує."""
    is_trailer = bool(TRAILER_RE.search(title))
    if not is_trailer and not VIDEO_RE.search(title):
        return None
    sources = [entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content", []) or []]
    raw = fetch_page(link)
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        root = soup.find("article") or soup.find("main") or soup.body
        sources.append(str(root) if root else "")
    for chunk in sources:
        m = YT_ID_RE.search(chunk or "")
        if m:
            label = "трейлер" if is_trailer else ("кліп" if cat_id == "music" else "відео")
            return f'▶️ <a href="https://youtu.be/{m.group(1)}">Дивитися {label}</a>'
    return None


LISTEN_RE = re.compile(
    r"^(?P<artist>.+?)\s+(?:release|releases|drop|drops|share|shares|unveil|unveils|debut|debuts|premiere|premieres|"
    r"reveal|reveals|stream|streams|announce|announces|deliver|delivers|launch|launches|return with|post|posts)\b.*?"
    r"[\"“‘'](?P<work>[^\"”’]{2,80})[\"”’]",
    re.I,
)


def listen_line(title):
    """🎧 Слухати: пошук пісні чи альбому в Spotify і YouTube Music (без API)."""
    m = LISTEN_RE.search(title or "")
    if not m:
        return None
    artist = re.sub(r"^(watch|listen|hear)\s*:\s*", "", m.group("artist"), flags=re.I).strip(" -–—:")
    if not artist or len(artist) > 60:
        return None
    q = requests.utils.quote(f"{artist} {m.group('work')}")
    return (f'🎧 Слухати: <a href="https://open.spotify.com/search/{q}">Spotify</a> · '
            f'<a href="https://music.youtube.com/search?q={q}">YouTube Music</a>')


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
    """Слова з * у кінці — за початком (штучн* → штучний, штучного…)."""
    parts = [re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) for w in words]
    return re.compile(r"(?<![\w-])(" + "|".join(parts) + r")(?![\w-])", re.I)


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
    skip_rx = [re.compile(p, re.I) for p in config.get("filters", {}).get("skip_patterns", []) or []]
    # новини про «залізо» (відеокарти, пам'ять, процесори) з ігрових сайтів — у рубрику софту
    hw = config.get("filters", {}).get("hardware") or {}
    hw_rx = re.compile(hw["pattern"], re.I) if hw.get("pattern") else None
    hw_to = hw.get("to", "software")
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
                if any(k in title.lower() for k in skip) or any(rx.search(title) for rx in skip_rx):
                    state["seen"][key] = now
                    stats_add(state, "filtered", feed["name"])
                    continue
                if not keywords_ok(feed, title):
                    continue
                c_id, c_cat = cat_id, cat
                if hw_rx and cat_id == "games" and hw_to in config["categories"] and hw_rx.search(title):
                    c_id, c_cat = hw_to, config["categories"][hw_to]
                fresh.append(
                    {
                        "key": key,
                        "ts": ts,
                        "title": title,
                        "entry": entry,
                        "cat": c_cat,
                        "cat_id": c_id,
                        "source": feed["name"],
                        "lang": feed.get("lang", ""),
                        "social": social,
                        "summarize": feed.get("summarize", True),
                    }
                )
            fresh.sort(key=lambda c: c["ts"], reverse=True)
            take = int(feed.get("max_per_run", limits["max_per_source_per_run"]))
            per_day = feed.get("max_per_day", limits.get("max_per_source_per_day"))
            if per_day:
                # не більше N постів на добу з одного джерела (типово — limits.max_per_source_per_day)
                day_log = [t for t in state.setdefault("posted_by_source", {}).get(source_id, []) if now - t < 86400]
                take = min(take, max(0, int(per_day) - len(day_log)))
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
    global FETCH_PROXY, QUIET_HOURS
    FETCH_PROXY = (config.get("fetch_proxy") or "").strip()
    qh = config.get("quiet_hours")
    if isinstance(qh, list) and len(qh) == 2:
        QUIET_HOURS = (int(qh[0]), int(qh[1]))
    state, first_run = load_state()
    alerts = Alerts(state, notify_admin if (ADMIN_ID or DRY_RUN) else None)
    pub = {"ok": 0, "failed": 0, "error": ""}

    # улюблене: рядок (будь-яка рубрика) або {name: …, only: music} — лише в одній рубриці
    favorites, fav_only = [], {}
    for x in config.get("favorites") or []:
        name = str(x.get("name", "") if isinstance(x, dict) else x).strip()
        if name:
            favorites.append(name)
            if isinstance(x, dict) and x.get("only"):
                fav_only[name.lower()] = str(x["only"])
    sp = (config.get("filters") or {}).get("spoiler_pattern")
    spoiler_rx = re.compile(sp, re.I) if sp else None
    tg_cfg = config.get("telegraph") or {}
    state["telegraph"] = [t for t in state.get("telegraph", []) if time.time() - t < 86400]

    def longreads_left():
        return int(tg_cfg.get("max_per_day", 5)) - len(state["telegraph"])
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
    # денний ліміт каналу: розподіляємо рівномірно до кінця доби, щоб вечірні новини теж влізли
    now_k = datetime.now(wishlist.KYIV)
    day_start = now_k.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [p for p in state.get("post_log", []) if p[0] >= day_start]
    daily_cap = limits.get("max_posts_per_day")
    if daily_cap:
        left = max(0, int(daily_cap) - len(today))
        runs_left = max(1, (24 * 60 - (now_k.hour * 60 + now_k.minute)) // 30)
        budget = min(budget, max(1 if left else 0, -(-left // runs_left)))
        log.info("Сьогодні вже %d постів з %d — у цьому запуску до %d", len(today), daily_cap, budget)
    cat_today = {}
    for p in today:
        cat_today[p[2]] = cat_today.get(p[2], 0) + 1
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
        cat_cap = c["cat"].get("max_per_day")
        emoji = c["cat"].get("emoji", "")
        if cat_cap and cat_today.get(emoji, 0) + sum(1 for s in selected if s["cat"] is c["cat"]) >= int(cat_cap):
            continue  # рубрика на сьогодні заповнена — новина почекає (якщо ще не застаріє)
        dup = is_duplicate_story(c["title"], state["titles"] + [(0, s["title"]) for s in selected])
        if dup:
            log.info("↪ дубль іншої новини: %s", c["title"])
            merge_source(find_post(state, orig=dup), c["source"], c["entry"].get("link", ""))
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
        summary = tidy_excerpt(clean_text(entry.get("summary", "")))
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
        LAST_TAGS.clear()
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
        dup = is_duplicate_published(title_plain, state.setdefault("titles_uk", []))
        if dup:
            log.info("↪ така новина вже була в каналі: %s", title_plain)
            merge_source(find_post(state, title=dup), c["source"], link)
            state["seen"][c["key"]] = time.time()
            continue

        image = best_image(entry, link, social=c.get("social"))
        STEAM_IMAGE.clear()
        extra_lines = steam_price_lines(games, extra_games) if (games or extra_games) else []
        if not image and STEAM_IMAGE.get("first"):
            image = STEAM_IMAGE["first"]  # новина без фото — обкладинка гри зі Steam
        vl = None if c.get("social") else video_line(c["title"], entry, link, c["cat_id"])
        if vl:
            extra_lines.append(vl)
        if c["cat_id"] == "music":
            ll = listen_line(c["title"])
            if ll:
                extra_lines.append(ll)
        # лонгрід — повний переклад на telegra.ph
        if (tg_cfg.get("enabled") and summary_ready is not None and not already_uk
                and "Gemini" not in _disabled and longreads_left() > 0):
            paras = article_paragraphs(link, 15000)
            if sum(len(p) for p in paras) >= int(tg_cfg.get("min_chars", 4000)):
                try:
                    page = telegraph_page(title_plain, paras, link, c["source"], image)
                    extra_lines.append(f'📖 <a href="{html.escape(page, quote=True)}">Читати повністю українською</a>')
                    state.setdefault("telegraph", []).append(time.time())
                    log.info("📖 Лонгрід на Telegraph: %s", page)
                except Exception as e:  # noqa: BLE001
                    log.info("Лонгрід не вдався: %s", str(e)[:150])
        spoiler = bool(spoiler_rx and spoiler_rx.search(c["title"]))
        tags = list(LAST_TAGS) if summary_ready is not None else []
        caption = build_caption(c["cat"], title_html, summary_uk, c["source"], link, extra_lines, tags, spoiler)
        favorite = fav_rx and (fav_rx.search(c["title"]) or fav_rx.search(title_plain))
        if favorite and fav_only.get(favorite.group(0).lower(), c["cat_id"]) != c["cat_id"]:
            favorite = None
        if favorite:
            caption = "⭐ " + caption
        try:
            msg_id = post(caption, image, link, spoiler=spoiler)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося опублікувати «%s»: %s", c["title"], e)
            continue
        log_post(state, msg_id, c["cat"].get("emoji", ""), title_plain, c["source"],
                 orig=c["title"], caption=caption, kind=LAST_KIND, link=link)
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
