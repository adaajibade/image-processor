ARG PYTHON_IMAGE=python:3.14.4-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

FROM base AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv && pip install --no-cache-dir --require-hashes -r requirements.txt

FROM dependencies AS check
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY pyproject.toml ./
COPY image_service ./image_service
COPY tests ./tests
RUN ruff check . && ruff format --check . && mypy && pytest -q

FROM base AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH HOST=0.0.0.0 PORT=3000
WORKDIR /app
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=check --chown=app:app /app/image_service ./image_service
USER app
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health/ready', timeout=2)"
CMD ["python", "-m", "image_service"]
