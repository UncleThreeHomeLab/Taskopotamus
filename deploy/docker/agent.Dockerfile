FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex
WORKDIR /app
COPY requirements.txt requirements-storage.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /home/agent/.codex \
    && chmod 700 /home/agent/.codex \
    && chown -R 10001:10001 /home/agent
COPY src/ ./src/
COPY config/agent/ ./config/agent/
USER 10001:10001
CMD ["python", "-m", "src.agent.server"]
