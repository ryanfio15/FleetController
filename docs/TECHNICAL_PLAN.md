# FleetController — Technical Plan

**Author:** Ryan Fioravanti · CSC478
**Source of truth:** `FleetController.pdf` (Chapters 1–2)
**Status:** Stage 0 complete and tagged `stage-0` (§14). Stage 2 in progress — Envoy
mTLS and the Kubernetes manifests are being built together, so the mTLS rehearsal
under Compose is skipped rather than done twice.

This document turns the outline into a buildable plan. The organizing principle is
**local first**: every service runs on one laptop under Docker Compose before any
of it touches Kubernetes, and the Kubernetes manifests are introduced on a local
`kind` cluster before RKE2/FABRIC. Nothing in the pipeline should be discovered to
be broken for the first time on FABRIC.

---

## 1. What the outline already decides

These are fixed. Everything in this plan is downstream of them.

| Concern | Decision from the PDF |
|---|---|
| Data path | Vehicle Sim → Envoy (mTLS) → ingest-api → NATS JetStream → stream-processor → TimescaleDB → Fleet API / Grafana → browser |
| Device identity | X.509 client cert; `vehicle_id` taken **only** from the cert CN |
| User identity | Keycloak OIDC; JWT with roles `owner` (driver), `admin`, `operator` |
| Buffer | NATS JetStream, durable, replayable |
| Storage | TimescaleDB hypertable + 1m/1h aggregates |
| Scaling target | stream-processor, KEDA 1→6 on JetStream consumer lag (500 msgs/replica) |
| Secondary scaling | ingest 2→4 on CPU (HPA); NATS and TimescaleDB scale vertically |
| Cluster | RKE2 on 5 FABRIC VMs — `cp-1`, `edge-1`, `worker-1`, `worker-2`, `state-1` |
| External entry | NodePort only (FABRIC has no cloud LB): `30443` ingest, `30080` ingress-nginx |
| Fleet size | 3 → 50 simulated vehicles by changing one number |

## 2. What the outline leaves open

These need a decision before code. My recommendation is given for each; they're
revisited in context later in the document.

| # | Gap | Recommendation |
|---|---|---|
| G1 | **Per-vehicle ordering vs. a shared consumer.** KEDA runs 1–6 processor replicas off one durable consumer. Two readings from the same car can land on different replicas, out of order. Any in-memory per-vehicle state (trip tracking, "has this been hot for 30s") is then wrong. | v1: make the processor **stateless** — it writes readings and evaluates single-sample alerts only. Derive trips and sustained-condition alerts in SQL on a schedule. See §6.3. |
| G2 | **Device provisioning.** `POST /api/v1/devices` "issues cert" — by what mechanism, and what does revoke actually enforce? | Fleet API signs a CSR against the project CA, returns the bundle **once**, records a fingerprint. Revoke = flip `revoked_at` in the DB; ingest-api checks the fingerprint against a cached denylist. Short-lived certs (24h) make CRL/OCSP unnecessary. See §7.2. |
| G3 | **Location / distance.** Trip logs usually imply a map. OBD-II has no GPS. | Stay OBD-only. Distance = integral of PID 0x0D speed over the 1 Hz samples. Honest to the hardware you eventually want to talk to, and removes a whole subsystem. |
| G4 | **"Live" delivery.** `GET /vehicles/{id}/live` is a poll. | Poll at 2 s for v1. SSE is a stretch goal; it adds sticky-session concerns behind NodePort. |
| G5 | **Alert delivery.** "Admin is alerted" — in-app only, or email/webhook? | In-app badge + `GET /api/v1/alerts` for v1. A webhook sink is a one-afternoon add later. |
| G6 | **Retention.** Not specified, and the `state-1` PVC is 20Gi. | Mandatory, not optional — see the capacity math in §6.4. Compress at 24h, drop raw at 7d, keep 1m for 90d and 1h for 1y. |
| G7 | **vehicle-sim is specified as a Deployment with "one cert each."** A Deployment's replicas are identical — they cannot each mount a different certificate, and they have no stable ordinal to derive an identity from. | Make vehicle-sim a **StatefulSet**, giving stable ordinals `sim-car-0..49`. The certificate half needs more than that, because a StatefulSet also has one pod template — see §10 for the cert-manager-csi-driver resolution. Surfaced while building Stage 0, where scaled Compose replicas hit exactly this problem (see §14). |

---

## 3. Delivery stages

Four stages. Each has a hard exit criterion. Do not start the next stage until the
current one's criterion is demonstrably met.

```
Stage 0          Stage 1              Stage 2                 Stage 3
Compose on   →   Compose, full    →   kind on laptop,     →   RKE2 on 5 FABRIC VMs
laptop,          security +           real manifests,         real node labels,
happy path       tests + 50 sims      KEDA/cert-mgr/HPA       NodePort + bastion
```

### Stage 0 — One `docker compose up`, end to end
**Exit:** a sim publishes a reading over mTLS and that value appears on a Grafana
panel in the browser within 5 seconds, with no manual steps after `make up`.

This is the single most important milestone in the project. It proves all seven
hops at once, and after it every later change is an incremental one.

Scope: 3 sims, local CA, Envoy + ingest-api, NATS JetStream, processor, TimescaleDB,
Grafana. Keycloak and the Fleet API can be stubbed (hardcoded dev JWT) until Stage 1.

### Stage 1 — Correct, secure, and tested at target load
**Exit:** `make test` is green; 50 sims run for 30 minutes with consumer lag
returning to zero; every row in the API contract test matrix (§8) passes.

