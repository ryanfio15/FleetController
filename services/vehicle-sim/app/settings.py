from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIM_", extra="ignore")

    # Identity. Empty means "claim the lowest free ordinal from identity_dir";
    # see resolve_vehicle_id(). P2 replaces this with the certificate CN.
    vehicle_id: str = ""
    identity_dir: str = "/identity"
    max_vehicles: int = 50

    ingest_url: str = "http://ingest:8080/v1/telemetry"

    sample_hz: float = 1.0

    # The batch interval is a hard floor on end-to-end latency: the oldest
    # reading in a batch is already this many seconds old when it is sent. The
    # plan's SLO is 5 s sim-to-visible (§9), so this must stay well under that.
    # Raising it trades freshness for fewer requests, which is the tradeoff a
    # real cellular device makes to save power.
    batch_seconds: float = 2.0

    # none | overheat | weak_battery | dtc | flatline
    fault: str = "none"
    fault_after_seconds: float = 60.0

    # Store-and-forward. A real device loses signal; this is what makes
    # JetStream's "replay after failures" demonstrable (§6.5).
    buffer_max_readings: int = 20_000      # ~30 min at 10 metrics / 1 Hz
    max_readings_per_batch: int = 500
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0

    metrics_port: int = 9101


settings = Settings()
