FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS prepare
RUN apk add --no-cache \
    build-base=0.5-r4 linux-headers=7.0.0-r1 bison=3.8.2-r3 flex=2.6.4-r8 perl=5.42.2-r1 pkgconf=2.5.1-r0 \
    icu-dev=78.1-r0 openssl-dev=3.5.9-r0 readline-dev=8.3.3-r1 zlib-dev=1.3.2-r1 \
    curl=8.23.0-r0 bzip2=1.0.8-r6
WORKDIR /build/postgres
RUN curl --fail --silent --show-error --location \
      https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2 \
      --output /tmp/postgresql.tar.bz2 \
    && echo '555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f  /tmp/postgresql.tar.bz2' | sha256sum -c - \
    && tar -xjf /tmp/postgresql.tar.bz2 --strip-components=1 \
    && awk '$1 == "#define" && $2 == "DEFAULT_PGSOCKET_DIR" && $3 == "\"/tmp\"" { $3 = "\"/var/run/postgresql\""; print; next } { print }' src/include/pg_config_manual.h > src/include/pg_config_manual.h.new \
    && grep -F '#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql"' src/include/pg_config_manual.h.new \
    && mv src/include/pg_config_manual.h.new src/include/pg_config_manual.h \
    && ./configure --prefix=/opt/postgresql --with-ssl=openssl --without-libxml --without-libxslt \
    && make -j2 \
    && make install
RUN sharedir="$(/opt/postgresql/bin/pg_config --sharedir)" \
    && cp "$sharedir/postgresql.conf.sample" "$sharedir/postgresql.conf.sample.orig" \
    && sed -E -i "s!^#?(listen_addresses)[[:space:]]*=[[:space:]]*[^[:space:]]+.*!\1 = '*'!" "$sharedir/postgresql.conf.sample" \
    && grep -Fx "listen_addresses = '*'" "$sharedir/postgresql.conf.sample"

FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
# Alpine reserves the empty ping group at the chart's required PostgreSQL GID.
RUN apk add --no-cache bash=5.3.9-r1 setpriv=2.42.3-r1 \
      icu-libs=78.1-r0 icu-data-full=78.1-r0 readline=8.3.3-r1 zlib=1.3.2-r1 \
      libssl3=3.5.9-r0 libcrypto3=3.5.9-r0 tzdata=2026e-r0 xz=5.8.4-r0 zstd=1.5.7-r2 \
    && grep -qx 'ping:x:999:' /etc/group \
    && delgroup ping \
    && addgroup -S -g 999 postgres \
    && adduser -S -D -H -u 999 -G postgres -h /var/lib/postgresql -s /bin/sh postgres \
    && install -d -o postgres -g postgres -m 1777 /var/lib/postgresql \
    && install -d -o postgres -g postgres -m 3775 /var/run/postgresql \
    && mkdir /docker-entrypoint-initdb.d
COPY --from=prepare /opt/postgresql /opt/postgresql
ADD --checksum=sha256:9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5 \
    https://raw.githubusercontent.com/docker-library/postgres/4f9ced003ba58a854656ba150d146243d27ae3ac/18/alpine3.24/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Preserve the official entrypoint's root handoff with the native tool already used by the worker.
RUN grep -F 'exec gosu postgres "$BASH_SOURCE" "$@"' /usr/local/bin/docker-entrypoint.sh \
    && sed -i 's/exec gosu postgres /exec setpriv --reuid=postgres --regid=postgres --init-groups /' /usr/local/bin/docker-entrypoint.sh \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh
ENV PATH=/opt/postgresql/bin:$PATH LANG=en_US.utf8 PG_MAJOR=18 PG_VERSION=18.6 PGDATA=/var/lib/postgresql/18/docker
VOLUME /var/lib/postgresql
EXPOSE 5432
STOPSIGNAL SIGINT
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
