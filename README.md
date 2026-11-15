# FleetController

Live monitoring for a small fleet of vehicles: streaming OBD-II telemetry, trip
logs, and early warnings for overheating, trouble codes and weak batteries.

Ryan Fioravanti · CSC478 · [Technical plan](docs/TECHNICAL_PLAN.md)

**Status: Stage 0 complete, Stage 2 in progress.** The whole pipeline runs on one
laptop under Docker Compose, and the Kubernetes manifests now exist for all of it
— see [`deploy/k8s/`](deploy/k8s/README.md). They get debugged on `kind` before
FABRIC, deliberately: see [§3 of the plan](docs/TECHNICAL_PLAN.md) for why.

```
vehicle-sim ──HTTP──> ingest-api ──> NATS JetStream ──> stream-processor ──> TimescaleDB ──> Grafana
   (×3→50)            202/400/403/        durable           stateless,          hypertable
                      413/503             buffer            SQL rollups         + alerts
```

In Stage 1–2 the plain HTTP hop becomes mTLS through an Envoy sidecar, Keycloak and
the Fleet API land in front of the database, and the whole thing moves to `kind`
and then RKE2 on FABRIC.

## Quick start

Requires Docker Desktop. `make` is optional — `./fc.sh` is the real entrypoint.

```bash
./fc.sh up        # build and start everything
./fc.sh verify    # check the Stage 0 exit criterion
```

Then open **http://localhost:3000/d/fc-fleet-overview** (anonymous access is on,
so there is nothing to log into).

`./fc.sh verify` passes when readings are arriving and the pipeline is less than
5 seconds behind:

```
+----------------------+----------+----------------+
| readings_last_minute | vehicles | seconds_behind |
+----------------------+----------+----------------+
|                 1760 |        3 |            0.7 |
+----------------------+----------+----------------+
```

> **If you raise `SIM_BATCH_SECONDS` or `PROCESSOR_FETCH_TIMEOUT_SECONDS`, watch
> `seconds_behind`.** Each is an independent floor under end-to-end latency, and
> neither reports an error when it blows the 5 s target — see
> [plan §14](docs/TECHNICAL_PLAN.md).

## Commands

| | |
|---|---|
| `./fc.sh up` / `down` / `nuke` | start · stop · stop and wipe volumes |
| `./fc.sh verify` | check the Stage 0 exit criterion |
| `./fc.sh logs [service]` | tail logs |
| `./fc.sh scale 50` | grow the fleet — the "one number" from the outline |
| `./fc.sh scale-processor 3` | run N processor replicas (rehearses KEDA) |
| `./fc.sh fault overheat` | inject a fault: `overheat`, `weak_battery`, `dtc`, `flatline`, `none` |
| `./fc.sh psql` | shell on the database |
| `./fc.sh certs [N]` | generate the local CA and N vehicle certs (for P2) |
| `./fc.sh test` | run the unit tests |

### Seeing an alert

```bash
./fc.sh fault overheat     # coolant climbs past 110 °C
# ~2 minutes later:
./fc.sh psql -c "SELECT vehicle_id, kind, severity, detail FROM alerts WHERE closed_at IS NULL;"
./fc.sh fault none         # back to healthy
```

## What Stage 0 demonstrates

Each of these was run against the stack, not just designed:

| Claim | How it was checked |
|---|---|
| End-to-end in under 5 s | `./fc.sh verify` — ~1750 readings/min, 0.7–2.2 s behind |
| Full API contract | 202 · 400 · 403 · 413 · 503 all returned correctly |
| A bad reading rejects the whole batch | 400 returned, **zero** rows written |
| Duplicate batches don't double-write | same `batch_id` twice → 202, one row |
| Scaling the processor is safe | 3 replicas on one durable consumer → **0 duplicate rows** |
| Buffer survives an outage | NATS killed 60 s: ingest 503s with `Retry-After`, sims buffer, 2,701 readings backfill on recovery |
| Trips are derived correctly | gap-and-island SQL rollup, idempotent across re-runs |
| Alerts fire on real thresholds | injected overheat → warn at 117 °C sustained 30 s |

## Repository layout

```
docs/TECHNICAL_PLAN.md   the plan: staging, risks, schema, phase-by-phase
libs/fc_common/          telemetry models + OBD-II PID definitions, shared by all services
services/
  ingest-api/            FastAPI; owns the API contract and device identity
  stream-processor/      JetStream consumer → TimescaleDB; stateless by design
  vehicle-sim/           drive-cycle model + store-and-forward client
db/migrations/           numbered SQL, applied once by the migrate service
deploy/compose/          the Stage 0 stack
deploy/envoy/            mTLS proxy config — one copy, used by Compose and Kubernetes
deploy/grafana/          datasource + dashboards, provisioned from files
deploy/k8s/              kustomize base + kind and fabric overlays
deploy/kind/             local cluster definition for the Stage 2 rehearsal
tools/certs/             local CA for the Compose path
tests/unit/              contract and identity tests
```

## Kubernetes

```bash
cp deploy/k8s/overlays/kind/secret.env.example deploy/k8s/overlays/kind/secret.env
./fc.sh k8s-up        # kind cluster, dependencies, build, apply
./fc.sh k8s-verify    # the same exit criterion, now through mTLS and NodePort
```

Needs `kind`, `helm` and `kubectl`. Details and the list of things that fail
confusingly are in [`deploy/k8s/README.md`](deploy/k8s/README.md).

## Design notes worth knowing before you change things

**The vehicle ID comes from the certificate, never the request body.**
`services/ingest-api/app/identity.py` is the only place allowed to decide who a
device is. Stage 0 has no certs yet, so `INGEST_ALLOW_INSECURE_IDENTITY=true`
falls back to the body — that flag disappears in P2.

**The processor is stateless on purpose.** KEDA will run up to six replicas off
one shared consumer, so there is no per-vehicle ordering and no replica affinity.
Any in-memory window ("hot for 30 seconds") would be wrong the moment it scales
past one replica — and would pass every single-replica test. Trips and
sustained-condition alerts are therefore SQL on a ticker
([plan §6.3](docs/TECHNICAL_PLAN.md)).

**Retention is not optional.** At 50 cars this writes ~43M rows/day, about
3–4 GB. The cluster PVC is 20Gi. Compression and retention policies land in P4,
before the fleet is scaled up.

## Known Stage 0 limitations

These are expected and scheduled, not oversights:

- **No transport security.** Plain HTTP, and ingest binds `0.0.0.0` instead of
  loopback. Anything on the Docker network can forge an `x-forwarded-client-cert`
  header. Fixed in P2 by Envoy + mTLS, after which ingest is reachable only
  through its sidecar.
- **No user authentication.** Grafana is anonymous and there is no Fleet API
  yet. Keycloak, JWT and row-level security arrive in P3.
- **Vehicle identity is claimed from a shared volume**, not from a certificate.
  This emulates the StatefulSet ordinals Stage 2 needs — a Deployment's replicas
  cannot each mount a different cert, which is a correction to the original
  outline (plan §G7).
- **`GRAFANA_PORT`** is configurable in `.env` because 3000 is a popular port.
