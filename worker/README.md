# Кнопка «🔄 Новини зараз»

Маленький Cloudflare Worker, який слухає твого бота в Telegram. Натискаєш кнопку
або пишеш `/news` — він запускає GitHub Actions, і бот одразу публікує нові новини.
Безкоштовно (Cloudflare Free: 100 000 запитів на день).

## Налаштування (≈10 хвилин, один раз)

### 1. Токен GitHub
1. github.com → аватар → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Назва: `news-bot-button`. Expiration — найдовший доступний термін.
3. **Repository access → Only select repositories → AlegzhaNews**.
4. **Permissions → Repository permissions → Actions → Read and write**.
5. **Generate token** і скопіюй його (показується один раз).

### 2. Воркер на Cloudflare
1. Зареєструйся на **dash.cloudflare.com** (безкоштовно).
2. **Workers & Pages → Create → Create Worker** (шаблон «Hello World»), назва, наприклад, `alegzha-news` → **Deploy**.
3. **Edit code** → видали все → встав вміст файлу `worker/worker.js` → **Deploy**.
4. Відкрий воркер → **Settings → Variables and Secrets → Add**:

| Тип | Назва | Значення |
|---|---|---|
| Secret | `TG_TOKEN` | токен бота від @BotFather |
| Secret | `GH_TOKEN` | токен GitHub з кроку 1 |
| Secret | `WEBHOOK_SECRET` | будь-яка довга фраза латиницею без пробілів, напр. `my_long_random_phrase_2026` |
| Text | `GH_REPO` | `alegzha/AlegzhaNews` |

   Натисни **Deploy**.

### 3. Підключення до Telegram
1. Відкрий у браузері `https://АДРЕСА-ВОРКЕРА/setup?key=ТВІЙ_WEBHOOK_SECRET`
   (адресу видно на сторінці воркера, вона закінчується на `.workers.dev`).
   Має з'явитися «✅ Готово!».
2. Напиши своєму боту `/start` — він відповість твоїм Telegram id.
3. Додай у воркер змінну **Text** `OWNER_ID` з цим числом → **Deploy**.
4. Знову напиши боту `/start` — з'явиться кнопка **🔄 Новини зараз**.

Кнопка працює лише для тебе: іншим людям бот відповідає, що він приватний.

## Якщо щось не так
- **«GitHub відповів 401»** — токен GitHub прострочений або неправильний. Створи новий і заміни `GH_TOKEN`.
- **«GitHub відповів 403/404»** — перевір `GH_REPO` і що токен має доступ до AlegzhaNews з правом Actions: Read and write.
- **Бот не відповідає** — ще раз відкрий посилання `/setup?key=…`.
