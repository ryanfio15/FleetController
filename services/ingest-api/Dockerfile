# Build context is the repository root, so the shared library can be copied in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY libs/fc_common /tmp/fc_common
RUN pip install --no-cache-dir /tmp/fc_common && rm -rf /tmp/fc_common

COPY services/ingest-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ingest-api/app ./app

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${INGEST_HOST:-127.0.0.1} --port ${INGEST_PORT:-8080} --no-access-log"]
