#!/bin/sh
# Деплой backend-части «Кабинета репетитора» на сервере.
# Запускать на сервере в каталоге с репозиторием:  sh deploy.sh
set -e
cd "$(dirname "$0")"

echo "==> Бэкап базы"
if [ -f data/tutor.db ]; then
  mkdir -p backups
  cp data/tutor.db "backups/tutor.db.bak-$(date +%Y%m%d-%H%M%S)"
  echo "    сохранено в backups/"
fi

echo "==> Обновление кода"
git pull --ff-only

echo "==> Сборка и запуск"
docker compose up -d --build

echo "==> Готово. Проверка:"
sleep 3
curl -fsS -o /dev/null -w "GET / -> %{http_code}\n" http://127.0.0.1:8080/ || echo "    (сервис ещё поднимается — гляньте docker compose logs)"
