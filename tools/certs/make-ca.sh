#!/usr/bin/env bash
# Local development CA for FleetController.
#
# Produces the same chain shape cert-manager will issue in Stage 2, so the
# Envoy config and the client code do not change between environments:
#
#   fc-root-ca            self-signed root
#   ├── ingest-server     server cert, SAN: ingest, localhost, 127.0.0.1
#   └── sim-car-N         one client cert per vehicle, CN == vehicle_id
#
# The CN is load-bearing: ingest takes the vehicle_id from it and from nowhere
# else (docs/TECHNICAL_PLAN.md §6.1).
#
#   ./tools/certs/make-ca.sh [vehicle_count]
#
# Output lands in tools/certs/out/, which is gitignored. Never commit a key.
set -euo pipefail

# Git Bash / MSYS on Windows rewrites arguments that look like absolute paths,
# turning "/O=FleetController/CN=sim-car-0" into "C:/Program Files/Git/O=...".
# Harmless to export on Linux and macOS.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

COUNT="${1:-3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/out"
DAYS_CA=3650
# 24-hour leaf certs. Short lifetimes are what let us skip CRL/OCSP entirely
# and force the renewal path to exist from the start (§7.2).
DAYS_LEAF=1

mkdir -p "$OUT"
cd "$OUT"

log() { printf '  %s\n' "$*"; }

# ------------------------------------------------------------------- root CA
if [[ -f ca.key && -f ca.crt ]]; then
    log "root CA exists, reusing"
else
    log "creating root CA"
    openssl genrsa -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS_CA" \
        -subj "/O=FleetController/CN=fc-root-ca" -out ca.crt
fi

# --------------------------------------------------------------- server cert
log "issuing ingest server cert"
openssl genrsa -out ingest-server.key 2048
openssl req -new -key ingest-server.key \
    -subj "/O=FleetController/CN=ingest" -out ingest-server.csr

cat > ingest-server.ext <<'EXT'
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:ingest, DNS:localhost, DNS:ingest.fleet.svc.cluster.local, IP:127.0.0.1
EXT

openssl x509 -req -in ingest-server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out ingest-server.crt -days "$DAYS_LEAF" -sha256 \
    -extfile ingest-server.ext

# -------------------------------------------------------------- client certs
cat > client.ext <<'EXT'
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EXT

# 0-based and unpadded, because the CN has to equal the StatefulSet pod name
# that cert-manager-csi-driver templates in Kubernetes: pod name == hostname ==
# CN == vehicle_id, with no translation step anywhere.
for i in $(seq 0 $((COUNT - 1))); do
    id="sim-car-$i"
    log "issuing client cert $id"

    openssl genrsa -out "$id.key" 2048
    # CN carries the vehicle_id. Everything downstream depends on this.
    openssl req -new -key "$id.key" \
        -subj "/O=FleetController/OU=vehicles/CN=$id" -out "$id.csr"
    openssl x509 -req -in "$id.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
        -out "$id.crt" -days "$DAYS_LEAF" -sha256 -extfile client.ext

    # The fingerprint ingest checks against its revocation denylist.
    openssl x509 -in "$id.crt" -noout -fingerprint -sha256 \
        | sed 's/.*=//; s/://g' | tr 'A-Z' 'a-z' > "$id.fingerprint"
done

rm -f ./*.csr ./*.ext
chmod 600 ./*.key

echo
echo "Wrote $((COUNT + 1)) leaf certs to tools/certs/out/"
echo "Verify one with:"
echo "  openssl verify -CAfile tools/certs/out/ca.crt tools/certs/out/sim-car-0.crt"
echo "  openssl x509 -in tools/certs/out/sim-car-0.crt -noout -subject"
