from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration arrives as environment variables (§4 rule 2).

    The prefix is `FLEET_`, not `FLEET_API_`, and that is deliberate. Every
    other service uses its own name as the prefix, but Kubernetes injects a
    legacy Docker-link variable per Service, and the Service in front of this
    pod is called `fleet-api`:

        FLEET_API_PORT=tcp://10.96.11.4:8081

    A `FLEET_API_` prefix would read that as this service's listen port and
    uvicorn would exit with "Invalid value for '--port'" — the exact failure
    ingest.yaml documents at length. `enableServiceLinks: false` in the
    manifest already prevents it; the shorter prefix means the bug cannot come
    back if someone removes that line.
    """

    model_config = SettingsConfigDict(env_prefix="FLEET_", extra="ignore")

    db_dsn: str = "postgresql://fleet:devpassword@timescaledb:5432/fleet"
    db_pool_min: int = 1
    db_pool_max: int = 4

    host: str = "0.0.0.0"
    port: int = 8081

    # --- identity -----------------------------------------------------------
    # P3 replaces this with RS256 validation against Keycloak's JWKS (§6.6).
    # Until the realm exists, the caller is whoever the gateway says they are:
    # X-Fleet-User / X-Fleet-Role, defaulted below. The plan explicitly allows
    # this ("Keycloak and the Fleet API can be stubbed (hardcoded dev JWT)"),
    # and it is only tolerable because the cluster is reachable solely through
    # the SSH bastion tunnel — the same gap Grafana's anonymous access accepts.
    #
    # Setting this false makes every request 401, which is what a deployment
    # that has gained real auth should look like if the header path is left in.
    allow_dev_identity: bool = True
    dev_user_id: str = "11111111-1111-1111-1111-111111111111"
    dev_role: str = "admin"

    # --- query shaping ------------------------------------------------------
    # A vehicle with no reading inside this window is "offline" in the grid.
    # Three sample intervals of slack over the sim's 10 s batch period, so a
    # single late flush does not flip the whole fleet's chips.
    stale_after_seconds: int = 45
    # Caps on what one request may ask the database for. The UI never exceeds
    # them; a hand-written curl can, and a 50-vehicle hypertable scan from an
    # unbounded `since` is the one query here that could hurt.
    max_series_points: int = 2000
    max_window_hours: int = 24
    max_rows: int = 500


settings = Settings()
