FROM python:3.11-slim

WORKDIR /app

# Ставим зависимости отдельным слоем, чтобы кэшировалось при правках bot.py
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Сюда монтируем volume с бд, чтобы статистика не терялась при пересборке
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
