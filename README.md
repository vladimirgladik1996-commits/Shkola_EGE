# Бот репетитора + мини-апп

Telegram-бот и мини-апп (Telegram Web App) для репетитора: расписание с
длительностью занятий, ученики, оплаты, автоматический расчёт долгов,
регулярные занятия и печать недели в PDF.

- **Бот** — ведение расписания через кнопки Telegram (`aiogram`).
- **Мини-апп** — «Кабинет»: сетка недели, карточки дней, оплаты, печать.
- **Backend** — один процесс `bot.py`: бот + HTTP-сервер (`aiohttp`).

---

## Быстрый старт

1. Создайте бота в @BotFather, получите токен. Telegram ID — у @userinfobot.
2. `pip install -r requirements.txt`
3. Мини-аппе нужен публичный https. Для теста: `cloudflared tunnel --url http://localhost:8080` (или ngrok).
4. Заполните окружение (см. [`env/dev.example`](env/dev.example)) и запустите:

```bash
export BOT_TOKEN="***"
export AUTH_LOGIN=... AUTH_PASSWORD_HASH=... PUBLIC_URL=https://ваш-адрес   # python auth.py — хэш пароля
export ALLOWED_USERNAMES=Gladik_Vladimir,Gladik_N,hungerrr
export WEBAPP_URL=https://ваш-адрес
export CURRENCY=₽        # необязательно
python bot.py
```

5. Откройте бота → `/start` → кнопка «Кабинет».

Подробные сценарии деплоя (MVP, Pages + отдельный backend, сервер) — в [`DEPLOY.md`](DEPLOY.md).

---

## Структура проекта

```
tutor-app/
├── bot.py              # ВСЁ backend: Telegram-бот + aiohttp-сервер + API + PDF
├── auth.py             # вход, сессии, белый список аккаунтов
├── docs/
│   └── index.html      # Мини-апп (SPA, без сборки) — фронтенд
├── fonts/              # DejaVu для кириллицы в reportlab (PDF)
│   ├── DejaVuSans.ttf
│   └── DejaVuSans-Bold.ttf
├── requirements.txt    # aiogram, aiohttp, reportlab
├── Dockerfile          # образ backend
├── docker-compose.yml  # запуск + volume ./data → /app/data (tutor.db)
├── env/
│   ├── dev.example     # шаблон окружения для разработки
│   └── prod.example    # шаблон окружения для продакшена
├── .gitignore          # .env, data/, tutor.db — в git не попадают
├── DEPLOY.md           # сценарии деплоя
└── README.md           # этот файл
```

Сознательно **без фреймворков и сборки**: один Python-файл и один HTML-файл.
Менять = править эти два файла, деплой фронта = закоммитить `docs/`.

---

## Архитектура

**Одно-доменное приложение.** `bot.py` — это одновременно Telegram-бот и
HTTP-сервер, который раздаёт:

| Путь | Что отдаёт |
|---|---|
| `GET /` | страницу мини-аппа (`docs/index.html`) |
| `GET/POST /api/*` | JSON API мини-аппа |
| `GET /api/week.pdf` | PDF недели |

Мини-апп ходит в API **по относительным путям** (один домен) и передаёт
заголовок `X-Init` с Telegram `initData`.

```
Telegram ──► bot.py (aiogram) ──► SQLite (tutor.db)
                  │
                  └── aiohttp :8080
                        ├── GET  /            → docs/index.html
                        └── /api/*            → тот же код, что и бот
                              ▲
                              │ HTTPS + X-Init (initData)
                        Telegram Mini App (docs/index.html)
```

### Авторизация

После `/start` бот присылает кнопку «Войти» — одноразовую ссылку (10 минут, привязана к вашему Telegram-ID) на страницу `/login`.
Бот, мини-апп и API работают только после ввода логина и пароля. Выйти: `/logout`.

**Кто допущен.** Только три Telegram-аккаунта: `@Gladik_Vladimir`, `@Gladik_N`, `@hungerrr` (список — `ALLOWED_USERNAMES` в `.env`; можно добавить числовые `ALLOWED_IDS`).
Остальным бот не отвечает вовсе, API отдаёт 403. После первого успешного входа ник закрепляется за числовым Telegram-ID: тот, кто потом займёт освободившийся ник, доступа не получит.

**Пароль — один раз.** При первом входе спрашиваются логин и пароль; дальше сессия «скользящая» и продлевается при каждом использовании
(`SESSION_DAYS`, по умолчанию 30 дней *без активности*). Повторный ввод нужен только после `/logout`, смены пароля, исключения человека из списка или долгого простоя.

Настройка (на сервере, в `.env`): `AUTH_LOGIN`, `AUTH_PASSWORD_HASH`, `PUBLIC_URL` (только https), `ALLOWED_USERNAMES`/`ALLOWED_IDS`.
Хэш: `python auth.py` (свой пароль, от 12 символов) или `python auth.py --gen` (случайный). В Docker: `docker compose run --rm bot python auth.py`.
После смены пароля перезапустите бота — все прежние входы автоматически станут недействительными.

Защита: scrypt (64 МиБ), пароль не хранится в коде; токен ссылки в `#`-фрагменте и в БД только хэшем; лимит 5 неверных попыток / 15 минут
и 15 / сутки, уведомление владельцу о блокировке и о каждом входе; запросы к БД только с параметрами; белый список аккаунтов с привязкой ника к ID; CSP с nonce, проверка Origin и Sec-Fetch-Site,
HSTS; `initData` Telegram принимается не старше суток; фото только строгим JPEG data-URL; чужим бот не отвечает.
