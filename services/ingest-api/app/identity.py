"""Device identity extraction.

The whole security model of the ingest path rests on one rule: the vehicle_id
is whatever the *certificate* says, and never what the request body says. This
module is the only place allowed to decide a device's identity, so that when
Stage 0's insecure fallback is removed in P2 there is exactly one line to change.
"""

import re
from urllib.parse import unquote

# Envoy sets x-forwarded-client-cert with `forward_client_cert_details:
# SANITIZE_SET`, so anything a client sent under that header is discarded first.
# Format: By=...;Hash=...;Subject="CN=sim-car-1,O=FleetController";URI=...
_SUBJECT = re.compile(r'Subject="([^"]*)"')
_HASH = re.compile(r"Hash=([0-9a-fA-F]+)")
_CN = re.compile(r"CN=([^,/]+)")


class Identity:
    __slots__ = ("vehicle_id", "cert_fp", "verified")

    def __init__(self, vehicle_id: str, cert_fp: str | None, verified: bool):
        self.vehicle_id = vehicle_id
        self.cert_fp = cert_fp
        self.verified = verified


def from_xfcc(header: str | None) -> Identity | None:
    """Parse the CN and cert fingerprint out of Envoy's XFCC header."""
    if not header:
        return None
    # Envoy emits one comma-separated element per cert in the chain, with the
    # client's own cert first. Subject values are quoted and may themselves
    # contain commas, so take the first match rather than splitting on commas.
    subject_match = _SUBJECT.search(header)
    if not subject_match:
        return None
    cn_match = _CN.search(unquote(subject_match.group(1)))
    if not cn_match:
        return None

    hash_match = _HASH.search(header)
    return Identity(
        vehicle_id=cn_match.group(1).strip(),
        cert_fp=hash_match.group(1).lower() if hash_match else None,
        verified=True,
    )
