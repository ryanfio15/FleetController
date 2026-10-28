"""vehicle-sim — one simulated car, one process.

Stands in for an OBD-II dongle until real hardware exists. Two jobs beyond
generating plausible data:

  * store-and-forward — buffers locally when ingest is unreachable or returns
    503, then backfills. This is what makes "durable buffer; replay after
    failures" something you can demonstrate by killing NATS for two minutes.
  * correct retry semantics — 503/429/5xx and network errors are retried;
    400 is not. Retrying a malformed batch forever is a poison-pill livelock,
    so a rejected batch is dropped and counted.
"""

import asyncio
import atexit
import os
import random
import signal
import socket
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from prometheus_client import Counter, Gauge, start_http_server

from app.model import Vehicle
from app.settings import settings
from fc_common.logging import setup

log = setup("vehicle-sim")

SENT = Counter("sim_readings_sent_total", "Readings accepted by ingest")
DROPPED = Counter("sim_readings_dropped_total", "Readings dropped", ["reason"])
POSTS = Counter("sim_posts_total", "Telemetry POSTs by status", ["status"])
BUFFERED = Gauge("sim_buffer_readings", "Readings held in the forward buffer")

_shutdown = asyncio.Event()


def resolve_vehicle_id() -> str:
    """Claim a stable ordinal identity.

    Scaled Compose replicas are identical and get hex hostnames, so identity
    has to come from somewhere outside the container. Each sim atomically
    claims the lowest free ordinal by creating sim-car-NN.claim with O_EXCL on
    a shared volume, and releases it on shutdown.

    This deliberately emulates StatefulSet ordinals, which is how Stage 2 must
    assign identity too: a Deployment's replicas cannot each mount a different
    certificate, so vehicle-sim becomes a StatefulSet in Kubernetes and the
    claim file is replaced by the per-ordinal cert volume.
    """
    if settings.vehicle_id:
        return settings.vehicle_id

    claims = Path(settings.identity_dir)
    claims.mkdir(parents=True, exist_ok=True)

    for ordinal in range(1, settings.max_vehicles + 1):
        path = claims / f"sim-car-{ordinal:02d}.claim"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(socket.gethostname())
        atexit.register(path.unlink, missing_ok=True)
        return path.stem

    raise RuntimeError(
        f"all {settings.max_vehicles} vehicle identities are claimed; "
        "raise SIM_MAX_VEHICLES or clear the identity volume"
    )


class Forwarder:
    """Bounded ring buffer plus the POST loop."""

    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id
        self.buffer: deque[dict] = deque(maxlen=settings.buffer_max_readings)
        self.backoff = settings.retry_base_seconds

    def add(self, readings: list[dict]) -> None:
        overflow = len(self.buffer) + len(readings) - self.buffer.maxlen
        if overflow > 0:
            # deque drops the oldest automatically; count it so the loss is
            # visible in Grafana rather than silent.
            DROPPED.labels(reason="buffer_full").inc(overflow)
        self.buffer.extend(readings)
        BUFFERED.set(len(self.buffer))

    async def flush(self, client: httpx.AsyncClient, dtcs: list[str]) -> None:
        """Send everything buffered, oldest first, in contract-sized batches."""
        while self.buffer and not _shutdown.is_set():
            chunk = [
                self.buffer.popleft()
                for _ in range(min(settings.max_readings_per_batch, len(self.buffer)))
            ]
            body = {
                "batch_id": uuid.uuid4().hex,
                "vehicle_id": self.vehicle_id,
                "readings": chunk,
                "dtcs": dtcs,
            }

            try:
                response = await client.post(settings.ingest_url, json=body, timeout=10)
            except httpx.HTTPError as exc:
                log.warning("post failed", extra={"err": str(exc)})
                POSTS.labels(status="network_error").inc()
                self.buffer.extendleft(reversed(chunk))
                await self._back_off()
                return

            POSTS.labels(status=str(response.status_code)).inc()

            if response.status_code == 202:
                SENT.inc(len(chunk))
                self.backoff = settings.retry_base_seconds
                BUFFERED.set(len(self.buffer))
                continue

            if response.status_code in (429, 503) or response.status_code >= 500:
                # Retryable. Put the chunk back and respect Retry-After.
                self.buffer.extendleft(reversed(chunk))
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else None
                await self._back_off(wait)
                return

            # 400 / 403 / 413 are the device's fault and will not improve on
            # retry. Drop, count, and keep the pipeline moving.
            log.error(
                "batch rejected, dropping",
                extra={
                    "status": response.status_code,
                    "vehicle_id": self.vehicle_id,
                    "readings": len(chunk),
                    "detail": response.text[:200],
                },
            )
            DROPPED.labels(reason=f"http_{response.status_code}").inc(len(chunk))
            BUFFERED.set(len(self.buffer))

    async def _back_off(self, hint: float | None = None) -> None:
        wait = hint if hint is not None else self.backoff
        # Jitter so 50 sims do not synchronise into a thundering herd when
        # ingest comes back up.
        wait = min(wait, settings.retry_max_seconds) * random.uniform(0.8, 1.2)
        self.backoff = min(self.backoff * 2, settings.retry_max_seconds)
        log.info("backing off", extra={"seconds": round(wait, 1)})
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    vehicle_id = resolve_vehicle_id()
    # Seeded per vehicle so a given car drives the same route every run, which
    # makes "did my change break the data?" answerable.
    rng = random.Random(vehicle_id)

    car = Vehicle(vehicle_id=vehicle_id, rng=rng, fault=settings.fault)
    forwarder = Forwarder(vehicle_id)

    period = 1.0 / settings.sample_hz
    samples_per_batch = max(1, int(settings.batch_seconds * settings.sample_hz))

    log.info(
        "starting",
        extra={"vehicle_id": vehicle_id, "fault": settings.fault, "url": settings.ingest_url},
    )

    pending: list[dict] = []
    samples_taken = 0
    async with httpx.AsyncClient() as client:
        while not _shutdown.is_set():
            car.step(period)
            if settings.fault != "none" and car.elapsed >= settings.fault_after_seconds:
                if not car.fault_active:
                    log.warning("injecting fault", extra={"fault": settings.fault})
                car.fault_active = True

            now = datetime.now(timezone.utc).isoformat()
            pending.extend(
                {"ts": now, "metric": metric, "value": value}
                for metric, value in car.sample().items()
            )

            samples_taken += 1
            if samples_taken >= samples_per_batch:
                samples_taken = 0
                if pending:
                    forwarder.add(pending)
                    pending = []
                    await forwarder.flush(client, car.active_dtcs())

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass

    log.info("stopped", extra={"vehicle_id": vehicle_id})


def main() -> None:
    start_http_server(settings.metrics_port)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)
    loop.run_until_complete(run())


if __name__ == "__main__":
    main()
