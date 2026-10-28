-- FleetController initial schema.  docs/TECHNICAL_PLAN.md §5.2
--
-- Narrow (metric, value) rows rather than a column per PID: vehicles support
-- different PID sets, and narrow rows compress far better when segmented by
-- (vehicle_id, metric).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------- vehicles --
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id  TEXT PRIMARY KEY,                  -- equals the client cert CN
    owner_id    UUID        NOT NULL,              -- Keycloak `sub`
    fleet_id    UUID        NOT NULL,
    label       TEXT,
    cert_fp     TEXT,                              -- SHA-256, set at provisioning
    revoked_at  TIMESTAMPTZ,                       -- set by DELETE .../{id}/cert
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ingest polls this to refresh its revocation denylist (§7.2).
CREATE INDEX IF NOT EXISTS vehicles_revoked ON vehicles (cert_fp)
    WHERE revoked_at IS NOT NULL;

-- ---------------------------------------------------------------- readings --
CREATE TABLE IF NOT EXISTS readings (
    ts          TIMESTAMPTZ      NOT NULL,
    vehicle_id  TEXT             NOT NULL REFERENCES vehicles ON DELETE CASCADE,
    metric      TEXT             NOT NULL,
    value       DOUBLE PRECISION NOT NULL
);

-- 1 day is right at the 3-car scale of Stage 0. Drop to 1-4 hours past ~10
-- cars so a chunk plus its index stays small relative to RAM (§6.4).
SELECT create_hypertable('readings', 'ts',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists       => TRUE);

-- The idempotency guarantee. JetStream may redeliver after a processor crash;
-- the processor inserts ON CONFLICT DO NOTHING against this index, which is
-- what turns at-least-once delivery into effectively-once storage (§6.2).
CREATE UNIQUE INDEX IF NOT EXISTS readings_pk
    ON readings (vehicle_id, metric, ts);

CREATE INDEX IF NOT EXISTS readings_vehicle_ts
    ON readings (vehicle_id, ts DESC);

-- ------------------------------------------------------------------- trips --
CREATE TABLE IF NOT EXISTS trips (
    trip_id     BIGSERIAL PRIMARY KEY,
    vehicle_id  TEXT        NOT NULL REFERENCES vehicles ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    distance_km DOUBLE PRECISION,
    max_speed   DOUBLE PRECISION,
    max_coolant DOUBLE PRECISION,
    UNIQUE (vehicle_id, started_at)                -- makes the rollup idempotent
);

-- ------------------------------------------------------------------ alerts --
CREATE TABLE IF NOT EXISTS alerts (
    alert_id    BIGSERIAL PRIMARY KEY,
    vehicle_id  TEXT        NOT NULL REFERENCES vehicles ON DELETE CASCADE,
    kind        TEXT        NOT NULL,              -- overheat|weak_battery|dtc|overrev
    severity    TEXT        NOT NULL,              -- warn|critical
    opened_at   TIMESTAMPTZ NOT NULL,
    closed_at   TIMESTAMPTZ,
    acked_at    TIMESTAMPTZ,
    acked_by    UUID,
    detail      JSONB
);

-- At 1 Hz an un-deduplicated overheat would open 3600 alerts an hour. This
-- partial unique index caps it at one open alert per (vehicle, kind).
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open
    ON alerts (vehicle_id, kind) WHERE closed_at IS NULL;

CREATE INDEX IF NOT EXISTS alerts_open ON alerts (opened_at DESC)
    WHERE closed_at IS NULL;

-- -------------------------------------------------------------- dtc_events --
CREATE TABLE IF NOT EXISTS dtc_events (
    vehicle_id  TEXT        NOT NULL REFERENCES vehicles ON DELETE CASCADE,
    code        TEXT        NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL,
    last_seen   TIMESTAMPTZ NOT NULL,
    cleared_at  TIMESTAMPTZ,
    PRIMARY KEY (vehicle_id, code, first_seen)
);
