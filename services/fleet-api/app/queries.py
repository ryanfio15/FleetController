"""Every statement the Fleet API runs, in one file.

Two conventions hold throughout and are what make the authorisation rule
reviewable:

1. **The scope predicate is in the WHERE clause, never in Python.** Each
   statement that touches a vehicle carries

       (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)

   so "an owner cannot see another owner's car" is enforced by the database on
   every path, including the ones added later. An endpoint that forgets it
   fails to compile against these constants rather than quietly over-sharing.

   This is the hand-rolled stand-in for the row-level security policies of
   §6.6. The real thing is `SET LOCAL app.user_id` inside the transaction with
   RLS policies on the tables; moving to it means deleting the predicate from
   these statements, not restructuring the service. Until then the API connects
   as the owning role, so the predicate is the only thing enforcing scope —
   which is exactly why it lives in SQL where it can be read.

2. **Time windows are always bounded.** `readings` is a hypertable with a
   year's chunks ahead of it; an unbounded scan is the one query here capable
   of hurting a shared database.
"""

# A vehicle's current state. One row per vehicle, metrics folded into a JSON
# object so adding a PID to the sim needs no change here (§5.2 — the schema is
# narrow precisely so that vehicles may carry different PID sets).
#
# DISTINCT ON is the right shape for "latest per group" in Postgres: it walks
# the readings_vehicle_ts index backwards and stops at the first row of each
# group, rather than sorting the window and filtering with a row_number().
FLEET = """
WITH scoped AS (
    SELECT v.vehicle_id, v.label, v.owner_id, v.created_at, v.revoked_at
    FROM vehicles v
    WHERE (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
      AND (%(vehicle_id)s::text IS NULL OR v.vehicle_id = %(vehicle_id)s)
),
latest AS (
    SELECT DISTINCT ON (r.vehicle_id, r.metric) r.vehicle_id, r.metric, r.value, r.ts
    FROM readings r
    JOIN scoped s ON s.vehicle_id = r.vehicle_id
    WHERE r.ts > now() - INTERVAL '15 minutes'
    ORDER BY r.vehicle_id, r.metric, r.ts DESC
),
open_alerts AS (
    SELECT a.vehicle_id,
           count(*) AS n,
           bool_or(a.severity = 'critical') AS has_critical
    FROM alerts a
    JOIN scoped s ON s.vehicle_id = a.vehicle_id
    WHERE a.closed_at IS NULL
    GROUP BY a.vehicle_id
)
SELECT s.vehicle_id,
       s.label,
       s.owner_id::text                                  AS owner_id,
       s.revoked_at,
       max(l.ts)                                         AS last_seen,
       coalesce(
           jsonb_object_agg(l.metric, round(l.value::numeric, 2))
               FILTER (WHERE l.metric IS NOT NULL),
           '{}'::jsonb
       )                                                 AS metrics,
       coalesce(o.n, 0)                                  AS open_alerts,
       coalesce(o.has_critical, false)                   AS has_critical
FROM scoped s
LEFT JOIN latest l      ON l.vehicle_id = s.vehicle_id
LEFT JOIN open_alerts o ON o.vehicle_id = s.vehicle_id
GROUP BY s.vehicle_id, s.label, s.owner_id, s.revoked_at, o.n, o.has_critical
-- Length first, so the grid reads sim-car-2, sim-car-10 rather than the
-- lexical sim-car-10, sim-car-2. The IDs are pod names (migration 003), so
-- they are `<prefix>-<ordinal>` by construction and this sorts them naturally
-- without parsing anything out of the string.
ORDER BY length(s.vehicle_id), s.vehicle_id
"""

# Downsampled series for the detail view's charts. time_bucket is the reason
# TimescaleDB is in this architecture rather than plain Postgres: one hour of a
# 1 Hz metric is 3600 rows, and the browser wants a couple of hundred.
#
# avg() per bucket, except that a gauge the UI draws as "max coolant" would lie
# under averaging — so the caller picks, and overheat spikes survive the
# downsample.
SERIES = """
SELECT time_bucket(%(bucket)s::interval, r.ts) AS ts,
       r.metric,
       round(avg(r.value)::numeric, 2) AS avg,
       round(max(r.value)::numeric, 2) AS max,
       round(min(r.value)::numeric, 2) AS min
FROM readings r
JOIN vehicles v ON v.vehicle_id = r.vehicle_id
WHERE r.vehicle_id = %(vehicle_id)s
  AND r.metric = ANY(%(metrics)s)
  AND r.ts > now() - %(window)s::interval
  AND (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
GROUP BY 1, 2
ORDER BY 1
"""

