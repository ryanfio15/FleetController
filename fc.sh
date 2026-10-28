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
#   ./fc.sh certs [N]     generate the local CA and N vehicle certs (P2)
#   ./fc.sh test          run the test suite
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
cd "$ROOT"

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

  help|*)
    sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
