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
export AUTH_LOGIN="..."         # свой логин и хэш пароля: python auth.py
export AUTH_PASSWORD_HASH="..."
export PUBLIC_URL="https://<туннель>"
# кто допущен — по умолчанию три ника из ALLOWED_USERNAMES (см. ниже)
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

  # Проще и безопаснее всего — проксировать ВСЁ на бота: он сам отдаёт страницу, /login и API.
  # (/login обязан доходить до бота, иначе вход не откроется; не подменяйте его на index.html.)
  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    client_max_body_size 2m;
  }
}
```
> Порт `8080` в `docker-compose.yml` привязан к `127.0.0.1` — наружу бот доступен только через nginx/Caddy с HTTPS.
> В `.env` задайте `PUBLIC_URL=https://app.вашдомен.ru` (на него ссылается кнопка «Войти»).

### systemd
```ini
# /etc/systemd/system/tutor-bot.service
[Unit]
Description=Tutor bot + API
After=network.target

[Service]
WorkingDirectory=/opt/tutor-app          # каталог с bot.py: рядом лежат docs/, fonts/ и data/
EnvironmentFile=/opt/tutor-app/.env      # секреты — в .env (chmod 600), а не в самом unit-файле
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
| `ALLOWED_USERNAMES` | @ники (без @) через запятую, кому разрешён доступ. По умолчанию: `Gladik_Vladimir,Gladik_N,hungerrr` | да (или `ALLOWED_IDS`) |
| `ALLOWED_IDS` | числовые Telegram-ID, допускаются без проверки ника (`OWNER_ID` — то же самое, старое имя) | нет |
| `AUTH_LOGIN`, `AUTH_PASSWORD_HASH` | логин и scrypt-хэш пароля (`python auth.py`) | да |
| `PUBLIC_URL` | https-адрес backend, где открывается `/login` | да (если не совпадает с `WEBAPP_URL`) |
| `SESSION_DAYS` | срок сессии без активности, по умолчанию 30; пока пользуются — продлевается, пароль повторно не спрашивается | нет |
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

## Безопасность деплоя (чек-лист)

- **CI/CD:** добавьте секрет `SERVER_KNOWN_HOSTS` (`ssh-keyscan -t ed25519 <host>`) — деплой проверяет отпечаток сервера и не стартует без него.
  Деплой идёт только после успешных тестов. Включите 2FA на GitHub и Branch protection для `main`; для SSH заведите отдельного
  пользователя без sudo (по возможности с `command="cd /opt/tutor-app && sh deploy.sh"` в `authorized_keys`).
- **Первый деплой этой версии:** контейнер теперь работает не от root. Если `data/tutor.db` создан раньше (владелец root), выполните один раз
  `sudo chown -R $(id -u):$(id -g) data` — `deploy.sh` подскажет, если это нужно.
- **Бэкапы:** `deploy.sh` делает консистентную копию (`sqlite3 backup`), права 600, хранит 14 последних. Копии лежат на том же сервере —
  настройте регулярную выгрузку в зашифрованное хранилище вне сервера (restic/age).
- **Секреты:** `.env` — `chmod 600`, владелец — пользователь деплоя. Токен бота скомпрометирован → перевыпустите в @BotFather, перезапустите бота.
- **Доступ по людям:** трое допущенных входят каждый со своего аккаунта по одному логину/паролю. Хотите отозвать человека — уберите ник из
  `ALLOWED_USERNAMES` и перезапустите: его сессия перестаёт действовать сразу. Сменили Telegram-аккаунт — удалите старую привязку:
  `sqlite3 data/tutor.db "delete from auth_pins where username='<ник маленькими буквами>'"`.

## Журнал, логи, миграции, восстановление

- **Журнал действий** (таблица `audit_log`, хранится год): кто из троих (`tg_id`), какое изменение (`/api/payment/add` и т.п.) и над какими записями
  (id, день, сумма — без имён и фото). Смотреть:
  `sqlite3 data/tutor.db "select ts, tg_id, action, target from audit_log order by id desc limit 50"`.
- **Логи:** `docker compose logs -f bot`. Входы (успех/ошибка/блокировка), отказы постороннему, превышение лимита и непредвиденные ошибки API.
  Пароли и токены не логируются. Уровень — `LOG_LEVEL`.
- **Лимит запросов** к API: `API_RATE_PER_MIN` (по умолчанию 120 в минуту на аккаунт). Для `/login` добавьте ещё `limit_req` в nginx.
- **Миграции схемы:** версия хранится в базе (`pragma user_version`), новые изменения — отдельным пунктом в `MIGRATIONS` в `bot.py`. Применяются сами при запуске.
- **БД в режиме WAL:** рядом с `tutor.db` появятся `tutor.db-wal` и `-shm` — это нормально. Копировать вручную только через `sqlite3 .backup` (так делает `deploy.sh`),
  а не `cp`.
- **Проверка бэкапа:** `deploy.sh` после копирования выполняет `integrity_check`; если копия битая — деплой останавливается до обновления кода.
- **Восстановление:** остановить бота (`docker compose stop`), положить копию на место `data/tutor.db` (удалив `tutor.db-wal` и `-shm`), `docker compose up -d`.
  Проверьте восстановление на тестовой копии заранее, а не в день аварии.
