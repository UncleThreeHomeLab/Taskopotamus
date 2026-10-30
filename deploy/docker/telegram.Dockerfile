FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources \
    && ! grep -qE '^URIs:.*http://' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-storage.txt ./
RUN pip install --no-cache-dir -r requirements-storage.txt
COPY src/telegram/ ./src/telegram/
COPY src/runtime.py ./src/runtime.py
COPY src/storage/ ./src/storage/
USER 10001:10001
CMD ["python", "-m", "src.telegram.bot"]
