"""
Пошук ігор зі заголовка новини в Steam.

Два способи, від найточнішого:
1. Посилання store.steampowered.com/app/<id> всередині самої статті —
   беремо назву гри зі Steam і шукаємо її в заголовку.
2. Кандидати з заголовка (назви з великої літери, текст у лапках) —
   шукаємо в Steam і приймаємо лише ТОЧНИЙ збіг назви (з поправкою на
   римські цифри та "Definitive Edition"/"Remastered" тощо).
"""

import logging
import re
import time

log = logging.getLogger("news-bot")

SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"
APP_URL = "https://store.steampowered.com/app/{id}/"

CACHE_TTL = 30 * 86400
MAX_LOOKUPS_PER_TITLE = 12
MAX_SPAN_WORDS = 6

# слова-сполучники, які можуть бути всередині назви, але не на краях
CONNECTORS = {"of", "the", "and", "&", "in", "to", "a", "an", "for", "on", "at", ":", "-", "vs", "vs."}

# слова/назви, які майже ніколи не є грою в заголовку новини
NOT_GAMES = {
    "i", "i'm", "i've", "we", "you", "my", "it", "it's", "this", "that", "these", "here",
    "why", "how", "what", "when", "where", "who", "don't", "can't", "after", "before",
    "new", "review", "reviews", "preview", "update", "patch", "trailer", "dlc", "season",
    "report", "official", "exclusive", "rumor", "rumour", "leak", "interview", "guide",
    "early access", "free", "steam", "valve", "pc", "pc gamer", "nintendo", "switch",
    "switch 2", "sony", "playstation", "ps5", "ps4", "xbox", "xbox series x", "microsoft",
    "epic", "epic games", "epic games store", "gog", "ubisoft", "ea", "bethesda", "capcom",
    "sega", "square enix", "bandai namco", "cd projekt", "cd projekt red", "rockstar",
    "netflix", "hbo", "amazon", "paramount", "disney", "hollywood", "keanu reeves",
    "windows", "linux", "mac", "android", "ios", "nvidia", "amd", "intel", "geforce",
    "rtx", "oled", "gaming", "games", "game", "game pass", "the game awards", "gamescom",
    "summer game fest", "tokyo game show", "consider", "remember", "watch", "play",
}

EDITION_RE = re.compile(
    r"\s*(?:[:\-–—]\s*)?(?:the\s+)?(?:definitive|remastered|enhanced|complete|ultimate|deluxe|"
    r"digital deluxe|gold|special|legendary|standard|anniversary|goty|game of the year|"
    r"director's cut|directors cut)(?:\s+edition)?\s*$",
    re.I,
)
SKIP_ITEM_RE = re.compile(r"\b(demo|soundtrack|ost|playtest|artbook|dedicated server|sdk)\b", re.I)

ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\.\-]*|&|:|[,;!?()\"“”‘—–/|]")


def norm(name):
    s = name.lower().replace("’", "'")
    s = re.sub(r"[™®©]", "", s)
    s = re.sub(r"[^a-z0-9']+", " ", s)
    words = [ROMAN.get(w, w) for w in s.split()]
    return " ".join(words).strip()


def name_variants(steam_name):
    """Повна назва + назва без «: Definitive Edition» тощо."""
    variants = {norm(steam_name)}
    base = EDITION_RE.sub("", steam_name).strip()
    if base and base != steam_name:
        variants.add(norm(base))
    return {v for v in variants if v}


CACHE_MISS_TTL = 2 * 86400


