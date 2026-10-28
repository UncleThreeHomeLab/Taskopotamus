FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-mcp.txt requirements-storage.txt ./
RUN pip install --no-cache-dir -r requirements-mcp.txt
COPY src/mcp_server/ ./src/mcp_server/
COPY src/profiles/ ./src/profiles/
COPY src/runtime.py ./src/runtime.py
COPY src/storage/ ./src/storage/
COPY config/agent/ ./config/agent/
USER 10001:10001
CMD ["python", "-m", "src.mcp_server.server"]
