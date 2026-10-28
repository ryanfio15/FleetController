"""OBD-II Mode 01 PIDs that FleetController understands.

Ranges here are the real limits defined by SAE J1979, not arbitrary sanity
bounds. Keeping to genuine PIDs now is what makes the eventual swap from the
simulator to an ELM327 / socketCAN reader a change of data source rather than a
change of schema.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    """One decoded PID: what it is called, what it means, what values are legal."""

    name: str
    pid: str
    unit: str
    lo: float
    hi: float
    description: str


# Keyed by the `metric` string that travels on the wire and lands in the
# readings hypertable.
METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in (
        MetricSpec("engine_load", "0x04", "%", 0, 100, "Calculated engine load"),
        MetricSpec("coolant_temp", "0x05", "C", -40, 215, "Engine coolant temperature"),
        MetricSpec("rpm", "0x0C", "rpm", 0, 16383.75, "Engine speed"),
        MetricSpec("speed", "0x0D", "km/h", 0, 255, "Vehicle speed"),
        MetricSpec("intake_temp", "0x0F", "C", -40, 215, "Intake air temperature"),
        MetricSpec("throttle_pos", "0x11", "%", 0, 100, "Throttle position"),
        MetricSpec("fuel_level", "0x2F", "%", 0, 100, "Fuel tank level input"),
        MetricSpec("module_voltage", "0x42", "V", 0, 65.535, "Control module voltage"),
        MetricSpec("mil_on", "0x01", "bool", 0, 1, "Malfunction indicator lamp"),
        MetricSpec("dtc_count", "0x01", "count", 0, 255, "Stored trouble code count"),
    )
}

# Normal operating envelopes, used by the simulator and by the alert rules in
# the stream processor. Documented in docs/TECHNICAL_PLAN.md §6.3.
COOLANT_NORMAL_C = 95.0
COOLANT_WARN_C = 110.0
COOLANT_CRITICAL_C = 120.0

BATTERY_RESTING_V = 12.6
BATTERY_WEAK_RESTING_V = 12.2
BATTERY_CHARGING_V = 14.0
BATTERY_WEAK_CHARGING_V = 13.0

RPM_IDLE = 800.0
RPM_OVERREV = 6000.0


def in_range(metric: str, value: float) -> bool:
    """True if `value` is a physically possible reading for `metric`."""
    spec = METRICS.get(metric)
    if spec is None:
        return False
    return spec.lo <= value <= spec.hi
