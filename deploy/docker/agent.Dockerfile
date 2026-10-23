FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --home-dir /home/agent --no-create-home agent
COPY requirements.txt requirements-storage.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /home/agent/.codex \
    && chmod 700 /home/agent/.codex \
    && chown -R 10001:10001 /home/agent
COPY src/ ./src/
COPY config/agent/ ./config/agent/
COPY src/agent/worker_transport.py /usr/local/bin/codex-worker
RUN chmod 755 /usr/local/bin/codex-worker
USER 10001:10001
CMD ["python", "-m", "src.agent.server"]
