FROM python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS trust

FROM postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6 AS test-base
# Bootstrap HTTPS trust before ca-certificates can be installed.
COPY --from=trust /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://apt.postgresql.org/|https://apt.postgresql.org/|g' /etc/apt/sources.list.d/pgdg.list \
    && ! grep -qE '^(URIs:|deb ).*http://' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends python3 python3-venv openssh-client curl ca-certificates \
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
# Keep native-snapshotter disk use bounded with one allowlisted context copy.
COPY . ./
RUN python -c "import shutil, site; shutil.copytree('/app/src/worker', site.getsitepackages()[0] + '/taskopotamus_worker')" \
    && install -m 644 deploy/testing/run_tests.sh /run_tests.sh
USER root
ENTRYPOINT ["sh", "/run_tests.sh"]

FROM test-base AS test
RUN --network=none sh /run_tests.sh

FROM test-base AS test-runner
