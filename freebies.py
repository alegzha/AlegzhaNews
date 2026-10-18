"""
Халява: безкоштовні ігри, які можна забрати назавжди (Epic Games Store, Steam, GOG…).
Дані — з безкоштовного API GamerPower (без ключів).
"""

import logging
import re
import time
from datetime import datetime

from wishlist import KYIV

log = logging.getLogger("news-bot")

API = "https://www.gamerpower.com/api/giveaways"
PLATFORM_LABELS = {
    "Epic Games Store": "Epic Games Store",
    "Steam": "Steam",
    "GOG": "GOG",
    "PC": "PC",
}


def _end(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def format_post(g, cfg, esc):
    platforms = [p.strip() for p in (g.get("platforms") or "").split(",") if p.strip()]
    shown = [p for p in platforms if p in PLATFORM_LABELS and p != "PC"] or platforms[:2]
    url = g.get("open_giveaway_url") or g.get("gamerpower_url") or ""
    title = re.sub(r"\s*\((?:Epic Games|Steam|GOG|PC)[^)]*\)", "", g.get("title", ""))
    title = re.sub(r"\s+(Giveaway|Free Game)$", "", title, flags=re.I).strip()
    lines = [f"{cfg.get('emoji', '🎁')} <b>Безкоштовно назавжди:</b> <a href=\"{url}\">{esc(title)}</a>"]
    if shown:
        lines.append("🛒 " + esc(", ".join(shown)))
    worth = (g.get("worth") or "").strip()
    if worth and worth.upper() != "N/A":
        lines.append(f"💰 Зазвичай коштує {esc(worth)}")
    end = _end(g.get("end_date"))
    if end:
        lines.append(f"⏳ Забрати до {end:%d.%m}")
    return "\n".join(lines) + f"\n\n{cfg.get('hashtag', '#халява')} #ігри"


def check(http, state, cfg, publish, esc):
    """Повертає кількість постів."""
    st = state.setdefault("freebies", {"seen": {}, "last_check": 0})
    every = int(cfg.get("check_every_minutes", 120)) * 60
    if time.time() - st.get("last_check", 0) < every - 120:
        return 0
    platforms = cfg.get("platforms", "epic-games-store.steam.gog")
    r = http.get(API, params={"platform": platforms, "type": "game", "sort-by": "date"}, timeout=20)
    if r.status_code == 201:  # GamerPower: «немає активних роздач»
        items = []
    else:
        r.raise_for_status()
        items = r.json() if isinstance(r.json(), list) else []
    first_time = not st["seen"]
    now = time.time()
    posted = 0
    for g in reversed(items[:30]):  # від старіших до новіших
        gid = str(g.get("id"))
        if not gid or gid in st["seen"]:
            continue
        if g.get("status", "Active") != "Active":
            continue
        st["seen"][gid] = now
        published = _end(g.get("published_date"))
        if first_time and (not published or (datetime.now() - published).days > 3):
            continue  # перший запуск: старі роздачі лише запам'ятовуємо, свіжі публікуємо
        try:
            publish(format_post(g, cfg, esc), g.get("image") or g.get("thumbnail"), g.get("open_giveaway_url", ""))
            posted += 1
            log.info("🎁 Халява: %s", g.get("title"))
            time.sleep(1.5)
        except Exception as e:  # noqa: BLE001
            st["seen"].pop(gid, None)
            log.error("Халява: не вдалося опублікувати %s: %s", g.get("title"), e)
    # забуваємо роздачі старші за 60 днів
    st["seen"] = {k: v for k, v in st["seen"].items() if now - v < 60 * 86400}
    st["last_check"] = now
    log.info("Халява: активних роздач %d, нових постів %d", len(items), posted)
    return posted
