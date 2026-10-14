"""
Нові релізи улюблених виконавців зі Spotify.

Бот бере список виконавців, на яких ти підписаний у Spotify, і по черзі перевіряє
їхні дискографії. Новий альбом, EP чи сингл → пост з обкладинкою і посиланням.

Потрібні секрети GitHub: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
(refresh token видає сторінка воркера /spotify/login після входу в Spotify).
"""

import base64
import logging
import os
import time
from datetime import date, timedelta

log = logging.getLogger("news-bot")

API = "https://api.spotify.com/v1"
CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()


class SpotifyError(Exception):
    pass


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


def access_token(http):
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = http.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        headers={"Authorization": f"Basic {auth}"},
        timeout=20,
    )
    if r.status_code in (400, 401):
        raise SpotifyError(
            f"Spotify не прийняв ключ ({r.status_code}: {r.text[:150]}). "
            "Увійди ще раз через сторінку воркера /spotify/login і онови секрет SPOTIFY_REFRESH_TOKEN."
        )
    r.raise_for_status()
    return r.json()["access_token"]


class Api:
    def __init__(self, http, token):
        self.http, self.token = http, token
        self.calls = 0

    def get(self, url, params=None):
        if not url.startswith("http"):
            url = API + url
        for _ in range(3):
            self.calls += 1
            r = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5") or 5)
                if wait > 30:
                    raise SpotifyError(f"Spotify просить зачекати {wait} с — продовжу наступного разу")
                time.sleep(wait + 1)
                continue
            if r.status_code == 403:
                raise SpotifyError(
                    f"Spotify відмовив у доступі (403: {r.text[:150]}). Перевір, що Premium активний "
                    "і твій акаунт доданий у User Management застосунку."
                )
            r.raise_for_status()
            return r.json()
        raise SpotifyError("Spotify постійно відповідає 429")


def followed_artists(api):
    artists, url = [], "/me/following"
    params = {"type": "artist", "limit": 50}
    while url:
        data = api.get(url, params).get("artists", {})
        for a in data.get("items", []):
            artists.append(a["id"])  # лише id: state.json лежить у публічному репозиторії
        url, params = data.get("next"), None
    return artists


def latest_releases(api, artist_id, groups):
    """Останні релізи кожного типу окремо: Spotify групує відповідь за типами,
    тож у спільному запиті свіжі сингли могли б не влізти після старих альбомів."""
    items = []
    for group in [g.strip() for g in groups.split(",") if g.strip()]:
        data = api.get(f"/artists/{artist_id}/albums", {"include_groups": group, "limit": 10})
        items.extend(data.get("items", []))
    return items


def release_day(album):
    raw = album.get("release_date") or ""
    precision = album.get("release_date_precision")
    try:
        if precision == "day":
            return date.fromisoformat(raw)
        if precision == "month":
            return date.fromisoformat(raw + "-01")
    except ValueError:
        pass
    return None


def kind(album):
    t = album.get("album_type")
    n = album.get("total_tracks") or 0
    if t == "album":
        return "Альбом"
    if t == "single":
        return "EP" if n >= 4 else "Сингл"
    if t == "compilation":
        return "Збірка"
    return "Реліз"


def format_post(album, artist_name, cfg, esc):
    url = (album.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/album/{album['id']}"
    artists = ", ".join(a["name"] for a in album.get("artists", [])) or artist_name
    n = album.get("total_tracks") or 0
    day = release_day(album)
    info = [kind(album)]
    if n > 1:
        last2, last = n % 100, n % 10
        word = "трек" if last == 1 and last2 != 11 else "треки" if 2 <= last <= 4 and not 12 <= last2 <= 14 else "треків"
        info.append(f"{n} {word}")
    if day:
        info.append(day.strftime("%d.%m.%Y"))
    return (
        f"{cfg.get('emoji', '🎧')} <b>{esc(artists)}</b> — <a href=\"{url}\">{esc(album.get('name', ''))}</a>\n"
        f"💿 {' · '.join(info)}\n\n"
        f"{cfg.get('hashtag', '#нові_релізи')}"
    )


def check(http, state, cfg, publish, esc):
    """Повертає кількість постів. publish(text, image_url, link)."""
    if not configured():
        log.info("Spotify: секрети не задано — пропускаю")
        return 0
    st = state.setdefault("spotify", {"artists": [], "seen": {}, "cursor": 0, "artists_checked": 0})
    if isinstance(st.get("artists"), dict):
        st["artists"] = sorted(st["artists"])
    groups = cfg.get("include_groups", "album,single")
    per_run = int(cfg.get("artists_per_run", 40))
    fresh_days = int(cfg.get("max_age_days", 14))

    api = Api(http, access_token(http))

    # список підписок оновлюємо раз на 6 годин
    if time.time() - st.get("artists_checked", 0) > 6 * 3600 or not st["artists"]:
        st["artists"] = followed_artists(api)
        st["artists_checked"] = time.time()
        log.info("Spotify: підписок на виконавців — %d", len(st["artists"]))

    ids = sorted(st["artists"])
    following = set(ids)
    if not ids:
        log.info("Spotify: ти ні на кого не підписаний")
        return 0
    start = st.get("cursor", 0) % len(ids)
    batch = (ids + ids)[start:start + min(per_run, len(ids))]
    seen = st["seen"]  # artist_id -> [album ids]
    today = date.today()
    posted = 0

    for i, artist_id in enumerate(batch):
        try:
            albums = latest_releases(api, artist_id, groups)
        except SpotifyError:
            st["cursor"] = (start + i) % len(ids)
            raise
        except Exception as e:  # noqa: BLE001
            log.info("Spotify: виконавець %s — %s", artist_id, e)
            continue
        known = set(seen.get(artist_id, []))
        first_time = artist_id not in seen
        for album in sorted(albums, key=lambda a: a.get("release_date") or ""):
            if album["id"] in known:
                continue
            known.add(album["id"])
            day = release_day(album)
            if first_time or not day or (today - day) > timedelta(days=fresh_days) or day > today:
                continue
            images = album.get("images") or []
            image = images[0]["url"] if images else None
            text = format_post(album, "", cfg, esc)
            try:
                publish(text, image, (album.get("external_urls") or {}).get("spotify", ""))
                posted += 1
                log.info("🎧 Spotify: %s", album.get("name"))
                time.sleep(3)
            except Exception as e:  # noqa: BLE001
                known.discard(album["id"])  # спробуємо наступного разу
                log.error("Spotify: не вдалося опублікувати %s: %s", album.get("name"), e)
        current = [a["id"] for a in albums if a["id"] in known]  # без тих, що не вдалося опублікувати
        seen[artist_id] = (current + [x for x in seen.get(artist_id, []) if x not in current and x in known])[:100]
        for x in known:
            if x not in seen[artist_id] and len(seen[artist_id]) < 100:
                seen[artist_id].append(x)
        time.sleep(0.3)

    st["cursor"] = (start + len(batch)) % len(ids)
    # прибираємо тих, від кого відписався
    for artist_id in [a for a in seen if a not in following]:
        del seen[artist_id]
    log.info("Spotify: перевірено %d виконавців (%d запитів), постів %d", len(batch), api.calls, posted)
    return posted
