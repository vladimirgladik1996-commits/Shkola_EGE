# ============================================================
# ЗАПАСНОЙ ФАЙЛ (shim) для сервиса tutor-bot на Render.
#
# Канонический Dockerfile — prod/bot-mini-app/Dockerfile (рядом с кодом).
# Этот файл в корне нужен только потому, что сервис tutor-bot был создан
# ДО перестройки репозитория на dev/ и prod/ и может продолжать собирать
# образ из корня репозитория (старый dockerContext: ./ без rootDir).
# Он собирает ту же prod-версию из prod/bot-mini-app.
#
# Когда в Render Dashboard будет задан Root Directory = prod/bot-mini-app
# (или синхронизация render.yaml это применит), этот файл станет
# неиспользуемым — можно удалить.
# ============================================================
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# зависимости (версии зафиксированы в requirements.txt)
COPY prod/bot-mini-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код и ассеты (docs/ — мини-апп, fonts/ — шрифты для PDF)
COPY prod/bot-mini-app/ ./

# не root: UID 1000 — владелец /app/data, чтобы sqlite создавал tutor.db
RUN mkdir -p /app/data && chown 1000:1000 /app/data
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/'%os.getenv('PORT','8080'),timeout=3)" || exit 1
CMD ["python", "bot.py"]
