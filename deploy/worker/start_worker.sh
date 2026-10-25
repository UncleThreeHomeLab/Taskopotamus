#!/bin/sh
set -eu
umask 077
mkdir -p /run/sshd /home/agent/.codex /workspace
mkdir -p /saved /var/lib/taskopotamus/auth
chmod 711 /home/agent /var/lib/taskopotamus /var/lib/taskopotamus/auth
chmod 700 /run /tmp /dev/shm /dev/mqueue
chmod 755 /run/sshd
cp /run/secrets/worker_host_key /run/sshd/host_key
cp /run/secrets/worker_client_public /run/sshd/authorized_keys
chmod 600 /run/sshd/host_key /run/sshd/authorized_keys
python /usr/local/bin/worker-firewall "$WORKER_MCP_IP" "$WORKER_DNS_IP" "$WORKER_AGENT_IP"
# Drop the firewall capability before any SSH session or model-generated command can run.
exec setpriv --bounding-set=-net_admin --inh-caps=-all --ambient-caps=-all \
    python /usr/local/bin/worker-workspace serve
