"""The telemetry wire contract, shared by sim, ingest and processor.

One definition, imported by every service, so a schema change cannot silently
desynchronise the producer from the consumer.
"""

import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fc_common.obd import METRICS, in_range

# The 413 threshold from the API contract. Deliberately NOT enforced as a
# pydantic `max_length` below: an over-long batch must produce 413, whereas a
# model validation failure produces 400. Ingest checks the count first.
MAX_READINGS_PER_BATCH = 500

# P0301, C1234, B0001, U0100 — SAE J2012 diagnostic trouble codes.
DTC_PATTERN = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")


class Reading(BaseModel):
    """A single decoded PID sample at a point in time."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    metric: str
    value: float

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        # Naive timestamps are ambiguous once they reach a TIMESTAMPTZ column.
        # Assume UTC rather than rejecting, so a sloppy device still reports.
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v

    @field_validator("metric")
    @classmethod
    def _known_metric(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"unknown metric {v!r}")
        return v

    @field_validator("value")
    @classmethod
    def _plausible_value(cls, v: float, info) -> float:
        metric = info.data.get("metric")
        # If `metric` itself failed validation it is absent here; nothing to
        # check the value against, and the metric error is already reported.
        if metric and not in_range(metric, v):
            spec = METRICS[metric]
            raise ValueError(
                f"{metric}={v} outside J1979 range [{spec.lo}, {spec.hi}] {spec.unit}"
            )
        return v


class TelemetryBatch(BaseModel):
    """The body of POST /v1/telemetry.

    `vehicle_id` is present so the device can state who it believes it is, but
    ingest treats it as a *claim to be checked* against the client certificate
    CN, never as the identity itself (TECHNICAL_PLAN §6.1 step 3).
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=8, max_length=64)
    vehicle_id: str = Field(min_length=1, max_length=64)
    readings: list[Reading] = Field(min_length=1)
    dtcs: list[str] = Field(default_factory=list)

    @field_validator("dtcs")
    @classmethod
    def _valid_dtcs(cls, v: list[str]) -> list[str]:
        for code in v:
            if not DTC_PATTERN.match(code):
                raise ValueError(f"malformed DTC {code!r}")
        return v
