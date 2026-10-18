/**
 * Пульт керування новинним ботом у Telegram.
 *
 * Cloudflare Worker слухає повідомлення боту і виконує команди власника:
 *   🔄 Новини зараз  /news      — перевірити джерела й опублікувати нове
 *   📋 Джерела       /sources   — список джерел за рубриками
 *   ➕ Додати        /add <url> — додати сайт (бот сам знайде RSS і спитає рубрику)
 *   ➖ Прибрати      /remove    — прибрати джерело (кнопками)
 *   📅 Релізи        /releases  — опублікувати «Релізи тижня» зараз
 *   ℹ️ Стан          /status    — як пройшов останній запуск
 * Змінює feeds.yaml у репозиторії через GitHub API — бот підхоплює це з наступного запуску.
 *
 * Змінні воркера (Settings → Variables and Secrets):
 *   TG_TOKEN        (secret) — токен бота від @BotFather
 *   GH_TOKEN        (secret) — GitHub fine-grained token до репозиторію з правами
 *                              Actions: Read and write і Contents: Read and write
 *   WEBHOOK_SECRET  (secret) — будь-яка довга випадкова фраза (латиниця, цифри, _ або -)
 *   GH_REPO         (text)   — alegzha/AlegzhaNews
 *   OWNER_ID        (text)   — твій Telegram id (бот сам підкаже його після /start)
 *   SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET (secret, необов'язково) — для /spotify/login
 *   PROXY_HOSTS     (text, необов'язково) — сайти через кому, які бот може читати
 *                    через воркер (типово: mezha.ua, bloomberg.com)
 *
 * /feed?url=… — читає RSS сайту, який не пускає сервери GitHub, і віддає боту.
 * Працює лише для сайтів зі списку PROXY_HOSTS, тож чужим цей проксі не знадобиться.
 */

const BTN = {
  news: "🔄 Новини зараз",
  sources: "📋 Джерела",
  add: "➕ Додати",
  remove: "➖ Прибрати",
  releases: "📅 Релізи",
  status: "ℹ️ Стан",
};
const WORKFLOW_FILE = "news.yml";
const FEEDS_FILE = "feeds.yaml";
const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

