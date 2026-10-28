"""stream-processor — JetStream pull consumer → TimescaleDB.

Stateless by design. It writes readings, records DTCs, and opens alerts that
can be decided from a single sample. Trips and sustained-condition alerts are
derived in SQL (see rollup.py) precisely because this process may be running in
up to six copies with no per-vehicle ordering guarantee.

Acknowledgement discipline: a message is acked only after its rows are
committed. A crash mid-batch means redelivery, and the unique index on
readings absorbs the duplicate — at-least-once delivery, effectively-once
storage.
"""

import asyncio
import json
import signal

import nats
from nats.js.api import AckPolicy, ConsumerConfig
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg_pool import AsyncConnectionPool

from app import alerts, rollup
from app.settings import settings
from fc_common import TelemetryBatch
from fc_common.logging import setup

log = setup("stream-processor")

ROWS = Counter("processor_rows_written_total", "Reading rows inserted")
BATCHES = Counter("processor_batches_total", "Batches processed", ["outcome"])
ALERTS = Counter("processor_alerts_opened_total", "Alerts opened", ["kind"])
BATCH_SECONDS = Histogram(
    "processor_batch_duration_seconds", "Wall time to process one fetched batch"
)
PENDING = Gauge(
    "processor_consumer_pending", "Messages pending on the durable consumer"
)

_shutdown = asyncio.Event()

INSERT_READINGS = """
    INSERT INTO readings (ts, vehicle_id, metric, value)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (vehicle_id, metric, ts) DO NOTHING
"""

UPSERT_DTC = """
    INSERT INTO dtc_events (vehicle_id, code, first_seen, last_seen)
    VALUES (%(vid)s, %(code)s, %(ts)s, %(ts)s)
    ON CONFLICT (vehicle_id, code, first_seen) DO UPDATE
        SET last_seen = GREATEST(dtc_events.last_seen, EXCLUDED.last_seen)
"""

OPEN_ALERT = """
    INSERT INTO alerts (vehicle_id, kind, severity, opened_at, detail)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (vehicle_id, kind) WHERE closed_at IS NULL DO NOTHING
"""


async def handle(pool: AsyncConnectionPool, payload: bytes) -> int:
    """Persist one telemetry batch. Returns the number of readings written."""
    batch = TelemetryBatch.model_validate_json(payload)

    rows = [(r.ts, batch.vehicle_id, r.metric, r.value) for r in batch.readings]
    opened: list[tuple] = []

    latest_ts = max(r.ts for r in batch.readings)
    for reading in batch.readings:
        hit = alerts.evaluate(reading)
        if hit:
            kind, severity, detail = hit
            opened.append((batch.vehicle_id, kind, severity, reading.ts, json.dumps(detail)))

    dtc_hit = alerts.from_dtcs(batch.dtcs)
    if dtc_hit:
        kind, severity, detail = dtc_hit
        opened.append((batch.vehicle_id, kind, severity, latest_ts, json.dumps(detail)))

    # One transaction per batch: either the whole batch is durable and we ack,
    # or nothing is and JetStream redelivers it.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(INSERT_READINGS, rows)
            for code in batch.dtcs:
                await cur.execute(
                    UPSERT_DTC, {"vid": batch.vehicle_id, "code": code, "ts": latest_ts}
                )
            for alert in opened:
                await cur.execute(OPEN_ALERT, alert)

    for alert in opened:
        ALERTS.labels(kind=alert[1]).inc()
    ROWS.inc(len(rows))
    return len(rows)


async def consume(pool: AsyncConnectionPool) -> None:
    nc = await nats.connect(
        settings.nats_url,
        name="stream-processor",
        max_reconnect_attempts=-1,
        reconnect_time_wait=1,
    )
    js = nc.jetstream()

    sub = await js.pull_subscribe(
        settings.subject_filter,
        durable=settings.durable_name,
        stream=settings.stream_name,
        config=ConsumerConfig(
            durable_name=settings.durable_name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=settings.ack_wait_seconds,
            max_ack_pending=settings.max_ack_pending,
            max_deliver=settings.max_deliver,
        ),
    )
    log.info("consuming", extra={"durable": settings.durable_name})

    while not _shutdown.is_set():
        try:
            msgs = await sub.fetch(
                settings.fetch_batch, timeout=settings.fetch_timeout_seconds
            )
        except (asyncio.TimeoutError, nats.errors.TimeoutError):
            # No traffic. Still refresh the lag gauge so the KEDA signal and
            # the Grafana panel stay live on an idle fleet.
            await _publish_lag(sub)
            continue

        with BATCH_SECONDS.time():
            for msg in msgs:
                try:
                    await handle(pool, msg.data)
                    await msg.ack()
                    BATCHES.labels(outcome="ok").inc()
                except Exception as exc:
                    # Nak with a delay rather than acking: the message stays on
                    # the stream and is retried, up to max_deliver, then lands
                    # in the dead-letter path.
                    log.error("batch failed", exc_info=exc)
                    BATCHES.labels(outcome="error").inc()
                    await msg.nak(delay=5)

        await _publish_lag(sub)

    await nc.drain()


async def _publish_lag(sub) -> None:
    """Mirror the consumer's pending count — the same number KEDA scales on."""
    try:
        info = await sub.consumer_info()
        PENDING.set(info.num_pending)
    except Exception:
        pass


async def main() -> None:
    start_http_server(settings.metrics_port)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)

    pool = AsyncConnectionPool(
        settings.db_dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
    )
    await pool.open(wait=True, timeout=60)
    log.info("database ready")

    try:
        await asyncio.gather(
            consume(pool),
            rollup.run(pool, _shutdown),
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