TRIPS = """
SELECT t.trip_id,
       t.vehicle_id,
       t.started_at,
       t.ended_at,
       round(t.distance_km::numeric, 2) AS distance_km,
       round(t.max_speed::numeric, 1)   AS max_speed,
       round(t.max_coolant::numeric, 1) AS max_coolant
FROM trips t
JOIN vehicles v ON v.vehicle_id = t.vehicle_id
WHERE t.vehicle_id = %(vehicle_id)s
  AND (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
ORDER BY t.started_at DESC
LIMIT %(limit)s
"""

# The alerts inbox. `vehicle_id` NULL means the whole (scoped) fleet, which is
# the inbox view; passing one narrows it to the detail page's panel.
ALERTS = """
SELECT a.alert_id,
       a.vehicle_id,
       a.kind,
       a.severity,
       a.opened_at,
       a.closed_at,
       a.acked_at,
       a.acked_by::text AS acked_by,
       a.detail
FROM alerts a
JOIN vehicles v ON v.vehicle_id = a.vehicle_id
WHERE (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
  AND (%(vehicle_id)s::text IS NULL OR a.vehicle_id = %(vehicle_id)s)
  AND (NOT %(only_open)s OR a.closed_at IS NULL)
ORDER BY a.closed_at IS NOT NULL,          -- open first
         a.severity = 'critical' DESC,     -- then critical
         a.opened_at DESC
LIMIT %(limit)s
"""

# Acknowledge. The scope predicate is inside the UPDATE rather than checked
# beforehand, so there is no window between the check and the write, and a
# caller who cannot see the alert simply updates zero rows.
#
# `acked_at IS NULL` is *not* in the predicate: re-acking is idempotent and
# returns the original acknowledgement, which is what a double-clicked button
# should do. Distinguishing "not yours" from "already acked" is what the empty
# result below is for.
ACK = """
UPDATE alerts a
SET acked_at = now(),
    acked_by = %(user_id)s::uuid
FROM vehicles v
WHERE v.vehicle_id = a.vehicle_id
  AND a.alert_id = %(alert_id)s
  AND a.acked_at IS NULL
  AND (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
RETURNING a.alert_id, a.vehicle_id, a.kind, a.severity,
          a.opened_at, a.closed_at, a.acked_at, a.acked_by::text AS acked_by,
          a.detail
"""

# Run when ACK touches nothing, to tell "already acknowledged" (200, return it)
# apart from "no such alert, or not yours" (404). Same predicate, so the 404
# covers both of the latter — see identity.py on why it is not a 403.
ACK_LOOKUP = """
SELECT a.alert_id, a.vehicle_id, a.kind, a.severity,
       a.opened_at, a.closed_at, a.acked_at, a.acked_by::text AS acked_by,
       a.detail
FROM alerts a
JOIN vehicles v ON v.vehicle_id = a.vehicle_id
WHERE a.alert_id = %(alert_id)s
  AND (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
"""

# Fleet-wide counters for the header strip. Deliberately one round trip: the
# UI polls this every few seconds and three separate queries would triple that.
SUMMARY = """
WITH scoped AS (
    SELECT v.vehicle_id FROM vehicles v
    WHERE (%(whole_fleet)s OR v.owner_id = %(user_id)s::uuid)
)
SELECT (SELECT count(*) FROM scoped)                                AS vehicles,
       (SELECT count(DISTINCT r.vehicle_id)
          FROM readings r JOIN scoped s ON s.vehicle_id = r.vehicle_id
         WHERE r.ts > now() - %(stale)s::interval)                  AS online,
       (SELECT count(*)
          FROM alerts a JOIN scoped s ON s.vehicle_id = a.vehicle_id
         WHERE a.closed_at IS NULL)                                 AS open_alerts,
       (SELECT count(*)
          FROM alerts a JOIN scoped s ON s.vehicle_id = a.vehicle_id
         WHERE a.closed_at IS NULL AND a.acked_at IS NULL)          AS unacked_alerts,
       (SELECT coalesce(round(sum(t.distance_km)::numeric, 1), 0)
          FROM trips t JOIN scoped s ON s.vehicle_id = t.vehicle_id
         WHERE t.started_at > date_trunc('day', now()))             AS distance_today_km,
       (SELECT count(*)
          FROM readings r JOIN scoped s ON s.vehicle_id = r.vehicle_id
         WHERE r.ts > now() - INTERVAL '1 minute')                  AS readings_last_minute
"""

READYZ = "SELECT 1"
