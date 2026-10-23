FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-storage.txt requirements-telegram.txt ./
RUN pip install --no-cache-dir -r requirements-telegram.txt
COPY src/telegram/ ./src/telegram/
COPY src/runtime.py ./src/runtime.py
COPY src/storage/ ./src/storage/
USER 10001:10001
CMD ["python", "-m", "src.telegram.bot"]
