# Деплой: GitHub Pages и Selectel

## ⚠️ Главное про архитектуру

Это **одно-доменное приложение**: `bot.py` — это и Telegram-бот, и HTTP-сервер
(aiohttp), который раздаёт **и страницу** (`/` → `docs/index.html`), **и API**
(`/api/*`), и PDF. Мини-апп ходит в API по относительным путям с заголовком
`X-Init` (Telegram `initData`, проверяется HMAC-ом по токену бота) и работает
только для владельца (`OWNER_ID`).

**GitHub Pages умеет только статику.** Он не запустит Python-бота и не отдаст
`/api/*`. Поэтому «просто залить на Pages» приложение не заработает — нужен
запущенный backend с публичным HTTPS. Дальше три рабочих сценария.

---

## Сценарий A — MVP без сервера (рекомендую для старта)

Весь backend локально + публичный HTTPS-туннель. GitHub — только под код.

```bash
cd tutor-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="***"          # от @BotFather
export OWNER_ID="111222333"     # ваш Telegram ID (узнать у @userinfobot)
export WEBAPP_URL="https://<туннель>"   # подставите после запуска туннеля
python bot.py
```

Второй терминал — туннель:
```bash
cloudflared tunnel --url http://localhost:8080
# или: ngrok http 8080
```
Скопируйте выданный `https://...` адрес →
1. в `WEBAPP_URL` (перезапустить бота),
2. в `@BotFather` → `/setmenubutton` (или `/newapp`) → этот URL.

Готово: открываете бота → кнопка «Кабинет» → мини-апп работает.
`API_BASE` в `docs/index.html` оставить `""` (всё с одного домена — туннель
проксирует и страницу, и API).

---

## Сценарий B — фронт на GitHub Pages, backend отдельно

Когда хотите именно Pages для интерфейса.

1. **Backend** запускаете так же, как в A (туннель или сервер с HTTPS),
   но с разрешённым источником Pages:
   ```bash
   export ALLOW_ORIGIN="https://<username>.github.io"
   ```
2. **Frontend** — в `docs/index.html` укажите адрес backend:
   ```js
   const API_BASE=(window.__API_BASE__||"").replace(/\/+$/,"");
   ```
   задайте `window.__API_BASE__="https://<ваш-backend>"` (или пропишите значение прямо в константу).
3. Кладёте **содержимое `docs/`** в репозиторий (чтобы `index.html` был в нужной папке Pages).
4. Settings → Pages → Source: `Deploy from a branch`.
5. Мини-апп: `https://<username>.github.io/<repo>/` → регистрируете в `@BotFather`.

CORS в боте включается только при заданном `ALLOW_ORIGIN`, так что сценарий A/C не затрагивается.

> Мини-апп всё равно открывается **из Telegram** (иначе `initData` пуст → 403 «Нет доступа»).

---

## Сценарий C — Selectel (прод, всё вместе)

Самый надёжный: один домен, страница + API + бот на одном HTTPS — без CORS и без `API_BASE`.

| Шаг | Действие |
|---|---|
| 1 | Домен → A-запись на сервер Selectel |
| 2 | TLS Let's Encrypt (certbot / Caddy / Load Balancer) |
| 3 | nginx отдаёт `docs/` (статика) и проксирует `/api/*` на бота `:8080` |
| 4 | Бот как сервис (systemd/Docker), на проде — webhook |
| 5 | `WEBAPP_URL` = `https://app.вашдомен.ru`, в `BotFather` тот же URL |
| 6 | Секреты (`BOT_TOKEN`, `OWNER_ID`) — в env, не в коде |

### nginx (статика + API на одном домене)
```nginx
server {
  listen 443 ssl;
  server_name app.вашдомен.ru;
  ssl_certificate     /etc/letsencrypt/live/app.вашдомен.ru/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/app.вашдомен.ru/privkey.pem;

  # можно отдавать статику nginx-ом (быстрее), а API проксировать:
  root /opt/tutor-app/docs;
  location / { try_files $uri /index.html; }

  location /api/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
  }
}
```
> Если не хочетесь разделять — просто проксируйте весь `/` на бота: он сам отдаёт и страницу, и API.

### systemd
```ini
# /etc/systemd/system/tutor-bot.service
[Unit]
Description=Tutor bot + API
After=network.target

[Service]
WorkingDirectory=/opt/tutor-app          # каталог с bot.py: рядом лежат docs/, fonts/ и data/
Environment=BOT_TOKEN=***
Environment=OWNER_ID=***
Environment=WEBAPP_URL=https://app.вашдомен.ru/
ExecStart=/opt/tutor-app/.venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Переменные окружения

| Переменная | Назначение | Обязательна |
|---|---|---|
| `BOT_TOKEN` | токен от @BotFather | да |
| `OWNER_ID` | Telegram ID владельца (кабинет только для него) | рекомендуется |
| `WEBAPP_URL` | публичный https-адрес мини-аппа (для кнопки меню) | да для кнопки |
| `ALLOW_ORIGIN` | разрешённый источник для CORS (для сценария B) | только для B |
| `PORT` | порт HTTP (по умолчанию 8080) | нет |
| `CURRENCY` | символ валюты (по умолчанию ₽) | нет |

## Зависимости и шрифты

- `pip install -r requirements.txt` (aiogram, aiohttp, reportlab)
- Шрифты `fonts/DejaVuSans.ttf` и `fonts/DejaVuSans-Bold.ttf` уже в репозитории —
  нужны для PDF с кириллицей. Без них бот не стартует (или положите системные
  в `/usr/share/fonts/truetype/dejavu/`).
- База `./data/tutor.db` (SQLite) создаётся автоматически, мигрирует сама
  (старый файл `tutor.db` рядом с кодом переносится в `data/` при первом запуске).

## Публикация кода на GitHub

Я не могу запушить за вас (нужен ваш аккаунт GitHub). Локально репозиторий уже
собран и закоммичен. Дальше:

```bash
cd tutor-app
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```
Затем Settings → Pages → включить (для сценария B) — см. выше.
