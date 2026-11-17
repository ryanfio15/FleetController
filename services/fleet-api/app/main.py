"""fleet-api — the read path. Everything the web UI knows comes from here.

Mirror image of ingest-api: that service is the only way telemetry gets *in*,
this one is the only way it comes back *out*. The asymmetry in their security
models follows from that. Ingest authenticates a machine with a client
certificate and binds loopback behind Envoy; this authenticates a person and
sits behind ingress-nginx, so it binds 0.0.0.0 and its probes are plain
httpGet.

Scope is in SQL, not here — see queries.py. The routes below pass the caller's
scope through and otherwise do presentation: deriving a status chip, choosing a
bucket width, turning an empty result into a 404.

Today this is read-only plus one write (acknowledging an alert). Device
provisioning — POST /api/v1/devices, DELETE .../cert (§7.2) — is not here: it
signs certificates, which needs the cert-manager CA seam and belongs with the
Keycloak work rather than ahead of it.
"""

import math
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import queries
from app.identity import Caller, from_headers
from app.settings import settings
from fc_common import METRICS
from fc_common.logging import setup

log = setup("fleet-api")

REQUESTS = Counter(
    "fleet_api_requests_total", "API requests", ["route", "status"]
)
QUERY_SECONDS = Histogram(
    "fleet_api_query_duration_seconds", "Database time per route", ["route"]
)

_pool: AsyncConnectionPool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pool
    _pool = AsyncConnectionPool(
        settings.db_dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        # Same as the processor: constructing a pool that connects eagerly
        # makes the service's start-up depend on the database being up at that
        # instant, which under Kubernetes it frequently is not.
        open=False,
    )
    await _pool.open(wait=True, timeout=60)
    log.info("database ready")
    try:
        yield
    finally:
        await _pool.close()


