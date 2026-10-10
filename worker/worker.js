/**
 * Кнопка «Новини зараз» для Telegram-бота.
 *
 * Cloudflare Worker постійно слухає повідомлення боту. Коли власник
 * натискає кнопку або пише /news, воркер запускає GitHub Actions
 * (workflow news.yml), і бот одразу перевіряє джерела та публікує новини.
 *
 * Змінні воркера (Settings → Variables and Secrets):
 *   TG_TOKEN        (secret) — токен бота від @BotFather
 *   GH_TOKEN        (secret) — GitHub fine-grained token з правом Actions: Read and write
 *   WEBHOOK_SECRET  (secret) — будь-яка довга випадкова фраза (латиниця, цифри, _ або -)
 *   GH_REPO         (text)   — alegzha/AlegzhaNews
 *   OWNER_ID        (text)   — твій Telegram id (бот сам підкаже його після /start)
 */

const BUTTON_NEWS = "🔄 Новини зараз";
const WORKFLOW_FILE = "news.yml";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Одноразове налаштування: підключає воркер до Telegram.
    if (url.pathname === "/setup") {
      if (url.searchParams.get("key") !== env.WEBHOOK_SECRET) {
        return new Response("Неправильний key", { status: 403 });
      }
      const hook = await tg(env, "setWebhook", {
        url: `${url.origin}/telegram`,
        secret_token: env.WEBHOOK_SECRET,
        allowed_updates: ["message"],
        drop_pending_updates: true,
      });
      await tg(env, "setMyCommands", {
        commands: [{ command: "news", description: "Перевірити й запостити новини зараз" }],
      });
      const ok = hook && hook.ok;
      return new Response(
        ok
          ? "✅ Готово! Тепер напиши своєму боту /start у Telegram."
          : "❌ Не вдалося підключити: " + JSON.stringify(hook),
        { headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }

    // Повідомлення від Telegram.
    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json().catch(() => ({}));
      try {
        await handle(update, env);
      } catch (e) {
        console.log("handle error", e && e.stack);
      }
      return new Response("ok"); // Telegram має отримати 200, інакше повторюватиме
    }

    return new Response("News bot worker працює", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function handle(update, env) {
  const msg = update.message;
  if (!msg || !msg.text || msg.chat.type !== "private") return;

  const chatId = msg.chat.id;
  const userId = String((msg.from && msg.from.id) || "");
  const text = msg.text.trim();

  // Поки власника не вказано — підказуємо його id.
  if (!env.OWNER_ID) {
    await send(env, chatId,
      `Твій Telegram id: <code>${userId}</code>\n\n` +
      "Додай його у змінну <b>OWNER_ID</b> воркера (Settings → Variables and Secrets), " +
      "натисни Deploy і знову напиши /start.");
    return;
  }

  // Кнопка працює лише для власника.
  if (userId !== String(env.OWNER_ID).trim()) {
    await send(env, chatId, "Цей бот приватний 🙂");
    return;
  }

  if (text.startsWith("/start") || text.startsWith("/help")) {
    await send(env, chatId,
      "Натисни кнопку внизу або напиши /news — і я одразу перевірю всі джерела " +
      "та опублікую нові новини в канал.",
      keyboard());
    return;
  }

  if (text === BUTTON_NEWS || text.startsWith("/news")) {
    const res = await fetch(
      `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "news-bot-worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    if (res.status === 204) {
      await send(env, chatId,
        "🔄 Запустив перевірку. Нові пости з'являться в каналі приблизно за 1–2 хвилини.\n" +
        "Якщо нових новин немає — у каналі нічого не з'явиться.",
        keyboard());
    } else {
      const body = (await res.text()).slice(0, 300);
      let hint = "";
      if (res.status === 401) hint = "\nСхоже, GH_TOKEN недійсний або прострочений.";
      if (res.status === 403 || res.status === 404)
        hint = "\nПеревір GH_REPO і що GH_TOKEN має доступ до репозиторію з правом Actions: Read and write.";
      await send(env, chatId, `⚠️ GitHub відповів ${res.status}.${hint}\n<code>${escapeHtml(body)}</code>`, keyboard());
    }
    return;
  }

  await send(env, chatId, "Не зрозумів 🙂 Натисни кнопку або напиши /news.", keyboard());
}

function keyboard() {
  return {
    reply_markup: {
      keyboard: [[{ text: BUTTON_NEWS }]],
      resize_keyboard: true,
      is_persistent: true,
    },
  };
}

async function send(env, chatId, text, extra = {}) {
  return tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", ...extra });
}

async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json().catch(() => null);
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
