FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CODEX_HOME=/home/agent/.codex HOME=/home/agent
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server git curl ca-certificates iptables util-linux build-essential \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && passwd -d root
RUN pip install --no-cache-dir openai-codex==0.163.0
COPY deploy/worker/sshd_config /etc/ssh/taskopotamus_config
COPY deploy/worker/start_worker.sh /usr/local/bin/start-worker
COPY deploy/worker/worker_codex.sh /usr/local/bin/worker-codex
COPY deploy/worker/firewall.py /usr/local/bin/worker-firewall
RUN chmod 755 /usr/local/bin/start-worker /usr/local/bin/worker-codex
WORKDIR /workspace
CMD ["/usr/local/bin/start-worker"]
