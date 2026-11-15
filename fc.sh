#!/usr/bin/env bash
# FleetController task runner.
#
# Primary entrypoint, because `make` is not present on a default Windows
# install. The Makefile delegates here, so both work.
#
#   ./fc.sh up            build and start the Stage 0 stack
#   ./fc.sh down          stop, keep volumes
#   ./fc.sh nuke          stop and delete volumes (fresh database)
#   ./fc.sh logs [svc]    tail logs
#   ./fc.sh ps            service status
#   ./fc.sh scale N       run N simulated vehicles
#   ./fc.sh scale-processor N   run N processor replicas (the KEDA rehearsal)
#   ./fc.sh fault KIND    restart sims with an injected fault
#   ./fc.sh psql          open a shell on the database
#   ./fc.sh verify        check the Stage 0 exit criterion
#   ./fc.sh certs [N]     generate the local CA and N vehicle certs
#   ./fc.sh test          run the test suite
#
# Kubernetes (Stage 2). Needs kind, helm and kubectl on PATH.
#
#   ./fc.sh k8s-up        create the kind cluster, install deps, build, apply
#   ./fc.sh k8s-cluster   create the kind cluster only
#   ./fc.sh k8s-deps      install cert-manager, csi-driver, KEDA, metrics, ingress
#   ./fc.sh k8s-build     build the three service images
#   ./fc.sh k8s-load      side-load the images into the kind nodes
#   ./fc.sh k8s-apply     render the overlay and apply it
#   ./fc.sh k8s-diff      show what an apply would change
#   ./fc.sh k8s-status    pods, autoscalers and certificates at a glance
#   ./fc.sh k8s-verify    check the Stage 0 exit criterion, on the cluster
#   ./fc.sh k8s-scale N   run N simulated vehicles
#   ./fc.sh k8s-psql      psql on the cluster database
#   ./fc.sh k8s-logs SVC  tail a workload's logs
#   ./fc.sh k8s-down      delete the kind cluster
#
# Target the FABRIC cluster instead of kind with:  K8S_OVERLAY=fabric
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
cd "$ROOT"

# --- Kubernetes settings ---------------------------------------------------
# Pinned on purpose. Bump these deliberately rather than discovering a new
# version mid-debug.
CERT_MANAGER_VERSION="v1.16.2"
KEDA_VERSION="2.16.1"
METRICS_SERVER_VERSION="v0.7.2"
INGRESS_NGINX_VERSION="controller-v1.12.0"

KIND_CLUSTER="${KIND_CLUSTER:-fleet}"
K8S_OVERLAY="${K8S_OVERLAY:-kind}"
K8S_NS="fleet"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/ryanfio15/fleetcontroller}"
IMAGE_TAG="${IMAGE_TAG:-dev}"
SERVICES="ingest-api stream-processor vehicle-sim"

k() { kubectl -n "$K8S_NS" "$@"; }

kz() {
    # --load-restrictor is not optional here. The configMapGenerators in
    # deploy/k8s/base read db/, deploy/envoy/ and deploy/grafana/ directly so
    # that there is only ever one copy of each shared file, and kustomize
    # refuses to read above its own root without this. `kubectl apply -k` has no
    # equivalent flag, which is why everything below pipes through apply -f -.
    kubectl kustomize --load-restrictor LoadRestrictionsNone \
        "$ROOT/deploy/k8s/overlays/$K8S_OVERLAY"
}

if [[ ! -f .env ]]; then
    echo "No .env found — copying .env.example"
    cp .env.example .env
fi
set -a; . ./.env; set +a

dc() { docker compose --env-file "$ROOT/.env" -f "$COMPOSE_FILE" "$@"; }

cmd="${1:-help}"
shift || true

