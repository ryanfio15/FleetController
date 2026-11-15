-- Re-seed the simulated fleet with unpadded, 0-based vehicle IDs.
--
-- Why: in Kubernetes vehicle-sim is a StatefulSet (plan §G7), so its pods are
-- named `sim-car-0`..`sim-car-N`. Making the vehicle_id equal that pod name
-- exactly collapses four identifiers into one:
--
--     pod name == hostname == certificate CN == vehicle_id
--
-- That is what lets the StatefulSet inject identity with a plain
-- `fieldRef: metadata.name` and lets cert-manager-csi-driver template each pod's
-- certificate CN from `${POD_NAME}`. The zero-padded `sim-car-01` from migration
-- 002 would have needed a translation step in both places, and a translation
-- step is somewhere the two can disagree.
--
-- 002 is left untouched: it is already recorded in schema_migrations on every
-- existing database, and rewriting an applied migration means two databases
-- claiming the same version with different contents.

INSERT INTO vehicles (vehicle_id, owner_id, fleet_id, label)
SELECT
    format('sim-car-%s', n),
    -- Same parity rule as 002, so the §8.1 "owner sees 404 for another owner's
    -- car" test still has two owners to assert against.
    CASE WHEN n % 2 = 1
         THEN '11111111-1111-1111-1111-111111111111'::uuid
         ELSE '22222222-2222-2222-2222-222222222222'::uuid END,
    '33333333-3333-3333-3333-333333333333'::uuid,
    format('Simulated car %s', n)
FROM generate_series(0, 49) AS n
ON CONFLICT (vehicle_id) DO NOTHING;

-- The two schemes overlap more than it looks: lpad('10', 2, '0') is just '10',
-- so `sim-car-10`..`sim-car-49` are spelled identically under both and are
-- already correct. Only 002's single-digit padded rows and its `sim-car-50`
-- are left orphaned.
--
-- The NOT EXISTS guards are not decoration. All four of these tables declare
-- `REFERENCES vehicles ON DELETE CASCADE`, so deleting a vehicle that has
-- history silently takes the history with it. A row with readings is evidence
-- from an earlier run; this migration retires placeholders, not data.
DELETE FROM vehicles v
WHERE (v.vehicle_id ~ '^sim-car-0[1-9]$' OR v.vehicle_id = 'sim-car-50')
  AND NOT EXISTS (SELECT 1 FROM readings   r WHERE r.vehicle_id = v.vehicle_id)
  AND NOT EXISTS (SELECT 1 FROM trips      t WHERE t.vehicle_id = v.vehicle_id)
  AND NOT EXISTS (SELECT 1 FROM alerts     a WHERE a.vehicle_id = v.vehicle_id)
  AND NOT EXISTS (SELECT 1 FROM dtc_events d WHERE d.vehicle_id = v.vehicle_id);
