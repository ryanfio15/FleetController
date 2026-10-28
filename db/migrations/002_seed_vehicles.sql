-- Stage 0 seed: the three simulated cars from the outline, plus headroom to 50.
--
-- Once the Fleet API's POST /api/v1/devices exists (P3) this is replaced by the
-- real provisioning flow and `cert_fp` is filled in at signing time. Until then
-- the readings FK needs rows to point at.

INSERT INTO vehicles (vehicle_id, owner_id, fleet_id, label)
SELECT
    format('sim-car-%s', lpad(n::text, 2, '0')),
    -- Two owners, so the §8.1 "owner sees 404 for another owner's car" test has
    -- something to assert against the moment the Fleet API lands.
    CASE WHEN n % 2 = 1
         THEN '11111111-1111-1111-1111-111111111111'::uuid
         ELSE '22222222-2222-2222-2222-222222222222'::uuid END,
    '33333333-3333-3333-3333-333333333333'::uuid,
    format('Simulated car %s', n)
FROM generate_series(1, 50) AS n
ON CONFLICT (vehicle_id) DO NOTHING;
