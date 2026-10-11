FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# зависимости (версии зафиксированы в requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код и ассеты (docs/ — мини-апп, fonts/ — шрифты для PDF)
COPY bot.py auth.py ./
COPY docs/ docs/
COPY fonts/ fonts/

# не root: UID/GID задаются из docker-compose (см. APP_UID/APP_GID в deploy.sh), чтобы писать в ./data
RUN mkdir -p /app/data && chown 1000:1000 /app/data   # владелец = USER ниже: без этого sqlite не создаст tutor.db
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/'%os.getenv('PORT','8080'),timeout=3)" || exit 1
CMD ["python", "bot.py"]
