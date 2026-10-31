FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex
WORKDIR /app
RUN apk add --no-cache openssh-client-default=10.3_p1-r1 tini=0.19.0-r3 \
    && ln -s /sbin/tini /usr/bin/tini \
    && test -x /usr/bin/tini \
    && addgroup -S -g 10001 agent \
    && adduser -S -D -H -u 10001 -G agent -h /home/agent agent
COPY requirements.txt requirements-storage.txt requirements-security.txt ./
RUN pip install --no-cache-dir -c requirements-security.txt -r requirements.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip \
    && python -c "import shutil, sysconfig; shutil.rmtree(sysconfig.get_path('stdlib') + '/ensurepip')" \
    && mkdir -p /home/agent/.codex \
    && chmod 700 /home/agent/.codex \
    && chown -R 10001:10001 /home/agent
COPY src/ ./src/
COPY config/agent/ ./config/agent/
COPY src/agent/worker_transport.py /usr/local/bin/codex-worker
RUN chmod 755 /usr/local/bin/codex-worker
USER 10001:10001
CMD ["python", "-m", "src.agent.server"]
