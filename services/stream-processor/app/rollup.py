"""Windowed derivations: trips and sustained-condition alerts.

These run as SQL on a ticker rather than as streaming state, because the
processor may be running in up to six replicas sharing one consumer with no
per-vehicle ordering (docs/TECHNICAL_PLAN.md §6.3). A Postgres advisory lock
elects one replica to run each pass; the statements are idempotent anyway, so a
lock failure is harmless rather than dangerous.
"""

import asyncio

from prometheus_client import Counter
from psycopg_pool import AsyncConnectionPool

from fc_common.logging import setup

log = setup("rollup")

PASSES = Counter("processor_rollup_passes_total", "Rollup passes", ["outcome"])

INTERVAL_SECONDS = 60
LOCK_KEY = 0x11E7C0  # arbitrary, just has to be stable across replicas

# --- sustained overheat: >110 C for at least 30 s in the recent window -------
SUSTAINED_OVERHEAT = """
    INSERT INTO alerts (vehicle_id, kind, severity, opened_at, detail)
    SELECT vehicle_id, 'overheat', 'warn', min(ts),
           jsonb_build_object('peak_c', max(value), 'samples', count(*))
    FROM readings
    WHERE metric = 'coolant_temp'
      AND value > 110
      AND ts > now() - INTERVAL '2 minutes'
    GROUP BY vehicle_id
    HAVING max(ts) - min(ts) >= INTERVAL '30 seconds'
    ON CONFLICT (vehicle_id, kind) WHERE closed_at IS NULL DO NOTHING
"""

# --- charging-system fault: engine running but bus voltage low ---------------
WEAK_CHARGING = """
    WITH running AS (
        SELECT r.vehicle_id, r.ts, r.value AS volts
        FROM readings r
        JOIN readings e
          ON e.vehicle_id = r.vehicle_id AND e.ts = r.ts AND e.metric = 'rpm'
        WHERE r.metric = 'module_voltage'
          AND r.ts > now() - INTERVAL '2 minutes'
          AND e.value > 500
    )
    INSERT INTO alerts (vehicle_id, kind, severity, opened_at, detail)
    SELECT vehicle_id, 'weak_battery', 'warn', min(ts),
           jsonb_build_object('min_volts', min(volts))
    FROM running
    WHERE volts < 13.0
    GROUP BY vehicle_id
    HAVING count(*) >= 10
    ON CONFLICT (vehicle_id, kind) WHERE closed_at IS NULL DO NOTHING
"""

# --- auto-close: condition absent for 5 minutes ------------------------------
CLOSE_STALE = """
    UPDATE alerts a SET closed_at = now()
    WHERE a.closed_at IS NULL
      AND a.kind IN ('overheat', 'weak_battery', 'overrev')
      AND NOT EXISTS (
          SELECT 1 FROM readings r
          WHERE r.vehicle_id = a.vehicle_id
            AND r.ts > now() - INTERVAL '5 minutes'
            AND (
                 (a.kind = 'overheat'     AND r.metric = 'coolant_temp'   AND r.value > 110)
              OR (a.kind = 'weak_battery' AND r.metric = 'module_voltage' AND r.value < 13.0)
              OR (a.kind = 'overrev'      AND r.metric = 'rpm'            AND r.value > 6000)
            )
      )
"""

# --- trips: sessions separated by >=180 s of engine-off -----------------------
# Sessions are found with a gap-and-island scan over the rpm series, then speed
# is integrated trapezoidally over the 1 Hz samples for distance. UNIQUE
# (vehicle_id, started_at) makes re-running this a no-op.
TRIPS = """
    WITH running AS (
        SELECT vehicle_id, ts
        FROM readings
        WHERE metric = 'rpm' AND value > 400
          AND ts > now() - INTERVAL '6 hours'
    ),
    gaps AS (
        SELECT vehicle_id, ts,
               CASE WHEN ts - lag(ts) OVER (PARTITION BY vehicle_id ORDER BY ts)
                         > INTERVAL '180 seconds'
                    OR lag(ts) OVER (PARTITION BY vehicle_id ORDER BY ts) IS NULL
                    THEN 1 ELSE 0 END AS is_start
        FROM running
    ),
    sessions AS (
        SELECT vehicle_id, ts,
               sum(is_start) OVER (PARTITION BY vehicle_id ORDER BY ts) AS session
        FROM gaps
    ),
    bounds AS (
        SELECT vehicle_id, session, min(ts) AS started_at, max(ts) AS ended_at
        FROM sessions GROUP BY vehicle_id, session
        HAVING max(ts) - min(ts) >= INTERVAL '60 seconds'
    ),
    measured AS (
        SELECT b.vehicle_id, b.started_at, b.ended_at,
               -- km/h sampled at 1 Hz: sum(v) / 3600 approximates distance in km
               coalesce(sum(s.value) FILTER (WHERE s.metric = 'speed'), 0) / 3600.0
                   AS distance_km,
               max(s.value) FILTER (WHERE s.metric = 'speed')        AS max_speed,
               max(s.value) FILTER (WHERE s.metric = 'coolant_temp') AS max_coolant
        FROM bounds b
        JOIN readings s
          ON s.vehicle_id = b.vehicle_id
         AND s.ts BETWEEN b.started_at AND b.ended_at
        GROUP BY b.vehicle_id, b.started_at, b.ended_at
    )
    INSERT INTO trips (vehicle_id, started_at, ended_at, distance_km, max_speed, max_coolant)
    SELECT vehicle_id, started_at, ended_at, distance_km, max_speed, max_coolant
    FROM measured
    ON CONFLICT (vehicle_id, started_at) DO UPDATE
        SET ended_at    = EXCLUDED.ended_at,
            distance_km = EXCLUDED.distance_km,
            max_speed   = EXCLUDED.max_speed,
            max_coolant = EXCLUDED.max_coolant
"""

STATEMENTS = (
    ("sustained_overheat", SUSTAINED_OVERHEAT),
    ("weak_charging", WEAK_CHARGING),
    ("close_stale", CLOSE_STALE),
    ("trips", TRIPS),
)


async def run_once(pool: AsyncConnectionPool) -> bool:
    """One rollup pass. Returns False if another replica holds the lock."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
            row = await cur.fetchone()
            if not row or not row[0]:
                return False
            try:
                for name, sql in STATEMENTS:
                    await cur.execute(sql)
            finally:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return True


async def run(pool: AsyncConnectionPool, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            ran = await run_once(pool)
            PASSES.labels(outcome="ok" if ran else "skipped").inc()
        except Exception as exc:
            PASSES.labels(outcome="error").inc()
            log.error("rollup pass failed", exc_info=exc)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
