FROM python:3.12-slim
WORKDIR /app

# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код и ассеты (docs/ — мини-апп, fonts/ — шрифты для PDF)
COPY bot.py .
COPY docs/ docs/
COPY fonts/ fonts/

# tutor.db создаётся здесь; подключите volume для сохранения данных
EXPOSE 8080
CMD ["python", "bot.py"]
