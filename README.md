# Школа ЕГЭ · Кабинет репетитора

Временный pat: %20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

Telegram-бот + мини-апп + Android-приложение для репетитора: расписание
занятий, ученики, оплаты и долги, финансы с графиками, PDF-отчёты.

## Структура репозитория

В репозитории **две независимые версии** — тестовая и рабочая. Разделение
по **папкам**, а не по веткам: обе версии лежат рядом и у каждой своя база,
свой бот Telegram и свой сервис.

```
dev/                        ← DEV: тестовая версия
├── bot-mini-app/           ←   бот + мини-апп (aiogram + aiohttp + docs/)
└── apk/                    ←   Android-приложение (WebView-обёртка кабинета)

prod/                       ← PROD: рабочая версия
├── bot-mini-app/           ←   бот + мини-апп
└── apk/                    ←   Android-приложение (+ собранный kabinet-repetitora.apk)

render.yaml                 ← Render Blueprint: два сервиса — tutor-bot (prod) и
                              tutor-bot-dev (тест); секреты вводятся в панели Render
Dockerfile                  ← запасной (shim): собирает prod/bot-mini-app из корня,
                              пока сервису не задан Root Directory в Render
docs/                       ← заглушка GitHub Pages: перенаправляет на сервис Render
.github/workflows/deploy.yml← опциональный SSH-деплой прод-версии
```

Внутри `bot-mini-app/` у обеих версий одинаковый состав:
`bot.py` (бот + HTTP API), `auth.py` (вход), `docs/` (мини-апп), `fonts/`
(шрифты для PDF), `tests/` (тесты), `Dockerfile` + `docker-compose.yml`,
`.env.example` (настройки среды), `DEPLOY.md` (как запускать).

## Чем отличается dev от prod

Код бота и мини-аппа **идентичен** — различия задаются настройками
(`.env`), поэтому перенос правок из dev в prod — это копирование файлов.

| | `dev/` | `prod/` |
|---|---|---|
| Назначение | проверка новых фич | живые ученики и платежи |
| Бот Telegram | **второй**, от @BotFather | рабочий |
| База | тестовая (`./data` можно удалить) | рабочая (нужны бэкапы) |
| Анти-сон Render | выключен (`KEEPALIVE_SEC=0`) | включён (180 с) |
| Маркировка | «DEV» в боте и мини-аппе (`APP_ENV=dev`) | без пометки |
| Логи | `DEBUG` | `INFO` |
| APK | `versionName 1.0-dev`, подписан своим ключом (`keystore.jks`, не в git) | `versionName 1.0`, подписан своим ключом (`keystore.jks`, не в git) |

⚠️ Dev и prod обязаны использовать **разные** токены ботов и **разные** базы.

## С чего начать

- Бот + мини-апп (dev): `dev/bot-mini-app/` → `DEPLOY.md` (запуск локально / туннель / Render)
- Бот + мини-апп (prod): `prod/bot-mini-app/` → `DEPLOY.md` (Selectel / Render / GitHub Pages)
- Android-приложение: `dev/apk/README.md` и `prod/apk/README.md` (сборка APK)
- Порядок релизов, что где лежит и как не потерять ключ подписи APK: `RELEASE.md`

Быстрый старт (dev, локально):

```bash
cd dev/bot-mini-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить: BOT_TOKEN (второго бота!), AUTH_* и пр.
python bot.py               # бот + API на http://localhost:8080
```

Тесты: `python tests/test_api.py` (и остальные файлы `tests/test_*.py` —
каждый запускается отдельно, сеть Telegram подменяется).

## Правки и перенос в прод

1. Меняем в `dev/bot-mini-app` (и/или `dev/apk`), проверяем на dev-ботов/кабинете.
2. Всё ок → копируем изменённые файлы в `prod/bot-mini-app` (и `prod/apk`).
3. `git push` → Render собирает прод (`autoDeploy`).
