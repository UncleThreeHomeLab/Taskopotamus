FROM postgres:18.6
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/tests
ENV PATH=/opt/tests/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) architecture=amd64; checksum=e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c ;; \
        arm64) architecture=arm64; checksum=21abd9354d39b2cd79a8d76be6912cd137a983cbf997193503fb8a6a6e2f2785 ;; \
        *) exit 1 ;; \
    esac; \
    curl --fail --silent --show-error --location "https://get.helm.sh/helm-v4.2.3-linux-$architecture.tar.gz" --output /tmp/helm.tar.gz; \
    echo "$checksum  /tmp/helm.tar.gz" | sha256sum --check; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install -m 755 "/tmp/linux-$architecture/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tar.gz "/tmp/linux-$architecture"
COPY requirements*.txt ./
RUN python -m pip install --no-cache-dir pip==26.2.2 \
    && pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt -r requirements-google.txt -r requirements-test.txt
COPY src/ ./src/
RUN python -c "import shutil, site; shutil.copytree('/app/src/worker', site.getsitepackages()[0] + '/taskopotamus_worker')"
COPY config/agent/ ./config/agent/
COPY tests/ ./tests/
COPY deploy/postgres/ ./deploy/postgres/
COPY deploy/worker/ ./deploy/worker/
COPY deploy/helm/ ./deploy/helm/
COPY deploy/testing/run_tests.sh /run_tests.sh
USER root
ENTRYPOINT ["sh", "/run_tests.sh"]
