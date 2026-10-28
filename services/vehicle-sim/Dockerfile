# Build context is the repository root, so the shared library can be copied in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY libs/fc_common /tmp/fc_common
RUN pip install --no-cache-dir /tmp/fc_common && rm -rf /tmp/fc_common

COPY services/vehicle-sim/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/vehicle-sim/app ./app

RUN useradd --create-home --uid 10001 app && chown -R app /app

# Docker seeds a fresh named volume from the image's content at this path,
# ownership included — without this the identity volume mounts root-owned and
# the unprivileged process cannot claim an ordinal.
RUN mkdir -p /identity && chown app /identity

USER app

EXPOSE 9101
CMD ["python", "-m", "app.main"]
