FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

ENV APP_PORT=5000
CMD gunicorn "app:app" \
    --bind 0.0.0.0:${APP_PORT} \
    --workers 2 --threads 4 --timeout 60