case "$cmd" in
  up)
    dc up -d --build
    echo
    echo "  Grafana    http://localhost:${GRAFANA_PORT:-3000}/d/fc-fleet-overview"
    echo "  Ingest     http://localhost:8080/healthz"
    echo "  NATS       http://localhost:8222/jsz?streams=1"
    echo "  Postgres   localhost:5432  (user: $POSTGRES_USER)"
    echo
    echo "Give it ~20s for the first readings, then: ./fc.sh verify"
    ;;

  down) dc down ;;
  nuke) dc down -v ;;
  ps)   dc ps ;;
  logs) dc logs -f --tail=100 "$@" ;;

  scale)
    n="${1:?usage: ./fc.sh scale N}"
    # "the fleet grows from 3 to 50 cars by changing one number"
    dc up -d --no-recreate --scale vehicle-sim="$n" vehicle-sim
    echo "Running $n simulated vehicles."
    ;;

  scale-processor)
    n="${1:?usage: ./fc.sh scale-processor N}"
    # Proves the §6.3 claim: replicas share one durable consumer, so scaling
    # out must not duplicate rows or corrupt trips.
    dc up -d --no-recreate --scale processor="$n" processor
    echo "Running $n processor replicas off one durable consumer."
    ;;

  fault)
    kind="${1:?usage: ./fc.sh fault none|overheat|weak_battery|dtc|flatline}"
    SIM_FAULT="$kind" dc up -d --force-recreate vehicle-sim
    echo "Sims restarted with fault=$kind. Alerts appear ~60s in (SIM_FAULT_AFTER_SECONDS)."
    ;;

  psql)
    dc exec -e PGPASSWORD="$POSTGRES_PASSWORD" timescaledb \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
    ;;

  verify)
    # The Stage 0 exit criterion, checked rather than eyeballed.
    echo "Checking the Stage 0 exit criterion..."
    dc exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" timescaledb \
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<'SQL'
\pset border 2
SELECT count(*)                       AS readings_last_minute,
       count(DISTINCT vehicle_id)     AS vehicles,
       round(extract(epoch FROM now() - max(ts))::numeric, 1) AS seconds_behind
FROM readings
WHERE ts > now() - INTERVAL '1 minute';

SELECT vehicle_id, metric, round(value::numeric, 2) AS value, ts
FROM readings
WHERE ts > now() - INTERVAL '1 minute' AND metric IN ('speed','coolant_temp')
ORDER BY ts DESC LIMIT 6;
SQL
    echo
    echo "PASS if readings_last_minute > 0 and seconds_behind < 5."
    echo "Then open http://localhost:${GRAFANA_PORT:-3000}/d/fc-fleet-overview and see it on a panel."
    ;;

  certs)
    bash "$ROOT/tools/certs/make-ca.sh" "${1:-${SIM_COUNT:-3}}"
    ;;

  test)
    python -m pytest tests/ -v "$@"
    ;;

  # ------------------------------------------------------- kubernetes --
  k8s-cluster)
    kind create cluster --name "$KIND_CLUSTER" --config "$ROOT/deploy/kind/cluster.yaml"
    ;;

  k8s-deps)
    echo "==> cert-manager $CERT_MANAGER_VERSION"
    kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.yaml"
    kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=300s

    # The answer to "a StatefulSet has one pod template, so how does pod n get
    # certificate n?" — issues a per-pod cert at mount time with the CN
    # templated from pod metadata. Helm-only; there is no static manifest.
    echo "==> cert-manager-csi-driver"
    helm repo add jetstack https://charts.jetstack.io >/dev/null
    helm repo update jetstack >/dev/null
    helm upgrade --install cert-manager-csi-driver jetstack/cert-manager-csi-driver \
        --namespace cert-manager --wait

    echo "==> KEDA $KEDA_VERSION"
    kubectl apply --server-side -f "https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml"

    if [[ "$K8S_OVERLAY" == "kind" ]]; then
        # RKE2 ships metrics-server already; kind does not, and without it the
        # ingest HPA reports <unknown> for CPU and never scales.
        echo "==> metrics-server $METRICS_SERVER_VERSION"
        kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER_VERSION/components.yaml"
        # kind's kubelet serving certs are self-signed, so the default
        # verification fails and every metric stays unavailable.
        kubectl -n kube-system patch deploy metrics-server --type=json \
            -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

        echo "==> ingress-nginx $INGRESS_NGINX_VERSION"
        kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/$INGRESS_NGINX_VERSION/deploy/static/provider/kind/deploy.yaml"
        # Move it to the NodePort the PDF specifies, so the browser reaches it
        # the same way here as through the FABRIC bastion tunnel. https lands on
        # 30444 rather than 30443 because ingest already owns that one — and a
        # NodePort collision fails in a way that reads like a cluster problem.
        kubectl -n ingress-nginx patch svc ingress-nginx-controller --type=merge -p '{"spec":{"type":"NodePort","ports":[
            {"name":"http","port":80,"targetPort":"http","nodePort":30080,"protocol":"TCP"},
            {"name":"https","port":443,"targetPort":"https","nodePort":30444,"protocol":"TCP"}]}}'
    else
        echo "==> RKE2 ships metrics-server, local-path and ingress-nginx; skipping"
        echo "    (ingress-nginx is moved to NodePort 30080 by k8s-apply)"
    fi
    echo
    echo "Dependencies installed. Next: ./fc.sh k8s-build && ./fc.sh k8s-apply"
    ;;

  k8s-build)
    for svc in $SERVICES; do
        echo "==> $svc"
        docker build -t "$IMAGE_PREFIX/$svc:$IMAGE_TAG" \
            -f "$ROOT/services/$svc/Dockerfile" "$ROOT"
    done
    ;;

  k8s-load)
    # Side-load rather than push. The manifests keep the ghcr.io path in both
    # environments, and the `dev` tag means Kubernetes defaults imagePullPolicy
    # to IfNotPresent, so the node uses what is already there.
    for svc in $SERVICES; do
        kind load docker-image --name "$KIND_CLUSTER" "$IMAGE_PREFIX/$svc:$IMAGE_TAG"
    done
    ;;

  k8s-apply)
    if [[ "$K8S_OVERLAY" == "fabric" ]]; then
        # Targets kube-system, so it cannot live in the namespaced overlay.
        kubectl apply -f "$ROOT/deploy/k8s/overlays/fabric/ingress-nginx-nodeport.yaml"
    fi
    # A Job's spec is immutable. Once a migration is added the generated
    # ConfigMap's name hash moves, the Job's volume reference changes, and the
    # apply fails with "field is immutable" — so the Job goes first.
    k delete job migrate --ignore-not-found
    kz | kubectl apply -f -
    echo
    echo "Applied overlay '$K8S_OVERLAY'. Watch it come up with:"
    echo "  ./fc.sh k8s-status"
    ;;

  k8s-diff)
    kz | kubectl diff -f - || true
    ;;

  k8s-status)
    k get pods -o wide
    echo
    k get hpa,scaledobject 2>/dev/null || true
    echo
    # Pods stuck Pending with a cert volume almost always means the issuer is
    # not Ready, so it is worth seeing both together.
    k get certificate,issuer 2>/dev/null || true
    ;;

  k8s-verify)
    # The same check as `./fc.sh verify`, run against the cluster. The password
    # is read from the container's own environment rather than pulled out of the
    # Secret, so nothing sensitive lands in a shell history.
    echo "Checking the Stage 0 exit criterion on the cluster..."
    k exec -i statefulset/timescaledb -c timescaledb -- sh -c \
      'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
