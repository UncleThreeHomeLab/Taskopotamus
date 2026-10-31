FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apk add --no-cache tini=0.19.0-r3 \
    && ln -s /sbin/tini /usr/bin/tini \
    && test -x /usr/bin/tini
COPY requirements-storage.txt requirements-security.txt ./
RUN pip install --no-cache-dir -c requirements-security.txt -r requirements-storage.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip \
    && python -c "import shutil, sysconfig; shutil.rmtree(sysconfig.get_path('stdlib') + '/ensurepip')"
COPY src/telegram/ ./src/telegram/
COPY src/runtime.py ./src/runtime.py
COPY src/storage/ ./src/storage/
USER 10001:10001
CMD ["python", "-m", "src.telegram.bot"]
