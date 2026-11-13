from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import jwt
import pytest
from django_bolt.testing import TestClient

from accounts.models import StaffRole, StaffUser
from clients.models import Client, ClientStatus
from tests.keys import API_CLIENT_ID, KID, OTHER_PRIVATE_KEY, PRIVATE_KEY, TENANT_ID

ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"


def make_token(
    *,
    oid: str,
    username: str,
    tid: str = TENANT_ID,
    aud: str = API_CLIENT_ID,
    iss: str = ISSUER,
    scp: str | None = "access_as_user",
    exp_in: int = 600,
    key=PRIVATE_KEY,
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": uuid.uuid4().hex,  # pairwise subject: deliberately NOT trusted by the app
        "oid": oid,
        "tid": tid,
        "preferred_username": username,
        "iat": now,
        "nbf": now - 5,
        "exp": now + exp_in,
        "ver": "2.0",
    }
    if scp is not None:
        claims["scp"] = scp
    claims.update(extra or {})
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def api_client():
    from project.api import api

    with TestClient(api, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def make_staff(db) -> Callable[..., tuple[StaffUser, dict[str, str]]]:
    def _make(email: str, role: str = StaffRole.STAFF, bound: bool = True, active: bool = True):
        oid = str(uuid.uuid4())
        staff = StaffUser.objects.create_staff(email, email.split("@")[0].title(), role)
        if bound:
            staff.entra_tenant_id, staff.entra_object_id = TENANT_ID, oid
        staff.is_active = active
        staff.save()
        token = make_token(oid=oid, username=email)
        return staff, {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def admin(make_staff):
    return make_staff("admin@contoso.com", StaffRole.ADMIN)


@pytest.fixture
def staff(make_staff):
    return make_staff("james.wright@contoso.com", StaffRole.STAFF)


@pytest.fixture
def client_factory(db):
    def _make(name="Fabrikam", domain="fabrikam.com", status=ClientStatus.ACTIVE, **kw):
        return Client.objects.create(name=name, primary_domain=domain, status=status, **kw)

    return _make


__all__ = ["make_token", "OTHER_PRIVATE_KEY", "TENANT_ID", "API_CLIENT_ID", "ISSUER"]


from integrations.graph import GraphUser, MockGraphClient, set_graph_client  # noqa: E402

GUEST_TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def graph_user(n: int, **kw) -> GraphUser:
    base = dict(
        id=str(uuid.UUID(int=n + 1000)),
        user_principal_name=f"user{n}@client.onmicrosoft.com",
        display_name=f"User {n}",
        mail=f"user{n}@client.com",
        job_title="Eng",
        department="IT",
        account_enabled=True,
    )
    base.update(kw)
    return GraphUser(**base)


@pytest.fixture
def graph():
    """A controllable fake Graph; restored after each test."""
    fake = MockGraphClient({GUEST_TENANT: [graph_user(i) for i in range(1, 4)]})
    set_graph_client(fake)
    yield fake
    set_graph_client(None)


@pytest.fixture
def linked_client(client_factory):
    """A client with a tenant whose consent is already verified."""
    from integrations.models import ConsentStatus, TenantIntegration

    def _make(status=ClientStatus.ACTIVE, tenant=GUEST_TENANT, domain="linked.com", **kw):
        c = client_factory("Linked", domain, status)
        TenantIntegration.objects.create(client=c, tenant_id=tenant, consent_status=ConsentStatus.GRANTED, **kw)
        return c

    return _make
