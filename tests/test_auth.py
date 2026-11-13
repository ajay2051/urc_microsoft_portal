"""Identity: how tokens are verified and how people are mapped to staff records."""

import uuid

import jwt
import pytest

from accounts.models import StaffRole, StaffUser
from tests.conftest import make_token
from tests.keys import KID, OTHER_PRIVATE_KEY, TENANT_ID

pytestmark = pytest.mark.django_db(transaction=True)


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_returns_profile(api_client, admin):
    staff, headers = admin
    r = api_client.get("/api/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["email"] == "admin@contoso.com" and r.json()["role"] == "admin"
    assert r.headers["x-request-id"]


@pytest.mark.parametrize("value", [None, "", "Bearer", "Bearer ", "Basic abc", "Token abc", "bearer not.a.jwt"])
def test_missing_or_malformed_authorization_is_401(api_client, value):
    headers = {} if value is None else {"Authorization": value}
    r = api_client.get("/api/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["extra"]["code"] in {"missing_token", "invalid_token"}


def test_expired_token_rejected(api_client, admin):
    staff, _ = admin
    token = make_token(oid=staff.entra_object_id, username=staff.email, exp_in=-3600)
    assert api_client.get("/api/me", headers=hdr(token)).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": str(uuid.uuid4())},  # token minted for a different application
        {"iss": "https://login.microsoftonline.com/99999999-9999-9999-9999-999999999999/v2.0"},  # foreign tenant
        {"iss": "https://sts.windows.net/" + TENANT_ID + "/"},  # v1 issuer
        {"extra": {"ver": "1.0"}},  # v1 token
        {"extra": {"tid": "99999999-9999-9999-9999-999999999999"}},  # tid mismatch with right issuer
    ],
)
def test_claim_pinning(api_client, admin, overrides):
    staff, _ = admin
    token = make_token(oid=staff.entra_object_id, username=staff.email, **overrides)
    assert api_client.get("/api/me", headers=hdr(token)).status_code == 401


def test_token_signed_by_attacker_key_rejected(api_client, admin):
    staff, _ = admin
    token = make_token(oid=staff.entra_object_id, username=staff.email, key=OTHER_PRIVATE_KEY)
    assert api_client.get("/api/me", headers=hdr(token)).status_code == 401


def test_alg_none_and_hs256_downgrade_rejected(api_client, admin):
    staff, _ = admin
    claims = jwt.decode(make_token(oid=staff.entra_object_id, username=staff.email), options={"verify_signature": False})
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": KID})
    assert api_client.get("/api/me", headers=hdr(unsigned)).status_code == 401
    hs = jwt.encode(claims, "x" * 64, algorithm="HS256", headers={"kid": KID})
    assert api_client.get("/api/me", headers=hdr(hs)).status_code == 401


def test_unknown_kid_rejected(api_client, admin):
    staff, _ = admin
    from tests.keys import PRIVATE_KEY

    claims = jwt.decode(make_token(oid=staff.entra_object_id, username=staff.email), options={"verify_signature": False})
    token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "nope"})
    assert api_client.get("/api/me", headers=hdr(token)).status_code == 401


def test_oversized_token_rejected(api_client):
    # Oversized headers are refused by the HTTP layer itself (400) or by our verifier (401).
    assert api_client.get("/api/me", headers=hdr("a" * 20000)).status_code in (400, 401, 431)


def test_delegated_scope_required(api_client, admin):
    staff, _ = admin
    for scp in (None, "other_scope", ""):
        token = make_token(oid=staff.entra_object_id, username=staff.email, scp=scp)
        r = api_client.get("/api/me", headers=hdr(token))
        assert r.status_code == 403, scp


def test_app_only_token_rejected(api_client, admin):
    staff, _ = admin
    token = make_token(
        oid=staff.entra_object_id, username=staff.email, scp=None, extra={"roles": ["Admin"], "idtyp": "app"}
    )
    assert api_client.get("/api/me", headers=hdr(token)).status_code == 403


def test_valid_token_for_unprovisioned_person_is_403(api_client, db):
    token = make_token(oid=str(uuid.uuid4()), username="stranger@contoso.com")
    r = api_client.get("/api/me", headers=hdr(token))
    assert r.status_code == 403 and r.json()["extra"]["code"] == "not_provisioned"


def test_deactivated_staff_is_403(api_client, make_staff):
    _, headers = make_staff("gone@contoso.com", active=False)
    assert api_client.get("/api/me", headers=headers).status_code == 403


def test_first_login_binds_object_id_once(api_client, make_staff):
    staff, _ = make_staff("new.hire@contoso.com", bound=False)
    oid = str(uuid.uuid4())
    r = api_client.get("/api/me", headers=hdr(make_token(oid=oid, username="New.Hire@Contoso.com")))
    assert r.status_code == 200
    staff.refresh_from_db()
    assert staff.entra_object_id == oid and staff.entra_tenant_id == TENANT_ID
    # A different directory object claiming the same UPN can no longer take the account over.
    imposter = make_token(oid=str(uuid.uuid4()), username="new.hire@contoso.com")
    assert api_client.get("/api/me", headers=hdr(imposter)).status_code == 403


def test_authorisation_comes_from_database_not_token_claims(api_client, staff):
    _, headers = staff
    # Even a token that *claims* admin-ish things cannot elevate a staff member.
    s, _ = staff
    token = make_token(
        oid=s.entra_object_id, username=s.email, extra={"roles": ["admin"], "role": "admin", "groups": ["admins"]}
    )
    assert api_client.get("/api/me", headers=hdr(token)).json()["role"] == "staff"
    assert api_client.get("/api/audit", headers=hdr(token)).status_code == 403


def test_role_change_takes_effect_immediately(api_client, staff):
    s, headers = staff
    assert api_client.get("/api/audit", headers=headers).status_code == 403
    StaffUser.objects.filter(pk=s.pk).update(role=StaffRole.ADMIN)
    assert api_client.get("/api/audit", headers=headers).status_code == 200


def test_every_route_except_probes_requires_authentication(api_client):
    from core.auth import PUBLIC_PATHS
    from project.api import api

    checked = 0
    for method, path, *_ in api._routes:
        concrete = path.replace("{client_id}", str(uuid.uuid4()))
        if concrete in PUBLIC_PATHS:
            continue
        r = api_client.request(method, concrete)
        assert r.status_code == 401, f"{method} {path} is reachable without authentication"
        checked += 1
    assert checked >= 9


def test_probes_are_public(api_client, db):
    assert api_client.get("/api/health").status_code == 200
    assert api_client.get("/api/ready").status_code == 200
