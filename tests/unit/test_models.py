"""Contract validation tests.

These encode the rows of the API contract table that can be checked without a
running stack. The integration counterparts (403 on cert mismatch, 413, 503)
arrive in P2 alongside Envoy.
"""

import pytest
from pydantic import ValidationError

from fc_common import Reading, TelemetryBatch
from fc_common.obd import METRICS, in_range


def batch(**overrides) -> dict:
    body = {
        "batch_id": "0123456789abcdef",
        "vehicle_id": "sim-car-0",
        "readings": [{"ts": "2026-10-27T18:04:01Z", "metric": "rpm", "value": 1840}],
        "dtcs": [],
    }
    body.update(overrides)
    return body


def test_valid_batch_parses():
    parsed = TelemetryBatch.model_validate(batch())
    assert parsed.vehicle_id == "sim-car-0"
    assert parsed.readings[0].metric == "rpm"


def test_unknown_metric_rejected():
    with pytest.raises(ValidationError, match="unknown metric"):
        TelemetryBatch.model_validate(
            batch(readings=[{"ts": "2026-10-27T18:04:01Z", "metric": "warp_core", "value": 1}])
        )


def test_value_outside_j1979_range_rejected():
    # The §8.1 case: one impossible reading must reject the whole batch.
    with pytest.raises(ValidationError, match="outside J1979 range"):
        TelemetryBatch.model_validate(
            batch(readings=[
                {"ts": "2026-10-27T18:04:01Z", "metric": "coolant_temp", "value": 20},
                {"ts": "2026-10-27T18:04:02Z", "metric": "coolant_temp", "value": 9999},
            ])
        )


def test_naive_timestamp_assumed_utc():
    reading = Reading(ts="2026-10-27T18:04:01", metric="speed", value=50)
    assert reading.ts.tzinfo is not None
    assert reading.ts.utcoffset().total_seconds() == 0


def test_extra_fields_rejected():
    # extra="forbid" stops a device from smuggling unvalidated fields through.
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(batch(injected="surprise"))


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(batch(readings=[]))


def test_malformed_dtc_rejected():
    with pytest.raises(ValidationError, match="malformed DTC"):
        TelemetryBatch.model_validate(batch(dtcs=["NOTACODE"]))


@pytest.mark.parametrize("code", ["P0301", "C1234", "B0001", "U0100"])
def test_well_formed_dtcs_accepted(code):
    assert TelemetryBatch.model_validate(batch(dtcs=[code])).dtcs == [code]


def test_oversized_batch_is_not_a_model_error():
    """501 readings must reach ingest so it can answer 413, not 400.

    If a max_length ever gets added to the model this test fails, which is the
    point: the size cap belongs to ingest, not to the schema.
    """
    big = [
        {"ts": "2026-10-27T18:04:01Z", "metric": "rpm", "value": 1000}
        for _ in range(501)
    ]
    assert len(TelemetryBatch.model_validate(batch(readings=big)).readings) == 501


@pytest.mark.parametrize("metric,spec", METRICS.items())
def test_range_bounds_are_inclusive(metric, spec):
    assert in_range(metric, spec.lo)
    assert in_range(metric, spec.hi)
    assert not in_range(metric, spec.hi + 1)
