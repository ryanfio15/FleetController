"""Single-sample alert rules.

Only rules that can be decided from one reading in isolation live here.
Anything requiring a time window ("hot for 30 seconds") is deliberately NOT in
this file — with KEDA running up to six replicas off one shared consumer there
is no per-vehicle ordering and no replica affinity, so in-memory windows are
wrong the moment the deployment scales past one. Those rules are SQL, run on a
ticker under an advisory lock. See docs/TECHNICAL_PLAN.md §6.3.
"""

from fc_common.models import Reading
from fc_common.obd import COOLANT_CRITICAL_C, BATTERY_WEAK_RESTING_V

# (kind, severity, detail) tuples, keyed off a single reading.
Alert = tuple[str, str, dict]


def evaluate(reading: Reading) -> Alert | None:
    """Return an alert for this reading, or None."""
    if reading.metric == "coolant_temp" and reading.value > COOLANT_CRITICAL_C:
        return ("overheat", "critical", {"coolant_c": reading.value})

    if reading.metric == "mil_on" and reading.value >= 1:
        return ("dtc", "warn", {"source": "mil"})

    # A resting-voltage rule needs to know the engine is off, which is a second
    # metric and therefore a windowed question. Only the unambiguous floor —
    # below which the battery is flat regardless of engine state — is safe here.
    if reading.metric == "module_voltage" and reading.value < BATTERY_WEAK_RESTING_V - 1.0:
        return ("weak_battery", "critical", {"volts": reading.value})

    return None


def from_dtcs(codes: list[str]) -> Alert | None:
    if not codes:
        return None
    return ("dtc", "warn", {"codes": sorted(codes)})
