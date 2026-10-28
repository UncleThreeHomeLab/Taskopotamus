FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-google.txt ./
RUN pip install --no-cache-dir -r requirements-google.txt
COPY src/google_integration/ ./src/google_integration/
COPY src/agent/http_server.py ./src/agent/http_server.py
COPY src/runtime.py ./src/runtime.py
USER 10001:10001
CMD ["python", "-m", "src.google_integration.server"]
