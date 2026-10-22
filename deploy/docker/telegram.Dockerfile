FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY src/telegram/ ./src/telegram/
COPY src/runtime.py ./src/runtime.py
RUN mkdir -p /home/telegram/data \
    && chmod 700 /home/telegram/data \
    && chown -R 10001:10001 /home/telegram
USER 10001:10001
CMD ["python", "-m", "src.telegram.bot"]
