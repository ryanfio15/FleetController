"""Device identity and credentials, on the device side.

The mirror of `services/ingest-api/app/identity.py`: that module is the only
place allowed to decide who a device *is*, and this one is the only place
allowed to decide what a device *claims to be*. Keeping both small and free of
heavy imports is what makes the pair unit-testable without a running stack.

One invariant holds everywhere, and everything else follows from it:

    pod name == hostname == certificate CN == vehicle_id
"""

import atexit
import hashlib
import os
import socket
import ssl
from pathlib import Path

from app.settings import settings


def resolve_vehicle_id() -> str:
    """Return this sim's vehicle_id, claiming an ordinal if it wasn't given one.

    Under Kubernetes vehicle-sim is a StatefulSet (plan §G7) named `sim-car`, so
    its pods are `sim-car-0`..`sim-car-N` and the identity arrives as
    SIM_VEHICLE_ID from a plain `fieldRef: metadata.name`. That is why the IDs
    are unpadded `sim-car-0` and not `sim-car-00`: cert-manager-csi-driver
    templates each pod's certificate CN from `${POD_NAME}`, and an ID that
    needed reformatting to match would be an ID that could disagree with its
    own certificate.

    The branch below is the Compose path only. Scaled Compose replicas are
    identical and get hex hostnames, so identity has to come from outside the
    container: each sim atomically claims the lowest free ordinal by creating
    sim-car-N.claim with O_EXCL on a shared volume, and releases it on
    shutdown. It emulates the StatefulSet ordinals rather than competing
    with them.
    """
    if settings.vehicle_id:
        return settings.vehicle_id

    claims = Path(settings.identity_dir)
    claims.mkdir(parents=True, exist_ok=True)

    # 0-based, matching StatefulSet ordinals.
    for ordinal in range(settings.max_vehicles):
        path = claims / f"sim-car-{ordinal}.claim"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(socket.gethostname())
        atexit.register(path.unlink, missing_ok=True)
        return path.stem

    raise RuntimeError(
        f"all {settings.max_vehicles} vehicle identities are claimed; "
        "raise SIM_MAX_VEHICLES or clear the identity volume"
    )


def tls_kwargs() -> dict:
    """httpx arguments for presenting the device's client certificate.

    An empty SIM_TLS_CERT means plain HTTP, which is how the Compose stack runs
    until Envoy lands there. Nothing here branches on *which* environment this
    is — only on whether a certificate was supplied (plan §4 rule 2).

    The SSLContext is built here rather than letting httpx assemble one from
    `cert=` and `verify=` paths, because in httpx 0.28 **those two arguments do
    not compose**. Passing `verify=<str>` takes a deprecated path that builds its
    own context, and the `cert=` client certificate is quietly discarded. What
    you get is a sim that logs `mtls: true`, mounts a perfectly good
    certificate, never sends it, and fails the handshake with Envoy's
    TLSV13_ALERT_CERTIFICATE_REQUIRED surfacing as an *empty* error string.
    Every part of that is invisible from the outside, which is why the context
    is explicit and why test_sim_identity.py asserts the chain is loaded into it.
    """
    if not settings.tls_cert:
        return {}

    # cafile=None falls back to the system trust store, which cannot vouch for
    # a private CA — so a missing SIM_TLS_CA fails the handshake rather than
    # trusting the wrong roots.
    context = ssl.create_default_context(cafile=settings.tls_ca or None)
    context.load_cert_chain(settings.tls_cert, settings.tls_key)
    return {"verify": context}


def cert_stamp() -> bytes | None:
    """Content hash of the client certificate.

    Device certs live 24 hours and are renewed in place with 8 hours to spare
    (§7.2). httpx reads the cert once, when the client is constructed, so a sim
    that outlived its certificate would go on presenting the expired one and
    start failing the handshake. Comparing this stamp each batch is what makes
    short-lived certs survive a run longer than a day, rather than being a claim
    that happens to hold for the length of a demo.

    Returns None when there is no certificate to watch, and also when the file
    is briefly unreadable mid-rotation. Callers treat None as "no change", so
    neither case churns the connection.
    """
    if not settings.tls_cert:
        return None
    try:
        return hashlib.sha256(Path(settings.tls_cert).read_bytes()).digest()
    except OSError:
        return None