app = FastAPI(title="FleetController fleet-api", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class _Unauthorized(Exception):
    pass


async def caller(
    x_fleet_user: str | None = Header(default=None),
    x_fleet_role: str | None = Header(default=None),
) -> Caller:
    """FastAPI dependency: every scoped route takes this, so none can skip it."""
    who = from_headers(
        x_fleet_user,
        x_fleet_role,
        allow_dev_identity=settings.allow_dev_identity,
        default_user=settings.dev_user_id,
        default_role=settings.dev_role,
    )
    if who is None:
        # Raising here rather than returning None keeps the 401 out of every
        # route body. The headers are named in the challenge because there is
        # no token endpoint to point at yet.
        raise _Unauthorized()
    return who


@app.exception_handler(_Unauthorized)
async def _unauthorized(_: Request, __: _Unauthorized) -> Response:
    REQUESTS.labels(route="*", status="401").inc()
    return JSONResponse(
        {"detail": "no usable identity (X-Fleet-User / X-Fleet-Role)"},
        status_code=401,
    )


def _scope(who: Caller) -> dict:
    """The two parameters every statement in queries.py expects."""
    return {"whole_fleet": who.sees_whole_fleet, "user_id": who.user_id}


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


async def _fetch(route: str, sql: str, params: dict) -> list[dict]:
    assert _pool is not None  # lifespan opened it
    with QUERY_SECONDS.labels(route=route).time():
        async with _pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall() if cur.description else []


def _ok(route: str, payload) -> JSONResponse:
    REQUESTS.labels(route=route, status="200").inc()
    return JSONResponse(_encode(payload))


def _not_found(route: str, detail: str) -> JSONResponse:
    # 404 rather than 403 for anything the caller may not see (§6.6).
    REQUESTS.labels(route=route, status="404").inc()
    return JSONResponse({"detail": detail}, status_code=404)


def _encode(value):
    """datetime/Decimal to JSON, without pulling in a serialisation layer.

    Every timestamp in the schema is TIMESTAMPTZ, so isoformat() carries the
    offset and the browser can render local time without guessing.
    """
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _status(row: dict) -> str:
    """The grid's chip. Offline beats alerting: a car that stopped reporting
    mid-overheat is not "warn", it is unknown, and saying otherwise is the
    reassuring-but-wrong reading."""
    last_seen = row.get("last_seen")
    if last_seen is None:
        return "offline"
    age = (datetime.now(tz=last_seen.tzinfo) - last_seen).total_seconds()
    if age > settings.stale_after_seconds:
        return "offline"
    if row.get("has_critical"):
        return "critical"
    if row.get("open_alerts"):
        return "warn"
    return "ok"


def _shape_vehicle(row: dict) -> dict:
    row = dict(row)
    row["status"] = _status(row)
    row["open_alerts"] = int(row.get("open_alerts") or 0)
    return row


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/api/v1/summary")
async def summary(who: Caller = Depends(caller)) -> Response:
    rows = await _fetch(
        "summary",
        queries.SUMMARY,
        _scope(who) | {"stale": f"{settings.stale_after_seconds} seconds"},
    )
    return _ok("summary", rows[0] | {"role": who.role, "user_id": who.user_id})


@app.get("/api/v1/vehicles")
async def list_vehicles(who: Caller = Depends(caller)) -> Response:
    rows = await _fetch(
        "vehicles", queries.FLEET, _scope(who) | {"vehicle_id": None}
    )
    return _ok("vehicles", [_shape_vehicle(r) for r in rows])


@app.get("/api/v1/vehicles/{vehicle_id}")
async def get_vehicle(
    vehicle_id: str = Path(max_length=64),
    who: Caller = Depends(caller),
) -> Response:
    rows = await _fetch(
        "vehicle", queries.FLEET, _scope(who) | {"vehicle_id": vehicle_id}
    )
    if not rows:
        return _not_found("vehicle", f"no vehicle '{vehicle_id}'")
    return _ok("vehicle", _shape_vehicle(rows[0]))


@app.get("/api/v1/vehicles/{vehicle_id}/series")
async def get_series(
    vehicle_id: str = Path(max_length=64),
    metric: list[str] = Query(default=["speed", "coolant_temp"]),
    hours: float = Query(default=1.0, gt=0),
    who: Caller = Depends(caller),
) -> Response:
    unknown = [m for m in metric if m not in METRICS]
    if unknown:
        # The known set is fc_common's, so this list cannot drift from what the
        # sim actually emits (§5.1).
        REQUESTS.labels(route="series", status="400").inc()
        return JSONResponse(
            {"detail": f"unknown metric(s): {', '.join(sorted(unknown))}"},
            status_code=400,
        )

    hours = min(hours, settings.max_window_hours)
    window_seconds = hours * 3600
    # Bucket wide enough that the response can never exceed max_series_points
    # per metric, whatever window is asked for. Ceil, not round: rounding down
    # at the boundary puts the result one point over the cap.
    bucket = max(1, math.ceil(window_seconds / settings.max_series_points))

    rows = await _fetch(
        "series",
        queries.SERIES,
        _scope(who)
        | {
            "vehicle_id": vehicle_id,
            "metrics": list(metric),
            "window": f"{window_seconds} seconds",
            "bucket": f"{bucket} seconds",
        },
    )

    # An empty result is ambiguous — no such vehicle, not yours, or simply no
    # readings in the window — so confirm the vehicle before deciding. Only on
    # the empty path, so the common case stays one round trip.
    if not rows:
        exists = await _fetch(
            "series", queries.FLEET, _scope(who) | {"vehicle_id": vehicle_id}
        )
        if not exists:
            return _not_found("series", f"no vehicle '{vehicle_id}'")

    series: dict[str, list] = {m: [] for m in metric}
    for row in rows:
        series[row["metric"]].append(
            {
                "ts": row["ts"],
                "avg": row["avg"],
                "min": row["min"],
                "max": row["max"],
            }
        )
    return _ok(
        "series",
        {
            "vehicle_id": vehicle_id,
            "bucket_seconds": bucket,
            "window_hours": hours,
            "units": {m: METRICS[m].unit for m in metric},
            "series": series,
        },
    )


@app.get("/api/v1/vehicles/{vehicle_id}/trips")
async def get_trips(
    vehicle_id: str = Path(max_length=64),
    limit: int = Query(default=20, ge=1),
    who: Caller = Depends(caller),
) -> Response:
    rows = await _fetch(
        "trips",
        queries.TRIPS,
        _scope(who)
        | {"vehicle_id": vehicle_id, "limit": min(limit, settings.max_rows)},
    )
    return _ok("trips", rows)


@app.get("/api/v1/alerts")
async def list_alerts(
    vehicle_id: str | None = Query(default=None, max_length=64),
    only_open: bool = Query(default=True),
    limit: int = Query(default=100, ge=1),
    who: Caller = Depends(caller),
) -> Response:
    rows = await _fetch(
        "alerts",
        queries.ALERTS,
        _scope(who)
        | {
            "vehicle_id": vehicle_id,
            "only_open": only_open,
            "limit": min(limit, settings.max_rows),
        },
    )
    return _ok("alerts", rows)


@app.post("/api/v1/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: int = Path(ge=1),
    who: Caller = Depends(caller),
) -> Response:
    params = _scope(who) | {"alert_id": alert_id}
    rows = await _fetch("ack", queries.ACK, params)
    if rows:
        log.info(
            "alert acknowledged",
            extra={"alert_id": alert_id, "by": who.user_id, "role": who.role},
        )
        return _ok("ack", rows[0])

    # Nothing updated: either already acknowledged, or not visible to this
    # caller. The lookup tells those apart — and collapses "no such alert" and
    # "someone else's alert" into the same 404.
    existing = await _fetch("ack", queries.ACK_LOOKUP, params)
    if not existing:
        return _not_found("ack", f"no alert {alert_id}")
    return _ok("ack", existing[0])


# --------------------------------------------------------------------------- #
# Operational endpoints. Not under /api, so the Ingress does not expose them.
# --------------------------------------------------------------------------- #


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up. Deliberately does not touch the database —
    a database blip should not restart a pod that would recover on its own."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Response:
    """Readiness: we can actually answer a query right now."""
    try:
        await _fetch("readyz", queries.READYZ, {})
    except Exception as exc:  # noqa: BLE001
        log.warning("not ready", extra={"error": str(exc)[:200]})
        return JSONResponse({"status": "no database"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
