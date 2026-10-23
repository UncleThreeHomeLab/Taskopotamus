FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements-mcp.txt \
    && mkdir -p /home/mcp/data \
    && chmod 700 /home/mcp/data \
    && chown -R 10001:10001 /home/mcp
COPY src/mcp_server/ ./src/mcp_server/
COPY src/profiles/ ./src/profiles/
COPY src/runtime.py ./src/runtime.py
COPY config/agent/ ./config/agent/
USER 10001:10001
CMD ["python", "-m", "src.mcp_server.server"]
