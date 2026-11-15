# Kubernetes manifests

Stage 2 of [the plan](../../docs/TECHNICAL_PLAN.md). The PDF's Chapter 2
architecture, as kustomize.

```
base/                  everything real
overlays/kind/         the rehearsal target — no node tiers, generated secret
overlays/fabric/       RKE2 on five FABRIC VMs — tier pinning, sealed secrets
```

If an overlay ever has to patch application *behaviour*, that is a bug in the
base: push the difference into an environment variable instead (plan §4 rule 2).
Today the overlays differ only in node placement, StorageClass, how the secret
is supplied, and the site's own addresses.

## Quick start on kind

Needs `kind`, `helm`, `kubectl` and Docker.

```bash
cp overlays/kind/secret.env.example overlays/kind/secret.env
cd ../.. && ./fc.sh k8s-up          # cluster, deps, build, load, apply
./fc.sh k8s-status                   # watch it come up
./fc.sh k8s-verify                   # the Stage 0 exit criterion, on-cluster
```

Then open **http://localhost:30080/grafana/d/fc-fleet-overview**.

## What is here, and what is not

| PDF component | Written | Notes |
|---|---|---|
| ingest (envoy + ingest-api), 2→4 HPA | yes | NodePort 30443, TLS passthrough |
| nats (JetStream), 1 | yes | stream declared by ingest at startup |
| stream-processor, 1→6 KEDA | yes | the scaling target |
| timescaledb + postgres-exporter, 1 | yes | 20Gi, headless Service |
| grafana | yes | provisioned from `deploy/grafana/` |
| vehicle-sim, 3→50 | yes | StatefulSet, per-pod certs |
| migrations | yes | runs `db/migrate.sh` unmodified |
| pg-backup | yes | nightly `pg_dump -Fc`, 7-day retention |
| fleet-api, web-ui, keycloak | **no** | P3 — no code yet. `grafana.yaml` reserves their ingress paths |

## Things that will bite you

Each of these fails confusingly rather than obviously. They are commented at the
point they matter, and collected here because that is where someone looks second.

**`kubectl apply -k` will not work.** The `configMapGenerator`s read `db/`,
`deploy/envoy/` and `deploy/grafana/` directly, so there is exactly one copy of
every shared file. Kustomize refuses to read above its own root without
`--load-restrictor LoadRestrictionsNone`, and `apply -k` has no such flag. Use
`./fc.sh k8s-apply`, which renders and pipes to `apply -f -`.

**The migrate Job must be deleted before re-applying.** A Job's spec is
immutable; adding a migration moves the ConfigMap's name hash and the apply
fails with "field is immutable". `k8s-apply` handles it.

**ingest-api's probes are `exec`, not `httpGet`.** It binds `127.0.0.1` on
purpose — that is what makes "reachable only via Envoy" structural rather than a
convention. kubelet probes the pod IP, so an HTTP probe would fail forever and
CrashLoopBackOff a working pod.

**Nothing may terminate TLS in front of 30443.** Ingest is a NodePort Service
straight to Envoy and deliberately not behind ingress-nginx. A proxy in that
path strips the client certificate, and the identity model fails open-ish and
silently.

**The processor has no `hostPort` and must never get one.** Stage 0 found a fixed
host port blocking replica scaling; `hostPort` is the same mistake here, and the
second replica on a node would simply stay Pending.

**Never add an HPA to the processor.** KEDA creates and owns one. Two
autoscalers on one target flap.

**NetworkPolicy only filters *new* connections.** Deleting an allowance does not
sever established ones — conntrack keeps them, so the processor's psycopg pool
goes on writing happily for as long as the pod lives. A break/fix test that only
deletes a policy therefore "proves" enforcement works when nothing was tested.
Restart the workload after changing the policy, which is what the step below
does.

**`k8s-apply` reverts `k8s-scale`.** The overlay declares the sim replica count,
so an apply scales the fleet back to it. For a fleet size that survives, edit
`replicas` in the overlay — that is where "the fleet grows by changing one
number" actually lives.

**Steady-state lag at 50 cars is ~40 pending, not 500.** The processor is
comfortably faster than the fleet, so `lagThreshold: 500` never fires at steady
state and KEDA sits at one replica. That is the system working, not a
misconfiguration — but it means the 1→6 scaling claim can only be demonstrated
against a genuine backlog, as below. Do not "fix" it by tuning the threshold
down to the demo.

**cert-manager Issuers here are namespaced, not ClusterIssuers.** A ClusterIssuer
reads its CA keypair from cert-manager's own namespace, not from `fleet`, and the
mismatch reports a missing Secret you can plainly see.

## Verification

All of the following were run on kind (k8s v1.37) and passed. Beyond
`k8s-verify`:

| Claim | Result |
|---|---|
| End-to-end through mTLS and NodePort | 1740 readings/min, 3 cars, **2.2s behind** — inside the 5s SLO |
| Contract matrix on-cluster | 202 · 403 (CN≠body) · 413 (501 readings) · 400 (out of range) all correct |
| No client cert | fails at the **TLS handshake**, not with an HTTP status |
| Bad reading rejects the whole batch | **zero** rows written for the 400 |
| Per-pod certificates | 50 pods, 50 distinct CNs, each equal to its pod name |
| KEDA 1→6 under a real backlog | scaled **1 → 3 → 4 → 5 → 6**, drained ~600k messages to zero lag in ~60s |
| The §6.3 no-duplicates claim, at 6 replicas | **0 duplicate groups** across 246,982 rows from 50 vehicles |
| NetworkPolicy actually enforced | allowed path connects; denied paths time out |
| Grafana through ingress-nginx :30080 | HTTP 200, `database: ok` |

The commands:

```bash
# Negative mTLS — these must fail at the handshake, not merely return non-200.
curl -sv https://localhost:30443/v1/telemetry                     # no cert
curl -sv --cert foreign.crt --key foreign.key \
        https://localhost:30443/v1/telemetry                      # wrong CA

# Valid cert, mismatched body: 403 (never trust the body's vehicle_id)
# 501 readings: 413.  One reading at coolant_temp=9999: 400, zero rows written.

# KEDA 1->6. Steady state will not do it (lag sits near 40), so this blocks the
# processor's path to the database to build a genuine backlog. Note the restart:
# without it the existing connection pool survives the policy change and
# nothing happens at all.
./fc.sh k8s-scale 50
kubectl -n fleet delete networkpolicy processor
kubectl -n fleet rollout restart deploy/processor
kubectl -n fleet get hpa keda-hpa-processor -w     # lag climbs past 500, replicas 1->6

./fc.sh k8s-apply                                  # policy restored; lag drains to 0

# The §6.3 claim, now at six replicas draining a backlog: still zero duplicates
./fc.sh k8s-psql -c "SELECT count(*) FROM (SELECT vehicle_id, metric, ts FROM \
  readings GROUP BY 1,2,3 HAVING count(*) > 1) d;"

# That same sequence is also the NetworkPolicy break/fix test and the §8.2
# chaos case 'kill the database path': lag grows, nothing is lost, and the
# backlog drains on recovery.

# PDB: drain a node, ingest stays up
kubectl drain fleet-worker --ignore-daemonsets --delete-emptydir-data
```
