"""The device side of the identity invariant:

    pod name == hostname == certificate CN == vehicle_id

These guard the thing that makes a StatefulSet able to run 50 distinct vehicles
from one pod template. If the padding ever comes back, the vehicle_id stops
matching the CN that cert-manager-csi-driver issues from ${POD_NAME}, ingest
returns 403 for every reading, and nothing else in the suite notices.
"""

import pytest
from conftest import import_service

sim_identity = import_service("vehicle-sim", "app.identity")
settings = sim_identity.settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """A fresh identity dir and no certificate, unless a test says otherwise."""
    monkeypatch.setattr(settings, "vehicle_id", "")
    monkeypatch.setattr(settings, "identity_dir", str(tmp_path / "identity"))
    monkeypatch.setattr(settings, "max_vehicles", 50)
    monkeypatch.setattr(settings, "tls_cert", "")
    monkeypatch.setattr(settings, "tls_key", "")
    monkeypatch.setattr(settings, "tls_ca", "")


# --------------------------------------------------------------- identity --


def test_injected_id_passes_through_untouched(monkeypatch):
    """The Kubernetes path. A StatefulSet sets this from metadata.name."""
    monkeypatch.setattr(settings, "vehicle_id", "sim-car-7")
    assert sim_identity.resolve_vehicle_id() == "sim-car-7"


def test_injected_id_is_not_reformatted(monkeypatch):
    # Nothing may pad, trim or renumber an injected identity: whatever the pod
    # is called is what its certificate says, and the two must agree verbatim.
    monkeypatch.setattr(settings, "vehicle_id", "sim-car-42")
    assert sim_identity.resolve_vehicle_id() == "sim-car-42"


def test_claimed_ids_are_unpadded_and_zero_based():
    """The Compose fallback has to produce the same shape of ID."""
    assert sim_identity.resolve_vehicle_id() == "sim-car-0"


def test_claims_are_exclusive_and_ascend():
    # Three sims sharing one volume must land on three different cars. This is
    # what the StatefulSet gets for free from its ordinals.
    claimed = [sim_identity.resolve_vehicle_id() for _ in range(3)]
    assert claimed == ["sim-car-0", "sim-car-1", "sim-car-2"]


def test_exhausting_the_fleet_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "max_vehicles", 2)
    sim_identity.resolve_vehicle_id()
    sim_identity.resolve_vehicle_id()
    with pytest.raises(RuntimeError, match="claimed"):
        sim_identity.resolve_vehicle_id()


# ------------------------------------------------------------ credentials --


def test_no_certificate_means_plain_http():
    # The Compose stack runs this way until Envoy lands there. An empty dict is
    # how the sim stays identical in both environments (plan §4 rule 2).
    assert sim_identity.tls_kwargs() == {}
    assert sim_identity.cert_stamp() is None


def test_certificate_is_presented_and_ca_is_pinned(monkeypatch):
    monkeypatch.setattr(settings, "tls_cert", "/tls/tls.crt")
    monkeypatch.setattr(settings, "tls_key", "/tls/tls.key")
    monkeypatch.setattr(settings, "tls_ca", "/tls/ca.crt")

    kwargs = sim_identity.tls_kwargs()
    assert kwargs["cert"] == ("/tls/tls.crt", "/tls/tls.key")
    # Pinned to the project CA, not the system trust store: no public root
    # should be able to vouch for ingest.
    assert kwargs["verify"] == "/tls/ca.crt"


def test_stamp_changes_when_the_certificate_is_renewed(tmp_path, monkeypatch):
    """The 24h cert story only holds if the sim notices the swap (§7.2)."""
    cert = tmp_path / "tls.crt"
    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\nfirst\n")
    monkeypatch.setattr(settings, "tls_cert", str(cert))

    before = sim_identity.cert_stamp()
    assert before is not None

    assert sim_identity.cert_stamp() == before, "an unchanged cert must not churn the client"

    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\nrenewed\n")
    assert sim_identity.cert_stamp() != before


def test_missing_certificate_reads_as_no_change(tmp_path, monkeypatch):
    # cert-manager-csi-driver swaps the file in place; it can be briefly
    # unreadable. None means "no change", so a rotation in progress must not be
    # mistaken for a renewal and reconnect against a half-written file.
    monkeypatch.setattr(settings, "tls_cert", str(tmp_path / "not-there-yet.crt"))
    assert sim_identity.cert_stamp() is None