class SteamLinker:
    def __init__(self, http, state, ignore=None):
        self.http = http
        cache = state.setdefault("steam", {})
        now = time.time()
        # прибираємо застарілий кеш
        # «не знайдено» тримаємо 2 дні (гра могла з'явитися в Steam), решту — CACHE_TTL
        for k in [k for k, v in cache.items()
                  if now - v[2] > (CACHE_MISS_TTL if not v[0] else CACHE_TTL)]:
            del cache[k]
        self.cache = cache
        self.ignore = {norm(x) for x in (ignore or [])}
        self.lookups = 0

    # ---------- Steam API ----------

    def _get(self, url, params):
        try:
            r = self.http.get(url, params=params, timeout=15)
            if r.status_code == 429:
                log.info("Steam просить зачекати")
                time.sleep(10)
                r = self.http.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log.info("Steam недоступний (%s)", e)
            return None

    def search_exact(self, phrase):
        key = "q:" + norm(phrase)
        if key in self.cache:
            app_id, name, _ = self.cache[key]
            return (app_id, name) if app_id else None
        if self.lookups >= MAX_LOOKUPS_PER_TITLE:
            return None
        self.lookups += 1
        time.sleep(0.4)  # ввічливо до Steam: ~150 запитів / 5 хв максимум
        data = self._get(SEARCH_URL, {"term": phrase, "l": "english", "cc": "US"})
        if data is None:
            return None  # не кешуємо збої мережі
        wanted = norm(phrase)
        found = None
        for item in data.get("items", []):
            if item.get("type") != "app" or SKIP_ITEM_RE.search(item.get("name", "")):
                continue
            if wanted in name_variants(item["name"]):
                found = (item["id"], item["name"])
                break
        self.cache[key] = [found[0], found[1], time.time()] if found else [0, "", time.time()]
        return found

    def app_name(self, app_id):
        key = f"id:{app_id}"
        if key in self.cache:
            return self.cache[key][1] or None
        data = self._get(DETAILS_URL, {"appids": app_id, "filters": "basic", "l": "english"})
        if data is None:
            return None
        info = data.get(str(app_id), {})
        name = info.get("data", {}).get("name") if info.get("success") else None
        self.cache[key] = [app_id, name or "", time.time()]
        return name

    # ---------- пошук у заголовку ----------

    def _allowed(self, phrase):
        n = norm(phrase)
        if not n or n in NOT_GAMES or n in self.ignore:
            return False
        if len(n) < 3 and not any(ch.isdigit() for ch in n):
            return False
        return True

    def from_article_links(self, title, article_html):
        """Ігри, на які стаття сама посилається в Steam і які згадані в заголовку."""
        ids = []
        for app_id in re.findall(r"store\.steampowered\.com/app/(\d+)", article_html or ""):
            if app_id not in ids:
                ids.append(app_id)
        matches = []
        for app_id in ids[:6]:
            name = self.app_name(app_id)
            if not name:
                continue
            for variant_raw in {name, EDITION_RE.sub("", name).strip()}:
                if not variant_raw:
                    continue
                pattern = r"(?<![A-Za-z0-9])" + re.escape(variant_raw).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])"
                m = re.search(pattern, title, re.I)
                if m and self._allowed(m.group(0)):
                    matches.append((m.start(), m.end(), int(app_id), name))
                    break
        return matches

    def candidate_runs(self, title):
        """Послідовності слів з великої літери/цифр + текст у лапках."""
        tokens = [(m.group(0), m.start(), m.end()) for m in TOKEN_RE.finditer(title)]
        runs, current = [], []
        for i, (tok, s, e) in enumerate(tokens):
            is_name_word = tok[0].isupper() or tok[0].isdigit()
            is_connector = tok.lower() in CONNECTORS
            if is_name_word or (is_connector and current):
                current.append((tok, s, e))
            else:
                if current:
                    runs.append(current)
                current = []
        if current:
            runs.append(current)

        # перше слово речення з великої літери — не ознака назви, якщо воно одне
        cleaned = []
        for run in runs:
            while run and run[-1][0].lower() in CONNECTORS:
                run = run[:-1]
            if run:
                cleaned.append(run)
        return cleaned

    def from_title(self, title, taken):
        matches = []
        # 1) текст у лапках — найсильніший сигнал
        for m in re.finditer(r"[\"“‘']([^\"“”‘’']{2,60})[\"”’']", title):
            if m.start(0) > 0 and title[m.start(0) - 1].isalnum():
                continue  # апостроф усередині слова
            s, e = m.start(1), m.end(1)
            if any(s < te and e > ts for ts, te, *_ in taken + matches):
                continue
            if self._allowed(m.group(1)):
                hit = self.search_exact(m.group(1))
                if hit:
                    matches.append((s, e, hit[0], hit[1]))

        # 2) назви з великої літери: від найдовшого фрагмента до найкоротшого
        title_case = sum(1 for w in title.split() if w[:1].isupper()) > len(title.split()) * 0.6
        for run in self.candidate_runs(title):
            i = 0
            while i < len(run):
                hit = None
                for length in range(min(MAX_SPAN_WORDS, len(run) - i), 0, -1):
                    span = run[i : i + length]
                    if span[0][0].lower() in CONNECTORS or span[-1][0].lower() in CONNECTORS:
                        continue
                    s, e = span[0][1], span[-1][2]
                    text = title[s:e]
                    # прибираємо присвійне 's: "Cyberpunk 2077's" -> "Cyberpunk 2077"
                    if re.search(r"['’]s$", text):
                        text, e = text[:-2], e - 2
                    if length == 1 and s == 0 and not title_case and not any(c.isdigit() for c in text):
                        continue  # перше слово речення
                    if any(s < te and e > ts for ts, te, *_ in taken + matches):
                        continue
                    if not self._allowed(text):
                        continue
                    found = self.search_exact(text)
                    if found:
                        hit = (s, e, found[0], found[1], length)
                        break
                if hit:
                    matches.append(hit[:4])
                    i += hit[4]
                else:
                    i += 1
        return matches

    def find_games(self, title, article_html=""):
        """Повертає [(start, end, app_id, steam_name)] у порядку появи в заголовку."""
        self.lookups = 0
        matches = self.from_article_links(title, article_html)
        matches += self.from_title(title, matches)
        matches.sort(key=lambda m: m[0])
        return matches


def app_url(app_id):
    return APP_URL.format(id=app_id)
