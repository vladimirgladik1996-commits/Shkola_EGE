# DEV: запуск бота + мини-аппа

Тестовая версия «Кабинета репетитора». Код совпадает с `prod/` — различия
только в настройках (`.env`): см. таблицу в `prod/bot-mini-app/DEPLOY.md`
(раздел «Среды»).

## Что нужно

- **Второй бот от @BotFather** — dev-токен нельзя совмещать с продом
  (два сервиса с одним токеном конфликтуют).
- Python 3.10+ и зависимости: `pip install -r requirements.txt`.
- Логин и пароль для входа в кабинет: `python auth.py` (или `--gen` —
  случайный сильный пароль) → полученный `AUTH_PASSWORD_HASH` в `.env`.

## Запуск локально (сценарий A из прод-документации)

```bash
cd dev/bot-mini-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # заполнить: BOT_TOKEN, AUTH_LOGIN, AUTH_PASSWORD_HASH
python bot.py                 # бот + API + страница на http://localhost:8080
```

Мини-апп открывается **из Telegram**, поэтому нужен публичный HTTPS — туннель:

```bash
cloudflared tunnel --url http://localhost:8080
# или: ngrok http 8080
```

Выданный `https://...`:
1. вписать в `WEBAPP_URL` и `PUBLIC_URL` в `.env` (перезапустить `python bot.py`);
2. продублировать в @BotFather → `/setmenubutton` для dev-бота.

Готово: dev-бот → кнопка «Кабинет» → вход по логину/паролю.

## Запуск в Docker

```bash
cp .env.example .env   # заполнить
docker compose up -d --build
```

Тестовая база создаётся в `./data/tutor.db` — её можно просто удалить,
чтобы начать с чистого листа.

## Запуск на Render (сервис tutor-bot-dev)

В корне репозитория лежит `render.yaml` с двумя сервисами: `tutor-bot`
(prod) и `tutor-bot-dev` (dev, каталог `dev/bot-mini-app`). При подключении
Blueprint секреты Render спросит в веб-форме. У dev-сервиса:

- `APP_ENV=dev`, `LOG_LEVEL=DEBUG`, `KEEPALIVE_SEC=0` (спит без трафика —
  экономит бесплатные часы; пробуждение ~1 минута);
- нужен свой `BOT_TOKEN` (второй бот!).

## Тесты

Каждый файл запускается отдельно (сеть Telegram подменяется, база in-memory):

```bash
for t in tests/test_*.py; do python "$t"; done
```

## Если API недоступен

Мини-апп сам включает демо-режим с примерными данными (тост «Демо-режим —
данные примерные») — удобно верстать фронт без запущенного бота.