Adds: Keycloak realm import, real JWT validation, RLS, the full API surface, the
device provisioning flow, store-and-forward in the sim, alert rules, trip rollup,
retention and compression policies, load test.

### Stage 2 — Kubernetes on the laptop
**Exit:** the identical images from Stage 1 run on `kind` from
`deploy/k8s/overlays/kind`, KEDA visibly scales the processor past 1 replica under
a 50-sim burst, and the exit criterion from Stage 0 still holds through NodePort.

This is where the manifests, NetworkPolicies, cert-manager, KEDA, HPA and
ingress-nginx get debugged — on a cluster you can delete and recreate in 60 seconds.
`kind` supports NodePort via `extraPortMappings`, and cert-manager, KEDA and
ingress-nginx all install into it unmodified. There is very little about this stage
that won't transfer.

### Stage 3 — FABRIC
**Exit:** same as Stage 2, through the bastion tunnel, surviving a node drain of
`worker-1` and a restart of `nats-0` without data loss.

What's genuinely new here and *cannot* be rehearsed locally: FABRIC VM provisioning,
the FABNetv4 network, node labels and `nodeSelector` pinning, local-path PVs on
`state-1`, sealed-secrets, the SSH bastion tunnel, and real inter-node latency. If
the overlay is doing its job, that's the entire diff.

---

## 4. Repository layout

```
FleetController/
├── docs/
│   ├── TECHNICAL_PLAN.md          # this file
│   └── adr/                       # one short file per irreversible decision
├── libs/fc_common/                # pydantic models + settings, shared by all services
├── services/
│   ├── ingest-api/                # FastAPI, binds 127.0.0.1 only
│   ├── stream-processor/          # JetStream pull consumer → Timescale
│   ├── fleet-api/                 # FastAPI, JWT + RLS
│   ├── vehicle-sim/               # OBD-II model + store-and-forward client
│   └── web-ui/                    # static, served by nginx
├── db/migrations/                 # numbered SQL, applied by a job in both envs
├── deploy/
│   ├── compose/docker-compose.yml
│   ├── envoy/envoy.yaml           # ONE file, used by Compose and the ConfigMap
│   ├── keycloak/realm.json        # exported realm, committed
│   ├── grafana/{dashboards,provisioning}/
│   └── k8s/
│       ├── base/                  # kustomize base
│       └── overlays/{kind,fabric}/
├── tools/certs/                   # local CA + cert issuance scripts
├── tests/{unit,integration,load}/
├── Makefile
└── .github/workflows/ci.yml
```

Two rules that make Stage 2 cheap:

1. **One Envoy config, one Keycloak realm, one set of migrations** — referenced by
   Compose as a bind mount and by Kubernetes as a ConfigMap. Never two copies.
2. **All configuration via environment variables**, parsed by one
   `pydantic-settings` class per service. No `if RUNNING_IN_K8S` branches anywhere
   in application code.

---

## 5. Data model

### 5.1 Telemetry message

The sim emits standard OBD-II Mode 01 PIDs at 1 Hz. Keeping to real PIDs now is what
makes the eventual switch to an actual ELM327/socketCAN adapter a swap of the
reader, not a rewrite.

| PID | Metric | Unit | Range | Used for |
|---|---|---|---|---|
| 0x04 | `engine_load` | % | 0–100 | context |
| 0x05 | `coolant_temp` | °C | −40–215 | **overheat alert** |
| 0x0C | `rpm` | rpm | 0–16383.75 | trip state, over-rev |
| 0x0D | `speed` | km/h | 0–255 | **trip distance** |
| 0x0F | `intake_temp` | °C | −40–215 | context |
| 0x11 | `throttle_pos` | % | 0–100 | context |
| 0x2F | `fuel_level` | % | 0–100 | context |
| 0x42 | `module_voltage` | V | 0–65.5 | **weak battery alert** |
| 0x01 | `mil_on`, `dtc_count` | bool, int | — | **trouble code alert** |
| Mode 03 | `dtc_codes` | P/C/B/U codes | — | **trouble code alert** |

Wire format — `POST /v1/telemetry`:

```json
{
  "batch_id": "018f...",            // ULID, used as Nats-Msg-Id for dedup
  "vehicle_id": "sim-car-0",       // MUST equal cert CN, else 403
  "readings": [
    { "ts": "2026-10-27T18:04:01.000Z", "metric": "coolant_temp", "value": 92.5 },
    { "ts": "2026-10-27T18:04:01.000Z", "metric": "rpm",          "value": 1840 }
  ],
  "dtcs": ["P0301"]                 // optional, only when the set changes
}
```

Batch of 10 metrics × 10 s = 100 readings, comfortably under the 500 cap. The cap
matters during store-and-forward: a 5-minute outage queues ~3,000 readings, which
the sim must split into ≥6 batches.

### 5.2 TimescaleDB schema

Narrow (`metric`, `value`) rather than one column per PID. Vehicles support
different PID sets, and narrow rows compress far better with
`segmentby = vehicle_id, metric`.

