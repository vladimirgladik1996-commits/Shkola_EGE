#!/bin/sh
# Деплой backend-части «Кабинета репетитора» на сервере.
# Запускать на сервере в каталоге с репозиторием:  sh deploy.sh
set -e
cd "$(dirname "$0")"
umask 077                                  # бэкапы и база — только для владельца

# контейнер работает не от root, а от пользователя, запустившего деплой (владелец ./data)
APP_UID="$(id -u)"; APP_GID="$(id -g)"; export APP_UID APP_GID
mkdir -p data
if [ -f data/tutor.db ] && [ ! -w data/tutor.db ]; then
  echo "!! data/tutor.db принадлежит другому пользователю (раньше контейнер работал от root)."
  echo "   Выполните один раз:  sudo chown -R $APP_UID:$APP_GID data   и повторите деплой."
  exit 1
fi

echo "==> Бэкап базы"
if [ -f data/tutor.db ]; then
  mkdir -p backups
  chmod 700 backups
  OUT="backups/tutor.db.bak-$(date +%Y%m%d-%H%M%S)"
  # консистентная копия через sqlite (простое cp может поймать базу посреди записи)
  python3 - "$OUT" <<'PY'
import os, sqlite3, sys
out, ok, tables, n = sys.argv[1], "?", [], {}
try:
    src = sqlite3.connect("data/tutor.db"); dst = sqlite3.connect(out)
    src.backup(dst); src.close()
    ok = dst.execute("pragma integrity_check").fetchone()[0]
    tables = [r[0] for r in dst.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    n = {t: dst.execute(f'select count(*) from "{t}"').fetchone()[0] for t in ("students", "lessons", "payments") if t in tables}
    dst.close()
except sqlite3.Error as e:
    ok = str(e)
if ok != "ok" or "students" not in tables:
    if os.path.exists(out): os.remove(out)
    sys.exit(f"!! копия БД не прошла проверку ({ok}) — деплой остановлен, старая версия продолжает работать")
print("    проверка копии: целостность ok, строк:", n)
PY
  chmod 600 "$OUT"
  echo "    сохранено: $OUT"
  # храним 14 последних копий
  ls -1t backups/tutor.db.bak-* 2>/dev/null | tail -n +15 | while read -r f; do rm -f -- "$f"; done
  echo "    ВАЖНО: копия лежит на этом же сервере — периодически забирайте её в зашифрованное хранилище вне сервера."
fi

echo "==> Обновление кода"
git pull --ff-only

echo "==> Сборка и запуск"
docker compose up -d --build

echo "==> Готово. Проверка:"
sleep 3
curl -fsS -o /dev/null -w "GET / -> %{http_code}\n" http://127.0.0.1:8080/ || echo "    (сервис ещё поднимается — гляньте docker compose logs)"
