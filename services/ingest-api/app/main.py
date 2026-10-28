"""ingest-api — the only way telemetry enters FleetController.

Implements POST /v1/telemetry exactly as specified in the API contract:
202 accepted · 400 whole batch rejected · 403 identity mismatch ·
413 over 500 readings · 503 + Retry-After when the buffer is unavailable.

The ordering of those checks is deliberate and documented in
docs/TECHNICAL_PLAN.md §6.1. In particular the size check precedes model
validation, so an over-long batch yields 413 rather than 400.
"""

import json
from contextlib import asynccontextmanager

import nats
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from nats.js.api import DiscardPolicy, StorageType, StreamConfig
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import ValidationError

from app.identity import Identity, from_xfcc
from app.settings import settings
from fc_common import TelemetryBatch
from fc_common.logging import setup

log = setup("ingest-api")

REQUESTS = Counter(
    "ingest_requests_total", "Telemetry requests by outcome", ["status"]
)
READINGS = Counter("ingest_readings_total", "Readings accepted and published")
PUBLISH_SECONDS = Histogram(
    "ingest_publish_duration_seconds", "Time to publish a batch to JetStream"
)

_nc: nats.NATS | None = None
_js = None


async def _connect_jetstream() -> None:
    """Connect to NATS and declare the TELEMETRY stream.

    Declaring the stream here rather than in a separate init job keeps the
    topology in one place and makes it idempotent across restarts.
    """
    global _nc, _js
    _nc = await nats.connect(
        settings.nats_url,
        name="ingest-api",
        max_reconnect_attempts=-1,  # reconnect forever; 503s cover the gap
        reconnect_time_wait=1,
    )
    _js = _nc.jetstream()

    config = StreamConfig(
        name=settings.stream_name,
        subjects=[f"{settings.subject_prefix}.*"],
        storage=StorageType.FILE,
        max_age=settings.stream_max_age_seconds,
        max_bytes=settings.stream_max_bytes,
        # Deduplicates sim retries after a 503: a replayed batch carries the
        # same Nats-Msg-Id and is silently dropped inside this window (§6.2).
        duplicate_window=settings.stream_duplicate_window_seconds,
        discard=DiscardPolicy.OLD,
    )
    try:
        await _js.add_stream(config=config)
        log.info("stream created", extra={"stream": settings.stream_name})
    except Exception:
        await _js.update_stream(config=config)
        log.info("stream updated", extra={"stream": settings.stream_name})


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _connect_jetstream()
    yield
    if _nc is not None:
        await _nc.drain()


app = FastAPI(title="FleetController ingest", version="0.1.0", lifespan=lifespan)


def _reject(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    REQUESTS.labels(status=str(status)).inc()
    return JSONResponse({"detail": detail}, status_code=status, headers=headers or {})


def _resolve_identity(xfcc: str | None, claimed: str | None) -> Identity | None:
    """Decide who is talking to us. See app/identity.py for the rule."""
    identity = from_xfcc(xfcc)
    if identity is not None:
        return identity
    if settings.allow_insecure_identity and claimed:
        # Stage 0 only. P2 removes this branch along with the setting.
        return Identity(vehicle_id=claimed, cert_fp=None, verified=False)
    return None


@app.post("/v1/telemetry")
async def post_telemetry(
    request: Request,
    x_forwarded_client_cert: str | None = Header(default=None),
) -> Response:
    raw = await request.body()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _reject(400, f"malformed JSON: {exc.msg}")
    if not isinstance(payload, dict):
        return _reject(400, "body must be a JSON object")

    # 413 before 400: the size cap is checked on the raw payload so an
    # oversized batch is rejected for being oversized, not for failing
    # validation. Counting here also avoids validating 10k readings to then
    # discard them.
    readings = payload.get("readings")
    if isinstance(readings, list) and len(readings) > settings.max_readings_per_batch:
        return _reject(
            413,
            f"{len(readings)} readings exceeds the "
            f"{settings.max_readings_per_batch} per-batch limit",
        )

    identity = _resolve_identity(x_forwarded_client_cert, payload.get("vehicle_id"))
    if identity is None:
        return _reject(403, "no verified client certificate")

    # The body's vehicle_id is a claim; the certificate is the identity.
    if payload.get("vehicle_id") != identity.vehicle_id:
        log.warning(
            "identity mismatch",
            extra={"cert_cn": identity.vehicle_id, "claimed": payload.get("vehicle_id")},
        )
        return _reject(403, "vehicle_id does not match client certificate CN")

    try:
        batch = TelemetryBatch.model_validate(payload)
    except ValidationError as exc:
        # The contract rejects the whole batch, not the offending reading: for
        # a fleet you control, failing loudly beats silently dropping fields.
        first = exc.errors()[0]
        log.info(
            "batch rejected",
            extra={
                "vehicle_id": identity.vehicle_id,
                "errors": len(exc.errors()),
                "first": f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
            },
        )
        return _reject(400, f"invalid batch ({len(exc.errors())} errors)")

    try:
        with PUBLISH_SECONDS.time():
            await _js.publish(
                f"{settings.subject_prefix}.{batch.vehicle_id}",
                raw,
                headers={"Nats-Msg-Id": batch.batch_id},
                timeout=5,
            )
    except Exception as exc:
        # JetStream unavailable or at its limits. Tell the device to come back,
        # and let its store-and-forward buffer hold the data meanwhile.
        log.error(
            "publish failed",
            extra={"vehicle_id": batch.vehicle_id, "batch_id": batch.batch_id},
            exc_info=exc,
        )
        return _reject(
            503,
            "telemetry buffer unavailable",
            {"Retry-After": str(settings.retry_after_seconds)},
        )

    REQUESTS.labels(status="202").inc()
    READINGS.inc(len(batch.readings))
    return Response(status_code=202)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up. Deliberately does not touch NATS."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Response:
    """Readiness: we can actually accept telemetry right now."""
    if _nc is None or not _nc.is_connected:
        return JSONResponse({"status": "no nats"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