```sql
CREATE TABLE vehicles (
  vehicle_id   TEXT PRIMARY KEY,          -- == cert CN
  owner_id     UUID NOT NULL,             -- Keycloak sub
  fleet_id     UUID NOT NULL,
  label        TEXT,
  cert_fp      TEXT,                      -- SHA-256, set at provisioning
  revoked_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE readings (
  ts         TIMESTAMPTZ NOT NULL,
  vehicle_id TEXT NOT NULL REFERENCES vehicles,
  metric     TEXT NOT NULL,
  value      DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('readings', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Idempotent writes: the processor may redeliver after a crash.
CREATE UNIQUE INDEX readings_pk ON readings (vehicle_id, metric, ts);
-- then: INSERT ... ON CONFLICT (vehicle_id, metric, ts) DO NOTHING

CREATE TABLE trips (
  trip_id     BIGSERIAL PRIMARY KEY,
  vehicle_id  TEXT NOT NULL REFERENCES vehicles,
  started_at  TIMESTAMPTZ NOT NULL,
  ended_at    TIMESTAMPTZ,
  distance_km DOUBLE PRECISION,
  max_speed   DOUBLE PRECISION,
  max_coolant DOUBLE PRECISION,
  UNIQUE (vehicle_id, started_at)
);

CREATE TABLE alerts (
  alert_id    BIGSERIAL PRIMARY KEY,
  vehicle_id  TEXT NOT NULL REFERENCES vehicles,
  kind        TEXT NOT NULL,             -- overheat | weak_battery | dtc | overrev
  severity    TEXT NOT NULL,             -- warn | critical
  opened_at   TIMESTAMPTZ NOT NULL,
  closed_at   TIMESTAMPTZ,
  acked_at    TIMESTAMPTZ,
  acked_by    UUID,
  detail      JSONB
);
-- One open alert per (vehicle, kind) — prevents a 1 Hz alert storm.
CREATE UNIQUE INDEX alerts_one_open
  ON alerts (vehicle_id, kind) WHERE closed_at IS NULL;
```

Continuous aggregates give the API's `res=1m|1h` directly:

```sql
CREATE MATERIALIZED VIEW readings_1m WITH (timescaledb.continuous) AS
  SELECT time_bucket('1 minute', ts) AS bucket, vehicle_id, metric,
         avg(value) AS avg, min(value) AS min, max(value) AS max, count(*) AS n
  FROM readings GROUP BY 1,2,3;
SELECT add_continuous_aggregate_policy('readings_1m',
  start_offset => INTERVAL '3 hours', end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute');
-- readings_1h rolls up from readings_1m the same way.

ALTER TABLE readings SET (timescaledb.compress,
  timescaledb.compress_segmentby = 'vehicle_id, metric',
  timescaledb.compress_orderby   = 'ts DESC');
SELECT add_compression_policy('readings', INTERVAL '24 hours');
SELECT add_retention_policy('readings',  INTERVAL '7 days');
```

> **Verify against your pinned TimescaleDB version:** RLS support on continuous
> aggregates is limited. Plan on RLS protecting `readings` and the normal tables,
> and the Fleet API enforcing visibility on the aggregate path with an explicit
> `vehicle_id = ANY(:visible_ids)` predicate. Confirm the behavior in Stage 1
> rather than assuming it.

### 5.3 Row-level security

RLS is defense in depth under the API's own authorization check, so a bug in one
route can't leak another fleet's data.

```sql
ALTER TABLE readings ENABLE ROW LEVEL SECURITY;
CREATE POLICY readings_visible ON readings USING (
  current_setting('app.role', true) IN ('admin','operator')
  OR vehicle_id IN (SELECT vehicle_id FROM vehicles
                    WHERE owner_id = current_setting('app.user_id', true)::uuid)
);
```

The Fleet API opens a transaction per request and issues
`SET LOCAL app.user_id = ...` / `SET LOCAL app.role = ...` from validated JWT claims.
`SET LOCAL` is essential — it is scoped to the transaction and therefore safe with a
connection pool. The API's DB role must **not** be the table owner, or it bypasses
RLS silently.

---

## 6. Service design

### 6.1 Ingest (Envoy + ingest-api, one pod)

Envoy terminates TLS with `require_client_certificate: true` against the project CA,
and forwards the validated identity:

```yaml
forward_client_cert_details: SANITIZE_SET
set_current_client_cert_details: { subject: true, dns: true, uri: true }
```

ingest-api parses the CN out of `x-forwarded-client-cert`. The outline's
"API is only reachable via Envoy" is enforced structurally, not by convention:
**ingest-api binds `127.0.0.1` only**. Same pod, same network namespace, so Envoy
reaches it and nothing else in the cluster can — no NetworkPolicy required for that
specific guarantee, and it behaves identically under Compose.

Request handling, in order:

