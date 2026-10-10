"""
Сповіщення про поломки: бот пише власнику в особисті, коли щось зламалось,
і ще раз — коли запрацювало. Повторно про ту саму проблему нагадує не частіше,
ніж раз на repeat_hours, щоб не спамити.

Потрібен секрет TELEGRAM_ADMIN_ID — твій Telegram id (бот показує його у відповідь на /start).
"""

import html
import logging
import time

log = logging.getLogger("news-bot")


def _ago(seconds):
    h = int(seconds // 3600)
    if h >= 48:
        return f"{h // 24} дн."
    if h >= 1:
        return f"{h} год"
    return f"{max(1, int(seconds // 60))} хв"


class Alerts:
    def __init__(self, state, send):
        """send(text) — відправляє повідомлення власнику (або None, якщо його не задано)."""
        self.items = state.setdefault("alerts", {})
        self.send = send

    def _send(self, text):
        if not self.send:
            log.info("[сповіщення] %s", text)
            return True
        try:
            self.send(text)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося надіслати сповіщення: %s", e)
            return False

    def problem(self, key, label, detail="", after_hours=0.0, repeat_hours=24.0):
        """Фіксує проблему. Сповіщення — якщо вона триває довше за after_hours."""
        now = time.time()
        a = self.items.setdefault(key, {"since": now, "sent": 0})
        a["label"], a["detail"] = label, detail
        lasting = now - a["since"]
        if lasting < after_hours * 3600 or now - a["sent"] < repeat_hours * 3600:
            return
        text = f"⚠️ <b>{html.escape(label)}</b>"
        if detail:
            text += f"\n{html.escape(detail[:600])}"
        if lasting >= 3600:
            text += f"\n\n🕒 Триває вже {_ago(lasting)}"
        if self._send(text):
            a["sent"] = now

    def ok(self, key):
        """Проблему розв'язано. Якщо про неї вже повідомляли — пишемо, що відновилось."""
        a = self.items.pop(key, None)
        if a and a.get("sent"):
            took = _ago(time.time() - a["since"])
            self._send(f"✅ <b>Відновилось:</b> {html.escape(a['label'])} (не працювало {took})")

    def ok_prefix(self, prefix, keep):
        """Закриває проблеми з ключем prefix*, яких більше немає серед keep (джерело видалили)."""
        for key in [k for k in self.items if k.startswith(prefix) and k not in keep]:
            self.items.pop(key, None)
