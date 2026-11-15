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

**Check that your CNI enforces NetworkPolicy.** If it does not, every policy in
`base/networkpolicy.yaml` silently passes and proves nothing. The break/fix step
below is how you find out. RKE2's Canal enforces them; kind's default may not —
see `deploy/kind/cluster.yaml`.

**cert-manager Issuers here are namespaced, not ClusterIssuers.** A ClusterIssuer
reads its CA keypair from cert-manager's own namespace, not from `fleet`, and the
mismatch reports a missing Secret you can plainly see.

## Verification

Beyond `k8s-verify`, the things worth proving on kind before FABRIC:

```bash
# Negative mTLS — these must fail at the handshake, not merely return non-200.
curl -sv https://localhost:30443/v1/telemetry                     # no cert
curl -sv --cert foreign.crt --key foreign.key \
        https://localhost:30443/v1/telemetry                      # wrong CA

# Valid cert, mismatched body: 403 (never trust the body's vehicle_id)
# 501 readings: 413.  One reading at coolant_temp=9999: 400, zero rows written.

# KEDA scales the processor past one replica under load
./fc.sh k8s-scale 50
kubectl -n fleet get scaledobject,hpa -w

# The §6.3 claim, re-proven on-cluster: no duplicate rows at N replicas
./fc.sh k8s-psql -c "SELECT vehicle_id, metric, ts, count(*) FROM readings \
  GROUP BY 1,2,3 HAVING count(*) > 1 LIMIT 5;"

# NetworkPolicy break/fix — delete the processor's egress allowance to the
# database and confirm writes actually stop, then restore it.
kubectl -n fleet delete networkpolicy processor
./fc.sh k8s-verify          # seconds_behind should climb
./fc.sh k8s-apply           # restored

# PDB: drain a node, ingest stays up
kubectl drain fleet-worker --ignore-daemonsets --delete-emptydir-data
```