\pset border 2
SELECT count(*)                       AS readings_last_minute,
       count(DISTINCT vehicle_id)     AS vehicles,
       round(extract(epoch FROM now() - max(ts))::numeric, 1) AS seconds_behind
FROM readings
WHERE ts > now() - INTERVAL '1 minute';

SELECT vehicle_id, metric, round(value::numeric, 2) AS value, ts
FROM readings
WHERE ts > now() - INTERVAL '1 minute' AND metric IN ('speed','coolant_temp')
ORDER BY ts DESC LIMIT 6;
SQL
    echo
    echo "PASS if readings_last_minute > 0 and seconds_behind < 5."
    echo "Then open http://localhost:30080/grafana/d/fc-fleet-overview"
    ;;

  k8s-scale)
    n="${1:?usage: ./fc.sh k8s-scale N}"
    # Still one number. Pod names are the vehicle IDs, so this adds sim-car-3,
    # sim-car-4, ... each with its own certificate.
    k scale statefulset/sim-car --replicas="$n"
    ;;

  k8s-psql)
    k exec -it statefulset/timescaledb -c timescaledb -- sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    ;;

  k8s-logs)
    svc="${1:?usage: ./fc.sh k8s-logs ingest|processor|sim-car|timescaledb|nats|grafana}"
    k logs -l "app.kubernetes.io/name=$svc" --all-containers --tail=100 -f
    ;;

  k8s-up)
    "$0" k8s-cluster
    "$0" k8s-deps
    "$0" k8s-build
    "$0" k8s-load
    "$0" k8s-apply
    ;;

  k8s-down)
    kind delete cluster --name "$KIND_CLUSTER"
    ;;

  help|*)
    # Print the header block: every comment line from line 3 until the first
    # line that is not a comment.
    awk 'NR>2 && /^#/ { sub(/^# ?/, ""); print; next } NR>2 { exit }' "$0"
    ;;
esac
