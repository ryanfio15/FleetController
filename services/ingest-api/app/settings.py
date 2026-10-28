from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration arrives as environment variables.

    No `if RUNNING_IN_K8S` branch exists anywhere in this service: Compose and
    Kubernetes differ only in what they put in the environment.
    """

    model_config = SettingsConfigDict(env_prefix="INGEST_", extra="ignore")

    nats_url: str = "nats://nats:4222"
    stream_name: str = "TELEMETRY"
    subject_prefix: str = "TELEMETRY"

    max_readings_per_batch: int = 500

    # Stage 0 has no client certificates, so the vehicle_id in the body is
    # accepted as the identity. P2 flips this to false, at which point identity
    # comes only from the Envoy-supplied client cert CN.
    allow_insecure_identity: bool = False

    # Stream limits, mirrored from TECHNICAL_PLAN §6.2. The 5Gi JetStream PVC
    # is sized against max_bytes with deliberate headroom.
    stream_max_age_seconds: int = 24 * 3600
    stream_max_bytes: int = 4 * 1024**3
    stream_duplicate_window_seconds: int = 120

    retry_after_seconds: int = 5

    # Bind loopback-only in production: the Envoy sidecar shares this pod's
    # network namespace, so "the API is only reachable via Envoy" becomes a
    # structural fact rather than a convention (§6.1). Compose overrides this
    # to 0.0.0.0 because there is no sidecar yet in Stage 0.
    host: str = "127.0.0.1"
    port: int = 8080


settings = Settings()
