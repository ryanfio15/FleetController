from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROCESSOR_", extra="ignore")

    nats_url: str = "nats://nats:4222"
    stream_name: str = "TELEMETRY"
    subject_filter: str = "TELEMETRY.*"
    durable_name: str = "processor"

    # One durable pull consumer shared by every replica, so KEDA can scale this
    # deployment 1..6 and each message is still processed exactly once (§6.2).
    # A pull fetch returns when it has `fetch_batch` messages OR the timeout
    # expires, so the timeout is the latency ceiling at low throughput: at 3
    # cars the batch never fills, and a 5 s timeout alone blew the 5 s
    # end-to-end SLO. Keep the batch large for throughput at 50 cars (where it
    # fills in well under a second) and the timeout short for latency at 3.
    fetch_batch: int = 100
    fetch_timeout_seconds: float = 1.0
    ack_wait_seconds: int = 30
    max_ack_pending: int = 2000
    max_deliver: int = 5

    db_dsn: str = "postgresql://fleet:devpassword@timescaledb:5432/fleet"
    db_pool_min: int = 1
    db_pool_max: int = 4

    metrics_port: int = 9100


settings = Settings()
