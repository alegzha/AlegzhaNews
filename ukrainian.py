"""
Українська локалізація: пост, коли гра в Steam отримує український інтерфейс, субтитри чи озвучку.

Стежимо за іграми з вішліста і за популярними іграми Steam (топ продажів і популярні новинки).
"""

import json
import logging
import re
import time

from bs4 import BeautifulSoup

from wishlist import ASSET_BASE, ITEMS_URL, STORE_URL

log = logging.getLogger("news-bot")

UKRAINIAN = 26  # ELanguage у Steam
SEARCH_URL = "https://store.steampowered.com/search/results/"


def popular_appids(http, country, filters=("topsellers", "popularnew"), count=100):
    ids = []
    for f in filters:
        try:
            r = http.get(
                SEARCH_URL,
                params={"filter": f, "category1": 998, "infinite": 1, "count": count, "cc": country, "l": "english"},
                timeout=30,
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.json().get("results_html", ""), "html.parser")
            for row in soup.select("a.search_result_row"):
                a = (row.get("data-ds-appid") or "").split(",")[0]
                if a.isdigit():
                    ids.append(int(a))
        except Exception as e:  # noqa: BLE001
            log.info("Українська: не вдалося отримати список %s: %s", f, e)
    return ids


def fetch_languages(http, appids, country):
    """{appid: (name, flags, image)}; flags — рядок з i (інтерфейс), s (субтитри), a (озвучка)."""
    out = {}
    for start in range(0, len(appids), 50):
        chunk = appids[start:start + 50]
        payload = {
            "ids": [{"appid": a} for a in chunk],
            "context": {"language": "english", "country_code": country},
            "data_request": {"include_supported_languages": True, "include_assets": True},
        }
        r = http.get(ITEMS_URL, params={"input_json": json.dumps(payload)}, timeout=30)
        r.raise_for_status()
        for it in r.json().get("response", {}).get("store_items", []):
            appid = it.get("appid") or it.get("id")
            if not appid or not it.get("name"):
                continue
            flags = ""
            for lang in it.get("supported_languages") or []:
                if lang.get("elanguage") == UKRAINIAN:
                    if lang.get("supported"):
                        flags += "i"
                    if lang.get("subtitles"):
                        flags += "s"
                    if lang.get("full_audio"):
                        flags += "a"
            assets = it.get("assets") or {}
            image = None
            if assets.get("asset_url_format") and assets.get("header"):
                image = ASSET_BASE + assets["asset_url_format"].replace("${FILENAME}", assets["header"])
            out[int(appid)] = (it["name"], flags, image)
        time.sleep(1)
    return out


def confirm(http, appid):
    """Перевірка зі сторінки магазину: українська справді є в списку мов."""
    try:
        r = http.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": appid, "l": "english", "filters": "basic"}, timeout=15)
        r.raise_for_status()
        langs = r.json().get(str(appid), {}).get("data", {}).get("supported_languages", "")
        return "ukrainian" in re.sub(r"<[^>]+>", "", langs).lower()
    except Exception:  # noqa: BLE001
        return True  # не вдалося перевірити — довіряємо основним даним


def describe(flags):
    parts = []
    if "i" in flags:
        parts.append("інтерфейс")
    if "s" in flags:
        parts.append("субтитри")
    if "a" in flags:
        parts.append("озвучка")
    return " · ".join(parts)


def format_post(appid, name, old, new, in_wishlist, cfg, esc):
    link = f'<a href="{STORE_URL.format(id=appid)}">{esc(name)}</a>'
    if old and "a" in new and "a" not in old:
        head = f"🇺🇦 <b>{link} отримала українську озвучку</b>"
    else:
        head = f"🇺🇦 <b>{link} тепер українською</b>"
    lines = [head, f"✅ {describe(new)}"]
    if in_wishlist:
        lines.append("⭐ Гра з твого вішліста")
    return "\n".join(lines) + f"\n\n{cfg.get('hashtag', '#українською')} #ігри"


def check(http, state, cfg, publish, esc, country="UA"):
    st = state.setdefault("ukrainian", {"apps": {}, "last_check": 0})
    every = int(cfg.get("check_every_minutes", 360)) * 60
    if time.time() - st.get("last_check", 0) < every - 120:
        return 0
    wish = [int(a) for a in (state.get("wishlist", {}).get("apps") or {})]
    tracked = list(dict.fromkeys(wish + popular_appids(http, country)))
    info = fetch_languages(http, tracked, country)
    known = st["apps"]
    posted, limit = 0, int(cfg.get("max_per_run", 5))
    for appid, (name, flags, image) in info.items():
        key = str(appid)
        old = known.get(key)
        known[key] = flags
        if old is None or posted >= limit:
            continue  # нова гра в списку — лише запам'ятовуємо
        gained_uk = not old and flags
        gained_audio = old and "a" in flags and "a" not in old
        if not (gained_uk or gained_audio):
            continue
        if not confirm(http, appid):
            continue
        img = image or f"{ASSET_BASE}steam/apps/{appid}/header.jpg"
        try:
            publish(format_post(appid, name, old, flags, appid in wish, cfg, esc), img, STORE_URL.format(id=appid))
            posted += 1
            log.info("🇺🇦 Українська: %s (%s)", name, describe(flags))
            time.sleep(1.5)
        except Exception as e:  # noqa: BLE001
            known[key] = old  # спробуємо наступного разу
            log.error("Українська: не вдалося опублікувати %s: %s", name, e)
    # не тримаємо ігри, які давно випали з відстеження
    tracked_keys = {str(a) for a in tracked}
    if len(known) > 3000:
        for k in [k for k in known if k not in tracked_keys][: len(known) - 3000]:
            del known[k]
    st["with_uk"] = sum(1 for _, f, _ in info.values() if f)  # для діагностики
    st["last_check"] = time.time()
    log.info("Українська: перевірено %d ігор, постів %d", len(info), posted)
    return posted
