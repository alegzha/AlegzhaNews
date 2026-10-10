"""
«Релізи тижня»: раз на тиждень (типово в понеділок о 10:00 за Києвом) — пост
з іграми, що виходять у найближчі 7 днів:
  ⭐ ігри з твого вішліста (з цінами в гривнях);
  🔥 найочікуваніші релізи Steam (розділ «Популярні майбутні»).
"""

import logging
import re
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from wishlist import KYIV, STORE_URL

log = logging.getLogger("news-bot")

SEARCH_URL = "https://store.steampowered.com/search/results/"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
DAY_NAMES = {
    "mon": 0, "monday": 0, "пн": 0, "tue": 1, "tuesday": 1, "вт": 1,
    "wed": 2, "wednesday": 2, "ср": 2, "thu": 3, "thursday": 3, "чт": 3,
    "fri": 4, "friday": 4, "пт": 4, "sat": 5, "saturday": 5, "сб": 5,
    "sun": 6, "sunday": 6, "нд": 6,
}
DATE_FORMATS = ["%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%d %b %Y", "%b %d %Y"]


def parse_steam_date(text):
    """«13 Oct, 2026» -> date. Неточні дати («Q4 2026», «October 2026», «Coming soon») -> None."""
    text = re.sub(r"\s+", " ", (text or "").strip()).replace("Sept", "Sep")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def popular_upcoming(http, country, count=100):
    """Популярні майбутні ігри Steam у порядку популярності: [(appid, name, date)]."""
    r = http.get(
        SEARCH_URL,
        params={
            "filter": "popularcomingsoon",
            "category1": 998,  # лише ігри, без DLC і саундтреків
            "infinite": 1,
            "count": count,
            "cc": country,
            "l": "english",
        },
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.json().get("results_html", ""), "html.parser")
    out = []
    for row in soup.select("a.search_result_row"):
        appid = (row.get("data-ds-appid") or "").split(",")[0]
        title = row.select_one(".title")
        released = row.select_one(".search_released")
        if not appid.isdigit() or not title:
            continue
        day = parse_steam_date(released.get_text(" ", strip=True) if released else "")
        out.append((int(appid), title.get_text(strip=True), day))
    return out


def date_range_label(start, end):
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


def line(day, appid, name, esc, extra=""):
    link = f'<a href="{STORE_URL.format(id=appid)}">{esc(name)}</a>'
    return f"• {WEEKDAYS[day.weekday()]} {day:%d.%m} — {link}{extra}"


def due(cfg, state, now):
    """Чи настав час для щотижневого поста (і чи не опублікований він уже цього тижня)."""
    day = DAY_NAMES.get(str(cfg.get("weekday", "mon")).strip().lower(), 0)
    hour = int(cfg.get("hour", 10))
    week = now.strftime("%G-W%V")
    if state.get("weekly_releases_week") == week:
        return False
    scheduled = (now - timedelta(days=now.weekday() - day)).replace(hour=hour, minute=0, second=0, microsecond=0)
    # якщо запуск у потрібний час пропустили — догоняємо протягом доби, не пізніше
    return scheduled <= now < scheduled + timedelta(hours=24)


def build(http, state, cfg, country, esc, today):
    start, end = today, today + timedelta(days=6)
    in_range = lambda d: d is not None and start <= d <= end  # noqa: E731

    # ⭐ вішліст — дані збирає wishlist.check у state["wishlist"]["apps"]
    mine = []
    for key, app in (state.get("wishlist", {}).get("apps") or {}).items():
        ts = app.get("release")
        if not ts or not app.get("name"):
            continue
        day = datetime.fromtimestamp(ts, KYIV).date()
        if in_range(day):
            price = app.get("price")
            extra = f" — {esc(price)}" if price else ""
            mine.append((day, int(key), app["name"], extra))
    mine.sort()
    mine_ids = {m[1] for m in mine}

    # 🔥 популярне в Steam
    top = []
    try:
        for appid, name, day in popular_upcoming(http, country):
            if in_range(day) and appid not in mine_ids:
                top.append((day, appid, name))
            if len(top) >= int(cfg.get("top", 10)):
                break
    except Exception as e:  # noqa: BLE001
        log.warning("Релізи тижня: не вдалося отримати популярні релізи Steam (%s)", e)
        if not mine:
            raise
    top.sort()

    if not mine and not top:
        return None
    parts = [f"📅 <b>Релізи тижня ({date_range_label(start, end)})</b>"]
    if mine:
        parts.append("⭐ <b>З твого вішліста:</b>\n" + "\n".join(line(d, a, n, esc, x) for d, a, n, x in mine[:20]))
    if top:
        parts.append("🔥 <b>Найочікуваніші в Steam:</b>\n" + "\n".join(line(d, a, n, esc) for d, a, n in top))
    parts.append("#релізи #ігри")
    return "\n\n".join(parts)


def run(http, state, cfg, country, send, esc, force=False):
    """Повертає 1, якщо пост опубліковано. send(text) кидає виняток у разі помилки."""
    now = datetime.now(KYIV)
    if not force and not due(cfg, state, now):
        return 0
    text = build(http, state, cfg, country, esc, now.date())
    if text:
        send(text)
        log.info("📅 Релізи тижня опубліковано")
    else:
        log.info("📅 Релізи тижня: цього тижня нічого цікавого не виходить")
    if not force:
        state["weekly_releases_week"] = now.strftime("%G-W%V")
    return 1 if text else 0
