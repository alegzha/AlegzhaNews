# Пульт керування ботом у Telegram

Маленький Cloudflare Worker, який слухає твого бота в Telegram і виконує команди.
А ще кожні 30 хвилин запускає бота на GitHub (таймер Cloudflare точніший за розклад GitHub).
Безкоштовно (Cloudflare Free: 100 000 запитів на день).

| Кнопка | Команда | Що робить |
|---|---|---|
| 🔄 Новини зараз | `/news` | перевіряє джерела й одразу публікує нове |
| 📋 Джерела | `/sources` | список джерел за рубриками |
| ➕ Додати | `/add посилання` | знаходить RSS сайту і питає, в яку рубрику додати (можна просто надіслати посилання) |
| ➖ Прибрати | `/remove` | прибирає джерело кнопками, з підтвердженням |
| 📅 Релізи | `/releases` | публікує «Релізи тижня» зараз |
| ℹ️ Стан | `/status` | останні запуски бота з посиланнями на логи |

Додавання й видалення джерел змінює `feeds.yaml` у репозиторії — бот підхоплює зміни з наступної перевірки.
Кнопки працюють лише для тебе: іншим людям бот відповідає, що він приватний.

## Налаштування (один раз)

### 1. Токен GitHub
1. github.com → аватар → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Назва: `news-bot-button`. Expiration — найдовший доступний термін.
3. **Repository access → Only select repositories → AlegzhaNews**.
4. **Permissions → Repository permissions**:
   - **Actions → Read and write** (кнопки «Новини зараз», «Релізи», «Стан»);
   - **Contents → Read and write** (додавати й прибирати джерела).
5. **Generate token** і скопіюй його (показується один раз).

> Уже маєш токен лише з Actions? Відкрий його в тому ж розділі → **Edit** → додай **Contents: Read and write** → **Update**. Сам токен не зміниться.

### 2. Воркер на Cloudflare
1. **dash.cloudflare.com → Workers & Pages → Create → Create Worker**, назва `alegzhanews` → **Deploy**.
2. **Edit code** → встав вміст `worker/worker.js` → **Deploy**.
3. **Settings → Variables and Secrets → Add**:

| Тип | Назва | Значення |
|---|---|---|
| Secret | `TG_TOKEN` | токен бота від @BotFather |
| Secret | `GH_TOKEN` | токен GitHub з кроку 1 |
| Secret | `WEBHOOK_SECRET` | будь-яка довга фраза латиницею без пробілів |
| Text | `GH_REPO` | `alegzha/AlegzhaNews` |
| Text | `PROXY_HOSTS` | *необов'язково:* сайти, які читаються через воркер (типово `mezha.ua`) |

### 3. Підключення до Telegram
1. Відкрий `https://АДРЕСА-ВОРКЕРА/setup?key=ТВІЙ_WEBHOOK_SECRET` — має з'явитися «✅ Готово!».
2. Напиши боту `/start` — він відповість твоїм Telegram id.
3. Додай у воркер змінну **Text** `OWNER_ID` з цим числом → **Deploy**.
4. Знову напиши `/start` — з'являться кнопки.

Після оновлення коду воркера теж напиши `/start`: бот оновить список команд і ввімкне кнопки під повідомленнями.

### Необов'язково: автооновлення воркера з GitHub
Щоб не вставляти код вручну після кожної зміни: воркер → **Settings → Build → Connect** → репозиторій AlegzhaNews,
гілка `main` → **Connect** (решту полів не чіпай). Тепер кожна зміна в `worker/` сама публікується на Cloudflare.
Змінні й секрети з дашборду при цьому зберігаються (`keep_vars = true` у `wrangler.toml` в корені репозиторію).

## Якщо щось не так
- **«GitHub відповів 401»** — токен GitHub прострочений або неправильний. Створи новий і заміни `GH_TOKEN`.
- **«GitHub відповів 403/404» або «не віддав feeds.yaml»** — перевір `GH_REPO` і права токена (Actions і Contents: Read and write).
- **Не з'являються кнопки під повідомленнями** — напиши боту `/start`.
- **Бот не відповідає** — ще раз відкрий посилання `/setup?key=…`.
