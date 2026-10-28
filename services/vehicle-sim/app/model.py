"""A car, modelled well enough that the Grafana panels look like a car.

A random walk would satisfy the pipeline but would make every dashboard
meaningless and every alert threshold untestable. This is a small drive-cycle
state machine: speed comes from the cycle, RPM from speed and a gear ratio,
coolant warms on a first-order curve toward its thermostat setpoint, and bus
voltage tracks whether the alternator is turning.

Faults are injectable so that each alert rule in the processor has something to
fire on, and so the demo can show the product's actual value proposition.
"""

import random
from dataclasses import dataclass, field
from enum import Enum

from fc_common.obd import (
    BATTERY_CHARGING_V,
    BATTERY_RESTING_V,
    COOLANT_NORMAL_C,
    RPM_IDLE,
)

AMBIENT_C = 20.0
# Top speed in each gear, km/h. Shift points fall out of these.
GEAR_TOP_KMH = (30.0, 55.0, 85.0, 115.0, 145.0)


class Phase(str, Enum):
    PARKED = "parked"
    IDLE = "idle"
    ACCEL = "accel"
    CRUISE = "cruise"
    DECEL = "decel"


@dataclass
class Vehicle:
    vehicle_id: str
    rng: random.Random

    speed: float = 0.0            # km/h
    coolant: float = AMBIENT_C    # C
    fuel: float = 85.0            # %
    phase: Phase = Phase.PARKED
    phase_left: float = 0.0       # seconds remaining in the current phase
    cruise_target: float = 60.0
    elapsed: float = 0.0

    fault: str = "none"
    fault_active: bool = False
    dtcs: set[str] = field(default_factory=set)
    _dtcs_reported: bool = False

    # ------------------------------------------------------------------ step
    def step(self, dt: float) -> None:
        self.elapsed += dt
        self.phase_left -= dt
        if self.phase_left <= 0:
            self._next_phase()

        if self.phase is Phase.ACCEL:
            # ~0.9 m/s^2, tapering as speed approaches the cruise target.
            self.speed = min(self.cruise_target, self.speed + 3.2 * dt)
        elif self.phase is Phase.CRUISE:
            self.speed = max(0.0, self.speed + self.rng.uniform(-0.6, 0.6) * dt)
        elif self.phase is Phase.DECEL:
            self.speed = max(0.0, self.speed - 4.5 * dt)
        else:
            self.speed = 0.0

        self._update_coolant(dt)

        if self.running and self.speed > 0:
            # Crude but monotonic: burn faster under load.
            self.fuel = max(0.0, self.fuel - (0.00015 + self.speed * 2e-6) * dt)

        self._apply_fault()

    def _next_phase(self) -> None:
        if self.phase in (Phase.PARKED, Phase.IDLE):
            self.phase, self.phase_left = Phase.ACCEL, self.rng.uniform(10, 25)
            self.cruise_target = self.rng.choice([40.0, 60.0, 80.0, 100.0])
        elif self.phase is Phase.ACCEL:
            self.phase, self.phase_left = Phase.CRUISE, self.rng.uniform(60, 240)
        elif self.phase is Phase.CRUISE:
            # Mostly keep cruising; occasionally slow for a light or a turn.
            if self.rng.random() < 0.6:
                self.phase, self.phase_left = Phase.DECEL, self.rng.uniform(8, 20)
            else:
                self.phase_left = self.rng.uniform(60, 180)
        elif self.phase is Phase.DECEL:
            if self.speed < 5:
                # A long stop ends the trip; the rollup's 180 s gap rule will
                # split these into separate trips. An overheating car keeps
                # running, both because that is the scenario the alert exists
                # for and because parking would soak the heat back out.
                parks = self.rng.random() < 0.3 and not (
                    self.fault_active and self.fault == "overheat"
                )
                self.phase = Phase.PARKED if parks else Phase.IDLE
                self.phase_left = self.rng.uniform(200, 400) if self.phase is Phase.PARKED \
                    else self.rng.uniform(5, 40)
            else:
                self.phase, self.phase_left = Phase.ACCEL, self.rng.uniform(5, 15)

    def _update_coolant(self, dt: float) -> None:
        if not self.running:
            # Soak back toward ambient when the engine is off.
            self.coolant += (AMBIENT_C - self.coolant) * 0.002 * dt
            return

        target, rate = COOLANT_NORMAL_C, 0.01
        if self.fault_active and self.fault == "overheat":
            # A failed water pump heats far faster than the thermostat-regulated
            # warm-up: ~33 s time constant, so the 110 C threshold is crossed in
            # about 40 s and the sustained-30 s alert opens inside two minutes.
            # Without this the demo takes four-plus minutes and is at the mercy
            # of whether the drive cycle happens to park the car.
            target, rate = 135.0, 0.03

        # First-order warm-up: fast while cold, asymptotic near the setpoint.
        self.coolant += (target - self.coolant) * rate * dt
        self.coolant += self.rng.uniform(-0.15, 0.15)

    def _apply_fault(self) -> None:
        if self.fault == "none" or not self.fault_active:
            return
        if self.fault == "dtc" and not self._dtcs_reported:
            self.dtcs.add("P0301")  # cylinder 1 misfire
            self._dtcs_reported = True

    # -------------------------------------------------------------- readings
    @property
    def running(self) -> bool:
        return self.phase is not Phase.PARKED

    @property
    def rpm(self) -> float:
        if not self.running:
            return 0.0
        if self.speed < 3:
            return RPM_IDLE + self.rng.uniform(-30, 30)
        gear = next(
            (i for i, top in enumerate(GEAR_TOP_KMH) if self.speed <= top),
            len(GEAR_TOP_KMH) - 1,
        )
        lower = GEAR_TOP_KMH[gear - 1] if gear else 0.0
        span = max(GEAR_TOP_KMH[gear] - lower, 1.0)
        frac = (self.speed - lower) / span
        return 1200 + frac * 3300 + self.rng.uniform(-50, 50)

    @property
    def voltage(self) -> float:
        if self.fault_active and self.fault == "weak_battery":
            return (12.0 if self.running else 11.4) + self.rng.uniform(-0.1, 0.1)
        base = BATTERY_CHARGING_V if self.running else BATTERY_RESTING_V
        return base + self.rng.uniform(-0.15, 0.15)

    @property
    def throttle(self) -> float:
        return {
            Phase.ACCEL: self.rng.uniform(35, 75),
            Phase.CRUISE: self.rng.uniform(12, 25),
            Phase.DECEL: self.rng.uniform(0, 4),
        }.get(self.phase, self.rng.uniform(0, 3))

    @property
    def engine_load(self) -> float:
        return min(100.0, self.throttle * 1.15 + (18 if self.running else 0))

    def sample(self) -> dict[str, float]:
        """The PID set this vehicle reports, one sample's worth."""
        if self.fault_active and self.fault == "flatline":
            # Simulates a dead sensor bus: the device reports nothing at all.
            return {}
        return {
            "speed": round(self.speed, 1),
            "rpm": round(self.rpm, 0),
            "coolant_temp": round(self.coolant, 1),
            "module_voltage": round(self.voltage, 2),
            "throttle_pos": round(self.throttle, 1),
            "engine_load": round(self.engine_load, 1),
            "fuel_level": round(self.fuel, 1),
            "intake_temp": round(AMBIENT_C + self.rng.uniform(-2, 8), 1),
            "mil_on": 1.0 if self.dtcs else 0.0,
            "dtc_count": float(len(self.dtcs)),
        }

    def active_dtcs(self) -> list[str]:
        """Stored trouble codes, as Mode 03 would return them.

        Reported on every batch while present rather than only on change: the
        processor's dtc_events upsert is idempotent, and a device that only
        ever reports transitions loses the fault if one batch is dropped.
        """
        return sorted(self.dtcs)
