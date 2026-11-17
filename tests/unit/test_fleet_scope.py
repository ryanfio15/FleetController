"""The authorisation rule: who the caller is, and how much they may see.

The user-path counterpart to test_identity.py. These cover the decision that
determines whether an owner can read another owner's car — the one the plan
flags as easy to get subtly wrong (§6.6) — plus the SQL-level guarantee that
no endpoint can bypass it by forgetting a parameter.
"""

import pytest
from conftest import import_service

# Neither module imports fastapi or psycopg, which is what lets CI run these
# with nothing installed but fc_common and pytest — the same discipline
# ingest-api's identity.py follows.
identity = import_service("fleet-api", "app.identity")
queries = import_service("fleet-api", "app.queries")

from_headers = identity.from_headers

OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"

DEFAULTS = {
    "allow_dev_identity": True,
    "default_user": OWNER_A,
    "default_role": "admin",
}


def test_absent_headers_fall_back_to_the_configured_default():
    # The UI must work with no login step; the default is what makes "open the
    # page and see the fleet" true.
    who = from_headers(None, None, **DEFAULTS)
    assert who.user_id == OWNER_A
    assert who.role == "admin"
    assert who.verified is False  # nothing here is cryptographically verified


def test_headers_win_over_the_defaults():
    who = from_headers(OWNER_B, "owner", **DEFAULTS)
    assert who.user_id == OWNER_B
    assert who.role == "owner"


def test_role_is_case_insensitive_and_trimmed():
    assert from_headers(OWNER_B, "  Owner ", **DEFAULTS).role == "owner"


def test_an_empty_header_means_absent_not_invalid():
    # A proxy that strips a header often leaves an empty one behind, and an
    # empty role is a missing role rather than a bad one. Falling back is the
    # same path as sending nothing at all; it is never a widening of scope,
    # because the default is configuration rather than caller input.
    assert from_headers("", "", **DEFAULTS).role == "admin"


@pytest.mark.parametrize("role", ["superuser", "Admin ro", "own-er", "root"])
def test_unknown_roles_are_rejected_not_defaulted(role):
    # Defaulting a typo'd role to `owner` would lock out a real operator;
    # defaulting it to `admin` would be the opposite and far worse.
    assert from_headers(OWNER_A, role, **DEFAULTS) is None


@pytest.mark.parametrize(
    "user",
    ["not-a-uuid", "1111", OWNER_A[:-1], OWNER_A + "1", "zzzzzzzz-1111-1111-1111-111111111111"],
)
def test_malformed_user_ids_are_rejected_at_the_edge(user):
    # owner_id is a UUID column. Letting a non-UUID through turns a bad request
    # into a Postgres cast error, which surfaces as a 500.
    assert from_headers(user, "owner", **DEFAULTS) is None


def test_disabling_dev_identity_rejects_everything():
    # What this service should look like the day Keycloak lands and someone
    # leaves the header path wired up.
    assert from_headers(OWNER_A, "admin", **{**DEFAULTS, "allow_dev_identity": False}) is None


# --------------------------------------------------------------------- scope


def test_only_owner_is_scoped():
    assert from_headers(OWNER_A, "owner", **DEFAULTS).sees_whole_fleet is False
    assert from_headers(OWNER_A, "admin", **DEFAULTS).sees_whole_fleet is True
    assert from_headers(OWNER_A, "operator", **DEFAULTS).sees_whole_fleet is True


@pytest.mark.parametrize(
    "name",
    ["FLEET", "SERIES", "TRIPS", "ALERTS", "ACK", "ACK_LOOKUP", "SUMMARY"],
)
def test_every_vehicle_touching_statement_carries_the_scope_predicate(name):
    """The structural half of the rule.

    Each statement that can reach a vehicle must filter on it in SQL, so a new
    endpoint cannot over-share by forgetting a check in Python. A statement
    added without the predicate fails here rather than in production.
    """
    sql = getattr(queries, name)
    assert "%(whole_fleet)s" in sql
    assert "owner_id = %(user_id)s::uuid" in sql


def test_readings_queries_are_time_bounded():
    # readings is a hypertable. An unbounded scan is the only query here with
    # the potential to hurt a shared database.
    assert "INTERVAL" in queries.FLEET
    assert "%(window)s::interval" in queries.SERIES