1. `x-forwarded-client-cert` present and CN parses → else `403`
2. cert fingerprint not in the revoked denylist (cached 60 s from `vehicles`) → else `403`
3. body `vehicle_id` == CN → else `403` *(never trust the body's ID)*
4. `len(readings) <= 500` → else `413`
5. schema + range validation on every reading → else `400` for the **whole batch**
6. publish to `TELEMETRY.<vehicle_id>` with `Nats-Msg-Id: <batch_id>` → `202`
7. publish fails or JetStream reports limits → `503` + `Retry-After: 5`

Note the trade-off baked into step 5: one bad reading rejects 99 good ones. That is
what the outline specifies and it's the right call for a device fleet you control
(fail loudly rather than silently dropping fields). Its consequence lands on the sim
— see §6.5.

Subject-per-vehicle (`TELEMETRY.sim-car-0`) costs nothing now and is what makes the
G1 fix in §6.3 possible later without a schema change.

### 6.2 NATS JetStream

```
Stream   TELEMETRY   subjects TELEMETRY.*   file storage
         max_age 24h   max_bytes 4GB   duplicate_window 2m   discard old
Consumer processor   durable, PULL, ack_explicit
         max_ack_pending 2000   ack_wait 30s   max_deliver 5
         → DLQ subject TELEMETRY_DLQ after max_deliver
```

A **pull** consumer shared by all replicas is what makes "replicas share one durable
consumer, so each message is processed once" true. `duplicate_window` plus the
`Nats-Msg-Id` header absorbs sim retries after a 503; the unique index on `readings`
absorbs JetStream redeliveries after a processor crash. Together those give
effectively-once end to end without distributed transactions.

Size the PVC against `max_bytes`: a 5Gi PVC and a 4GB stream limit is deliberate
headroom, not a coincidence.

### 6.3 Stream processor — and the ordering trap

**This is the one place where the outline's architecture, implemented naively, is
wrong.** KEDA scales this deployment 1→6. All replicas pull from one consumer. There
is therefore *no per-vehicle ordering guarantee* and no replica affinity. A trip
state machine or a "coolant has been >110°C for 30 seconds" window held in process
memory will produce wrong trips and missed alerts the moment KEDA scales to 2 — and
it will look fine in every single-replica test you run.

v1 design, which sidesteps it entirely:

- **Processor is stateless.** It validates, writes readings with
  `ON CONFLICT DO NOTHING`, and evaluates only *single-sample* rules
  (`coolant_temp > 120` → critical; new DTC in the batch; `mil_on` true).
- **Sustained-condition alerts and trips are SQL**, run by a `pg_cron` job or a
  small ticker in the processor guarded by a Postgres advisory lock so exactly one
  replica runs it:

```sql
-- sustained overheat: >110 °C for 30 s, opened once thanks to alerts_one_open
INSERT INTO alerts (vehicle_id, kind, severity, opened_at, detail)
SELECT vehicle_id, 'overheat', 'warn', min(ts),
       jsonb_build_object('peak_c', max(value))
FROM readings
WHERE metric = 'coolant_temp' AND value > 110 AND ts > now() - INTERVAL '2 minutes'
GROUP BY vehicle_id
HAVING max(ts) - min(ts) >= INTERVAL '30 seconds'
ON CONFLICT DO NOTHING;
```

Trip detection, same pattern: group a vehicle's samples into sessions separated by
≥180 s of `rpm = 0`, integrate `speed` over the 1 Hz samples for `distance_km`.
Runs every 60 s, is idempotent via `UNIQUE (vehicle_id, started_at)`, and is
completely indifferent to how many replicas exist.

If you later want true streaming trips, the upgrade path is already in place:
partition the pull consumer by subject (`TELEMETRY.sim-car-0`) so a vehicle's
messages always reach one replica. Record that as an ADR; don't build it for v1.

Alert thresholds, grounded in real values:

| Alert | Rule |
|---|---|
| `overheat` warn | `coolant_temp > 110 °C` sustained 30 s (normal operating: 90–105) |
| `overheat` critical | `coolant_temp > 120 °C`, single sample |
| `weak_battery` warn | `module_voltage < 12.2 V` with `rpm = 0` (resting; healthy ≈ 12.6) |
| `weak_battery` critical | `module_voltage < 13.0 V` with `rpm > 500` (charging system fault; healthy 13.5–14.7) |
| `dtc` | any new code in Mode 03, or `mil_on` transitions false→true |
| `overrev` warn | `rpm > 6000` sustained 5 s |

Alerts auto-close when the condition has been absent for 5 minutes. `acked_at` is
set by the API and does not close the alert.

### 6.4 TimescaleDB capacity — why retention is mandatory

At the outline's target of 50 cars:

```
50 cars × 10 metrics × 1 Hz          = 500 rows/s
500 × 86,400                         = 43.2 M rows/day
× ~70 bytes/row with index overhead  ≈ 3–4 GB/day uncompressed
```

The `state-1` PVC is **20 Gi**. Uncompressed and unpruned, that is roughly **five
days** before the database wedges — most likely during a demo, since that's when
you'll have run the fleet longest. With the policies from §5.2 the steady state is
about 1 day raw (~4 GB) plus 6 days compressed at 10–20× (~1.5 GB) plus aggregates,
comfortably inside 20 Gi.

Two follow-ons: set up `pg-backup` CronJob → PVC (already in the `state-1` diagram)
from Stage 1, and drop `chunk_time_interval` from 1 day to 1–4 hours when you scale
past ~10 cars, so chunk+index working set stays small relative to RAM.

### 6.5 Vehicle sim

The sim is the load generator, the demo, and the stand-in for hardware. Build it
with more care than its name suggests.

- **Physical model**, not random walk: an idle/accelerate/cruise/decelerate/stop
  state machine driving speed, with RPM derived from speed and a gear ratio,
  coolant warming from ambient toward ~95 °C on a first-order curve, and voltage
  ~12.6 V at rest / ~14.0 V running. Grafana panels should look like a car.
- **Injectable faults** via env var or a local HTTP endpoint:
  `FAULT=overheat|weak_battery|dtc_p0301|flatline`. This is how every alert rule
  gets tested, and how you demo the product's actual value proposition.
- **Store-and-forward.** On `503`/`429`/5xx/connection error: buffer to a bounded
  on-disk ring (cap ~30 min, drop oldest), honor `Retry-After`, retry with
  exponential backoff + jitter, and re-send in ≤500-reading batches. This is what
  makes "durable buffer; replay after failures" demonstrable — kill NATS for two
  minutes, bring it back, watch the gap fill in.
- **On `400`, drop the batch and increment a counter.** 400 is not retryable;
  retrying a malformed batch forever is a poison-pill livelock.
- **One cert per sim**, read from a mounted path. Identical code under Compose
  (bind mount) and Kubernetes (Secret volume).
- **Fleet size is one number**: `replicas` in Kubernetes, `--scale vehicle-sim=N`
  under Compose. Each instance derives its identity from its ordinal.

### 6.6 Fleet API

FastAPI. Validates RS256 JWTs against Keycloak's JWKS
(`/realms/fleet/protocol/openid-connect/certs`), cached with a refresh on unknown
`kid`. Checks `iss`, `aud`/`azp`, `exp`. Maps `realm_access.roles` to
`owner|admin|operator`.

Per request: open transaction → `SET LOCAL app.user_id/app.role` → query → commit.

`404`, not `403`, for a vehicle the caller can't see — the outline says so, and it's
right: `403` confirms the resource exists.

### 6.7 Web UI

Static (nginx) + vanilla or a small framework; it is not the interesting part of this
project. Three views: fleet grid with status chips, vehicle detail with live gauges +
trip list, alerts inbox with ack. Grafana is embedded via iframe behind `/grafana`
for the deep time-series views rather than reimplemented — that's why Grafana is in
the architecture.

---

## 7. Security

The outline's security story is the strongest part of the design. Implement it in
this order, because each step is independently demonstrable.

### 7.1 Trust boundaries

| Boundary | Control |
|---|---|
| Device → cluster | mTLS, client cert required, CN is the only identity source |
| Envoy → ingest-api | same pod, API bound to loopback |
| ingest-api → NATS | NetworkPolicy; NATS accepts only from ingest + processor |
| User → cluster | ingress-nginx via SSH bastion tunnel, OIDC at the app |
| Fleet API → DB | non-owner role, RLS enforced |
| Everything else | default-deny NetworkPolicy per namespace |

Write the NetworkPolicies in Stage 2 on `kind`, where a mistake costs a `kubectl
delete` rather than a FABRIC debugging session. Default-deny first, then add the
specific `podSelector` → `podSelector` allowances one at a time and confirm each
break/fix.

### 7.2 Device provisioning (resolves G2)

```
operator → POST /api/v1/devices { vehicle_id, owner_id }
  fleet-api: insert vehicles row
             sign CSR against project CA (local: step-ca/openssl; cluster: cert-manager CA Issuer)
             store SHA-256 fingerprint
         ←  { cert_pem, key_pem, ca_pem }   returned exactly once, never stored in plaintext

operator → DELETE /api/v1/devices/{id}/cert
  fleet-api: set revoked_at
  ingest-api: denylist cache refreshes within 60 s → subsequent requests 403
```

Issue **24-hour certs**. Short lifetimes make CRL/OCSP distribution — which is
genuinely painful to wire through Envoy — unnecessary, and they force you to build
renewal, which is what a real fleet needs anyway. cert-manager renews automatically
in-cluster; a small loop in the sim re-reads its cert file on change.

### 7.3 Secrets

Local: `.env` (gitignored) + certs generated by `make certs` into a gitignored dir,
with an `.env.example` committed. Cluster: sealed-secrets (already in the `cp-1`
diagram), so the encrypted manifests are safe to commit.

Never commit a key, including a "throwaway local" one. Add `gitleaks` or
`detect-secrets` to CI in Stage 1.

---

## 8. Testing

### 8.1 API contract matrix

Every cell in the PDF's contract table becomes a named test. These are the
acceptance criteria for `POST /v1/telemetry`:

| Case | Expect |
|---|---|
| Valid batch, CN matches | `202` |
| No client cert | TLS handshake failure (Envoy, before the app) |
| Cert signed by a foreign CA | TLS handshake failure |
| Revoked cert | `403` |
| `vehicle_id` ≠ CN | `403` |
| 501 readings | `413` |
| One reading with `coolant_temp = 9999` | `400`, **zero** rows written |
| Duplicate `batch_id` within 2 min | `202`, no duplicate rows |
| NATS down | `503` with `Retry-After` |
| Malformed JSON | `400` |

And for the user API: a `GET /api/v1/vehicles/{other_owners_car}` as `owner`
returns `404`; the same call as `admin` returns `200`. That single pair is the
regression test for the entire authorization model — make it the first integration
test you write.

### 8.2 Layers

- **Unit** — OBD decoding, alert threshold logic, trip segmentation over fixture
  series, JWT claim mapping. Fast, no containers.
- **Integration** — testcontainers brings up TimescaleDB + NATS; exercise
  ingest → stream → DB for real. Covers the contract matrix above.
- **Load** — the sim *is* the load test. `--scale vehicle-sim=50`, run 30 min,
  assert: ingest p99 < 100 ms, JetStream consumer lag returns to 0 within 60 s of a
  burst, no DLQ messages, end-to-end latency (sim timestamp → row visible via API)
  p95 < 5 s.
- **Chaos**, from Stage 1 — scripted, each asserting recovery:
  `kill nats` (sim buffers, backfills), `kill processor` (lag grows, then drains,
  no duplicate rows), `kill timescaledb` (processor nacks, JetStream retains,
  backfills on return).

CI (GitHub Actions) runs unit + integration on every push. Keep it under ten
minutes or you'll stop waiting for it.

---

## 9. Observability

Build this in Stage 0, not at the end. Debugging a seven-hop async pipeline without
it is the single largest avoidable time sink in this project.

- **Metrics** (Prometheus, `/metrics` on every service):
  `ingest_requests_total{status}`, `ingest_batch_bytes`,
  `jetstream_consumer_pending` (the KEDA signal — graph it),
  `processor_batch_duration_seconds`, `processor_rows_written_total`,
  `db_write_duration_seconds`, `alerts_opened_total{kind}`,
  plus postgres-exporter as the sidecar the outline already specifies.
- **Tracing** (OpenTelemetry): propagate `traceparent` as a NATS message header so
  one trace spans ingest → JetStream → processor → DB. This is how you answer
  "where did those 4 seconds go."
- **Logs**: structured JSON, `vehicle_id` and `batch_id` on every line.
- **Grafana dashboards as code** in `deploy/grafana/dashboards/`, loaded by file
  provisioning. Hand-clicked dashboards do not survive a pod restart.

Four dashboards: Fleet Overview, Vehicle Detail, Pipeline Health (lag, throughput,
error rate, replica count), Infrastructure.

**SLOs** to assert in the load test: ingest p99 < 100 ms · end-to-end p95 < 5 s ·
consumer lag < 500 sustained · zero data loss across any single-pod restart.

---

## 10. Kubernetes specifics (Stages 2–3)

### Kustomize structure

`base/` holds everything. Overlays patch only what genuinely differs:

| | `overlays/kind` | `overlays/fabric` |
|---|---|---|
| nodeSelector | stripped out | `tier=edge/state/pipeline` |
| StorageClass | `standard` | `local-path` |
| Secrets | plain, generated | sealed-secrets |
| Sim replicas | 3 | 3 → 50 |
| Resource requests | small | sized |

If an overlay ever needs to patch application *behavior*, that's a bug in the base —
push the difference into an env var instead.

### KEDA

```yaml
triggers:
  - type: nats-jetstream
    metadata:
      natsServerMonitoringEndpoint: nats.fleet.svc:8222
      account: "$G"
      stream: TELEMETRY
      consumer: processor
      lagThreshold: "500"
      activationLagThreshold: "10"
# minReplicaCount: 1, maxReplicaCount: 6
```

Two traps: KEDA creates and owns an HPA for the processor, so **never** also define
your own HPA on that deployment — they will fight and flap. And set
`cooldownPeriod` (~300 s) high enough that scale-down doesn't thrash while a
backlog drains. Ingest's CPU HPA is a separate object on a separate deployment and
is fine.

### Ingest NodePort and TLS passthrough

`30443` must reach Envoy as raw TCP — nothing in front may terminate TLS, or the
client cert never arrives and the entire identity model collapses. That means ingest
does **not** go through ingress-nginx; it's a `type: NodePort` Service straight to
the Envoy container port. Under `kind`, replicate it with `extraPortMappings` so the
sims connect exactly as they will on FABRIC.

### vehicle-sim is a StatefulSet, not a Deployment

The outline lists vehicle-sim as a Deployment scaling 3→50 with "one cert each."
Those two requirements are incompatible: Deployment replicas are interchangeable,
share one pod template, and have no stable ordinal, so there is no way to give pod
*n* certificate *n*. Use a StatefulSet — the ordinal is the vehicle identity.

**A StatefulSet alone does not solve the certificate half of it**, which is worth
stating because the obvious fix doesn't work: a StatefulSet still has exactly one
pod template, so "mount Secret *n* into pod *n*" is not expressible either. The
resolution is **cert-manager-csi-driver** — an ephemeral CSI volume that issues a
certificate at mount time with the CN templated from pod metadata:

```yaml
csi.cert-manager.io/common-name: ${POD_NAME}
csi.cert-manager.io/duration: 24h
```

For that to work the pod name must *be* the vehicle_id, which is why the IDs are
unpadded `sim-car-0`..`sim-car-49` rather than `sim-car-00`. One invariant replaces
all the translation:

```
pod name == hostname == certificate CN == vehicle_id
```

Identity then arrives as a plain `fieldRef: metadata.name`, no pod can read another
vehicle's key, and renewal is the issuer's job rather than a script's. Migration
`003_vehicle_ids_ordinal.sql` re-seeds the fleet accordingly; `002` is left intact
because it is already recorded in `schema_migrations` everywhere.

Everything else about the workload is unchanged.

### Stateful placement

`nats-0`, `timescaledb-0`, `keycloak-db-0` pin to `state-1` via `nodeSelector:
tier=state` with local-path PVs. Consequence worth stating in the write-up: **the
data tier is a single point of failure by design**, accepted because the project's
redundancy story lives in JetStream's replay, not in database replication. A node
loss on `state-1` means restore from the `pg-backup` CronJob. Say this explicitly
rather than letting a reviewer find it.

### PodDisruptionBudgets

`minAvailable: 1` on ingest and fleet-api, so a `kubectl drain` during the Stage 3
exit test doesn't black out ingestion.

---

## 11. Phase plan

Relative weeks — map them onto your actual CSC478 deadlines. Effort is the
hard part of each phase, not the headcount.

| Phase | Work | Exit criterion |
|---|---|---|
| **P1** (wk 1–2) | Repo skeleton, `fc_common` models, local CA script, Compose with NATS + Timescale + migrations. Sim → ingest → NATS → processor → DB, no auth. Grafana reading the DB. | **Stage 0 exit**: reading visible on a panel in <5 s from `make up` |
| **P2** (wk 3–4) | Envoy mTLS, CN extraction, loopback binding, full contract matrix incl. 403/413/400/503. Metrics + structured logs. Integration tests in CI. | Contract matrix green in CI |
| **P3** (wk 5–6) | Keycloak realm import, Fleet API with JWT + RLS, device provisioning + revoke, `/vehicles`, `/trips`, `/telemetry?res=`, `/alerts`. | `owner` → `404` on another owner's car; `admin` → `200` |
| **P4** (wk 7) | Alert rules, SQL trip rollup, continuous aggregates, compression + retention, fault injection in the sim, web UI. | Every alert kind fires on an injected fault and acks |
| **P5** (wk 8) | 50 sims under Compose, store-and-forward, chaos scripts, SLO assertions, tracing. | **Stage 1 exit**: 30 min at 50 cars, lag drains, no loss |
| **P6** (wk 9–10) | Kustomize base + `kind` overlay, cert-manager, KEDA, HPA, ingress-nginx, NetworkPolicies, PDBs. | **Stage 2 exit**: KEDA scales processor >1 under burst on `kind` |
| **P7** (wk 11–12) | FABRIC slice, 5 RKE2 VMs, node labels, local-path PVs, sealed-secrets, bastion tunnel, `fabric` overlay. | **Stage 3 exit**: survives `drain worker-1` and `nats-0` restart |
| **P8** (wk 13–14) | Scale 3→50 on cluster, capture scaling evidence, backup/restore drill, write-up and demo. | Demo runs start to finish twice without intervention |

**Critical path:** P1 → P2 → P6 → P7. P3/P4 (user-facing features) can slip a week
without endangering the cluster work; P6/P7 cannot — they're the part that depends
on FABRIC availability, which you don't control.

**Buy insurance early.** Provision the FABRIC slice and get a bare RKE2 cluster
answering `kubectl get nodes` during P2, weeks before you need it. If FABRIC has a
queue, an outage, or a quota surprise, you want to know in week 3, not week 11.

---

## 12. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| **Per-vehicle ordering under KEDA scale-out** (G1) | Silently wrong trips and missed alerts; invisible at 1 replica | Stateless processor + SQL rollups (§6.3). Add a test that runs the integration suite at 3 replicas. |
| **20Gi fills in ~5 days at 50 cars** (§6.4) | DB wedges mid-demo | Compression + retention from P4, not P8. Alert on PVC >70%. |
| FABRIC unavailable or slow to provision | Blocks P7–P8, the graded cluster work | Stage 2 on `kind` means ~90% of the manifest work is already done and proven. Provision the slice in P2. |
| mTLS through NodePort misconfigured | Identity model collapses; silently accepts anonymous clients | Explicit negative tests: no cert, wrong CA, revoked (§8.1). Assert the handshake *fails*. |
| Keycloak time sink | Eats a week; it's incidental to the project's point | Commit a realm export early, import it everywhere, never configure through the UI twice. |
| KEDA + manual HPA conflict | Replica flapping, unexplainable | One autoscaler per deployment. Documented in §10. |
| Scope creep (GPS, mobile app, real OBD dongle) | Misses the cluster milestones | They're explicitly out of scope — see §13. |
| Single-node data tier | Node loss = data loss | Accepted by design; `pg-backup` CronJob from P4 + a rehearsed restore in P8. |

---

## 13. Explicitly out of scope for v1

Named so they can be declined quickly: real OBD-II hardware (ELM327/socketCAN),
GPS/mapping, mobile apps, multi-tenant billing, database HA/replication, geofencing,
predictive maintenance ML, bidirectional control or ECU flashing.

The last one is worth a sentence in the write-up: the architecture here is
**read-only by design**. A path from a web UI to a vehicle's CAN bus is a safety
and liability surface, not a feature. The ECU-flashing interest from Chapter 1 is
better served by a separate bench project against hardware you own.

---

## 14. Stage 0 result

Stage 0 is built and its exit criterion is met. What the build changed about the
plan, all of it discovered by running the thing rather than by reasoning about it:

| Finding | Consequence |
|---|---|
| Scaled replicas have no stable identity | vehicle-sim must be a StatefulSet, not a Deployment (G7, §10). Locally this is emulated with an atomic claim on a shared volume. |
| A fixed host port blocks scaling the processor | The KEDA target has to be scalable locally too; it publishes an ephemeral port instead. The equivalent trap in Kubernetes is a `hostPort`. |
| Named volumes mount root-owned | Containers running as a non-root UID need the directory pre-created in the image. The same applies to PVCs in Stage 2 — set `fsGroup` in the pod security context. |
| A realistic thermostat curve is too slow to demo | Fault injection needs its own, faster dynamics or the overheat demo takes four minutes and depends on the drive cycle. |
| **Two independent settings each put a floor under end-to-end latency** | The 5 s SLO was blown twice over by defaults that looked harmless. See below — this is the most transferable lesson of Stage 0. |

### The latency floors

The §9 SLO is 5 s from sample to visible. Two unrelated knobs each quietly
violated it, and neither shows up as an error:

1. **The sim's batch interval** is a floor by definition — the oldest reading in
   a batch is already that old when it is sent. At the original 10 s, worst-case
   latency was 11 s. Now 2 s.
2. **The processor's pull-fetch timeout** is a floor at low throughput. A pull
   consumer returns when it has `fetch_batch` messages *or* the timeout expires.
   At 3 cars the batch of 100 never fills, so every fetch waited the full 5 s
   timeout. Now 1 s, with the batch size left at 100 so throughput at 50 cars is
   unaffected — the batch fills in well under a second there.

Measured end-to-end after both fixes: **0.7–2.2 s**, and throughput rose from
~1300 to ~1750 readings/min because the pipeline had been stalling on the fetch.

The general lesson for Stage 2: latency in this pipeline is the *sum* of several
independently-configured waits, none of which is an error condition. When the
end-to-end SLO is missed, enumerate the waits rather than profiling the code.

Verified against the running stack: the full contract matrix (202/400/403/413/503),
whole-batch rejection writing zero rows, `Nats-Msg-Id` deduplication, **zero
duplicate rows with three processor replicas on one durable consumer** (the G1
claim), and a 60-second NATS outage that produced 503s with `Retry-After`,
sim-side buffering, and a complete backfill on recovery.

## 15. Stage 2 result (kind)

The manifests run on `kind` and the Stage 0 exit criterion still holds through
mTLS and NodePort: **1740 readings/min, 2.2 s behind**. What the build changed
about the plan, again all of it found by running the thing:

| Finding | Consequence |
|---|---|
| A StatefulSet has one pod template too, so "mount Secret *n* into pod *n*" is no more expressible than it was for a Deployment | §G7 was only half a fix. cert-manager-csi-driver templates each pod's CN from `${POD_NAME}`, which is what forced vehicle IDs to equal pod names exactly (§10) |
| Kubernetes injects `INGEST_PORT=tcp://10.96.x.x:8443` for the Service named `ingest` | It collides with this project's own `INGEST_`-prefixed config and uvicorn exits. `enableServiceLinks: false` on every pod. A project whose config convention is "one env prefix per service, named after the service" is maximally exposed to this |
| httpx 0.28's `cert=` and `verify=<str>` do not compose — the client certificate is silently dropped | Build the `SSLContext` explicitly. The failure mode is a sim that logs `mtls: true`, has a valid cert mounted, and fails the handshake with an **empty** error string |
| `kubectl apply -k` cannot read the shared files | Kustomize refuses paths above its root and `apply -k` has no `--load-restrictor`. Render and pipe to `apply -f -`, or keep a second copy of every shared file — which §4 rule 1 exists to prevent |
| NetworkPolicy filters only *new* connections | Deleting an allowance left the processor's psycopg pool writing happily. A break/fix test without a pod restart proves nothing |
| Steady-state consumer lag at 50 cars is ~40, not 500 | `lagThreshold: 500` never fires at steady state, so the 1→6 claim is only demonstrable against a real backlog. The threshold is right; the demo was wrong |

Verified against the running cluster: the full contract matrix (202/400/403/413)
with the no-certificate case failing at the **handshake** rather than returning a
status; whole-batch rejection writing zero rows; 50 pods holding 50 distinct
certificate CNs each equal to its own pod name; **KEDA scaling 1 → 3 → 4 → 5 → 6**
and draining ~600k backlogged messages to zero lag in about 60 s; **zero duplicate
rows across 246,982 rows from 50 vehicles** after that six-replica drain, which is
the G1 claim at the full replica count rather than Stage 0's three; NetworkPolicy
genuinely enforced by kindnetd; and Grafana answering through ingress-nginx on
30080.

Still outstanding for Stage 3: a FABRIC slice to run it on.
`deploy/fabric/fleetctl.py` provisions one and deploys onto it in a single
command — slice, FABNetv4 addressing, RKE2 server plus four labelled agents,
cert-manager/csi-driver/KEDA, and the overlay. Two things about it are design
decisions rather than conveniences:

- **It discovers `EDGE1_ADDR` and `NODE_CIDR` from the live slice** rather than
  having them typed into `site.yaml`. Typing edge-1's address by hand is how you
  get a cluster where every pod is Running, every probe is green, and every TLS
  handshake fails, because the address the sims dial is not in the server
  certificate's SANs. `./fc.sh k8s-apply` therefore *refuses* the fabric overlay.
- **It is a script, not a notebook**, because slice leases expire and this will
  be run more than once. `up` is idempotent: it reuses a slice, skips RKE2 where
  it is installed, and re-applies the manifests.

It has not been run against the real testbed, so the `check` subcommand talks to
FABRIC read-only and creates nothing — a wrong fablib call surfaces there for
free rather than halfway through a slice submission.

## 16. Next: P2 under Compose

Stage 2 took mTLS directly to Kubernetes, so the Compose stack is now the
environment *behind* on security — it still runs plain HTTP with
`INGEST_ALLOW_INSECURE_IDENTITY=true`. `deploy/envoy/envoy.yaml` is written and
validated, and the sim already presents a certificate whenever one is configured,
so catching Compose up is wiring, not design:

1. `./fc.sh certs 3` — already produces unpadded CNs matching the cluster's.
2. Add the Envoy sidecar to the ingest service in Compose against the existing
   `deploy/envoy/envoy.yaml`. The TLS paths in it are cert-manager's names, so the
   bind mounts map `make-ca.sh`'s output onto `tls.crt` / `tls.key` / `ca.crt`
   rather than growing a second set of paths.
3. Flip `INGEST_ALLOW_INSECURE_IDENTITY=false` and `INGEST_HOST=127.0.0.1` in the
   Compose env — a configuration change, not a code change, which the cluster has
   now demonstrated.
4. Mount per-vehicle certs into the sims and set `SIM_TLS_*`, replacing the
   shared-volume ordinal claim. The sim code needs nothing new.
5. Port the §8.1 negative tests down from the cluster, where they now pass, and
   add the one FABRIC cannot easily do: a **foreign CA**. Assert the handshake
   fails rather than merely returning a non-200.
6. Wire the revocation denylist: `revoked_at` in `vehicles`, cached 60 s in ingest.
   This is the last unimplemented piece of §7.2, and it is independent of
   environment.
