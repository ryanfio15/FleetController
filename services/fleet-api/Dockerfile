# Build context is the repository root, so the shared library can be copied in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY libs/fc_common /tmp/fc_common
RUN pip install --no-cache-dir /tmp/fc_common && rm -rf /tmp/fc_common

COPY services/fleet-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/fleet-api/app ./app

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8081
# Unlike ingest-api this binds all interfaces by default: there is no Envoy
# sidecar sharing the namespace, and ingress-nginx reaches it from another pod.
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${FLEET_HOST:-0.0.0.0} --port ${FLEET_PORT:-8081} --no-access-log"]
