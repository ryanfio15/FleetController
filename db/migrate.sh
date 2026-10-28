#!/usr/bin/env sh
# Applies db/migrations/*.sql in filename order, exactly once each.
#
# Runs as a one-shot Compose service on `up`, and is the same script the
# Kubernetes migration Job will run in Stage 2 — one migration path, not two.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
PGHOST="${PGHOST:-timescaledb}"
export PGPASSWORD="$POSTGRES_PASSWORD"

psql() { command psql -v ON_ERROR_STOP=1 -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"; }

echo "migrate: waiting for $PGHOST"
for i in $(seq 1 60); do
    if pg_isready -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        break
    fi
    [ "$i" = 60 ] && { echo "migrate: database never became ready" >&2; exit 1; }
    sleep 1
done

psql -c "CREATE TABLE IF NOT EXISTS schema_migrations (
             filename    TEXT PRIMARY KEY,
             applied_at  TIMESTAMPTZ NOT NULL DEFAULT now());"

for file in /migrations/*.sql; do
    name=$(basename "$file")
    applied=$(psql -tAc "SELECT 1 FROM schema_migrations WHERE filename = '$name'")
    if [ "$applied" = "1" ]; then
        echo "migrate: skip $name"
        continue
    fi
    echo "migrate: apply $name"
    # Each migration and its bookkeeping row commit together, so a failure
    # halfway through can never leave a migration recorded but unapplied.
    psql --single-transaction \
         -f "$file" \
         -c "INSERT INTO schema_migrations (filename) VALUES ('$name');"
done

echo "migrate: up to date"
