FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# зависимости (версии зафиксированы в requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium для «ИИ-проверки»: PDF-отчёт печатается из HTML-макета (essayreport.py) — точная копия макета.
# Ставится только headless-shell (~250 МБ с системными библиотеками). Нет браузера — отчёт собирается запасным
# путём (reportlab), проверка не падает; движок можно выключить переменной ESSAY_REPORT_ENGINE=reportlab.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/*
# шрифт макета Nunito (OFL) для отчёта: скачался — PDF в точности как в макете; нет — DejaVu (кириллица цела)
RUN mkdir -p assets/fonts && (python -c "import urllib.request as u;u.urlretrieve('https://github.com/google/fonts/raw/main/ofl/nunito/Nunito%5Bwght%5D.ttf','/tmp/Nunito.ttf')" \
    && mv /tmp/Nunito.ttf assets/fonts/Nunito.ttf || echo "Nunito не скачан — отчёт будет на DejaVu")

# код и ассеты копируем ЦЕЛИКОМ (см. .dockerignore — что исключено):
# так ни один новый файл или модуль не «пропадает» в релизе
# (контроль — tests/test_release_contract.py)
COPY . .

# не root: UID/GID задаются из docker-compose (см. APP_UID/APP_GID в deploy.sh), чтобы писать в ./data
RUN mkdir -p /app/data && chown 1000:1000 /app/data   # владелец = USER ниже: без этого sqlite не создаст tutor.db
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/'%os.getenv('PORT','8080'),timeout=3)" || exit 1
CMD ["python", "bot.py"]
