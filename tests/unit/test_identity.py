"""The identity rule: vehicle_id comes from the certificate, never the body.

These guard the single most security-relevant line in the ingest path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "ingest-api"))

from app.identity import from_xfcc  # noqa: E402

XFCC = (
    'By=spiffe://cluster/ingest;'
    'Hash=a1b2c3d4e5f6;'
    'Subject="O=FleetController,OU=vehicles,CN=sim-car-07"'
)


def test_extracts_cn_and_fingerprint():
    identity = from_xfcc(XFCC)
    assert identity.vehicle_id == "sim-car-07"
    assert identity.cert_fp == "a1b2c3d4e5f6"
    assert identity.verified is True


def test_takes_the_client_cert_when_a_chain_is_present():
    # Envoy lists the client's own cert first; an intermediate must not win.
    chained = XFCC + ',By=spiffe://cluster/ca;Hash=999;Subject="CN=fc-root-ca"'
    assert from_xfcc(chained).vehicle_id == "sim-car-07"


def test_handles_url_encoded_subject():
    encoded = 'Hash=abc;Subject="O=FleetController%2COU=vehicles%2CCN=sim-car-02"'
    assert from_xfcc(encoded).vehicle_id == "sim-car-02"


@pytest.mark.parametrize("header", [None, "", "garbage", 'Hash=abc;Subject=""'])
def test_unusable_headers_yield_no_identity(header):
    assert from_xfcc(header) is None
