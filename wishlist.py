"""
Стеження за вішлістом Steam: пост, коли гра вийшла або отримала знижку.

Потрібно: публічний вішліст (Steam → Профіль → Налаштування приватності →
«Інформація про ігри» = Відкрито).
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("news-bot")

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
STORE_URL = "https://store.steampowered.com/app/{id}/"
try:
    from zoneinfo import ZoneInfo

    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # noqa: BLE001
    try:
        KYIV = ZoneInfo("Europe/Kiev")
    except Exception:  # noqa: BLE001
        KYIV = timezone(timedelta(hours=3))


# ---------- профіль ----------

def resolve_steamid(http, profile):
    """Приймає SteamID64, посилання на профіль або нікнейм з /id/<нік>."""
    profile = (profile or "").strip().rstrip("/")
    if not profile:
        return None
    m = re.search(r"(\d{17})", profile)
    if m:
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)", profile)
    vanity = m.group(1) if m else profile
    try:
        r = http.get(f"https://steamcommunity.com/id/{vanity}/?xml=1", timeout=15)
        r.raise_for_status()
        m = re.search(r"<steamID64>(\d{17})</steamID64>", r.text)
        return m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        log.warning("Не вдалося визначити SteamID для «%s»: %s", profile, e)
        return None


def fetch_wishlist(http, steamid):
    r = http.get(WISHLIST_URL, params={"steamid": steamid}, timeout=20)
    r.raise_for_status()
    items = r.json().get("response", {}).get("items", [])
    return [int(i["appid"]) for i in items if i.get("appid")]


# ---------- дані про ігри ----------

def _cents(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def uah(cents):
    """449900 копійок -> «4 499₴»."""
    if cents is None:
        return None
    return f"{cents / 100:,.0f}".replace(",", " ") + "₴"


def release_ts(release):
    """Точна дата релізу (unix) або None, якщо Steam знає лише місяць/квартал/рік."""
    ts = _cents(release.get("steam_release_date") or release.get("original_release_date"))
    if not ts:
        return None
    display = release.get("coming_soon_display")
    if release.get("is_coming_soon") and display not in (None, "", "date_full"):
        return None
    if release.get("custom_release_date_message") or release.get("is_abridged_release_date"):
        return None
    return ts


def parse_date_text(text):
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime((text or "").strip().replace("Sept", "Sep"), fmt)
            return int(d.replace(hour=12, tzinfo=KYIV).timestamp())
        except ValueError:
            pass
    return None


def fetch_items(http, appids, country, language):
    """Пакетний запит до IStoreBrowseService. Повертає {appid: info}."""
    result = {}
    for start in range(0, len(appids), 50):
        chunk = appids[start : start + 50]
        payload = {
            "ids": [{"appid": a} for a in chunk],
            "context": {"language": language, "country_code": country},
            "data_request": {"include_release": True, "include_assets": True},
        }
        r = http.get(ITEMS_URL, params={"input_json": json.dumps(payload)}, timeout=30)
        r.raise_for_status()
        for it in r.json().get("response", {}).get("store_items", []):
            appid = it.get("appid") or it.get("id")
            if not appid or not it.get("success", 1) == 1 or not it.get("name"):
                continue
            release = it.get("release") or {}
            buy = it.get("best_purchase_option") or {}
            discounts = buy.get("active_discounts") or []
            end = max((d.get("discount_end_date") or 0 for d in discounts), default=0)
            assets = it.get("assets") or {}
            image = None
            if assets.get("asset_url_format") and assets.get("header"):
                image = ASSET_BASE + assets["asset_url_format"].replace("${FILENAME}", assets["header"])
            result[int(appid)] = {
                "name": it["name"],
                "release": release_ts(release),
                "coming_soon": bool(release.get("is_coming_soon")),
                "early_access": bool(release.get("is_early_access")),
                "is_free": bool(it.get("is_free")),
                "discount": int(buy.get("discount_pct") or 0),
                "price": buy.get("formatted_final_price") or uah(_cents(buy.get("final_price_in_cents"))),
                "old_price": buy.get("formatted_original_price")
                or uah(_cents(buy.get("original_price_in_cents"))),
                "discount_end": int(end) or None,
                "image": image,
            }
        time.sleep(1)
    return result


def fetch_appdetails(http, appids, country, language, limit=120):
    """Запасний шлях: по одному запиту на гру (повільніше, тому з лімітом)."""
    result = {}
    for appid in appids[:limit]:
        try:
            r = http.get(
                APPDETAILS_URL,
                params={"appids": appid, "cc": country.lower(), "l": language},
                timeout=15,
            )
            if r.status_code == 429:
                log.info("Steam просить зачекати — решту перевірю наступного разу")
                break
            r.raise_for_status()
            info = r.json().get(str(appid), {})
            if not info.get("success"):
                continue
            d = info["data"]
            price = d.get("price_overview") or {}
            result[appid] = {
                "name": d.get("name", str(appid)),
                "release": parse_date_text((d.get("release_date") or {}).get("date")),
                "coming_soon": bool((d.get("release_date") or {}).get("coming_soon")),
                "early_access": False,
                "is_free": bool(d.get("is_free")),
                "discount": int(price.get("discount_percent") or 0),
                "price": price.get("final_formatted"),
                "old_price": price.get("initial_formatted"),
                "discount_end": None,
                "image": d.get("header_image"),
            }
        except Exception as e:  # noqa: BLE001
            log.info("appdetails %s: %s", appid, e)
        time.sleep(1.6)  # ~190 запитів / 5 хв — межа Steam
    return result


# ---------- пости ----------

def header_image(appid, info):
    return info.get("image") or (
        f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
    )


def format_post(appid, info, released, discounted, esc):
    link = f'<a href="{STORE_URL.format(id=appid)}">{esc(info["name"])}</a>'
    lines = []
    if released and discounted:
        lines.append(f"🚀🔥 <b>Вийшла і вже зі знижкою −{info['discount']}%:</b> {link}")
    elif released:
        lines.append(f"🚀 <b>Вийшла гра з твого вішліста:</b> {link}")
    else:
        lines.append(f"🔥 <b>Знижка −{info['discount']}%:</b> {link}")

    if info.get("is_free"):
        lines.append("💰 Безкоштовно")
    elif info.get("price"):
        if info["discount"] and info.get("old_price"):
            lines.append(f"💰 {esc(info['price'])} замість <s>{esc(info['old_price'])}</s>")
        else:
            lines.append(f"💰 {esc(info['price'])}")
    if discounted and info.get("discount_end"):
        end = datetime.fromtimestamp(info["discount_end"], KYIV)
        lines.append(f"⏳ До {end:%d.%m} {end:%H:%M}")

    tags = ["#вішліст"]
    if released:
        tags.append("#реліз")
    if discounted:
        tags.append("#знижка")
    return "\n".join(lines) + "\n\n" + " ".join(tags)


def format_digest(sale, esc):
    """Підбірка знижок з вішліста (коли їх багато одночасно — зазвичай старт розпродажу Steam)."""
    sale = sorted(sale, key=lambda e: -e[1]["discount"])
    lines = [f"🔥 <b>Розпродаж: {len(sale)} ігор з твого вішліста зі знижкою</b>", ""]
    for appid, cur, _, _ in sale[:30]:
        link = f'<a href="{STORE_URL.format(id=appid)}">{esc(cur["name"])}</a>'
        price = ""
        if cur.get("price"):
            price = f" · {esc(cur['price'])}"
            if cur.get("old_price"):
                price += f" (було {esc(cur['old_price'])})"
        lines.append(f"• {link} — −{cur['discount']}%{price}")
    if len(sale) > 30:
        lines.append(f"…і ще {len(sale) - 30}")
    ends = {e[1].get("discount_end") for e in sale if e[1].get("discount_end")}
    if len(ends) == 1:
        end = datetime.fromtimestamp(ends.pop(), KYIV)
        lines += ["", f"⏳ До {end:%d.%m} {end:%H:%M}"]
    return "\n".join(lines) + "\n\n#вішліст #знижка #розпродаж"


def remember(cur):
    return {
        "coming_soon": cur["coming_soon"],
        "discount": cur["discount"],
        "name": cur["name"],
        "release": cur.get("release"),
        "price": cur.get("price") if not cur.get("is_free") else "безкоштовно",
    }


class WishlistUnavailable(Exception):
    pass


def check(http, state, cfg, publish, esc, force=False):
    """Повертає кількість опублікованих постів. Якщо вішліст недоступний — WishlistUnavailable."""
    ws = state.setdefault("wishlist", {"apps": {}, "last_check": 0})
    every = int(cfg.get("check_every_minutes", 60)) * 60
    if not force and time.time() - ws.get("last_check", 0) < every - 120:
        return 0

    steamid = ws.get("steamid") if ws.get("profile") == cfg.get("profile") else None
    if not steamid:
        steamid = resolve_steamid(http, cfg.get("profile"))
        if not steamid:
            raise WishlistUnavailable("не знайдено профіль Steam (wishlist.profile у feeds.yaml)")
        ws["steamid"], ws["profile"] = steamid, cfg.get("profile")

    try:
        appids = fetch_wishlist(http, steamid)
    except Exception as e:  # noqa: BLE001
        raise WishlistUnavailable(f"Steam не віддав вішліст ({e}). Перевір, що він публічний.") from e
    if not appids:
        raise WishlistUnavailable("вішліст порожній або приватний")

    country = cfg.get("country", "UA")
    language = cfg.get("language", "english")
    try:
        info = fetch_items(http, appids, country, language)
    except Exception as e:  # noqa: BLE001
        log.info("GetItems не спрацював (%s), перевіряю по одній грі", e)
        info = {}
    if len(info) < len(appids) * 0.5:
        missing = [a for a in appids if a not in info]
        info.update(fetch_appdetails(http, missing, country, language))

    min_discount = int(cfg.get("min_discount", 1))
    notify_release = cfg.get("notify_release", True)
    notify_discount = cfg.get("notify_discount", True)
    known = ws["apps"]
    posted = 0

    events = []
    for appid in appids:
        cur = info.get(appid)
        if not cur:
            continue
        key = str(appid)
        prev = known.get(key)
        if prev is None:
            # гра щойно з'явилась у вішлісті — просто запам'ятовуємо стан
            known[key] = remember(cur)
            continue
        released = notify_release and prev["coming_soon"] and not cur["coming_soon"]
        discounted = (
            notify_discount
            and cur["discount"] >= min_discount
            and cur["discount"] > prev["discount"]
        )
        if released or discounted:
            events.append((appid, cur, released, discounted))
        else:
            known[key] = remember(cur)

    # великий розпродаж: багато знижок одночасно — один пост-підбірка замість десятка окремих
    sale = [e for e in events if e[3] and not e[2]]
    digest_min = int(cfg.get("sale_digest_min", 4))
    if len(sale) >= digest_min:
        events = [e for e in events if e not in sale]
        try:
            publish(format_digest(sale, esc), None, STORE_URL.format(id=sale[0][0]))
            posted += 1
            for appid, cur, _, _ in sale:
                known[str(appid)] = remember(cur)
            log.info("🔥 Вішліст: підбірка знижок (%d ігор)", len(sale))
            time.sleep(1.5)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося опублікувати підбірку знижок: %s", e)

    for appid, cur, released, discounted in events:
        text = format_post(appid, cur, released, discounted, esc)
        try:
            publish(text, header_image(appid, cur), STORE_URL.format(id=appid))
            posted += 1
            log.info("🕹 Вішліст: %s (%s)", cur["name"], "реліз" if released else f"−{cur['discount']}%")
            time.sleep(1.5)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося опублікувати пост про %s: %s", cur["name"], e)
            continue  # стан не оновлюємо — спробуємо наступного разу
        known[str(appid)] = remember(cur)

    # прибираємо ігри, яких уже немає у вішлісті
    current = {str(a) for a in appids}
    for key in [k for k in known if k not in current]:
        del known[key]

    ws["last_check"] = time.time()
    sample = next((v["price"] for v in info.values() if v.get("price")), None)
    if sample and "₴" not in sample and "грн" not in sample.lower():
        log.warning("Вішліст: ціни приходять не в гривнях (%s) — перевір country у feeds.yaml", sample)
    log.info("Вішліст: перевірено %d ігор, постів %d", len(info), posted)
    return posted
