FROM postgres:18.6
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/tests
ENV PATH=/opt/tests/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt -r requirements-google.txt
COPY src/ ./src/
RUN python -c "import shutil, site; shutil.copytree('/app/src/worker', site.getsitepackages()[0] + '/taskopotamus_worker')"
COPY config/agent/ ./config/agent/
COPY tests/ ./tests/
COPY deploy/postgres/ ./deploy/postgres/
COPY deploy/worker/ ./deploy/worker/
COPY deploy/testing/run_tests.sh /run_tests.sh
USER postgres
ENTRYPOINT ["sh", "/run_tests.sh"]