export default {
  // Таймер Cloudflare (кожні 30 хв, див. wrangler.toml): запускає бота на GitHub.
  // Розклад самого GitHub для безкоштовних репозиторіїв спрацьовує раз на 2–4 години.
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const busy = await gh(env, `/actions/workflows/${WORKFLOW_FILE}/runs?status=in_progress&per_page=1`)
        .then((r) => r.json()).catch(() => ({}));
      if (busy.total_count) return; // попередній запуск ще працює
      const res = await gh(env, `/actions/workflows/${WORKFLOW_FILE}/dispatches`, {
        method: "POST",
        body: JSON.stringify({ ref: "main" }),
      });
      if (res.status !== 204) console.log("scheduled dispatch failed", res.status, await res.text());
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    // Одноразове налаштування: підключає воркер до Telegram.
    if (url.pathname === "/setup") {
      if (url.searchParams.get("key") !== env.WEBHOOK_SECRET) {
        return new Response("Неправильний key", { status: 403 });
      }
      const hook = await setupWebhook(env, url.origin, true);
      const ok = hook && hook.ok;
      return new Response(
        ok
          ? "✅ Готово! Тепер напиши своєму боту /start у Telegram."
          : "❌ Не вдалося підключити: " + JSON.stringify(hook),
        { headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }

    // Проксі для RSS сайтів, які блокують сервери GitHub.
    // Одноразовий вхід у Spotify: видає refresh token для секрету SPOTIFY_REFRESH_TOKEN.
    if (url.pathname === "/spotify/login") return spotifyLogin(url, env);
    if (url.pathname === "/spotify/callback") return spotifyCallback(url, env);

    if (url.pathname === "/feed") {
      return proxyFeed(url, env);
    }

    // Повідомлення від Telegram.
    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json().catch(() => ({}));
      try {
        if (update.callback_query) await handleCallback(update.callback_query, env);
        else await handle(update, env, url.origin);
      } catch (e) {
        console.log("handle error", e && e.stack);
        const chatId =
          (update.message && update.message.chat.id) ||
          (update.callback_query && update.callback_query.message && update.callback_query.message.chat.id);
        if (chatId) await send(env, chatId, "⚠️ Щось пішло не так: " + escapeHtml(String(e && e.message || e)));
      }
      return new Response("ok"); // Telegram має отримати 200, інакше повторюватиме
    }

    return new Response("News bot worker працює", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function setupWebhook(env, origin, dropPending) {
  const hook = await tg(env, "setWebhook", {
    url: `${origin}/telegram`,
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message", "callback_query"],
    drop_pending_updates: !!dropPending,
  });
  await tg(env, "setMyCommands", {
    commands: [
      { command: "news", description: "Перевірити й запостити новини зараз" },
      { command: "sources", description: "Список джерел" },
      { command: "add", description: "Додати джерело: /add посилання" },
      { command: "remove", description: "Прибрати джерело" },
      { command: "releases", description: "Опублікувати «Релізи тижня» зараз" },
      { command: "status", description: "Як пройшов останній запуск" },
    ],
  });
  return hook;
}

// ---------- повідомлення ----------

function isOwner(env, userId) {
  return env.OWNER_ID && String(userId) === String(env.OWNER_ID).trim();
}

async function handle(update, env, origin) {
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
  if (!isOwner(env, userId)) {
    await send(env, chatId, "Цей бот приватний 🙂");
    return;
  }

  if (text.startsWith("/start") || text.startsWith("/help")) {
    await setupWebhook(env, origin, false); // вмикає кнопки під повідомленнями
    await send(env, chatId,
      "<b>Що я вмію</b>\n" +
      `${BTN.news} — перевірити джерела й опублікувати нове (/news)\n` +
      `${BTN.sources} — список джерел (/sources)\n` +
      `${BTN.add} — додати сайт: надішли мені посилання (/add)\n` +
      `${BTN.remove} — прибрати джерело (/remove)\n` +
      `${BTN.releases} — «Релізи тижня» зараз (/releases)\n` +
      `${BTN.status} — як пройшов останній запуск (/status)`,
      keyboard());
    return;
  }

  if (text === BTN.news || text.startsWith("/news")) return runWorkflow(env, chatId, false);
  if (text === BTN.releases || text.startsWith("/releases")) return runWorkflow(env, chatId, true);
  if (text === BTN.status || text.startsWith("/status")) return showStatus(env, chatId);
  if (text === BTN.sources || text.startsWith("/sources")) return showSources(env, chatId);
  if (text === BTN.remove || text.startsWith("/remove")) return askRemove(env, chatId);

  const link = (text.match(/https?:\/\/\S+/) || [])[0];
  if (link && (text.startsWith("/add") || text.startsWith("http"))) return askAdd(env, chatId, link);
  if (text === BTN.add || text.startsWith("/add")) {
    await send(env, chatId, "Надішли посилання на сайт або його RSS — я знайду стрічку і спитаю рубрику.", keyboard());
    return;
  }

  await send(env, chatId, "Не зрозумів 🙂 Натисни кнопку внизу або надішли посилання на сайт.", keyboard());
}

function keyboard() {
  return {
    reply_markup: {
      keyboard: [
        [{ text: BTN.news }],
        [{ text: BTN.sources }, { text: BTN.add }, { text: BTN.remove }],
        [{ text: BTN.releases }, { text: BTN.status }],
      ],
      resize_keyboard: true,
      is_persistent: true,
    },
  };
}

// ---------- GitHub ----------

async function gh(env, path, options = {}) {
  return fetch(`https://api.github.com/repos/${env.GH_REPO}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "news-bot-worker",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
}

function ghHint(status, need) {
  if (status === 401) return "\nСхоже, GH_TOKEN недійсний або прострочений.";
  if (status === 403 || status === 404)
    return `\nПеревір, що GH_TOKEN має доступ до репозиторію з правом ${need}: Read and write.`;
  return "";
}

async function runWorkflow(env, chatId, releases) {
  const body = { ref: "main" };
  if (releases) body.inputs = { releases: "true" };
  const res = await gh(env, `/actions/workflows/${WORKFLOW_FILE}/dispatches`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (res.status === 204) {
    await send(env, chatId,
      releases
        ? "📅 Запустив. «Релізи тижня» з'являться в каналі приблизно за 1–2 хвилини."
        : "🔄 Запустив перевірку. Нові пости з'являться в каналі приблизно за 1–2 хвилини.\n" +
          "Якщо нових новин немає — у каналі нічого не з'явиться.",
      keyboard());
  } else {
    const text = (await res.text()).slice(0, 300);
    await send(env, chatId,
      `⚠️ GitHub відповів ${res.status}.${ghHint(res.status, "Actions")}\n<code>${escapeHtml(text)}</code>`,
      keyboard());
  }
}

async function showStatus(env, chatId) {
  const res = await gh(env, `/actions/workflows/${WORKFLOW_FILE}/runs?per_page=5`);
  if (!res.ok) {
    await send(env, chatId, `⚠️ GitHub відповів ${res.status}.${ghHint(res.status, "Actions")}`);
    return;
  }
  const runs = (await res.json()).workflow_runs || [];
  if (!runs.length) {
    await send(env, chatId, "Запусків ще не було.");
    return;
  }
  const icon = (r) =>
    r.status !== "completed" ? "⏳" : r.conclusion === "success" ? "✅" : r.conclusion === "cancelled" ? "⏹" : "❌";
  const when = (r) =>
    new Date(r.created_at).toLocaleString("uk-UA", {
      timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  const how = (r) => (r.event === "schedule" ? "за розкладом" : "вручну");
  const lines = runs.map((r) => `${icon(r)} ${when(r)} — ${how(r)} · <a href="${r.html_url}">лог</a>`);
  await send(env, chatId, "<b>Останні запуски</b>\n" + lines.join("\n"), { link_preview_options: { is_disabled: true } });
}

// ---------- feeds.yaml ----------

function b64decode(s) {
  const bin = atob(s.replace(/\n/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

function b64encode(s) {
  let bin = "";
  for (const b of new TextEncoder().encode(s)) bin += String.fromCharCode(b);
  return btoa(bin);
}

async function readFeeds(env) {
  const res = await gh(env, `/contents/${FEEDS_FILE}?ref=main`);
  if (!res.ok) throw new Error(`GitHub не віддав ${FEEDS_FILE} (${res.status})${ghHint(res.status, "Contents")}`);
  const data = await res.json();
  return { text: b64decode(data.content), sha: data.sha };
}

async function writeFeeds(env, text, sha, message) {
  const res = await gh(env, `/contents/${FEEDS_FILE}`, {
    method: "PUT",
    body: JSON.stringify({ message, content: b64encode(text), sha, branch: "main" }),
  });
  if (!res.ok) {
    const body = (await res.text()).slice(0, 200);
    throw new Error(`GitHub не прийняв зміни (${res.status})${ghHint(res.status, "Contents")} ${body}`);
  }
}

const unquote = (s) => s.trim().replace(/^["']|["']$/g, "");

/** Розбирає feeds.yaml: рубрики, їхні джерела і рядки, де кожне з них записане. */
function parseFeeds(text) {
  const lines = text.split("\n");
  const cats = [];
  let inCats = false;
  let cat = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line === "categories:") { inCats = true; continue; }
    if (!inCats) continue;
    if (/^\S/.test(line) && !line.startsWith("#")) { inCats = false; cat = null; continue; }
    let m;
    if ((m = line.match(/^  ([A-Za-z0-9_]+):\s*$/))) {
      cat = { id: m[1], emoji: "", hashtag: "", feeds: [], feedsLine: -1, line: i };
      cats.push(cat);
      continue;
    }
    if (!cat) continue;
    if ((m = line.match(/^    emoji:\s*(.+)$/))) cat.emoji = unquote(m[1]);
    else if ((m = line.match(/^    hashtag:\s*(.+)$/))) cat.hashtag = unquote(m[1].replace(/\s+#.*$/, ""));
    else if (/^    feeds:\s*$/.test(line)) cat.feedsLine = i;
    else if ((m = line.match(/^      - name:\s*(.+)$/))) {
      let end = i;
      while (end + 1 < lines.length && /^ {8,}\S/.test(lines[end + 1])) end++;
      const block = lines.slice(i, end + 1).join("\n");
      const url = (block.match(/^        url:\s*(\S+)/m) || [])[1] || "";
      cat.feeds.push({ name: unquote(m[1].replace(/\s+#.*$/, "")), url, start: i, end });
    }
  }
  return { lines, cats };
}

const catLabel = (c) => `${c.emoji} ${c.hashtag || c.id}`.trim();

async function showSources(env, chatId) {
  const { text } = await readFeeds(env);
  const { cats } = parseFeeds(text);
  const parts = cats.map((c) =>
    `<b>${escapeHtml(catLabel(c))}</b>\n` +
    (c.feeds.length ? c.feeds.map((f) => `• ${escapeHtml(f.name)}`).join("\n") : "—"));
  await send(env, chatId, parts.join("\n\n") + `\n\nДодати — надішли посилання, прибрати — ${BTN.remove}.`, keyboard());
}

// ---------- додати джерело ----------

function looksLikeFeed(body) {
  return /<rss[\s>]|<feed[\s>]|<rdf:RDF/i.test(body.slice(0, 3000)) && /<item[\s>]|<entry[\s>]/i.test(body);
}

function decodeEntities(s) {
  return s
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#0?39;|&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n))
    .trim();
}

async function getText(url) {
  const res = await fetch(url, {
    headers: { "User-Agent": BROWSER_UA, Accept: "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.5" },
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return { body: await res.text(), url: res.url || url };
}

/** Знаходить RSS/Atom-стрічку сайту. Повертає {url, title, uk} або кидає помилку з поясненням. */
async function discoverFeed(input) {
  let start = new URL(input);
  const host = start.hostname.replace(/^www\./, "");
  if (host === "x.com" || host === "twitter.com") {
    throw new Error("X (Twitter) не дає безкоштовної стрічки. Пошукай цей акаунт у Bluesky, Mastodon чи Telegram.");
  }
  // Bluesky: у кожного профілю є RSS за адресою /profile/<нік>/rss
  const bsky = start.pathname.match(/^\/profile\/([^/]+)/);
  if (host === "bsky.app" && bsky) {
    const rss = `https://bsky.app/profile/${bsky[1]}/rss`;
    const r = await getText(rss).catch(() => null);
    if (!r || !looksLikeFeed(r.body)) throw new Error("Не вдалося відкрити стрічку цього профілю Bluesky.");
    const info = describeFeed(rss, r.body);
    return { ...info, title: `${bsky[1].replace(/\.bsky\.social$/, "")} (Bluesky)` };
  }
  const candidates = [start.toString()];
  const tried = new Set();
  let page = null;
  try {
    page = await getText(start.toString());
  } catch (e) {
    page = null;
  }
  if (page && looksLikeFeed(page.body)) return describeFeed(page.url, page.body);
  if (page) {
    const re = /<link\b[^>]*>/gi;
    let m;
    const found = [];
    while ((m = re.exec(page.body))) {
      const tag = m[0];
      if (!/rel=["']?alternate/i.test(tag) || !/(rss|atom)\+xml/i.test(tag)) continue;
      const href = (tag.match(/href=["']([^"']+)["']/i) || [])[1];
      if (href && !/comments/i.test(href)) found.push(new URL(decodeEntities(href), page.url).toString());
    }
    candidates.push(...found);
  }
  const base = new URL(page ? page.url : start.toString());
  const root = base.origin + base.pathname.replace(/\/?$/, "/");
  for (const p of ["feed/", "feed", "rss", "rss.xml", "feed.xml", "index.xml", "atom.xml"]) {
    candidates.push(root + p);
    candidates.push(base.origin + "/" + p);
  }
  for (const c of candidates) {
    if (tried.has(c) || tried.size >= 12) continue;
    tried.add(c);
    try {
      const r = await getText(c);
      if (looksLikeFeed(r.body)) return describeFeed(r.url, r.body);
    } catch (e) {
      // пробуємо далі
    }
  }
  throw new Error(page
    ? "Не знайшов на цьому сайті RSS-стрічки. Напиши в чат з Claude — підключимо інакше."
    : "Сайт не відкрився (можливо, він блокує сервери). Напиши в чат з Claude — підключимо інакше.");
}

function describeFeed(url, body) {
  const head = body.split(/<item[\s>]|<entry[\s>]/i)[0];
  let title = decodeEntities((head.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1] || "");
  title = title.replace(/\s*[–—|:-]\s*(RSS|Feed|Стрічка).*$/i, "").replace(/\s+/g, " ").slice(0, 40) || new URL(url).hostname;
  const sample = body.slice(0, 20000);
  const ukLetters = (sample.match(/[іїєґІЇЄҐ]/g) || []).length;
  return { url, title, uk: ukLetters > 20 };
}

async function askAdd(env, chatId, link) {
  await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });
  let feed;
  try {
    feed = await discoverFeed(link);
  } catch (e) {
    await send(env, chatId, "❌ " + escapeHtml(e.message), keyboard());
    return;
  }
  const { text } = await readFeeds(env);
  const { cats } = parseFeeds(text);
  const already = cats.filter((c) => c.feeds.some((f) => f.url === feed.url)).map(catLabel);
  const buttons = cats.map((c) => [{ text: catLabel(c), callback_data: `add:${c.id}` }]);
  buttons.push([{ text: "Скасувати", callback_data: "cancel" }]);
  await send(env, chatId,
    `Знайшов стрічку.\nНазва: ${escapeHtml(feed.title)}\nRSS: ${escapeHtml(feed.url)}\n` +
    `Мова: ${feed.uk ? "українська (без перекладу)" : "інша (перекладатиму)"}\n` +
    (already.length ? `\n⚠️ Вже є в: ${escapeHtml(already.join(", "))}\n` : "") +
    "\nВ яку рубрику додати?",
    { reply_markup: { inline_keyboard: buttons }, link_preview_options: { is_disabled: true } });
}

function yamlString(s) {
  return '"' + s.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}

async function doAdd(env, cq, catId) {
  const t = cq.message.text || "";
  const url = (t.match(/^RSS: (\S+)$/m) || [])[1];
  const title = (t.match(/^Назва: (.+)$/m) || [])[1];
  const uk = /^Мова: українська/m.test(t);
  if (!url || !title) throw new Error("не бачу посилання в повідомленні — надішли його ще раз");

  const { text, sha } = await readFeeds(env);
  const { lines, cats } = parseFeeds(text);
  const cat = cats.find((c) => c.id === catId);
  if (!cat || cat.feedsLine < 0) throw new Error("рубрику не знайдено у feeds.yaml");
  if (cat.feeds.some((f) => f.url === url)) {
    await editMessage(env, cq, `Це джерело вже є в ${escapeHtml(catLabel(cat))} 🙂`);
    return;
  }
  const at = cat.feeds.length ? cat.feeds[cat.feeds.length - 1].end + 1 : cat.feedsLine + 1;
  const entry = [`      - name: ${yamlString(title)}`, `        url: ${url}`];
  if (uk) entry.push("        lang: uk");
  lines.splice(at, 0, ...entry);
  await writeFeeds(env, lines.join("\n"), sha, `Додано джерело ${title} (${catId}) з Telegram`);
  await editMessage(env, cq,
    `✅ Додав <b>${escapeHtml(title)}</b> у ${escapeHtml(catLabel(cat))}.\n` +
    `Перші новини з нього — з наступної перевірки (до 30 хв) або натисни ${BTN.news}.`);
}

// ---------- прибрати джерело ----------

function cbName(name) {
  // callback_data — максимум 64 байти
  let s = name;
  while (new TextEncoder().encode(s).length > 24) s = s.slice(0, -1);
  return s;
}

async function askRemove(env, chatId) {
  const { text } = await readFeeds(env);
  const { cats } = parseFeeds(text);
  const buttons = [];
  for (const c of cats) {
    c.feeds.forEach((f, i) => {
      buttons.push([{ text: `${c.emoji} ${f.name}`, callback_data: `rm:${c.id}:${i}:${cbName(f.name)}` }]);
    });
  }
  buttons.push([{ text: "Скасувати", callback_data: "cancel" }]);
  await send(env, chatId, "Яке джерело прибрати?", { reply_markup: { inline_keyboard: buttons } });
}

function findFeed(cats, catId, index, namePrefix) {
  const cat = cats.find((c) => c.id === catId);
  const feed = cat && cat.feeds[+index];
  if (!feed || !feed.name.startsWith(namePrefix)) return null;
  return { cat, feed };
}

async function confirmRemove(env, cq, data) {
  const [, catId, index, ...rest] = data.split(":");
  const namePrefix = rest.join(":");
  const { text } = await readFeeds(env);
  const found = findFeed(parseFeeds(text).cats, catId, index, namePrefix);
  if (!found) throw new Error("список змінився — натисни «Прибрати» ще раз");
  await editMessage(env, cq,
    `Прибрати <b>${escapeHtml(found.feed.name)}</b> з ${escapeHtml(catLabel(found.cat))}?`,
    { inline_keyboard: [[
      { text: "Так, прибрати", callback_data: "rmok:" + data.slice(3) },
      { text: "Ні", callback_data: "cancel" },
    ]] });
}

async function doRemove(env, cq, data) {
  const [, catId, index, ...rest] = data.split(":");
  const namePrefix = rest.join(":");
  const { text, sha } = await readFeeds(env);
  const { lines, cats } = parseFeeds(text);
  const found = findFeed(cats, catId, index, namePrefix);
  if (!found) throw new Error("список змінився — натисни «Прибрати» ще раз");
  lines.splice(found.feed.start, found.feed.end - found.feed.start + 1);
  await writeFeeds(env, lines.join("\n"), sha, `Прибрано джерело ${found.feed.name} (${catId}) з Telegram`);
  await editMessage(env, cq, `🗑 Прибрав <b>${escapeHtml(found.feed.name)}</b> з ${escapeHtml(catLabel(found.cat))}.`);
}

// ---------- кнопки під повідомленнями ----------

async function handleCallback(cq, env) {
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
  if (!isOwner(env, cq.from && cq.from.id) || !cq.message) return;
  const data = cq.data || "";
  try {
    if (data === "cancel") await editMessage(env, cq, "Скасовано.");
    else if (data.startsWith("add:")) await doAdd(env, cq, data.slice(4));
    else if (data.startsWith("rm:")) await confirmRemove(env, cq, data);
    else if (data.startsWith("rmok:")) await doRemove(env, cq, "rm:" + data.slice(5));
  } catch (e) {
    await send(env, cq.message.chat.id, "⚠️ " + escapeHtml(String(e.message || e)), keyboard());
  }
}

async function editMessage(env, cq, text, markup) {
  return tg(env, "editMessageText", {
    chat_id: cq.message.chat.id,
    message_id: cq.message.message_id,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: true },
    ...(markup ? { reply_markup: markup } : {}),
  });
}

// ---------- Telegram ----------

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
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------- проксі для RSS ----------

async function proxyFeed(url, env) {
  let target;
  try {
    target = new URL(url.searchParams.get("url") || "");
  } catch {
    return new Response("bad url", { status: 400 });
  }
  const allowed = (env.PROXY_HOSTS || "mezha.ua,bloomberg.com")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
  const host = target.hostname.toLowerCase();
  const ok = target.protocol === "https:" && allowed.some((h) => host === h || host.endsWith("." + h));
  if (!ok) return new Response("host not allowed", { status: 403 });

  const resp = await fetch(target.toString(), {
    headers: {
      "User-Agent": BROWSER_UA,
      Accept: "application/rss+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
      "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    },
    cf: { cacheEverything: true, cacheTtlByStatus: { "200-299": 300, "300-599": 0 } }, // помилки не кешуємо
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: { "content-type": resp.headers.get("content-type") || "application/xml; charset=utf-8" },
  });
}

// ---------- Spotify: одноразовий вхід ----------

function htmlPage(title, body) {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
    `<title>${title}</title><body style="font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 16px;` +
    `background:#111;color:#eee">${body}</body>`,
    { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } },
  );
}

function spotifyLogin(url, env) {
  if (!env.SPOTIFY_CLIENT_ID || !env.SPOTIFY_CLIENT_SECRET) {
    return htmlPage("Spotify", "<h2>Спершу додай у воркер секрети SPOTIFY_CLIENT_ID і SPOTIFY_CLIENT_SECRET</h2>");
  }
  const params = new URLSearchParams({
    client_id: env.SPOTIFY_CLIENT_ID,
    response_type: "code",
    redirect_uri: `${url.origin}/spotify/callback`,
    scope: "user-follow-read user-library-read",
    show_dialog: "true",
  });
  return Response.redirect(`https://accounts.spotify.com/authorize?${params}`, 302);
}

async function spotifyCallback(url, env) {
  const code = url.searchParams.get("code");
  if (!code) {
    return htmlPage("Spotify", `<h2>Вхід скасовано</h2><p>${escapeHtml(url.searchParams.get("error") || "")}</p>`);
  }
  const res = await fetch("https://accounts.spotify.com/api/token", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Authorization: "Basic " + btoa(`${env.SPOTIFY_CLIENT_ID}:${env.SPOTIFY_CLIENT_SECRET}`),
    },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: `${url.origin}/spotify/callback`,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.refresh_token) {
    return htmlPage("Spotify", `<h2>❌ Spotify не видав ключ</h2><pre>${escapeHtml(JSON.stringify(data))}</pre>`);
  }
  return htmlPage("Spotify підключено",
    "<h2>✅ Spotify підключено</h2>" +
    "<p>Скопіюй ключ нижче і додай його як секрет GitHub <b>SPOTIFY_REFRESH_TOKEN</b> " +
    "(репозиторій → Settings → Secrets and variables → Actions → New repository secret). " +
    "Нікому його не надсилай. Після цього сторінку можна закрити.</p>" +
    `<textarea readonly onclick="this.select()" style="width:100%;height:140px;font:14px monospace;background:#222;color:#eee">` +
    `${escapeHtml(data.refresh_token)}</textarea>` +
    `<p><button onclick="navigator.clipboard.writeText(document.querySelector('textarea').value);this.textContent='Скопійовано ✅'" ` +
    `style="font-size:16px;padding:10px 16px">Скопіювати</button></p>`);
}
