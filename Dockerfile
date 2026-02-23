FROM python:3.11-slim

# Устанавливаем переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    # Убираем предупреждения от Werkzeug
    WERKZEUG_LOG_LEVEL=WARNING

# Создаем рабочую директорию
WORKDIR /app

# Копируем файл с зависимостями
COPY backend/requirements.txt /app/backend/requirements.txt

# Устанавливаем зависимости, включая Gunicorn для production
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/backend/requirements.txt && \
    pip install --no-cache-dir gunicorn

# Копируем исходный код бэкенда и фронтенда
COPY backend /app/backend
COPY frontend /app/frontend

# Копируем файлы из корня проекта, которые нужны бэкенду во время работы
COPY function_schemas.json /app/function_schemas.json
COPY system_prompt.md /app/system_prompt.md

# Создаем директорию для логов, чтобы не было ошибок при записи
RUN mkdir -p /app/logs && chmod 777 /app/logs

# Открываем порт
EXPOSE 8080

# Запускаем через Gunicorn. 
# Подставляем порт из переменной окружения, если она задана платформой, иначе 8080.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 120 --chdir /app/backend app:app"]
