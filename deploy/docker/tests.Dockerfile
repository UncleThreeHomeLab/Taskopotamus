FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS prepare
RUN apk add --no-cache \
    build-base=0.5-r4 bison=3.8.2-r3 flex=2.6.4-r8 perl=5.42.2-r1 pkgconf=2.5.1-r0 \
    icu-dev=78.1-r0 openssl-dev=3.5.9-r0 readline-dev=8.3.3-r1 zlib-dev=1.3.2-r1 \
    curl=8.23.0-r0 bzip2=1.0.8-r6
WORKDIR /build/postgres
# SQL/XML is unused by the tests; omit its vulnerable libxml2 dependency.
RUN curl --fail --silent --show-error --location \
      https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2 \
      --output /tmp/postgresql.tar.bz2 \
    && echo '555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f  /tmp/postgresql.tar.bz2' | sha256sum -c - \
    && tar -xjf /tmp/postgresql.tar.bz2 --strip-components=1 \
    && ./configure --prefix=/opt/postgresql --with-ssl=openssl --without-libxml --without-libxslt \
    && make -j2 \
    && make install
RUN set -eu; \
    case "$(apk --print-arch)" in \
        x86_64) architecture=amd64; checksum=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b ;; \
        aarch64) architecture=arm64; checksum=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88 ;; \
        *) exit 1 ;; \
    esac; \
    curl --fail --silent --show-error --location "https://get.helm.sh/helm-v4.3.1-linux-$architecture.tar.gz" --output /tmp/helm.tar.gz; \
    echo "$checksum  /tmp/helm.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    mkdir -p /opt/helm; \
    install -m 755 "/tmp/linux-$architecture/helm" /opt/helm/helm

FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS test-environment
RUN apk add --no-cache \
      icu-libs=78.1-r0 icu-data-full=78.1-r0 readline=8.3.3-r1 zlib=1.3.2-r1 \
      libssl3=3.5.9-r0 libcrypto3=3.5.9-r0 openssh-keygen=10.3_p1-r1 \
      setpriv=2.42.3-r1 tzdata=2026e-r0 \
    && addgroup -S postgres \
    && adduser -S -D -H -G postgres postgres \
    && python -m venv /opt/tests
COPY --from=prepare /opt/postgresql /opt/postgresql
COPY --from=prepare /opt/helm/helm /usr/local/bin/helm
ENV PATH=/opt/tests/bin:/opt/postgresql/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements*.txt ./
RUN python -m pip install --no-cache-dir pip==26.2.2 \
    && pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt -r requirements-google.txt -r requirements-test.txt \
    && python -c "import os, site; os.symlink('/app/src/worker', site.getsitepackages()[0] + '/taskopotamus_worker')"

FROM test-environment AS test-runner
# Keep native-snapshotter disk use bounded with one allowlisted context copy.
COPY . ./
RUN install -m 644 deploy/testing/run_tests.sh /run_tests.sh
USER root
ENTRYPOINT ["sh", "/run_tests.sh"]
