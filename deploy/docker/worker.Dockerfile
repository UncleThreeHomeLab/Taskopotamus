FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS installer
RUN apk add --no-cache git=2.54.0-r0
COPY deploy/docker/pip/ /build/pip/
RUN python /build/pip/build.py /build/artifacts /out

FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex HOME=/home/agent
RUN apk add --no-cache openssh-server=10.3_p1-r1 git=2.54.0-r0 curl=8.23.0-r0 \
      ca-certificates=20261020-r0 iptables=1.8.13-r0 setpriv=2.42.3-r1 build-base=0.5-r4 tini=0.19.0-r3 \
    && ln -s /sbin/tini /usr/bin/tini \
    && test -x /usr/bin/tini \
    && rm -f /etc/ssh/ssh_host_* \
    && passwd -d root
RUN addgroup -S -g 10001 agent && adduser -S -D -H -u 10001 -G agent agent \
    && addgroup -S -g 10002 codex-auth && adduser -S -D -H -u 10002 -G codex-auth codex-auth
COPY --from=installer /out/pip-26.2.2+taskopotamus.1-py3-none-any.whl /tmp/
COPY deploy/docker/pip/refresh_ensurepip.py /tmp/refresh-ensurepip.py
COPY deploy/docker/pip/verify.py /tmp/verify-installer.py
COPY requirements-security.txt /tmp/
RUN python -m pip install --no-cache-dir --no-deps --force-reinstall /tmp/pip-26.2.2+taskopotamus.1-py3-none-any.whl \
    && python /tmp/refresh-ensurepip.py /tmp/pip-26.2.2+taskopotamus.1-py3-none-any.whl \
    && pip install --no-cache-dir -c /tmp/requirements-security.txt openai-codex==0.163.0 mcp==2.3.0 \
    && python -m pip check \
    && python /tmp/verify-installer.py \
    && rm /tmp/pip-26.2.2+taskopotamus.1-py3-none-any.whl /tmp/refresh-ensurepip.py /tmp/verify-installer.py /tmp/requirements-security.txt
COPY src/worker/ /usr/local/lib/python3.13/site-packages/taskopotamus_worker/
COPY deploy/worker/sshd_config /etc/ssh/taskopotamus_config
COPY deploy/worker/start_worker.sh /usr/local/bin/start-worker
COPY deploy/worker/worker_codex.sh /usr/local/bin/worker-codex
COPY deploy/worker/firewall.py /usr/local/bin/worker-firewall
COPY deploy/worker/workspace_lifecycle.py /usr/local/bin/worker-workspace
RUN chmod 755 /usr/local/bin/start-worker /usr/local/bin/worker-codex /usr/local/bin/worker-workspace
WORKDIR /workspace
CMD ["/usr/local/bin/start-worker"]
