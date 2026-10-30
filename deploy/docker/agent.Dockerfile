FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex
WORKDIR /app
RUN sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources \
    && ! grep -qE '^URIs:.*http://' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends openssh-client tini \
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
