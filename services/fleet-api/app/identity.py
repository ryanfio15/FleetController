"""Who is asking, and how much of the fleet they may see.

The device path gets its identity from a client certificate (ingest-api's
`identity.py`); the user path gets it from an OIDC token. Keycloak does not
exist yet, so this module resolves a caller from gateway headers instead —
but it keeps the *shape* P3 needs, so swapping in JWKS validation is replacing
`from_headers` and nothing else:

    from_headers(...) -> Caller | None        # None means 401

Everything downstream consumes `Caller`, never a header.

The authorisation rule itself is already final, because it is the one the plan
calls out as easy to get subtly wrong (§6.6): an `owner` sees only their own
vehicles, and a vehicle they do not own is **404, not 403** — a 403 confirms
the resource exists, which is itself a disclosure. That is expressed here as a
scope predicate rather than a per-endpoint check, so a new endpoint cannot
forget it.
"""

from dataclasses import dataclass

# Keycloak's `realm_access.roles` maps onto exactly these (§2, §6.6). Anything
# else is rejected rather than defaulted: a typo'd role silently becoming
# `owner` would deny a real operator, and silently becoming `admin` would be
# the opposite and worse.
ROLES = frozenset({"owner", "admin", "operator"})

# `owner` is the only role whose view is narrowed. `operator` provisions
# devices and `admin` administers, and both need the whole fleet to do it.
SCOPED_ROLES = frozenset({"owner"})

_UUID_LEN = 36


@dataclass(frozen=True)
class Caller:
    user_id: str  # Keycloak `sub` once the realm exists
    role: str
    verified: bool  # False while the dev header path is in use

    @property
    def sees_whole_fleet(self) -> bool:
        return self.role not in SCOPED_ROLES


def _looks_like_uuid(value: str) -> bool:
    """Cheap shape check, not a parse.

    `owner_id` is a UUID column, so a non-UUID user id reaches Postgres as a
    cast error — a 500 for what is really a bad request. Rejecting the shape
    here turns that into a 401 at the edge.
    """
    if len(value) != _UUID_LEN:
        return False
    parts = value.split("-")
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdefABCDEF" for p in parts for c in p)


def from_headers(
    user: str | None,
    role: str | None,
    *,
    allow_dev_identity: bool,
    default_user: str,
    default_role: str,
) -> Caller | None:
    """Resolve the caller from the dev identity headers.

    Returns None when there is no usable identity, which the API turns into a
    401. Absent headers fall back to the configured defaults rather than
    failing, so the UI works out of the box and the exit criterion stays "open
    the page and see the fleet" with no login step to perform.
    """
    if not allow_dev_identity:
        return None

    user_id = (user or default_user).strip()
    claimed = (role or default_role).strip().lower()

    if claimed not in ROLES or not _looks_like_uuid(user_id):
        return None

    return Caller(user_id=user_id, role=claimed, verified=False)
